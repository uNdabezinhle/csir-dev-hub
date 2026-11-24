# ubuntu-server template
Ubuntu 24.04/26.04 Docker workspace with VS Code Server (code-server), persistent home volume,
per-workspace bridge network, `dev`/`isolated` network profiles, CPU/RAM hard limits, read-only
per-user secrets volume at /run/csir-secrets. SSH: `coder ssh <workspace>`.
Image: `images/ubuntu-server` (build with scripts/build-images.sh).
