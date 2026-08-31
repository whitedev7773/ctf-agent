"""Persistent per-solver workspaces and lightweight recovery checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _safe_segment(value: str) -> str:
    """Return a short, Windows-safe, collision-resistant path segment."""
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")[:64] or "item"
    return f"{cleaned}-{digest}"


def solver_workspace_path(settings: object, challenge_name: str, model_spec: str) -> str:
    """Create and return the persistent host workspace for one solver lane."""
    root = Path(getattr(settings, "workspace_root", "workspace")).expanduser().resolve()
    path = root / _safe_segment(challenge_name) / _safe_segment(model_spec)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def write_checkpoint(
    workspace_dir: str,
    *,
    challenge: str,
    model_spec: str,
    status: str,
    attempt: int,
    steps: int,
    tokens: int,
    estimated_cost_usd: float,
    findings: str = "",
    stop_reason: str = "",
) -> str:
    """Atomically persist enough state for an operator or later run to resume."""
    workspace = Path(workspace_dir)
    workspace.mkdir(parents=True, exist_ok=True)
    destination = workspace / ".ctf-agent-state.json"
    temporary = workspace / ".ctf-agent-state.tmp"
    payload: dict[str, Any] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "challenge": challenge,
        "model_spec": model_spec,
        "status": status,
        "attempt": attempt,
        "steps": steps,
        "tokens": tokens,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "findings": findings[:8000],
        "stop_reason": stop_reason[:2000],
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return str(destination)
