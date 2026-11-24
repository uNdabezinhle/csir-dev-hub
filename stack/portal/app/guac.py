"""Brokered RDP via Apache Guacamole (guacamole-auth-json).
The portal mints a signed+encrypted, short-lived connection token; guacd is attached to the workspace's private
network on demand, so RDP is never exposed on the host and users never see the RDP credential."""
import base64, hashlib, hmac, json, time, logging
from urllib.parse import quote
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from .config import GUAC_JSON_SECRET, GUAC_PUBLIC_URL, GUACD_CONTAINER
log = logging.getLogger("guac")

def _encrypt(payload: dict) -> str:
    key = bytes.fromhex(GUAC_JSON_SECRET)
    data = json.dumps(payload).encode()
    signed = hmac.new(key, data, hashlib.sha256).digest() + data
    p = padding.PKCS7(128).padder(); padded = p.update(signed) + p.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode()

def attach_guacd(network_name):
    """Connect guacd to the workspace network (idempotent)."""
    import docker
    cli = docker.from_env()
    net = cli.networks.get(network_name)
    g = cli.containers.get(GUACD_CONTAINER)
    if network_name not in g.attrs["NetworkSettings"]["Networks"]:
        net.connect(g)
    return True

def rdp_link(username, ws, hostname, rdp_password, network_name, ttl_s=300):
    attach_guacd(network_name)
    conn = f"{ws['name']} desktop"
    payload = {"username": username, "expires": int((time.time() + ttl_s) * 1000), "connections": {conn: {
        "protocol": "rdp", "parameters": {"hostname": hostname, "port": "3389", "username": "coder", "password": rdp_password,
        "security": "any", "ignore-cert": "true", "resize-method": "display-update", "enable-wallpaper": "false",
        "server-layout": "en-us-qwerty", "timezone": "Africa/Johannesburg"}}}}
    return f"{GUAC_PUBLIC_URL}/?data={quote(_encrypt(payload), safe='')}"
