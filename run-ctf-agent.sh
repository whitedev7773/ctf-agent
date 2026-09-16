#!/usr/bin/env bash
set -euo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_root"

port="${CTF_AGENT_PORT:-9400}"
host="${CTF_AGENT_HOST:-127.0.0.1}"

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] uv was not found in PATH." >&2
  echo "Run 'bash scripts/setup.sh' after installing uv." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "[SETUP] Creating the project environment..."
  uv sync
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

