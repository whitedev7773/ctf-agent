#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_root"

port="${CTF_AGENT_PORT:-9400}"
host="${CTF_AGENT_HOST:-0.0.0.0}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv was not found in PATH." >&2
  echo "Run 'bash scripts/setup.sh' after installing uv." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "[SETUP] Creating the project environment..."
  uv sync
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "[SETUP] Created .env from .env.example."
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Docker was not found in PATH." >&2
  echo "Install and start Docker Engine or Docker Desktop, then run this file again." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "[ERROR] Docker is installed, but the Docker daemon is not available." >&2
  echo "Start Docker Engine or Docker Desktop, then run this file again." >&2
  exit 1
fi

sandbox_image="$(.venv/bin/python -c 'from backend.config import Settings; print(Settings().sandbox_image)')"
if [[ -z "$sandbox_image" ]]; then
  echo "[ERROR] Could not determine SANDBOX_IMAGE from the project settings." >&2
  exit 1
fi

if ! docker image inspect "$sandbox_image" >/dev/null 2>&1; then
  echo "[SETUP] Docker image '$sandbox_image' was not found. Building it now..."
  docker build -f sandbox/Dockerfile.sandbox -t "$sandbox_image" .
  echo "[SETUP] Docker image '$sandbox_image' is ready."
fi

if [[ -n "${CTF_AGENT_MAX_CHALLENGES:-}" ]]; then
  set -- --max-challenges "$CTF_AGENT_MAX_CHALLENGES" "$@"
fi

echo "[CTF Agent] Starting coordinator..."
echo "[Dashboard] http://127.0.0.1:${port}"
echo "[Stop] Press Ctrl+C in this terminal."
echo

exec .venv/bin/python -m backend.cli \
  --dashboard-host "$host" \
  --dashboard-port "$port" \
  "$@"
