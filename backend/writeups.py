"""Evidence-backed writeup assembly and artifact discovery."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.artifacts import challenge_workspace_path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_REPRO_SUFFIXES = {".c", ".cpp", ".go", ".js", ".py", ".rs", ".sage", ".sh"}
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_IMAGES = 8
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]+`")


def _has_korean_narrative(text: str, minimum_characters: int = 20) -> bool:
    """Require real Korean prose while allowing English-heavy technical tokens."""
    prose = _CODE_BLOCK_RE.sub(" ", text)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    return len(re.findall(r"[가-힣]", prose)) >= minimum_characters


def _valid_image(path: Path) -> bool:
    try:
        if path.stat().st_size > _MAX_IMAGE_BYTES:
            return False
        magic = path.read_bytes()[:12]
    except OSError:
        return False
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return magic.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return magic.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return magic.startswith(b"RIFF") and magic[8:12] == b"WEBP"
    return False


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _discover_images(root: Path) -> list[Path]:
    candidates: list[tuple[int, int, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in _IMAGE_SUFFIXES:
            continue
        if not _valid_image(path):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        name = path.name.casefold()
        score = 20
        if any(word in name for word in ("proof", "success", "flag", "exploit", "result")):
            score += 50
        if "screenshot" in name or "evidence" in path.as_posix().casefold():
            score += 30
        candidates.append((score, stat.st_mtime_ns, path))
    return [item[2] for item in sorted(candidates, reverse=True)[:_MAX_IMAGES]]


def _discover_reproducers(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in _REPRO_SUFFIXES:
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        candidates.append(path)
    return sorted(
        candidates,
        key=lambda path: (
            not any(word in path.name.casefold() for word in ("solve", "exploit", "poc", "repro")),
            len(path.parts),
            path.name.casefold(),
        ),
    )[:12]


def _source_report(root: Path, prefer_canonical: bool = False) -> tuple[Path | None, str]:
    canonical = root / "_shared" / "writeup" / "WRITEUP.md"
    solver_reports = (
        root / "_shared" / "lead" / "WRITEUP.md",
        root / "_shared" / "lead" / "SOLUTION.md",
        root / "_shared" / "analyst" / "SOLUTION.md",
    )
    preferred = (canonical,) if prefer_canonical else solver_reports
    for path in preferred:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text.strip()) >= 80:
            return path, text[:250_000]
    return None, ""


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _atomic_text(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def begin_writeup_generation(
    settings: object,
    challenge_name: str,
    model_spec: str,
) -> dict[str, Any]:
    """Persist a restart-visible generating state before starting the model."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    output_dir = root / "_shared" / "writeup"
    output_dir.mkdir(parents=True, exist_ok=True)
    writeup_path = output_dir / "WRITEUP.md"
    images = _discover_images(root)
    reproducers = _discover_reproducers(root)
    now = datetime.now(UTC).isoformat()
    payload = {
        "status": "generating",
        "documented": False,
        "active": True,
        "started_at": now,
        "updated_at": now,
        "model_spec": model_spec,
        "writeup_path": _relative(writeup_path, root) if writeup_path.is_file() else "",
        "source_path": "",
        "previous_writeup_sha256": _file_sha256(writeup_path),
        "issues": [],
        "screenshots": [
            {
                "path": _relative(path, root),
                "caption": path.stem.replace("-", " ").replace("_", " ")[:160],
                "size_bytes": path.stat().st_size,
            }
            for path in images
        ],
        "reproducers": [_relative(path, root) for path in reproducers],
    }
    _atomic_json(output_dir / "manifest.json", payload)
    return payload


def fail_writeup_generation(
    settings: object,
    challenge_name: str,
    issue: str,
) -> dict[str, Any]:
    """Record a retryable generation failure without discarding prior artifacts."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    manifest_path = root / "_shared" / "writeup" / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(
        {
            "status": "needs_attention",
            "documented": False,
            "active": False,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    issues = [str(item)[:500] for item in payload.get("issues", []) if str(item).strip()]
    clean_issue = " ".join(issue.split())[:500]
    if clean_issue and clean_issue not in issues:
        issues.append(clean_issue)
    payload["issues"] = issues or ["라이트업 생성 작업이 완료되지 않았습니다"]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(manifest_path, payload)
    return payload


def interrupted_writeup_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Present an orphaned generating manifest as retryable after a restart."""
    if payload.get("status") != "generating":
        return payload
    recovered = dict(payload)
    recovered.update({"status": "needs_attention", "documented": False, "active": False})
    issues = list(recovered.get("issues") or [])
    message = "이전 라이트업 생성 작업이 서버 종료로 중단되었습니다. 재생성을 요청할 수 있습니다"
    if message not in issues:
        issues.append(message)
    recovered["issues"] = issues
    return recovered


def _append_screenshot_gallery(writeup_path: Path, root: Path, images: list[Path]) -> str:
    report = writeup_path.read_text(encoding="utf-8", errors="replace")[:500_000]
    missing = [path for path in images if path.name not in report]
    if not missing:
        return report
    lines = [report.rstrip(), ""]
    if "## 주요 스크린샷" not in report:
        lines.extend(["## 주요 스크린샷", ""])
    for index, path in enumerate(missing, 1):
        relative = _relative(path, root)
        lines.extend(
            [
                f"### 증거 {index}: {path.stem}",
                "",
                f"![{path.stem}](../../{relative})",
                "",
            ]
        )
    report = "\n".join(lines).rstrip() + "\n"
    _atomic_text(writeup_path, report)
    return report


