"""Evidence-backed writeup assembly and artifact discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
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
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:<)?([^)>\s]+)(?:>)?(?:\s+[^)]*)?\)")
_REQUIRED_REVIEW_SECTIONS = (
    "핵심 원리",
    "풀이 순서",
    "재현",
    "검증",
)
_LEGACY_REVIEW_SECTIONS = (
    "요약",
    "취약점 또는 핵심 원리",
    "풀이 과정",
    "재현 방법",
    "검증",
    "주요 스크린샷",
)
_CORE_EVIDENCE_WORDS = (
    "취약점",
    "핵심 원리",
    "핵심",
    "코어",
    "core",
    "메커니즘",
    "원인",
    "공격 지점",
    "분석 근거",
    "고장",
    "결함",
    "손상",
    "우회",
)
_SUCCESS_EVIDENCE_WORDS = ("해결", "성공", "플래그", "flag", "정답", "결과", "복구")
_APPROVED_REVIEW_RE = re.compile(r"(?im)^Verdict:\s*APPROVED\s*$")
_REJECTED_REVIEW_RE = re.compile(r"(?im)^Verdict:\s*REJECTED\s*$")


def _has_korean_narrative(text: str, minimum_characters: int = 20) -> bool:
    """Require real Korean prose while allowing English-heavy technical tokens."""
    prose = _CODE_BLOCK_RE.sub(" ", text)
    prose = _INLINE_CODE_RE.sub(" ", prose)
    return len(re.findall(r"[가-힣]", prose)) >= minimum_characters


def _section_body(text: str, title: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(title)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
    )
    return match.group(1).strip() if match else ""


def _writeup_schema(report: str) -> tuple[tuple[str, ...], str, str]:
    """Accept already-generated verbose writeups while producing compact new ones."""
    if all(_section_body(report, title) for title in _REQUIRED_REVIEW_SECTIONS):
        return _REQUIRED_REVIEW_SECTIONS, "재현", "증거 화면"
    return _LEGACY_REVIEW_SECTIONS, "재현 방법", "주요 스크린샷"


def _referenced_screenshot_paths(report: str, writeup_path: Path, root: Path) -> list[Path]:
    # A concise writeup may place the mechanism image directly in `핵심 원리`
    # and the success image in `검증`. Do not require an artificial gallery
    # section: validate every image that the final Markdown actually references.
    selected: list[Path] = []
    seen: set[Path] = set()
    for _label, raw_path in _MARKDOWN_IMAGE_RE.findall(report):
        if re.match(r"^[a-z][a-z0-9+.-]*://", raw_path, re.IGNORECASE):
            continue
        try:
            path = (writeup_path.parent / raw_path).resolve()
        except OSError:
            continue
        if path in seen or not path.is_relative_to(root) or not _valid_image(path):
            continue
        seen.add(path)
        selected.append(path)
    return selected


def _review_quality_issues(
    report: str,
    writeup_path: Path,
    root: Path,
    reproducers: list[Path],
) -> tuple[list[str], list[Path]]:
    """Apply the organizer-facing proof standard to an AI-authored document."""
    issues: list[str] = []
    sections, reproduction_title, screenshot_title = _writeup_schema(report)
    missing_sections = [title for title in sections if not _section_body(report, title)]
    if missing_sections:
        issues.append("대회 관계자 검토에 필요한 절이 빠졌습니다: " + ", ".join(missing_sections))

    if sections == _REQUIRED_REVIEW_SECTIONS:
        steps = re.findall(r"(?m)^\s*\d+[.)]\s+.+$", _section_body(report, "풀이 순서"))
        if not 3 <= len(steps) <= 6:
            issues.append("풀이 순서는 핵심 3~6개 단계로 작성해야 합니다")

    reproduction = _section_body(report, reproduction_title)
    if not _CODE_BLOCK_RE.search(reproduction):
        issues.append("재현 절에 핵심 payload, 식, 요청 또는 실행 명령을 짧은 code block으로 포함해야 합니다")

    screenshot_section = _section_body(report, screenshot_title)
    selected_images = _referenced_screenshot_paths(report, writeup_path, root)
    if len(selected_images) != 2:
        issues.append("핵심 원리와 해결 성공을 보여 주는 실제 스크린샷 두 장이 필요합니다")
    image_labels = "\n".join(label for label, _path in _MARKDOWN_IMAGE_RE.findall(report))
    lowered = f"{screenshot_section}\n{image_labels}".casefold()
    if not any(word.casefold() in lowered for word in _CORE_EVIDENCE_WORDS):
        issues.append("증거 화면에 핵심 원리 또는 취약점이 보이는 스크린샷과 caption이 필요합니다")
    if not any(word.casefold() in lowered for word in _SUCCESS_EVIDENCE_WORDS):
        issues.append("증거 화면에 해결 성공 또는 Flag 결과 스크린샷과 caption이 필요합니다")
    return issues, selected_images


def _valid_image(path: Path) -> bool:
    try:
        if path.stat().st_size > _MAX_IMAGE_BYTES:
            return False
        # Reading the whole screenshot just to inspect its signature made large
        # evidence directories needlessly expensive to inventory.
        with path.open("rb") as image:
            magic = image.read(12)
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


def _discover_artifacts(root: Path) -> tuple[list[Path], list[Path]]:
    """Inventory images and reproducers in one failure-tolerant tree walk."""
    candidates: list[tuple[int, int, Path]] = []
    reproducers: list[Path] = []
    seen_hashes: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _exc: None):
        # Never follow directory symlinks out of the challenge workspace.
        dirnames[:] = [
            name for name in dirnames if not (Path(directory) / name).is_symlink()
        ]
        for filename in filenames:
            path = Path(directory) / filename
            suffix = path.suffix.casefold()
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
            except OSError:
                # Solver processes may still be atomically replacing artifacts.
                continue

            if suffix in _IMAGE_SUFFIXES and _valid_image(path):
                digest = _file_sha256(path)
                if digest and digest in seen_hashes:
                    continue
                if digest:
                    seen_hashes.add(digest)
                name = path.name.casefold()
                score = 20
                if any(
                    word in name for word in ("proof", "success", "flag", "exploit", "result")
                ):
                    score += 50
                if "screenshot" in name or "evidence" in path.as_posix().casefold():
                    score += 30
                candidates.append((score, stat.st_mtime_ns, path))

            if suffix in _REPRO_SUFFIXES:
                try:
                    relative_parts = path.relative_to(root).parts
                except ValueError:
                    continue
                if (
                    not any(part.startswith(".") for part in relative_parts)
                    and stat.st_size <= 2 * 1024 * 1024
                ):
                    reproducers.append(path)

    images = [item[2] for item in sorted(candidates, reverse=True)[:_MAX_IMAGES]]
    reproducers.sort(
        key=lambda path: (
            not any(word in path.name.casefold() for word in ("solve", "exploit", "poc", "repro")),
            len(path.parts),
            path.name.casefold(),
        )
    )
    return images, reproducers[:12]


def _discover_images(root: Path) -> list[Path]:
    return _discover_artifacts(root)[0]


def _image_manifest_entry(path: Path, root: Path) -> dict[str, Any]:
    """Return bounded, auditable metadata without trusting image contents."""
    return {
        "path": _relative(path, root),
        "caption": path.stem.replace("-", " ").replace("_", " ")[:160],
        "size_bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _image_manifest_entries(paths: list[Path], root: Path) -> list[dict[str, Any]]:
    """Ignore evidence removed between discovery and manifest serialization."""
    entries: list[dict[str, Any]] = []
    for path in paths:
        try:
            entries.append(_image_manifest_entry(path, root))
        except (OSError, ValueError):
            continue
    return entries


def _discover_reproducers(root: Path) -> list[Path]:
    return _discover_artifacts(root)[1]


def _stage_writeup_reproducers(
    root: Path,
    output_dir: Path,
    sources: list[Path] | None = None,
) -> list[Path]:
    """Expose solver scripts to fresh documentation containers through shared storage.

    Each writer/reviewer receives a new private `/challenge/workspace`, so paths
    from the solving container are otherwise unavailable just when a short
    evidence rerun is needed.  Keep small reproducible scripts under the shared
    writeup directory without modifying the original solver workspace.
    """
    staged_root = output_dir / "reproducers"
    staged: list[Path] = []
    for source in sources if sources is not None else _discover_reproducers(root):
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        if relative.parts[:2] == ("_shared", "writeup"):
            continue
        destination = staged_root / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError:
            continue
        staged.append(destination)
    return staged


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
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _writeup_history(payload: dict[str, Any], event: str, detail: str = "") -> list[dict[str, str]]:
    """Keep a short, restart-visible account of generation attempts."""
    history = [
        item for item in payload.get("history", [])
        if isinstance(item, dict) and str(item.get("event", "")).strip()
    ][-11:]
    history.append(
        {
            "at": datetime.now(UTC).isoformat(),
            "event": event,
            "detail": " ".join(detail.split())[:300],
        }
    )
    return history


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def begin_writeup_generation(
    settings: object,
    challenge_name: str,
    model_spec: str,
    review_model_spec: str = "",
    *,
    targeted_revision: bool = False,
    revision_scope: list[str] | None = None,
) -> dict[str, Any]:
    """Persist a restart-visible generating state before starting the model."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    output_dir = root / "_shared" / "writeup"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    writeup_path = output_dir / "WRITEUP.md"
    review_path = output_dir / "REVIEW.md"
    images, discovered_reproducers = _discover_artifacts(root)
    staged_reproducers = _stage_writeup_reproducers(root, output_dir, discovered_reproducers)
    reproducers = staged_reproducers or discovered_reproducers
    now = datetime.now(UTC).isoformat()
    clean_revision_scope = [
        " ".join(str(item).split())[:500]
        for item in (revision_scope or [])
        if str(item).strip()
    ]
    initial_phase = "revising" if targeted_revision else "writing"
    payload = {
        "status": "generating",
        "documented": False,
        "active": True,
        "started_at": now,
        "updated_at": now,
        "model_spec": model_spec,
        "review_model_spec": review_model_spec,
        "phase": initial_phase,
        "writeup_path": _relative(writeup_path, root) if writeup_path.is_file() else "",
        "review_path": _relative(review_path, root) if review_path.is_file() else "",
        "source_path": "",
        "previous_writeup_sha256": _file_sha256(writeup_path),
        "previous_review_sha256": _file_sha256(review_path),
        "issues": clean_revision_scope if targeted_revision else [],
        "revision_scope": clean_revision_scope if targeted_revision else [],
        "targeted_revision": targeted_revision,
        "history": _writeup_history(
            previous,
            "revision_resumed" if targeted_revision else "started",
            "완료된 검사에서 부족한 항목만 수정하고 재검수합니다"
            if targeted_revision
            else "라이트업 작성 및 검수를 시작했습니다",
        ),
        "screenshots": _image_manifest_entries(images, root),
        "reproducers": [_relative(path, root) for path in reproducers],
    }
    _atomic_json(manifest_path, payload)
    return payload


