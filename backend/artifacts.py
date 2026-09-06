"""Persistent per-solver workspaces and lightweight recovery checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STATE_FILES = {".ctf-agent-state.json", ".ctf-agent-state.tmp"}
_HIGH_VALUE_PREFIXES = (
    "exploit",
    "solve",
    "solution",
    "notes",
    "triage",
    "verify",
    "writeup",
    "progress",
    "state",
    "harness",
    "repro",
    "poc",
)
_EVIDENCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".gdb",
    ".go",
    ".ipynb",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".r2",
    ".rs",
    ".sage",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
_BULK_SUFFIXES = {
    ".bin",
    ".core",
    ".dump",
    ".efi",
    ".ffs",
    ".fv",
    ".img",
    ".iso",
    ".log",
    ".o",
    ".obj",
    ".pe",
    ".raw",
    ".rom",
}
_BULK_PATH_PARTS = re.compile(
    r"^(?:volume|section|file)-[0-9a-f-]+$|^(?:rootfs|unpacked|extract(?:ed)?)$",
    re.IGNORECASE,
)


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


def challenge_workspace_path(settings: object, challenge_name: str) -> str:
    """Return the persistent host root shared by every lane of one challenge."""
    root = Path(getattr(settings, "workspace_root", "workspace")).expanduser().resolve()
    path = root / _safe_segment(challenge_name)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def challenge_shared_path(settings: object, challenge_name: str) -> str:
    """Create the challenge-level artifact exchange shared by every solver lane."""
    root = Path(getattr(settings, "workspace_root", "workspace")).expanduser().resolve()
    path = root / _safe_segment(challenge_name) / "_shared"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _artifact_score(path: Path, root: Path) -> int | None:
    """Rank reusable reasoning artifacts and reject mechanical extraction debris.

    A bulk extractor can create thousands of files without advancing the exploit.
    Those files must neither fill the resume manifest nor unlock another expensive
    automatic turn. Named PoCs, notes and scripts remain eligible even when their
    extension would otherwise look binary.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    lowered = path.name.casefold()
    named = any(lowered.startswith(prefix) for prefix in _HIGH_VALUE_PREFIXES)
    if path.name in _STATE_FILES:
        return None
    if not named and any(_BULK_PATH_PARTS.match(part) for part in relative.parts[:-1]):
        return None
    suffix = path.suffix.casefold()
    if not named and suffix in _BULK_SUFFIXES:
        return None
    if not named and suffix not in _EVIDENCE_SUFFIXES and suffix not in {".dis", ".asm"}:
        return None
    score = 100 if named else 40 if suffix in _EVIDENCE_SUFFIXES else 20
    return score - min(20, max(0, len(relative.parts) - 2) * 2)


def _checkpoint_summary(root: Path) -> str:
    state_path = root / ".ctf-agent-state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    status = str(payload.get("status", "unknown"))[:80]
    attempt = payload.get("attempt", "?")
    findings = " ".join(str(payload.get("findings", "")).split())[:500]
    stop_reason = " ".join(str(payload.get("stop_reason", "")).split())[:240]
    details = findings or stop_reason or "no concise findings were saved"
    return f"- CHECKPOINT {root.name}: status={status}, attempt={attempt}; {details}"


def workspace_progress_signature(*roots: str) -> str:
    """Return a bounded signature of solver-created artifacts.

    This intentionally hashes metadata rather than file contents. It is used only
    to decide whether another expensive solver slice earned an automatic resume.
    """
    digest = hashlib.sha256()
    entries = 0
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
            if entries >= 1000 or not path.is_file():
                continue
            if _artifact_score(path, root) is None:
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(root)
            except OSError:
                continue
            digest.update(str(relative).encode("utf-8", errors="replace"))
            digest.update(f":{stat.st_size}:{stat.st_mtime_ns}\n".encode())
            entries += 1
    digest.update(f"entries:{entries}".encode())
    return digest.hexdigest()


def workspace_resume_manifest(*roots: str, limit: int = 24) -> str:
    """Summarize the most useful existing artifacts for a fresh model thread."""
    candidates: list[tuple[int, int, str]] = []
    checkpoints: list[str] = []
    for raw_root in roots:
        if not raw_root:
            continue
        root = Path(raw_root)
        if not root.exists():
            continue
        checkpoint = _checkpoint_summary(root)
        if checkpoint:
            checkpoints.append(checkpoint)
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            score = _artifact_score(path, root)
            if score is None:
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(root)
            except OSError:
                continue
            candidates.append(
                (
                    score,
                    stat.st_mtime_ns,
                    f"{root.name}/{relative.as_posix()} ({stat.st_size} bytes)",
                )
            )
    selected = sorted(candidates, reverse=True)[: max(1, limit)]
    artifact_lines = [f"- {item[2]}" for item in selected]
    return "\n".join(checkpoints + artifact_lines)


