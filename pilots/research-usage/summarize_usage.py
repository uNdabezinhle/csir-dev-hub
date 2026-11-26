"""Summarize the versioned synthetic fixture using only the Python standard library."""
import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
DATASET_VERSION = "workspace-usage-v1"
METRICS = ("vcpu_hours", "memory_gib_hours")
DATASET = f"{DATASET_VERSION}.csv"
BASELINE = "expected-summary.json"
SCRIPT = Path(__file__).name


def normalize(content):
    # Git checkouts can vary by platform. Fingerprint the LF-normalized text.
    return content.replace(b"\r\n", b"\n")


def local_bytes(name):
    return normalize((ROOT / name).read_bytes())


def git(*args):
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], check=True, capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("A Git checkout containing the pilot files is required") from error


def verify_checkout(expected_commit):
    if not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", expected_commit):
        raise ValueError("Supply the full Git commit ID from the blueprint")
    head = git("rev-parse", "HEAD").decode("ascii").strip()
    if head != expected_commit.lower():
        raise ValueError(f"Checkout is at {head}, expected {expected_commit.lower()}")
    prefix = git("rev-parse", "--show-prefix").decode("utf-8").strip()
    contents = {}
    for name in (DATASET, BASELINE, SCRIPT):
        contents[name] = local_bytes(name)
        if contents[name] != normalize(git("show", f"{head}:{prefix}{name}")):
            raise ValueError(f"{name} differs from the expected Git commit")
    return head, contents


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def summarize(stream):
    projects = {}
    for row in csv.DictReader(stream):
        totals = projects.setdefault(row["project_id"], dict.fromkeys(METRICS, Decimal(0)))
        for metric in METRICS:
            totals[metric] += Decimal(row[metric])
    return {
        "dataset_version": DATASET_VERSION,
        "totals": {metric: format(sum(t[metric] for t in projects.values()), ".2f") for metric in METRICS},
        "projects": [
            {"project_id": project, **{metric: format(values[metric], ".2f") for metric in METRICS}}
            for project, values in sorted(projects.items())
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Compare the result with the checked-in acceptance baseline")
    parser.add_argument("--expected-commit", help="Require the full blueprint Git commit and emit a JSON execution record")
    args = parser.parse_args()
    make_record = args.expected_commit is not None
    try:
        if make_record:
            commit, contents = verify_checkout(args.expected_commit)
        else:
            contents = {DATASET: local_bytes(DATASET)}
            if args.verify:
                contents[BASELINE] = local_bytes(BASELINE)
        summary = summarize(io.StringIO(contents[DATASET].decode("utf-8"), newline=""))
        if (args.verify or make_record) and summary != json.loads(contents[BASELINE]):
            parser.exit(1, "Usage summary differs from the pilot baseline.\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    if make_record:
        encoded_summary = json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
        summary = {
            "schema_version": 1,
            "source_commit": commit,
            "inputs": {name: sha256(content) for name, content in contents.items()},
            "summary_sha256": sha256(encoded_summary),
            "summary": summary,
        }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
