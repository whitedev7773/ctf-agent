"""Small, restart-safe store for operator-reviewed runtime results.

Only standalone results and pending candidates are persisted here. Solver
workspaces and traces remain the source of truth for larger artifacts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILENAME = ".ctf-agent-runtime.json"


def runtime_state_path(settings: object) -> Path:
    return Path(getattr(settings, "workspace_root", "workspace")) / STATE_FILENAME


def _clean_records(value: Any) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for raw_name, raw_record in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_record, dict):
            continue
        name = raw_name.strip()[:500]
        if not name:
            continue
        record: dict[str, Any] = {}
        for key in ("flag", "submit", "source", "status"):
            item = raw_record.get(key)
            if isinstance(item, str):
                record[key] = item[:4000]
        flags = raw_record.get("flags")
        if isinstance(flags, list):
            record["flags"] = [item[:4000] for item in flags if isinstance(item, str)][:20]
        sources = raw_record.get("sources")
        if isinstance(sources, list):
            record["sources"] = [item[:500] for item in sources if isinstance(item, str)][:20]
        for key in ("review_required",):
            item = raw_record.get(key)
            if isinstance(item, bool):
                record[key] = item
        if record:
            cleaned[name] = record
    return cleaned


def _clean_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        item.strip()[:500]
        for item in value
        if isinstance(item, str) and item.strip()
    }


def load_runtime_state(settings: object) -> tuple[dict[str, dict], dict[str, dict]]:
    path = runtime_state_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load runtime state from %s: %s", path, exc)
        return {}, {}
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        logger.warning("Ignoring unsupported runtime state in %s", path)
        return {}, {}
    return _clean_records(payload.get("results")), _clean_records(payload.get("candidates"))


def load_dismissed_challenges(settings: object) -> set[str]:
    """Load operator-deleted challenge names without changing the legacy API."""
    path = runtime_state_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return set()
    return _clean_names(payload.get("dismissed_challenges"))


def save_runtime_state(
    settings: object,
    results: dict[str, dict],
    candidates: dict[str, dict],
    dismissed_challenges: set[str] | None = None,
) -> Path:
    path = runtime_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "version": STATE_VERSION,
        "results": _clean_records(results),
        "candidates": _clean_records(candidates),
        "dismissed_challenges": sorted(_clean_names(list(dismissed_challenges or set()))),
    }
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return path


def persist_deps_state(deps: object) -> None:
    settings = getattr(deps, "settings", None)
    if settings is None:
        return
    try:
        save_runtime_state(
            settings,
            getattr(deps, "results", {}),
            getattr(deps, "candidates", {}),
            getattr(deps, "dismissed_challenges", set()),
        )
    except OSError as exc:
        logger.warning("Could not persist runtime state: %s", exc)
