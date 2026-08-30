"""Dashboard HTTP and security boundary tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, FormData

from backend.ctfd import CTFdClient
from backend.dashboard.server import DashboardServer
from backend.prompts import ChallengeMeta, build_prompt


class DashboardServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = SimpleNamespace(
            ctfd_url="",
            ctfd_token="",
            ctfd_user="",
            ctfd_pass="",
        )
        self.deps = SimpleNamespace(
            ctfd=CTFdClient(),
            settings=settings,
            challenge_metas={
                "web/intro": SimpleNamespace(category="Web", value=100, solves=12),
            },
            swarms={},
            swarm_tasks={},
            results={},
            model_specs=["codex:gpt-5.6-luna"],
            max_concurrent_challenges=3,
            no_submit=True,
            force_no_submit=False,
            challenges_root=self.temp_dir.name,
            challenge_dirs={},
            operator_inbox=asyncio.Queue(),
        )
        self.poller = SimpleNamespace(
            known_challenges={"web/intro"},
            known_solved=set(),
            last_error="",
            reseed=AsyncMock(),
        )
        self.cost_tracker = SimpleNamespace(
            by_agent={},
            total_cost_usd=0.0,
            total_tokens=0,
        )
        self.server = DashboardServer(
            self.deps,
            self.poller,
            self.cost_tracker,
            port=0,
        )
        await self.server.start()
        self.base_url = f"http://127.0.0.1:{self.server.actual_port}"
        self.client = ClientSession()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()
        await self.deps.ctfd.close()
        self.temp_dir.cleanup()

    async def test_status_and_assets_are_served_locally(self) -> None:
        async with self.client.get(f"{self.base_url}/") as response:
            html = await response.text()
            self.assertEqual(response.status, 200)
            self.assertIn("CTF Agent", html)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

        async with self.client.get(f"{self.base_url}/api/status") as response:
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["stats"]["total"], 1)
            self.assertEqual(payload["challenges"][0]["name"], "web/intro")
            self.assertEqual(payload["challenges"][0]["status"], "idle")

    async def test_dashboard_mutation_requires_session_token(self) -> None:
        endpoint = f"{self.base_url}/api/operator/message"
        async with self.client.post(endpoint, json={"message": "prioritize web"}) as response:
            self.assertEqual(response.status, 403)

        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            endpoint,
            json={"message": "prioritize web"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(self.deps.operator_inbox.get_nowait(), "prioritize web")

    async def test_legacy_message_endpoint_only_accepts_json(self) -> None:
        endpoint = f"{self.base_url}/msg"
        async with self.client.post(endpoint, data="message=hello") as response:
            self.assertEqual(response.status, 415)

        async with self.client.post(endpoint, json={"message": "hello"}) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(self.deps.operator_inbox.get_nowait(), "hello")

    async def test_dashboard_can_switch_to_standalone_mode(self) -> None:
        await self.deps.ctfd.configure("https://ctf.example.com", "secret")
        self.deps.no_submit = False
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            f"{self.base_url}/api/settings/ctfd",
            json={"url": ""},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            self.assertEqual(response.status, 200)

        self.assertFalse(self.deps.ctfd.is_configured)
        self.assertTrue(self.deps.no_submit)
        self.poller.reseed.assert_awaited_once()

    async def test_dashboard_registers_local_challenge_with_attachment(self) -> None:
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        form = FormData()
        form.add_field("name", "local-pwn")
        form.add_field("category", "pwn")
        form.add_field("description", "플래그를 찾으세요")
        form.add_field("connection_info", "nc example.com 31337")
        form.add_field("flag_format", "TEAM{...}")
        form.add_field("value", "150")
        form.add_field("files", b"ELF", filename="chall", content_type="application/octet-stream")
        form.add_field("files", b"LIBC", filename="libc.so.6", content_type="application/octet-stream")
        async with self.client.post(
            f"{self.base_url}/api/challenges/local",
            data=form,
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["challenge"], "local-pwn")
            self.assertEqual(payload["file_count"], 2)

        challenge_dir = self.deps.challenge_dirs["local-pwn"]
        self.assertEqual(self.deps.challenge_metas["local-pwn"].value, 150)
        self.assertEqual(self.deps.challenge_metas["local-pwn"].flag_format, "TEAM{...}")
        reloaded = ChallengeMeta.from_yaml(Path(challenge_dir) / "metadata.yml")
        self.assertEqual(reloaded.description, "플래그를 찾으세요")
        self.assertIn(
            "**Flag format**: `TEAM{...}`",
            build_prompt(self.deps.challenge_metas["local-pwn"], []),
        )
        self.assertEqual((Path(challenge_dir) / "distfiles" / "chall").read_bytes(), b"ELF")
        self.assertEqual((Path(challenge_dir) / "distfiles" / "libc.so.6").read_bytes(), b"LIBC")


if __name__ == "__main__":
    unittest.main()
