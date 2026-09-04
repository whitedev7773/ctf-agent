from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.codex_cli import (
    CodexCLIError,
    _authenticated_executables,
    prepare_codex_cli,
    resolve_codex_executable,
)


class _FakeProcess:
    def __init__(self, returncode: int, output: bytes) -> None:
        self.returncode = returncode
        self.output = output

    async def communicate(self) -> tuple[bytes, None]:
        return self.output, None


class CodexCLIResolutionTests(unittest.TestCase):
    def test_path_lookup_returns_absolute_executable(self) -> None:
        executable = str(Path.cwd() / "tools" / "codex.exe")
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("backend.codex_cli.os.name", "nt"),
            patch("backend.codex_cli.shutil.which", return_value=executable),
        ):
            self.assertEqual(resolve_codex_executable(), str(Path(executable).resolve()))

    def test_invalid_explicit_path_has_actionable_error(self) -> None:
        with (
            patch.dict(os.environ, {"CODEX_CLI_PATH": "missing-codex.exe"}, clear=True),
            patch("backend.codex_cli.shutil.which", return_value=None),
            self.assertRaisesRegex(CodexCLIError, "CODEX_CLI_PATH"),
        ):
            resolve_codex_executable()


class CodexCLIAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        _authenticated_executables.clear()

    async def test_not_logged_in_has_actionable_error(self) -> None:
        with (
            patch("backend.codex_cli.resolve_codex_executable", return_value="codex.exe"),
            patch(
                "backend.codex_cli.asyncio.create_subprocess_exec",
                return_value=_FakeProcess(1, b"Not logged in"),
            ),
            self.assertRaisesRegex(CodexCLIError, "codex login"),
        ):
            await prepare_codex_cli()

    async def test_authenticated_executable_is_cached(self) -> None:
        fake_launch = AsyncMock(return_value=_FakeProcess(0, b"Logged in using ChatGPT"))
        with (
            patch("backend.codex_cli.resolve_codex_executable", return_value="codex.exe"),
            patch("backend.codex_cli.asyncio.create_subprocess_exec", fake_launch),
        ):
            self.assertEqual(await prepare_codex_cli(), "codex.exe")
            self.assertEqual(await prepare_codex_cli(), "codex.exe")

        fake_launch.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
