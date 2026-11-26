# Repository Guidelines

## Project Structure & Module Organization

The root `docker-compose.yml` defines the core Dev Hub stack; `docker-compose.*.yml` files add OIDC, Guacamole, KVM, public-access, or desktop behavior. Application code lives in `stack/portal/app/` (FastAPI, Jinja templates, and static CSS). Service images and configuration are grouped under `stack/`, while workspace images are in `images/`. Coder Terraform templates belong in `templates/<template-name>/`; golden-image definitions belong in `packer/`. Operational scripts are under `scripts/`, documentation under `docs/`, and systemd units under `ops/systemd/`. End-to-end checks use `scripts/test-*.py`; there is no separate unit-test directory.

## Build, Test, and Development Commands

- `cp .env.example .env` creates local, non-secret configuration; adjust `HOST_IP` before starting.
- `./scripts/build-images.sh` builds the Ubuntu, desktop, and Kali workspace images.
- `./scripts/start.sh` generates secrets, starts services, bootstraps identity, and pushes templates.
- `./scripts/dc.sh ps` runs Docker Compose with the correct environment and overlays; replace `ps` with other Compose subcommands.
- `./scripts/stop.sh` stops the stack while retaining data.
- `python -m compileall -q stack/portal/app scripts` performs the CI Python syntax check.
- `terraform fmt -check -recursive templates/` checks Terraform formatting.
- `packer fmt -check -recursive packer/` checks Packer formatting.

On Windows, use `powershell -ExecutionPolicy Bypass -File .\scripts\start-desktop.ps1`; see `docs/local-windows.md`.

## Coding Style & Naming Conventions

Preserve LF line endings as configured by `.gitattributes`. Use four spaces for Python, `snake_case` for functions/modules, `PascalCase` for classes, and uppercase names for constants. Keep shell scripts Bash-compatible and fail explicitly on errors. Format Terraform and Packer files with their native `fmt` commands. Name new Compose overlays `docker-compose.<feature>.yml` and templates with lowercase kebab-case directories.

## Testing Guidelines

Run the relevant live-stack checks after changes: `scripts/test-mfa.py`, `scripts/test-portal.py`, `scripts/test-rdp.py`, or `scripts/test-kvm.sh`. These tests require a running stack and generated `secrets/devhub.env`; use `scripts/devhub.ps1 <script>` on Windows. Add regression checks to the closest existing `test-*.py` flow. CI validates Compose configurations, Terraform, Packer, Python imports, Docker image builds, and advisory Hadolint/Trivy scans. No numeric coverage threshold is enforced.

## Commit & Pull Request Guidelines

Follow the established Conventional Commit pattern: `feat(desktop): ...`, `fix(tests): ...`, or `docs: ...`. Keep commits focused and use an imperative, specific subject. Pull requests should explain the change, affected overlays/services, configuration or migration steps, and verification commands/results. Link relevant issues and include screenshots for portal UI changes. Never commit `.env`, `secrets/devhub.env`, tokens, generated logs, or scan reports containing sensitive data.
