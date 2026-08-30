"""Locate the Codex CLI reliably across shells and Windows launchers."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_authenticated_executables: set[str] = set()


class CodexCLIError(RuntimeError):
    """Actionable Codex CLI setup error."""


def _extension_candidates() -> list[Path]:
    """Return Codex binaries bundled with common VS Code-compatible editors."""
    if os.name != "nt":
        return []

    user_dir = Path.home()
    roots = (
        user_dir / ".vscode" / "extensions",
        user_dir / ".vscode-insiders" / "extensions",
        user_dir / ".cursor" / "extensions",
    )
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob("openai.chatgpt-*/bin/windows-*/codex.exe"))
    return candidates


def resolve_codex_executable(configured_path: str | None = None) -> str:
    """Resolve Codex to an absolute executable path suitable for subprocess APIs."""
    configured = (configured_path or os.getenv("CODEX_CLI_PATH", "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return str(Path(resolved).resolve())
        raise CodexCLIError(
            f"Configured Codex CLI was not found: {configured}. "
            "Fix CODEX_CLI_PATH or remove it to enable auto-discovery."
        )

    resolved = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if not resolved:
        resolved = shutil.which("codex")
    if resolved:
        return str(Path(resolved).resolve())

    extension_bins = _extension_candidates()
    if extension_bins:
        newest = max(extension_bins, key=lambda path: path.stat().st_mtime)
        return str(newest.resolve())

    raise CodexCLIError(
        "Codex CLI executable not found. Run `codex --version`, or set "
        "CODEX_CLI_PATH to the full codex.exe path."
    )


async def prepare_codex_cli(configured_path: str | None = None) -> str:
    """Resolve Codex and verify that its local CLI session is authenticated."""
    executable = resolve_codex_executable(configured_path)
    if executable in _authenticated_executables:
        return executable

    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "login",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except TimeoutError as exc:
        raise CodexCLIError("Timed out while checking `codex login status`.") from exc
    except OSError as exc:
        raise CodexCLIError(f"Could not launch Codex CLI at {executable}: {exc}") from exc

    status = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = f" ({status})" if status else ""
        raise CodexCLIError(
            f"Codex CLI is not authenticated{detail}. Run `codex login`, "
            "verify with `codex login status`, then retry."
        )

    _authenticated_executables.add(executable)
    return executable
