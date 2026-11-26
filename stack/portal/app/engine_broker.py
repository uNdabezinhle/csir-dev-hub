"""Socket-only broker with explicit Docker operations and fixed container settings."""
import io
import json
import os
import re
import tarfile
from contextlib import contextmanager
from tempfile import TemporaryFile
from threading import BoundedSemaphore

import docker
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .extensions.kvm import KvmError, KvmProvider
from .engine import TRIVY_IMAGE

app = FastAPI(openapi_url=None)
GUACD = os.getenv("GUACD_CONTAINER", "csir-devhub-guacd-1")
SCAN_SLOT = BoundedSemaphore(1)
MAX_SCAN_ARCHIVE_BYTES = 8 * 1024**3


@contextmanager
def client():
    cli = docker.from_env()
    try:
        yield cli
    finally:
        cli.close()


@app.exception_handler(docker.errors.DockerException)
@app.exception_handler(KvmError)
def engine_error(_request: Request, error: Exception):
    return JSONResponse(status_code=502, content={"detail": str(error)[:300]})


class Network(BaseModel):
    network: str = Field(pattern=r"^csir-ws-[a-z0-9][a-z0-9_.-]{0,127}$")


class SecretSet(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    values: dict[str, str]


class Image(BaseModel):
    image: str


class ImageID(BaseModel):
    image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Command(BaseModel):
    args: list[str]


class Forward(BaseModel):
    domain: str = Field(pattern=r"^csir-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    guest_port: int = Field(ge=1, le=65535)


@app.post("/version")
def version():
    with client() as cli:
        return cli.version().get("Version")


@app.post("/images")
def images():
    with client() as cli:
        return [
            {"tags": im.tags, "id": im.short_id, "full_id": im.id,
             "size_mb": round(im.attrs["Size"] / 1e6), "created": im.attrs["Created"][:16],
             "kind": im.labels.get("org.csir.devhub.image")}
            for im in cli.images.list(filters={"label": "org.csir.devhub.image"})
        ]


@app.post("/image-id")
def image_id(body: Image):
    with client() as cli:
        return cli.images.get(body.image).id


@app.post("/attach-guacd")
def attach_guacd(body: Network):
    with client() as cli:
        net = cli.networks.get(body.network)
        if "org.csir.devhub.profile" not in (net.attrs.get("Labels") or {}):
            raise HTTPException(422, "Expected a Dev Hub workspace network")
        guacd = cli.containers.get(GUACD)
        if body.network not in guacd.attrs["NetworkSettings"]["Networks"]:
            net.connect(guacd)
    return True


@app.post("/sync-secrets")
def sync_secrets(body: SecretSet):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, value in body.values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", name):
                raise HTTPException(422, "Invalid secret name")
            data = value.encode()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid = len(data), 0o440, 1000, 1000
            tar.addfile(entry, io.BytesIO(data))
    volume = f"csir-secrets-{body.username}"
    with client() as cli:
        try:
            cli.volumes.get(volume)
        except docker.errors.NotFound:
            cli.volumes.create(volume, labels={"org.csir.devhub": "secrets", "coder.owner": body.username})
        ctr = cli.containers.run(
            "alpine:latest", ["sh", "-ec", "find /s -mindepth 1 -maxdepth 1 -exec rm -rf -- {} \\; && chown 1000:1000 /s && chmod 0750 /s"],
            volumes={volume: {"bind": "/s", "mode": "rw"}}, network_disabled=True, detach=True,
        )
        try:
            if ctr.wait(timeout=20)["StatusCode"] or not ctr.put_archive("/s", archive.getvalue()):
                raise HTTPException(502, "Could not update secrets volume")
        finally:
            ctr.remove(force=True)
    return True


@app.post("/scan")
def scan(body: ImageID):
    if not SCAN_SLOT.acquire(blocking=False):
        raise HTTPException(409, "Another image scan is running")
    try:
        return scan_archive(body.image_id)
    finally:
        SCAN_SLOT.release()


def scan_archive(image_id):
    with client() as cli, TemporaryFile() as image_file, TemporaryFile() as upload:
        image = cli.images.get(image_id)
        for chunk in image.save():
            if image_file.tell() + len(chunk) > MAX_SCAN_ARCHIVE_BYTES:
                raise HTTPException(413, "Image archive exceeds the scan size limit")
            image_file.write(chunk)
        entry = tarfile.TarInfo("image.tar")
        entry.size = image_file.tell()
        image_file.seek(0)
        with tarfile.open(fileobj=upload, mode="w") as archive:
            archive.addfile(entry, image_file)
        upload.seek(0)
        command = [
            "image", "--input", "/tmp/image.tar", "--quiet", "--scanners", "vuln", "--format", "json",
            "--output", "/tmp/report.json", "--timeout", "20m",
        ]
        try:
            ctr = cli.containers.create(TRIVY_IMAGE, command,
                volumes={"csir-devhub_trivy-cache": {"bind": "/root/.cache/trivy", "mode": "rw"}},
                mem_limit="1g", labels={"org.csir.devhub.role": "image-scan"})
        except docker.errors.ImageNotFound:
            cli.images.pull(TRIVY_IMAGE)
            ctr = cli.containers.create(TRIVY_IMAGE, command,
                volumes={"csir-devhub_trivy-cache": {"bind": "/root/.cache/trivy", "mode": "rw"}},
                mem_limit="1g", labels={"org.csir.devhub.role": "image-scan"})
        try:
            if not ctr.put_archive("/tmp", upload):
                raise HTTPException(502, "Could not stage the image archive")
            ctr.start()
            rc = ctr.wait(timeout=1500)["StatusCode"]
            if rc:
                raise HTTPException(502, f"Trivy exited with status {rc}")
            stream, _ = ctr.get_archive("/tmp/report.json")
            with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
                return json.load(tar.extractfile("report.json"))
        finally:
            ctr.remove(force=True)


class LocalKvm(KvmProvider):
    _virsh = KvmProvider._local_virsh


FIXED_COMMANDS = {
    ("version",), ("nodeinfo",), ("freecell", "--all"), ("domcapabilities",),
    ("list", "--all"), ("pool-list", "--all", "--details"), ("net-list", "--all"),
}
DOMAIN_COMMANDS = {"dominfo", "snapshot-list", "start", "shutdown", "reboot", "destroy"}
SNAPSHOT_COMMANDS = {"snapshot-revert", "snapshot-delete"}


def valid_virsh(args):
    if tuple(args) in FIXED_COMMANDS:
        return True
    if len(args) < 2 or not re.fullmatch(r"csir-[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args[1]):
        return False
    verb = args[0]
    if verb in DOMAIN_COMMANDS:
        return len(args) == 2
    if verb == "domifaddr":
        return args[2:] == ["--source", "lease"]
    if len(args) < 3 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}", args[2]):
        return False
    if verb in SNAPSHOT_COMMANDS:
        return len(args) == 3
    return (verb == "snapshot-create-as" and len(args) == 5 and args[4] == "--atomic"
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9 _.,:/()-]{0,511}", args[3]) is not None)


@app.post("/virsh")
def virsh(body: Command):
    if not valid_virsh(body.args):
        raise HTTPException(422, "Unsupported libvirt command")
    return LocalKvm()._virsh(*body.args)


@app.post("/rdp-forward")
def rdp_forward(body: Forward):
    return LocalKvm()._local_rdp_forward(body.domain, body.guest_port)
