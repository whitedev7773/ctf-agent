"""Dashboard HTTP and security boundary tests."""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession, FormData

from backend.artifacts import challenge_workspace_path
from backend.ctfd import CTFdClient
from backend.dashboard.server import DashboardServer, _clear_runtime_root
from backend.prompts import ChallengeMeta, build_prompt
from backend.runtime_settings import runtime_settings_path
from backend.runtime_state import load_dismissed_challenges
from backend.tracing import SolverTracer
from backend.writeups import finalize_writeup


class DashboardServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temp_dir.name) / "runtime"
        self.challenges_root = self.runtime_root / "challenges"
        self.workspace_root = self.runtime_root / "workspace"
        self.logs_root = self.runtime_root / "logs"
        self.experience_root = Path(self.temp_dir.name) / "experience"
        for root in (
            self.challenges_root,
            self.workspace_root,
            self.logs_root,
            self.experience_root,
        ):
            root.mkdir(parents=True)
        settings = SimpleNamespace(
            ctfd_url="",
            ctfd_token="",
            ctfd_user="",
            ctfd_pass="",
            workspace_root=str(self.workspace_root),
            experience_root=str(self.experience_root),
            logs_root=str(self.logs_root),
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
            candidates={},
            dismissed_challenges=set(),
            model_specs=["codex/gpt-5.6-luna/low"],
            max_concurrent_challenges=3,
            no_submit=True,
            force_no_submit=False,
            challenges_root=str(self.challenges_root),
            challenge_dirs={},
            coordinator_inbox=asyncio.Queue(),
            operator_inbox=asyncio.Queue(),
        )
        self.poller = SimpleNamespace(
            known_challenges={"web/intro"},
            known_solved=set(),
            last_error="",
            reseed=AsyncMock(),
            drain_events=MagicMock(return_value=[]),
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
            self.assertEqual(payload["runtime_revision"], 19)
            self.assertTrue(callable(self.deps.request_writeup_generation))
            self.assertFalse(payload["restart_required"])
            self.assertTrue(payload["runtime_policy"]["adaptive_delegation"])
            self.assertTrue(payload["runtime_policy"]["in_turn_budget_interrupt"])
            self.assertTrue(payload["runtime_policy"]["runtime_state_persistence"])
            self.assertEqual(payload["runtime_settings"]["models"], ["codex/gpt-5.6-luna/low"])

        async with self.client.get(f"{self.base_url}/api/session") as response:
            session = await response.json()
            self.assertTrue(session["capabilities"]["delete_challenge"])
            self.assertEqual(payload["runtime_policy"]["turn_idle_timeout_seconds"], 300)
            self.assertEqual(payload["runtime_policy"]["max_raw_tokens"], 12_000_000)
            self.assertEqual(payload["runtime_policy"]["delegate_max_attempts"], 4)
            self.assertTrue(
                payload["runtime_policy"]["delegate_postprocess_on_budget_stop"]
            )
            self.assertEqual(
                payload["runtime_policy"]["delegate_postprocess_max_tokens"],
                80_000,
            )
            self.assertEqual(payload["stats"]["effective_tokens"], 0)
            self.assertEqual(payload["stats"]["documented"], 0)
            self.assertEqual(payload["resources"]["container_count"], 0)
            self.assertEqual(payload["experience"]["record_count"], 0)
            self.assertEqual(payload["challenges"][0]["approach_notes"], [])

        async with self.client.get(f"{self.base_url}/api/resources") as response:
            resources = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(resources["resources"]["container_count"], 0)

        async with self.client.get(f"{self.base_url}/assets/dashboard.js") as response:
            javascript = await response.text()
            self.assertIn("지금까지의 접근 노트", javascript)
            self.assertIn('writeup.status === "generating"', javascript)
            self.assertIn("refresh({ renderSelected: true })", javascript)
            self.assertIn("captureDetailViewState", javascript)
            self.assertIn("document.createDocumentFragment()", javascript)
            self.assertIn("if (changed && !editing)", javascript)

        html_ids = re.findall(r'\bid="([^"]+)"', html)
        javascript_refs = {
            item
            for item in re.findall(r'byId\("([^"]+)"\)', javascript)
            if item.startswith("runtime-")
        }
        self.assertEqual(len(html_ids), len(set(html_ids)))
        self.assertEqual(javascript_refs - set(html_ids), set())

    async def test_detail_notes_are_bounded_and_loaded_from_shared_state(self) -> None:
        from backend.artifacts import challenge_shared_path

        shared = Path(challenge_shared_path(self.deps.settings, "web/intro"))
        state_file = shared / "lead" / "STATE.md"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(
            "# Solver state\n"
            "## Confirmed\n- 인증 우회 조건을 소스에서 확인함\n"
            "## Current blocker\n- nonce 재사용 여부를 동적으로 검증해야 함\n"
            "## Next experiment\n- 두 세션의 nonce를 캡처해 비교\n"
            "## Reproduction\n```sh\ncurl example.invalid\n```\n",
            encoding="utf-8",
        )
        recovery = shared / "recovery" / "delegate-01-budget-stop.md"
        recovery.parent.mkdir(parents=True)
        recovery.write_text(
            "# Budget stop\n"
            "- Source agent: `delegate-01`\n"
            "- Stop reason: token budget exhausted\n"
            "- Tool steps: 17\n"
            "- Original task: allocator stride 측정\n"
            "## Last findings\n- stride 후보를 0x130으로 좁힘\n",
            encoding="utf-8",
        )

        async with self.client.get(f"{self.base_url}/api/status") as response:
            payload = await response.json()

        notes = payload["challenges"][0]["approach_notes"]
        self.assertLessEqual(len(notes), 6)
        self.assertEqual(notes[0]["source"], "lead/STATE.md")
        self.assertIn("인증 우회", notes[0]["text"])
        self.assertTrue(any("nonce 재사용" in note["text"] for note in notes))
        self.assertFalse(any("curl" in note["text"] for note in notes))
        self.assertFalse(any("Source agent" in note["text"] for note in notes))
        self.assertFalse(any("Tool steps" in note["text"] for note in notes))

    async def test_cancelled_solver_without_outcome_is_not_labeled_winner(self) -> None:
        model_spec = "codex/gpt-5.6-sol/xhigh"
        cancelled = asyncio.Event()
        cancelled.set()
        self.deps.swarms["web/intro"] = SimpleNamespace(
            model_specs=[model_spec],
            solvers={model_spec: SimpleNamespace(
                sandbox=SimpleNamespace(
                    workspace_dir="workspace",
                    resource_snapshot=lambda: {},
                ),
                tracer=SimpleNamespace(path=""),
                _step_count=3,
                _budget_stop_reason="",
                _checkpoint_stop_reason="",
            )},
            outcomes={},
            winner=None,
            findings={},
            waiting_models=set(),
            cancel_event=cancelled,
        )

        async with self.client.get(f"{self.base_url}/api/status") as response:
            payload = await response.json()

        self.assertEqual(payload["challenges"][0]["agents"][0]["status"], "finished")

    async def test_active_agent_exposes_idle_watchdog_state(self) -> None:
        model_spec = "codex/gpt-5.6-sol/xhigh"
        solver = SimpleNamespace(
            sandbox=SimpleNamespace(
                workspace_dir="workspace",
                resource_snapshot=lambda: {},
            ),
            tracer=SimpleNamespace(path="trace.jsonl"),
            _step_count=9,
            _budget_stop_reason="",
            _checkpoint_stop_reason="",
            _resume_stop_reason="",
            activity_idle_seconds=lambda: 42.5,
            tool_call_active=False,
        )
        self.deps.swarms["web/intro"] = SimpleNamespace(
            model_specs=[model_spec],
            solvers={model_spec: solver},
            outcomes={},
            winner=None,
            findings={},
            waiting_models=set(),
            cancel_event=asyncio.Event(),
        )
        task = asyncio.create_task(asyncio.Event().wait())
        self.deps.swarm_tasks["web/intro"] = task
        try:
            async with self.client.get(f"{self.base_url}/api/status") as response:
                payload = await response.json()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        agent = payload["challenges"][0]["agents"][0]
        self.assertEqual(agent["status"], "running")
        self.assertEqual(agent["idle_seconds"], 42.5)
        self.assertEqual(agent["idle_limit_seconds"], 300)
        self.assertFalse(agent["tool_call_active"])

    async def test_writeup_api_serves_text_but_rejects_artifact_traversal(self) -> None:
        from backend.artifacts import challenge_shared_path

        shared = Path(challenge_shared_path(self.deps.settings, "web/intro"))
        lead = shared / "lead"
        lead.mkdir(parents=True)
        (lead / "SOLUTION.md").write_text(
            "# 풀이\n\n## 증거\n조작한 요청을 전송하자 인증 검사 없이 관리자 응답이 반환되는 것을 확인했습니다.\n\n"
            "## 재현\n```sh\npython3 solve.py\n```\n",
            encoding="utf-8",
        )
        (lead / "solve.py").write_text("print('ok')\n", encoding="utf-8")
        self.deps.results["web/intro"] = {"flag": "TEAM{done}"}
        finalize_writeup(self.deps.settings, "web/intro", "Web", "TEAM{done}")

        async with self.client.get(
            f"{self.base_url}/api/writeup",
            params={"challenge": "web/intro"},
        ) as response:
            payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIn("## 검증", payload["content"])
        self.assertTrue(payload["writeup"]["documented"])

        async with self.client.get(
            f"{self.base_url}/api/artifact",
            params={"challenge": "web/intro", "path": "../../.env"},
        ) as response:
            self.assertEqual(response.status, 404)

    async def test_writeup_can_be_requested_after_restart(self) -> None:
        from backend.artifacts import challenge_shared_path

        shared = Path(challenge_shared_path(self.deps.settings, "web/intro"))
        lead = shared / "lead"
        lead.mkdir(parents=True)
        (lead / "SOLUTION.md").write_text(
            "# 복구된 풀이\n\n"
            "## 증거\n서버 재시작 뒤에도 보존된 요청을 사용해 인증 우회를 동일하게 재현했습니다.\n\n"
            "## 재현\n```sh\npython solve.py\n```\n",
            encoding="utf-8",
        )
        (lead / "solve.py").write_text("print('reproduced')\n", encoding="utf-8")
        generation_started = asyncio.Event()
        finish_generation = asyncio.Event()

        class FakeWriteupSolver:
            model_spec = "codex/gpt-5.6-luna/low"
            agent_name = "web/intro/codex/gpt-5.6-luna/low/writeup"
            _step_count = 1
            tracer = SimpleNamespace(path="writeup-trace.jsonl")
            sandbox = SimpleNamespace(
                workspace_dir="writeup-workspace",
                resource_snapshot=lambda: {"status": "running"},
            )

            async def run_until_done_or_gave_up(self):
                generation_started.set()
                await finish_generation.wait()
                output = shared / "writeup"
                evidence = output / "evidence"
                evidence.mkdir(parents=True, exist_ok=True)
                (output / "WRITEUP.md").write_text(
                    "# web/intro 풀이\n\n"
                    "## 요약\n인증 검사가 누락된 요청 경로를 이용해 관리자 전용 응답을 확인한 문제입니다.\n\n"
                    "## 취약점 또는 핵심 원리\n서버가 요청의 권한 정보를 검증하지 않아 일반 사용자 입력이 관리자 처리 경로에 도달했습니다.\n\n"
                    "## 풀이 과정\n보존된 요청과 응답을 비교해 권한 검사 누락 지점을 찾고 조작한 요청을 두 번 전송해 같은 결과를 확인했습니다.\n\n"
                    "## 재현 방법\n```sh\npython solve.py\n```\n\n"
                    "## 검증\n재현 스크립트 실행 결과 관리자 응답과 검증된 결과가 일치했습니다.\n",
                    encoding="utf-8",
                )
                (evidence / "admin-response.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n" + b"0" * 24
                )
                return SimpleNamespace(status="gave_up")

            async def stop(self):
                return None

        fake_solver = FakeWriteupSolver()
        self.server._create_writeup_solver = MagicMock(return_value=fake_solver)
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        endpoint = f"{self.base_url}/api/control/request-writeup"
        async with self.client.post(
            endpoint,
            json={"challenge": "web/intro"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            self.assertEqual(response.status, 409)

        # A fresh coordinator can know the solve from CTFd even when the local
        # result record (including the submitted flag) was not persisted.
        self.poller.known_solved.add("web/intro")
        async with self.client.post(
            endpoint,
            json={"challenge": "web/intro"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 202)
        self.assertEqual(payload["writeup"]["status"], "generating")
        await generation_started.wait()
        task = self.server._writeup_tasks["web/intro"]

        async with self.client.get(f"{self.base_url}/api/status") as response:
            generating = await response.json()
        self.assertEqual(generating["challenges"][0]["writeup"]["status"], "generating")
        self.assertTrue(generating["challenges"][0]["writeup"]["active"])

        async with self.client.post(
            endpoint,
            json={"challenge": "web/intro"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            self.assertEqual(response.status, 409)

        finish_generation.set()
        await task

        async with self.client.get(
            f"{self.base_url}/api/writeup",
            params={"challenge": "web/intro"},
        ) as response:
            recovered = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(recovered["writeup"]["documented"])
        self.assertEqual(recovered["writeup"]["status"], "complete")
        self.assertEqual(len(recovered["writeup"]["screenshots"]), 1)
        self.assertIn("## 주요 스크린샷", recovered["content"])

    async def test_dashboard_mutation_requires_session_token(self) -> None:
        endpoint = f"{self.base_url}/api/operator/message"
        async with self.client.post(endpoint, json={"message": "prioritize web"}) as response:
            self.assertEqual(response.status, 403)

        async with self.client.get(f"{self.base_url}/api/session") as response:
            session = await response.json()
            token = session["csrf_token"]
            self.assertTrue(session["capabilities"]["reset_runtime"])

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

    async def test_runtime_settings_are_validated_persisted_and_reset(self) -> None:
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]
        headers = {"X-CTF-Dashboard-Token": token}
        endpoint = f"{self.base_url}/api/settings/runtime"

        async with self.client.post(
            endpoint,
            json={
                "models": ["codex/gpt-5.6-sol/high"],
                "max_concurrent_challenges": 2,
                "solver_max_estimated_cost_usd": 1.25,
            },
            headers=headers,
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["settings"]["models"], ["codex/gpt-5.6-sol/high"])
        self.assertEqual(self.deps.model_specs, ["codex/gpt-5.6-sol/high"])
        self.assertEqual(self.deps.max_concurrent_challenges, 2)
        self.assertEqual(self.deps.settings.solver_max_estimated_cost_usd, 1.25)
        saved = runtime_settings_path(self.deps.challenges_root).read_text(encoding="utf-8")
        self.assertNotIn("ctfd_token", saved)
        self.assertNotIn("secret", saved)

        async with self.client.get(endpoint) as response:
            current = await response.json()
        self.assertEqual(current["settings"]["max_concurrent_challenges"], 2)

        async with self.client.post(
            endpoint,
            json={"solver_turn_timeout_seconds": 60, "solver_turn_idle_timeout_seconds": 61},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 400)
        async with self.client.post(
            endpoint,
            json={"openai_api_key": "must-not-be-accepted"},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 400)

        async with self.client.post(
            f"{endpoint}/reset",
            json={},
            headers=headers,
        ) as response:
            reset = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(reset["settings"]["models"], ["codex/gpt-5.6-sol/high"])
        self.assertEqual(self.deps.max_concurrent_challenges, 1)

    async def test_runtime_settings_do_not_mutate_an_active_swarm_snapshot(self) -> None:
        swarm_settings = SimpleNamespace(solver_max_steps=300)
        swarm = SimpleNamespace(
            settings=swarm_settings,
            model_specs=[],
            solvers={},
            outcomes={},
            winner=None,
            findings={},
            waiting_models=set(),
            cancel_event=asyncio.Event(),
        )
        task = asyncio.create_task(asyncio.Event().wait())
        self.deps.swarms["web/intro"] = swarm
        self.deps.swarm_tasks["web/intro"] = task
        try:
            async with self.client.get(f"{self.base_url}/api/session") as response:
                token = (await response.json())["csrf_token"]
            async with self.client.post(
                f"{self.base_url}/api/settings/runtime",
                json={"solver_max_steps": 50},
                headers={"X-CTF-Dashboard-Token": token},
            ) as response:
                payload = await response.json()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["active_swarms_unchanged"], 1)
        self.assertEqual(swarm_settings.solver_max_steps, 300)
        self.assertEqual(self.deps.settings.solver_max_steps, 50)

    async def test_unverified_candidate_is_not_counted_as_solved(self) -> None:
        self.deps.candidates["web/intro"] = {
            "flag": "TEAM{guess}",
            "flags": ["TEAM{guess}"],
            "status": "unverified",
            "review_required": True,
        }

        async with self.client.get(f"{self.base_url}/api/status") as response:
            payload = await response.json()

        challenge = payload["challenges"][0]
        self.assertEqual(challenge["status"], "candidate")
        self.assertEqual(challenge["candidate"], "TEAM{guess}")
        self.assertTrue(challenge["candidate_review_required"])
        self.assertFalse(challenge["solved"])
        self.assertEqual(payload["stats"]["solved"], 0)
        self.assertEqual(payload["stats"]["candidates"], 1)

    async def test_operator_can_confirm_pending_local_candidate(self) -> None:
        self.deps.candidates["web/intro"] = {
            "flag": "TEAM{guess}",
            "flags": ["TEAM{guess}"],
            "status": "unverified",
            "review_required": True,
        }
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            f"{self.base_url}/api/control/review-candidate",
            json={
                "challenge": "web/intro",
                "flag": "TEAM{guess}",
                "accepted": True,
            },
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertIn("LOCAL CONFIRMED", payload["message"])
        self.assertEqual(self.deps.results["web/intro"]["flag"], "TEAM{guess}")
        self.assertNotIn("web/intro", self.deps.candidates)

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

    async def test_delete_challenge_stops_work_and_removes_only_its_runtime_data(self) -> None:
        challenge_dir = self.challenges_root / "web-intro"
        challenge_dir.mkdir()
        (challenge_dir / "metadata.yml").write_text("name: web/intro\n", encoding="utf-8")
        self.deps.challenge_dirs["web/intro"] = str(challenge_dir)

        workspace_dir = Path(challenge_workspace_path(self.deps.settings, "web/intro"))
        (workspace_dir / "solve.py").write_text("print('solve')", encoding="utf-8")
        tracer = SolverTracer("web/intro", "codex/test", str(self.logs_root))
        tracer.event("start")
        tracer.close()
        experience_file = self.experience_root / "web" / "shared.md"
        experience_file.parent.mkdir()
        experience_file.write_text("keep", encoding="utf-8")

        swarm = SimpleNamespace(kill=MagicMock())
        task = asyncio.create_task(asyncio.Event().wait())
        self.deps.swarms["web/intro"] = swarm
        self.deps.swarm_tasks["web/intro"] = task
        self.deps.results["web/intro"] = {"flag": "TEAM{done}"}
        self.deps.candidates["web/intro"] = {"flag": "TEAM{maybe}"}
        self.cost_tracker.by_agent["web/intro/codex/test"] = object()

        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]
        endpoint = f"{self.base_url}/api/challenges/delete"
        headers = {"X-CTF-Dashboard-Token": token}
        async with self.client.post(
            endpoint,
            json={"challenge": "web/intro", "confirmation": "wrong"},
            headers=headers,
        ) as response:
            self.assertEqual(response.status, 400)
        self.assertTrue(challenge_dir.exists())

        async with self.client.post(
            endpoint,
            json={"challenge": "web/intro", "confirmation": "web/intro"},
            headers=headers,
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["experience_preserved"])
        swarm.kill.assert_called_once_with()
        self.assertTrue(task.cancelled())
        self.assertFalse(challenge_dir.exists())
        self.assertFalse(workspace_dir.exists())
        self.assertEqual(list(self.logs_root.iterdir()), [])
        self.assertEqual(experience_file.read_text(encoding="utf-8"), "keep")
        self.assertNotIn("web/intro", self.deps.results)
        self.assertNotIn("web/intro", self.deps.candidates)
        self.assertNotIn("web/intro/codex/test", self.cost_tracker.by_agent)
        self.assertIn("web/intro", self.deps.dismissed_challenges)
        self.assertIn("web/intro", load_dismissed_challenges(self.deps.settings))

        async with self.client.get(f"{self.base_url}/api/status") as response:
            status = await response.json()
        self.assertEqual(status["challenges"], [])
        self.assertEqual(status["stats"]["total"], 0)
        self.assertEqual(status["stats"]["solved"], 0)

    async def test_runtime_reset_requires_exact_korean_confirmation(self) -> None:
        marker = self.workspace_root / "keep-until-confirmed.txt"
        marker.write_text("solver state", encoding="utf-8")
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            f"{self.base_url}/api/control/reset-runtime",
            json={"confirmation": "reset"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            message = await response.text()

        self.assertEqual(response.status, 400)
        self.assertIn("초기화", message)
        self.assertTrue(marker.exists())
        self.assertIn("web/intro", self.deps.challenge_metas)

    async def test_runtime_reset_rejects_project_source_path(self) -> None:
        self.deps.settings.workspace_root = str(Path.cwd())
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            f"{self.base_url}/api/control/reset-runtime",
            json={"confirmation": "초기화"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            message = await response.text()

        self.assertEqual(response.status, 409)
        self.assertIn("unsafe runtime path", message)
        self.assertIn("web/intro", self.deps.challenge_metas)

    async def test_runtime_reset_clears_traces_environment_and_memory_only(self) -> None:
        challenge_file = self.challenges_root / "local-pwn" / "metadata.yml"
        challenge_file.parent.mkdir()
        challenge_file.write_text("name: local-pwn", encoding="utf-8")
        workspace_file = self.workspace_root / "local-pwn" / "solve.py"
        workspace_file.parent.mkdir()
        workspace_file.write_text("print('work')", encoding="utf-8")
        trace_file = self.logs_root / "trace.jsonl"
        trace_file.write_text("{}\n", encoding="utf-8")
        preserved = Path(self.temp_dir.name) / ".env"
        preserved.write_text("OPENAI_API_KEY=preserved", encoding="utf-8")
        experience_file = self.experience_root / "pwn" / "prior.md"
        experience_file.parent.mkdir()
        experience_file.write_text("persistent knowledge", encoding="utf-8")

        await self.deps.ctfd.configure("https://ctf.example.com", "secret")
        self.deps.settings.ctfd_url = "https://ctf.example.com"
        self.deps.settings.ctfd_token = "secret"
        self.deps.results["web/intro"] = {"flag": "TEAM{done}"}
        self.deps.candidates["candidate"] = {"flag": "TEAM{maybe}"}
        self.deps.challenge_dirs["web/intro"] = str(challenge_file.parent)
        self.cost_tracker.by_agent["solver"] = object()
        self.deps.coordinator_inbox.put_nowait("old solver message")
        self.deps.operator_inbox.put_nowait("old operator message")

        swarm = SimpleNamespace(kill=MagicMock())
        task = asyncio.create_task(asyncio.Event().wait())
        self.deps.swarms["web/intro"] = swarm
        self.deps.swarm_tasks["web/intro"] = task

        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]
        async with self.client.post(
            f"{self.base_url}/api/control/reset-runtime",
            json={"confirmation": "초기화"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["retained_locked_entries"], 0)
        swarm.kill.assert_called_once_with()
        self.assertTrue(task.cancelled())
        for root in (self.challenges_root, self.workspace_root, self.logs_root):
            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(preserved.read_text(encoding="utf-8"), "OPENAI_API_KEY=preserved")
        self.assertEqual(experience_file.read_text(encoding="utf-8"), "persistent knowledge")
        self.assertFalse(self.deps.ctfd.is_configured)
        self.assertTrue(self.deps.no_submit)
        self.assertEqual(self.deps.swarms, {})
        self.assertEqual(self.deps.swarm_tasks, {})
        self.assertEqual(self.deps.results, {})
        self.assertEqual(self.deps.candidates, {})
        self.assertEqual(self.deps.dismissed_challenges, set())
        self.assertEqual(self.deps.challenge_dirs, {})
        self.assertEqual(self.deps.challenge_metas, {})
        self.assertEqual(self.cost_tracker.by_agent, {})
        self.assertTrue(self.deps.coordinator_inbox.empty())
        self.assertTrue(self.deps.operator_inbox.empty())
        self.poller.reseed.assert_awaited_once()
        self.poller.drain_events.assert_called_once_with()

    async def test_experience_reset_is_independent_and_requires_confirmation(self) -> None:
        record = self.experience_root / "web" / "record.md"
        record.parent.mkdir()
        record.write_text("verified tactic", encoding="utf-8")
        runtime_marker = self.workspace_root / "keep.txt"
        runtime_marker.write_text("active workspace", encoding="utf-8")
        async with self.client.get(f"{self.base_url}/api/session") as response:
            token = (await response.json())["csrf_token"]

        async with self.client.post(
            f"{self.base_url}/api/control/reset-experience",
            json={"confirmation": "wrong"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            self.assertEqual(response.status, 400)
        self.assertTrue(record.is_file())

        async with self.client.post(
            f"{self.base_url}/api/control/reset-experience",
            json={"confirmation": "경험 초기화"},
            headers={"X-CTF-Dashboard-Token": token},
        ) as response:
            payload = await response.json()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(record.exists())
        self.assertEqual(runtime_marker.read_text(encoding="utf-8"), "active workspace")

    def test_runtime_clear_retains_locked_file_and_continues(self) -> None:
        removable = self.logs_root / "old.log"
        locked = self.logs_root / "coordinator.stderr.log"
        removable.write_text("old", encoding="utf-8")
        locked.write_text("active", encoding="utf-8")
        original_unlink = Path.unlink

        def unlink_unless_locked(path: Path) -> None:
            if path == locked:
                raise PermissionError("file is in use")
            original_unlink(path)

        with patch.object(Path, "unlink", autospec=True, side_effect=unlink_unless_locked):
            removed, retained = _clear_runtime_root(self.logs_root)

        self.assertEqual(removed, 1)
        self.assertEqual(retained, [locked])
        self.assertFalse(removable.exists())
        self.assertTrue(locked.exists())


if __name__ == "__main__":
    unittest.main()
