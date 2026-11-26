"""Client for the Docker operations broker on a private Unix socket."""
import os

import httpx

TRIVY_IMAGE = "aquasec/trivy:0.76.2"


def call(operation, timeout=30, **payload):
    transport = httpx.HTTPTransport(uds=os.getenv("ENGINE_SOCKET", "/run/devhub-engine/broker.sock"))
    with httpx.Client(transport=transport, base_url="http://broker", timeout=timeout) as client:
        response = client.post("/" + operation, json=payload)
        if response.is_error:
            raise RuntimeError(f"Engine operation {operation} failed (HTTP {response.status_code})")
        return response.json()