def handoff_quality_issues(path: str | Path) -> list[str]:
    """Return concise reasons a delegated claim is unsafe to integrate."""
    handoff = Path(path)
    try:
        text = handoff.read_text(encoding="utf-8", errors="replace")[:100_000]
    except FileNotFoundError:
        return ["handoff missing"]
    except OSError as exc:
        return [f"handoff unreadable: {exc}"]
    folded = text.casefold()
    issues: list[str] = []
    required_sections = {
        "conclusion": ("## conclusion", "## 결론"),
        "evidence": ("## evidence", "## 증거"),
        "reproduction": ("## reproduction", "## reproducer", "## 재현"),
        "assumptions/conflicts": (
            "## assumptions and conflicts",
            "## assumptions/conflicts",
            "## 가정과 충돌",
        ),
    }
    for label, markers in required_sections.items():
        if not any(marker in folded for marker in markers):
            issues.append(f"missing {label} section")
    if not any(marker in folded for marker in ("supported", "refuted", "inconclusive")):
        issues.append("missing supported/refuted/inconclusive verdict")
    if "/challenge/workspace/" in folded:
        issues.append("references private delegate workspace; copy reproducible artifacts to shared")
    return issues


_NOTE_SECTIONS = (
    "approach",
    "assumption",
    "blocker",
    "confirmed",
    "conflict",
    "conclusion",
    "evidence",
    "finding",
    "hypothesis",
    "next experiment",
    "progress",
    "result",
    "가정",
    "결론",
    "다음 실험",
    "막힌",
    "접근",
    "진행",
    "충돌",
    "확인",
)
_NOTE_METADATA_PREFIXES = (
    "attempt:",
    "handoff audit:",
    "original handoff:",
    "requested deliverable:",
    "source agent:",
    "stop reason:",
    "tool steps:",
)


def _approach_fragments(text: str, limit: int = 3) -> list[str]:
    """Extract short human-readable claims while skipping code and tables."""
    fragments: list[str] = []
    section = ""
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("|"):
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level >= 2:
                section = line.lstrip("#").strip().rstrip(":")[:80]
            continue
        if section and not any(marker in section.casefold() for marker in _NOTE_SECTIONS):
            continue
        cleaned = re.sub(r"^[-*+]\s+", "", line)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        cleaned = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", cleaned)
        cleaned = cleaned.replace("`", "").replace("**", "").strip()
        if cleaned.casefold().startswith(_NOTE_METADATA_PREFIXES):
            continue
        if cleaned.startswith(("$ ", "> ", "./", "/challenge/")) or len(cleaned) < 12:
            continue
        if section:
            cleaned = f"{section}: {cleaned}"
        if len(cleaned) > 240:
            cleaned = cleaned[:237].rstrip() + "..."
        fragments.append(cleaned)
        if len(fragments) >= max(1, limit):
            break
    return fragments


def challenge_approach_notes(
    settings: object,
    challenge_name: str,
    live_findings: dict[str, str] | None = None,
    limit: int = 6,
) -> list[dict[str, str]]:
    """Build a bounded, restart-safe dashboard summary from solver evidence."""
    max_notes = max(1, min(12, int(limit)))
    notes: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(source: str, text: str) -> None:
        normalized = re.sub(r"\W+", " ", text.casefold()).strip()
        fingerprint = normalized[:160]
        if not fingerprint or fingerprint in seen or len(notes) >= max_notes:
            return
        seen.add(fingerprint)
        notes.append({"source": source[:160], "text": text[:240]})

    for model_spec, findings in (live_findings or {}).items():
        for fragment in _approach_fragments(str(findings), limit=1):
            add(model_spec.rsplit("/", 1)[-1], fragment)

    root = (
        Path(getattr(settings, "workspace_root", "workspace")).expanduser().resolve()
        / _safe_segment(challenge_name)
        / "_shared"
    )
    if not root.is_dir() or len(notes) >= max_notes:
        return notes

    candidates: list[tuple[int, int, Path]] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
            stat = path.stat()
        except (OSError, ValueError):
            continue
        lowered = path.name.casefold()
        priority = (
            100 if lowered == "state.md"
            else 90 if lowered.startswith("solution")
            else 80 if lowered.startswith("triage")
            else 70 if "recovery" in relative.parts
            else 60 if any(part in {"delegate", "delegates"} for part in relative.parts)
            else 40
        )
        candidates.append((priority, stat.st_mtime_ns, path))

    for _, _, path in sorted(candidates, reverse=True)[:100]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:100_000]
            source = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        for fragment in _approach_fragments(text):
            add(source, fragment)
            if len(notes) >= max_notes:
                return notes
    return notes


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
