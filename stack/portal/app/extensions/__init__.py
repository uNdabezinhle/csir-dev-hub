"""Extension points for enterprise modules not implemented in the PoC.
Each module has a small interface and a working stub so the portal runs end-to-end; replace the stub registration in
REGISTRY with a real implementation (e.g. from a plugin package) without touching the core portal code."""
from .base import REGISTRY, get  # noqa
