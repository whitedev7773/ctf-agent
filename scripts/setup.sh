#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$project_root"

skip_docker_build=false
rebuild_sandbox=false
skip_codex_check=false

for arg in "$@"; do
  case "$arg" in
    --skip-docker-build) skip_docker_build=true ;;
    --rebuild-sandbox) rebuild_sandbox=true ;;
    --skip-codex-check) skip_codex_check=true ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: bash scripts/setup.sh [--skip-docker-build] [--rebuild-sandbox] [--skip-codex-check]" >&2
      exit 2
      ;;
  esac
done

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[ERROR] '$1' command not found. $2" >&2
    exit 1
  fi
}

require_command uv "Install uv from https://docs.astral.sh/uv/getting-started/installation/."
require_command docker "Install and start Docker Engine or Docker Desktop."

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "[SETUP] Copied .env.example to .env."
else
  echo "[SETUP] Keeping the existing .env."
fi

echo "[SETUP] Syncing the Python environment and packages..."
uv sync

echo "[SETUP] Checking Docker..."
docker info >/dev/null

if [[ "$skip_docker_build" == false ]]; then
  if [[ "$rebuild_sandbox" == true ]] || ! docker image inspect ctf-sandbox >/dev/null 2>&1; then
    echo "[SETUP] Building the ctf-sandbox image..."
    docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
  else
    echo "[SETUP] Reusing the existing ctf-sandbox image."
  fi
fi

if [[ "$skip_codex_check" == false ]]; then
  echo "[SETUP] Checking Codex CLI login..."
  .venv/bin/python -c 'import asyncio; from backend.codex_cli import prepare_codex_cli; from backend.config import Settings; print(asyncio.run(prepare_codex_cli(Settings().codex_cli_path)))'
fi

echo
echo "[DONE] This computer is ready to run CTF Agent."
echo "[NEXT] bash run-ctf-agent.sh"