def finalize_writeup(
    settings: object,
    challenge_name: str,
    category: str,
    flag: str = "",
    *,
    prefer_canonical: bool = False,
    require_screenshots: bool = False,
) -> dict[str, Any]:
    """Assemble a canonical writeup without inventing evidence."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    output_dir = root / "_shared" / "writeup"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    writeup_path = output_dir / "WRITEUP.md"
    source, report = _source_report(root, prefer_canonical=prefer_canonical)
    images = _discover_images(root)
    reproducers = _discover_reproducers(root)
    try:
        generation_state = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        generation_state = {}
    if not isinstance(generation_state, dict):
        generation_state = {}
    issues: list[str] = []
    if source is None:
        issues.append(
            "AI가 최종 라이트업 파일을 생성하지 못했습니다"
            if prefer_canonical
            else "lead SOLUTION.md 또는 WRITEUP.md가 없습니다"
        )
    elif not _has_korean_narrative(report, 80 if prefer_canonical else 20):
        issues.append("풀이 설명을 한국어 문장으로 보강해야 합니다")
    if not reproducers and "```" not in report:
        issues.append("재현 스크립트 또는 명령 실행 기록이 없습니다")
    if require_screenshots and not images:
        issues.append("최종 라이트업에 포함할 실제 주요 스크린샷이 없습니다")
    if (
        prefer_canonical
        and source == writeup_path
        and generation_state.get("status") == "generating"
        and generation_state.get("previous_writeup_sha256")
        and generation_state.get("previous_writeup_sha256") == _file_sha256(writeup_path)
    ):
        issues.append("AI가 최종 라이트업 파일을 갱신하지 못했습니다")

    if source == writeup_path and images:
        report = _append_screenshot_gallery(writeup_path, root, images)

    if source != writeup_path and not prefer_canonical:
        source_label = _relative(source, root) if source else "none"
        verification_summary = (
            f"- 검증된 Flag: `{flag}`"
            if flag
            else "- 해결 검증: 라이트업 복구 요청 전에 정답 처리된 문제"
        )
        lines = [
            f"# {challenge_name} 풀이",
            "",
            "## 요약",
            "",
            f"- 카테고리: {category or 'Unknown'}",
            verification_summary,
            f"- 증거 출처: `{source_label}`",
            f"- 생성 시각: {datetime.now(UTC).isoformat()}",
            "",
            "## 풀이 분석",
            "",
            report.strip() if report else "아직 solver 풀이 보고서가 작성되지 않았습니다.",
            "",
            "## 재현 방법",
            "",
        ]
        if reproducers:
            lines.extend(
                f"- [`{_relative(path, root)}`](../../{_relative(path, root)})"
                for path in reproducers
            )
        else:
            lines.append("위 풀이 분석에 포함된 명령 실행 기록을 참고합니다.")
        lines.extend(["", "## 검증", "", "설정된 제출 경로에서 Flag가 정답으로 확인되었습니다.", ""])
        if images:
            lines.extend(["## 주요 스크린샷", ""])
            for index, path in enumerate(images, 1):
                relative = _relative(path, root)
                lines.extend(
                    [
                        f"### 증거 {index}: {path.stem}",
                        "",
                        f"![{path.stem}](../../{relative})",
                        "",
                    ]
                )
        _atomic_text(writeup_path, "\n".join(lines).rstrip() + "\n")

    status = "complete" if not issues else "needs_attention"
    payload = {
        "status": status,
        "documented": status == "complete",
        "active": False,
        "started_at": generation_state.get("started_at", ""),
        "updated_at": datetime.now(UTC).isoformat(),
        "model_spec": generation_state.get("model_spec", ""),
        "writeup_path": _relative(writeup_path, root) if writeup_path.is_file() else "",
        "source_path": _relative(source, root) if source else "",
        "issues": issues,
        "screenshots": [
            {
                "path": _relative(path, root),
                "caption": path.stem.replace("-", " ").replace("_", " ")[:160],
                "size_bytes": path.stat().st_size,
            }
            for path in images
        ],
        "reproducers": [_relative(path, root) for path in reproducers],
    }
    _atomic_json(manifest_path, payload)
    return payload


def writeup_status(settings: object, challenge_name: str, solved: bool = False) -> dict[str, Any]:
    root = Path(challenge_workspace_path(settings, challenge_name))
    manifest_path = root / "_shared" / "writeup" / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "pending" if solved else "not_started",
            "documented": False,
            "updated_at": "",
            "writeup_path": "",
            "issues": ["라이트업이 아직 완성되지 않았습니다"] if solved else [],
            "screenshots": [],
            "reproducers": [],
        }
    if not isinstance(payload, dict):
        return {"status": "invalid", "documented": False, "issues": ["라이트업 manifest가 올바르지 않습니다"]}
    return payload


def read_writeup(settings: object, challenge_name: str) -> tuple[str, dict[str, Any], Path]:
    root = Path(challenge_workspace_path(settings, challenge_name))
    status = writeup_status(settings, challenge_name, solved=True)
    relative = str(status.get("writeup_path", ""))
    path = (root / relative).resolve() if relative else root / "_shared" / "writeup" / "WRITEUP.md"
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError("writeup not found")
    return path.read_text(encoding="utf-8", errors="replace")[:500_000], status, root