def seed_writeup_from_solver_evidence(
    settings: object,
    challenge_name: str,
    category: str,
    flag: str = "",
) -> dict[str, Any] | None:
    """Create a readable fallback before starting the asynchronous AI editor.

    A writeup model can be interrupted, time out, or leave its turn open. Keep
    the solver's evidence usable in all of those cases, while never replacing a
    canonical document produced by an earlier successful generation.
    """
    root = Path(challenge_workspace_path(settings, challenge_name))
    writeup_path = root / "_shared" / "writeup" / "WRITEUP.md"
    if writeup_path.is_file():
        return None
    source, _ = _source_report(root)
    if source is None:
        return None
    return finalize_writeup(settings, challenge_name, category, flag)


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
    payload["history"] = _writeup_history(payload, "failed", clean_issue)
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


def writeup_review_verdict(settings: object, challenge_name: str) -> str:
    """Return a fresh persisted review verdict, ignoring stale prior attempts."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    output_dir = root / "_shared" / "writeup"
    manifest_path = output_dir / "manifest.json"
    review_path = output_dir / "REVIEW.md"
    try:
        state = json.loads(manifest_path.read_text(encoding="utf-8"))
        review = review_path.read_text(encoding="utf-8", errors="replace")[:100_000]
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(state, dict):
        return ""
    previous_hash = str(state.get("previous_review_sha256", ""))
    if previous_hash and previous_hash == _file_sha256(review_path):
        return ""
    if _APPROVED_REVIEW_RE.search(review):
        return "approved"
    if _REJECTED_REVIEW_RE.search(review):
        return "rejected"
    return ""


def finalize_writeup(
    settings: object,
    challenge_name: str,
    category: str,
    flag: str = "",
    *,
    prefer_canonical: bool = False,
    require_screenshots: bool = False,
    require_review: bool = False,
) -> dict[str, Any]:
    """Assemble a canonical writeup without inventing evidence."""
    root = Path(challenge_workspace_path(settings, challenge_name))
    output_dir = root / "_shared" / "writeup"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    writeup_path = output_dir / "WRITEUP.md"
    review_path = output_dir / "REVIEW.md"
    source, report = _source_report(root, prefer_canonical=prefer_canonical)
    images, reproducers = _discover_artifacts(root)
    manifest_images = images
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

    if prefer_canonical and source == writeup_path:
        quality_issues, selected_images = _review_quality_issues(
            report,
            writeup_path,
            root,
            reproducers,
        )
        issues.extend(issue for issue in quality_issues if issue not in issues)
        manifest_images = selected_images

    review_approved = False
    if require_review:
        try:
            review = review_path.read_text(encoding="utf-8", errors="replace")[:100_000]
        except OSError:
            review = ""
        review_approved = bool(_APPROVED_REVIEW_RE.search(review))
        if not review_approved:
            issues.append("Luna 검수 결과가 없거나 APPROVED가 아닙니다")
        elif (
            generation_state.get("previous_review_sha256")
            and generation_state.get("previous_review_sha256") == _file_sha256(review_path)
        ):
            review_approved = False
            issues.append("Luna 검수 에이전트가 REVIEW.md를 갱신하지 못했습니다")

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
        "review_model_spec": generation_state.get("review_model_spec", ""),
        "phase": "complete" if status == "complete" else "needs_attention",
        "reviewed": review_approved,
        "writeup_path": _relative(writeup_path, root) if writeup_path.is_file() else "",
        "review_path": _relative(review_path, root) if review_path.is_file() else "",
        "source_path": _relative(source, root) if source else "",
        "issues": issues,
        "history": _writeup_history(
            generation_state,
            "completed" if status == "complete" else "needs_attention",
            "라이트업 품질 검사를 완료했습니다",
        ),
        "screenshots": _image_manifest_entries(manifest_images, root),
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
