from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.agents.codex_solver import CodexSolver
from backend.agents.coordinator_core import (
    _generate_or_finalize_writeup,
    do_review_candidate,
    do_spawn_swarm,
)
from backend.agents.coordinator_core import do_submit_flag as coordinator_submit_flag
from backend.agents.coordinator_loop import _unsolved_names
from backend.agents.swarm import ChallengeSwarm
from backend.artifacts import (
    challenge_shared_path,
    handoff_quality_issues,
    solver_workspace_path,
    workspace_progress_signature,
    workspace_resume_manifest,
    write_checkpoint,
)
from backend.budgets import (
    solver_runtime_limit,
    solver_step_limit,
    solver_token_limits,
    solver_turn_idle_timeout_limit,
    solver_turn_timeout_limit,
    token_metrics,
)
from backend.challenge_profiles import (
    category_playbook,
    external_skill_path,
    solver_lane,
    solver_role,
)
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.flag_format import flag_matches_format
from backend.loop_detect import LoopDetector
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS
from backend.output_types import assess_solver_output, solver_output_json_schema
from backend.prompts import ChallengeMeta, build_prompt
from backend.runtime_settings import RuntimeSettings, load_runtime_settings, save_runtime_settings
from backend.runtime_state import (
    load_dismissed_challenges,
    load_runtime_state,
    runtime_state_path,
    save_runtime_state,
)
from backend.solver_base import (
    BUDGET_EXHAUSTED,
    CANDIDATE_FOUND,
    FLAG_FOUND,
    GAVE_UP,
    HANDOFF_COMPLETE,
    PROGRESS_CHECKPOINT,
    SolverResult,
)
from backend.tools.core import _truncate


class ArtifactAndProfileTests(unittest.TestCase):
    def test_workspace_is_stable_and_checkpoint_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(workspace_root=root)
            first = solver_workspace_path(settings, "리버싱/con?", "codex/gpt-5.6-sol/xhigh")
            second = solver_workspace_path(settings, "리버싱/con?", "codex/gpt-5.6-sol/xhigh")
            self.assertEqual(first, second)
            checkpoint = write_checkpoint(
                first,
                challenge="리버싱/con?",
                model_spec="codex/gpt-5.6-sol/xhigh",
                status="running",
                attempt=1,
                steps=12,
                tokens=3456,
                estimated_cost_usd=0.12,
            )
            self.assertTrue(Path(checkpoint).is_file())
            self.assertFalse((Path(first) / ".ctf-agent-state.tmp").exists())
            self.assertEqual(
                challenge_shared_path(settings, "리버싱/con?"),
                challenge_shared_path(settings, "리버싱/con?"),
            )
            self.assertNotEqual(first, challenge_shared_path(settings, "리버싱/con?"))

    def test_resume_manifest_prioritizes_solver_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "bulk.bin").write_bytes(b"x" * 10)
            (path / "exploit.py").write_text("print('resume')", encoding="utf-8")
            manifest = workspace_resume_manifest(root, limit=1)
            self.assertIn("exploit.py", manifest)
            prompt = build_prompt(
                ChallengeMeta(name="resume", category="pwn"),
                ["chall"],
                model_spec="codex/gpt-5.6-sol/xhigh",
                resume_manifest=manifest,
            )
            self.assertIn("Existing work", prompt)
            self.assertIn("Resume from the existing work manifest", prompt)

    def test_bulk_extraction_does_not_unlock_productive_resume(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            before = workspace_progress_signature(root)
            generated = path / "volume-0" / "file-deadbeef" / "section0.pe"
            generated.parent.mkdir(parents=True)
            generated.write_bytes(b"mechanical output")
            (path / "uefi_extract.log").write_text("lots of output", encoding="utf-8")

            self.assertEqual(before, workspace_progress_signature(root))
            self.assertNotIn("section0.pe", workspace_resume_manifest(root))
            self.assertNotIn("uefi_extract.log", workspace_resume_manifest(root))

            (path / "harness.py").write_text("print('measured')", encoding="utf-8")
            self.assertNotEqual(before, workspace_progress_signature(root))
            self.assertIn("harness.py", workspace_resume_manifest(root))

    def test_resume_manifest_includes_checkpoint_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_checkpoint(
                root,
                challenge="resume",
                model_spec="codex/gpt-5.6-sol/xhigh",
                status="progress_checkpoint",
                attempt=2,
                steps=20,
                tokens=1234,
                estimated_cost_usd=0.2,
                findings="heap layout measured; flag reachability remains unproven",
            )

            manifest = workspace_resume_manifest(root)
            self.assertIn("CHECKPOINT", manifest)
            self.assertIn("flag reachability remains unproven", manifest)

    def test_delegate_handoff_requires_reproducible_evidence_schema(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            handoff = Path(root) / "delegate.md"
            handoff.write_text("I think the offset is 0x20.", encoding="utf-8")
            self.assertIn("missing evidence section", handoff_quality_issues(handoff))

            handoff.write_text(
                "## Conclusion\nSUPPORTED\n"
                "## Evidence\nObserved address delta: 0x130.\n"
                "## Reproduction\n`python3 /challenge/workspace/harness.py`\n"
                "## Assumptions and conflicts\nConflicts with the old 0x10 claim.\n",
                encoding="utf-8",
            )
            self.assertIn("private delegate workspace", " ".join(handoff_quality_issues(handoff)))

            handoff.write_text(
                handoff.read_text(encoding="utf-8").replace(
                    "/challenge/workspace/harness.py",
                    "/challenge/shared/delegates/delegate-01/harness.py",
                ),
                encoding="utf-8",
            )
            self.assertEqual(handoff_quality_issues(handoff), [])

    def test_flag_format_rejects_mismatched_candidates(self) -> None:
        self.assertTrue(flag_matches_format("cce2026{real_flag}", "cce2026{...}"))
        self.assertFalse(flag_matches_format("flag{wrong_prefix}", "cce2026{...}"))
        self.assertTrue(flag_matches_format("TEAM-anything-42", r"regex:^TEAM-[a-z]+-\d+$"))

    def test_prompt_uses_category_and_model_specialization(self) -> None:
        prompt = build_prompt(
            ChallengeMeta(name="heap", category="pwn"),
            ["chall", "libc.so.6"],
            model_spec="codex/gpt-5.6-luna/medium",
        )
        self.assertIn("Pwn specialist playbook", prompt)
        self.assertIn("Rapid triage lane", prompt)
        self.assertIn("/challenge/shared/", prompt)
        self.assertIn("/challenge/skills/ctf-pwn/SKILL.md", prompt)
        self.assertIn("smallest discriminating experiment", prompt)
        self.assertIn("bulk extraction", prompt)
        self.assertIn("lattice", category_playbook("crypto"))
        self.assertIn("Primary solve owner", solver_lane("codex/gpt-5.6-sol/xhigh"))
        self.assertEqual(solver_role("codex/gpt-5.6-sol/xhigh").key, "lead")
        self.assertEqual(
            solver_role("codex/gpt-5.6-luna/low/delegate-01").key,
            "delegate",
        )
        self.assertEqual(solver_role("codex/gpt-5.6-terra/high").key, "analyst")
        self.assertEqual(external_skill_path("OSINT"), "/challenge/skills/ctf-osint/SKILL.md")
        self.assertEqual(external_skill_path("blockchain"), "/challenge/skills/ctf-misc/SKILL.md")
        analyst_prompt = build_prompt(
            ChallengeMeta(name="heap", category="pwn"),
            ["chall"],
            model_spec="codex/gpt-5.6-terra/high",
        )
        self.assertIn("do not repeat the scout's full skill read", analyst_prompt)

    def test_desktop_and_billing_safety_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.max_concurrent_challenges, 1)
        self.assertEqual(settings.container_memory_limit, "4g")
        self.assertFalse(settings.enable_api_fallback)
        self.assertEqual(settings.solver_handoff_wait_seconds, 180)
        self.assertEqual(settings.solver_max_tokens, 1_500_000)
        self.assertEqual(settings.solver_max_raw_tokens, 12_000_000)
        self.assertEqual(settings.solver_cached_token_weight, 0.10)
        self.assertEqual(DEFAULT_MODELS, ["codex/gpt-5.6-sol/high"])
        self.assertTrue(settings.dynamic_delegation_enabled)
        self.assertEqual(settings.delegate_max_concurrent, 2)
        self.assertEqual(settings.solver_turn_idle_timeout_seconds, 300)
        self.assertEqual(settings.delegate_turn_idle_timeout_seconds, 180)
        self.assertTrue(settings.delegate_postprocess_on_budget_stop)
        self.assertEqual(settings.delegate_postprocess_max_agents, 1)

    def test_dashboard_runtime_settings_survive_restart_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None)
            configured = RuntimeSettings(
                models=["codex/gpt-5.6-sol/medium"],
                max_concurrent_challenges=3,
                solver_max_estimated_cost_usd=2.5,
            )
            save_runtime_settings(configured, Path(root) / "challenges")

            restarted = Settings(_env_file=None)
            loaded = load_runtime_settings(
                restarted,
                list(DEFAULT_MODELS),
                Path(root) / "challenges",
            )

            self.assertEqual(loaded.models, ["codex/gpt-5.6-sol/medium"])
            self.assertEqual(restarted.max_concurrent_challenges, 3)
            self.assertEqual(restarted.solver_max_estimated_cost_usd, 2.5)
            self.assertEqual(restarted.openai_api_key, settings.openai_api_key)

    def test_postprocessor_has_small_non_recursive_budget(self) -> None:
        settings = Settings(_env_file=None)
        spec = "codex/gpt-5.6-luna/low/delegate-03-postprocess"
        limits = solver_token_limits(settings, spec)

        self.assertEqual(limits.effective_tokens, 80_000)
        self.assertEqual(limits.raw_tokens, 400_000)
        self.assertEqual(limits.attempts, 1)
        self.assertEqual(solver_step_limit(settings, spec), 40)
        self.assertEqual(solver_runtime_limit(settings, spec), 600)
        self.assertEqual(solver_turn_timeout_limit(settings, spec), 300)
        self.assertEqual(solver_turn_idle_timeout_limit(settings, spec), 120)

    def test_cache_weighted_budget_allows_productive_long_context(self) -> None:
        metrics = token_metrics(
            input_tokens=6_258_118,
            output_tokens=40_849,
            cached_input_tokens=6_055_424,
            cached_weight=0.10,
        )
        self.assertEqual(metrics.raw_tokens, 6_298_967)
        self.assertEqual(metrics.fresh_input_tokens, 202_694)
        self.assertEqual(metrics.effective_tokens, 849_085)

        settings = Settings(_env_file=None)
        scout = solver_token_limits(settings, "codex/gpt-5.6-luna/medium")
        verifier = solver_token_limits(settings, "codex/gpt-5.6-sol/xhigh")
        delegate = solver_token_limits(
            settings,
            "codex/gpt-5.6-luna/low/delegate-01",
        )
        self.assertEqual(scout.raw_tokens, 1_800_000)
        self.assertEqual(verifier.raw_tokens, 12_000_000)
        self.assertEqual(delegate.effective_tokens, 250_000)
        self.assertEqual(delegate.raw_tokens, 1_200_000)
        self.assertEqual(delegate.attempts, 4)
        self.assertLess(scout.effective_tokens, verifier.effective_tokens)

    def test_semantic_loop_guard_allows_progress_and_blocks_repeated_empty_boots(self) -> None:
        detector = LoopDetector()
        self.assertIsNone(detector.check("bash", {"command": "qemu-system-x86_64 -kernel one"}))
        self.assertIsNone(
            detector.record_result(
                "bash",
                {"command": "qemu-system-x86_64 -kernel one"},
                "BusyBox ready\nuid=0(root)",
            )
        )
        self.assertIsNone(
            detector.check("bash", {"command": "/usr/bin/qemu-system-x86_64 -kernel two"})
        )
        detector.record_result(
            "bash",
            {"command": "/usr/bin/qemu-system-x86_64 -kernel two"},
            "qemu-system-x86_64: terminating on signal\nCommand timed out",
        )
        self.assertIsNone(
            detector.check("bash", {"command": "timeout 30 qemu-system-aarch64 -kernel three"})
        )
        self.assertEqual(
            detector.record_result(
                "bash",
                {"command": "timeout 30 qemu-system-aarch64 -kernel three"},
                "(no output)\n[exit 124]",
            ),
            "warn",
        )
        self.assertEqual(
            detector.check("bash", {"command": "qemu-system-x86_64 -kernel four"}),
            "break",
        )
        detector.reset()
        self.assertIsNone(detector.check("bash", {"command": "qemu-system-x86_64 -kernel fresh"}))

    def test_tool_output_keeps_head_tail_and_candidate(self) -> None:
        text = "HEAD\n" + ("x" * 10_000) + "\nTEAM{preserve_me}\nTAIL"
        clipped = _truncate(text, 1_000)
        self.assertTrue(clipped.startswith("HEAD"))
        self.assertIn("TAIL", clipped)
        self.assertIn("TEAM{preserve_me}", clipped)
        self.assertLess(len(clipped), 1_500)

    def test_runtime_results_and_candidates_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(workspace_root=root)
            save_runtime_state(
                settings,
                {"done": {"flag": "TEAM{done}", "submit": "operator confirmed"}},
                {"review": {"flag": "TEAM{maybe}", "flags": ["TEAM{maybe}"], "review_required": True}},
                {"deleted"},
            )
            results, candidates = load_runtime_state(settings)
            self.assertEqual(results["done"]["flag"], "TEAM{done}")
            self.assertTrue(candidates["review"]["review_required"])
            self.assertEqual(load_dismissed_challenges(settings), {"deleted"})
            self.assertTrue(runtime_state_path(settings).is_file())

    def test_solver_output_distinguishes_progress_candidate_and_verified_flag(self) -> None:
        schema = solver_output_json_schema()
        self.assertEqual(schema["properties"]["type"]["enum"], ["flag_found", "incomplete"])

        progress = assess_solver_output(
            output_type="incomplete",
            flag="",
            method="located the final comparison",
            confirmed_flag=None,
            flag_format="TEAM{...}",
        )
        self.assertEqual(progress.status, GAVE_UP)
        self.assertIsNone(progress.flag)

        candidate = assess_solver_output(
            output_type="flag_found",
            flag="TEAM{guess}",
            method="static string",
            confirmed_flag=None,
            flag_format="TEAM{...}",
        )
        self.assertEqual(candidate.status, CANDIDATE_FOUND)
        self.assertEqual(candidate.flag, "TEAM{guess}")

        verified = assess_solver_output(
            output_type="flag_found",
            flag="TEAM{different_model_output}",
            method="model summary",
            confirmed_flag="TEAM{server_confirmed}",
            flag_format="TEAM{...}",
        )
        self.assertEqual(verified.status, FLAG_FOUND)
        self.assertEqual(verified.flag, "TEAM{server_confirmed}")


class _Tracer:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, name: str, **data) -> None:
        self.events.append((name, data))

    def tool_call(self, name: str, args: dict, step: int) -> None:
        self.events.append(("tool_call", {"name": name, "args": args, "step": step}))

    def tool_result(self, name: str, result: str, step: int) -> None:
        self.events.append(("tool_result", {"name": name, "result": result, "step": step}))

    def usage(self, input_tokens: int, output_tokens: int, cache_tokens: int, cost: float) -> None:
        self.events.append(
            (
                "usage",
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_tokens": cache_tokens,
                    "cost": cost,
                },
            )
        )


class _GaveUpSolver:
    def __init__(self, workspace: str) -> None:
        self.model_spec = "codex/test"
        self.agent_name = "budget/codex/test"
        self.sandbox = SimpleNamespace(workspace_dir=workspace)
        self.tracer = _Tracer()
        self._step_count = 0
        self.bump_count = 0

    async def start(self) -> None:
        return None

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self._step_count += 1
        return SolverResult(None, GAVE_UP, "partial finding", self._step_count, 0.0, "trace.jsonl")

    def bump(self, _insights: str) -> None:
        self.bump_count += 1

    async def stop(self) -> None:
        return None


class _CandidateSolver(_GaveUpSolver):
    async def run_until_done_or_gave_up(self) -> SolverResult:
        self._step_count += 1
        return SolverResult(
            "TEAM{guess}",
            CANDIDATE_FOUND,
            "Unverified candidate via static string: TEAM{guess}",
            self._step_count,
            0.01,
            "trace.jsonl",
        )


class _CheckpointSolver(_CandidateSolver):
    def __init__(self, workspace: str) -> None:
        super().__init__(workspace)
        self.sandbox.shared_workspace_dir = ""
        self.calls = 0

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self.calls += 1
        self._step_count += 1
        if self.calls == 1:
            Path(self.sandbox.workspace_dir, "progress.txt").write_text(
                "new primitive",
                encoding="utf-8",
            )
            return SolverResult(
                None,
                PROGRESS_CHECKPOINT,
                "primitive recovered",
                self._step_count,
                0.01,
                "trace.jsonl",
                stop_reason="turn slice checkpoint",
            )
        return await super().run_until_done_or_gave_up()


class _IdleThenCandidateSolver(_CandidateSolver):
    def __init__(self, workspace: str) -> None:
        super().__init__(workspace)
        self.calls = 0
        self.interrupt_reasons: list[str] = []
        self.resume = asyncio.Event()

    def activity_idle_seconds(self) -> float:
        return 999.0

    @property
    def tool_call_active(self) -> bool:
        return False

    def request_resume_interrupt(self, reason: str) -> bool:
        self.interrupt_reasons.append(reason)
        self.resume.set()
        return True

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self.calls += 1
        self._step_count += 1
        if self.calls == 1:
            await self.resume.wait()
            return SolverResult(
                None,
                PROGRESS_CHECKPOINT,
                "model turn stopped by idle watchdog",
                self._step_count,
                0.01,
                "trace.jsonl",
                stop_reason="resume interrupt: idle watchdog observed no activity",
            )
        return await super().run_until_done_or_gave_up()


class RuntimeBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_turn_is_interrupted_and_resumed_without_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=2,
                solver_turn_timeout_seconds=30,
                solver_turn_idle_timeout_seconds=1,
                solver_max_runtime_seconds=120,
                solver_max_steps=100,
                solver_max_tokens=0,
                solver_max_raw_tokens=0,
                solver_max_estimated_cost_usd=0,
            )
            swarm = ChallengeSwarm(
                challenge_dir=workspace,
                meta=ChallengeMeta(name="idle", category="reversing", flag_format="TEAM{...}"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/gpt-5.6-sol/xhigh"],
                no_submit=True,
            )
            solver = _IdleThenCandidateSolver(workspace)

            result, _ = await swarm._run_solver_loop(
                solver,
                "codex/gpt-5.6-sol/xhigh",
            )

            self.assertEqual(result.status, CANDIDATE_FOUND)
            self.assertEqual(solver.calls, 2)
            self.assertEqual(solver.bump_count, 1)
            self.assertEqual(len(solver.interrupt_reasons), 1)
            self.assertIn("idle watchdog", solver.interrupt_reasons[0])

    async def test_live_codex_bump_requests_immediate_resume_interrupt(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._bump_insights = None
        solver._resume_after_checkpoint = True
        solver._turn_active = True
        solver._interrupt_requested = False
        solver._resume_stop_reason = ""
        solver._current_turn_id = "turn-live"
        solver._thread_id = "thread-live"
        solver._step_count = 7
        solver._last_activity_at = time.monotonic()
        solver.agent_name = "challenge/codex/test"
        solver.loop_detector = LoopDetector()
        solver.tracer = _Tracer()
        calls: list[tuple[str, dict]] = []

        async def fake_rpc(method: str, params: dict) -> dict:
            calls.append((method, params))
            return {"result": {}}

        solver._rpc = fake_rpc

        solver.bump("delegate recovered a traffic key")
        await solver._interrupt_task

        self.assertEqual(
            calls,
            [("turn/interrupt", {"threadId": "thread-live", "turnId": "turn-live"})],
        )
        self.assertIn("traffic key", solver._bump_insights)
        self.assertIn("new coordinator", solver._resume_stop_reason)
        self.assertFalse(solver._resume_after_checkpoint)
        self.assertEqual(solver.tracer.events[-1][0], "resume_interrupt_sent")

    async def test_solved_challenge_starts_ai_writeup_generation(self) -> None:
        requested: list[str] = []

        async def request_writeup(challenge_name: str) -> dict[str, object]:
            requested.append(challenge_name)
            return {"status": "generating", "active": True}

        deps = SimpleNamespace(request_writeup_generation=request_writeup)
        status = await _generate_or_finalize_writeup(
            deps,
            "solved challenge",
            "pwn",
            "TEAM{done}",
        )

        self.assertEqual(requested, ["solved challenge"])
        self.assertEqual(status["status"], "generating")
        self.assertTrue(status["active"])

    async def test_persisted_solution_is_not_auto_spawned_after_restart(self) -> None:
        deps = SimpleNamespace(
            challenge_metas={"already solved": object(), "new challenge": object()},
            results={"already solved": {"flag": "TEAM{done}"}},
        )
        poller = SimpleNamespace(
            known_challenges={"already solved", "new challenge"},
            known_solved=set(),
        )

        self.assertEqual(_unsolved_names(deps, poller), {"new challenge"})

        spawn_deps = SimpleNamespace(
            swarms={},
            swarm_tasks={},
            results={"already solved": {"flag": "TEAM{done}"}},
            max_concurrent_challenges=3,
        )
        message = await do_spawn_swarm(spawn_deps, "already solved")
        self.assertIn("Already solved", message)

    async def test_new_swarm_gets_an_isolated_runtime_settings_snapshot(self) -> None:
        settings = Settings(_env_file=None, solver_max_steps=300)
        gate = asyncio.Event()

        async def fake_run(_swarm: ChallengeSwarm) -> None:
            await gate.wait()

        deps = SimpleNamespace(
            swarms={},
            swarm_tasks={},
            results={},
            candidates={},
            dismissed_challenges=set(),
            max_concurrent_challenges=1,
            ctfd=SimpleNamespace(is_configured=False),
            challenges_root="challenges",
            challenge_dirs={"snapshot": "."},
            challenge_metas={"snapshot": ChallengeMeta(name="snapshot", category="misc")},
            cost_tracker=CostTracker(),
            settings=settings,
            model_specs=["codex/gpt-5.6-sol/high"],
            no_submit=True,
            coordinator_inbox=asyncio.Queue(),
        )

        with patch("backend.agents.swarm.ChallengeSwarm.run", new=fake_run):
            await do_spawn_swarm(deps, "snapshot")
            swarm = deps.swarms["snapshot"]
            settings.solver_max_steps = 50
            task = deps.swarm_tasks["snapshot"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIsNot(swarm.settings, settings)
        self.assertEqual(swarm.settings.solver_max_steps, 300)
        self.assertEqual(swarm.model_specs, ["codex/gpt-5.6-sol/high"])

    async def test_kill_cancels_primary_solver_task_immediately(self) -> None:
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="stop-now", category="pwn"),
            ctfd=SimpleNamespace(),
            cost_tracker=CostTracker(),
            settings=Settings(_env_file=None),
            model_specs=["codex/gpt-5.6-sol/xhigh"],
        )
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def primary() -> None:
            started.set()
            await blocked.wait()

        task = asyncio.create_task(primary())
        swarm._primary_tasks.add(task)
        await started.wait()

        swarm.kill()
        await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(swarm.cancel_event.is_set())
        self.assertTrue(task.cancelled())

    async def test_cached_long_context_gets_checkpoint_not_global_budget_stop(self) -> None:
        class FakeStdout:
            def __init__(self, lines: list[bytes]) -> None:
                self.lines = lines

            async def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        notification = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-hard",
                "tokenUsage": {
                    "last": {"inputTokens": 0, "outputTokens": 0, "cachedInputTokens": 0},
                    "total": {
                        "inputTokens": 6_258_118,
                        "outputTokens": 40_849,
                        "cachedInputTokens": 6_055_424,
                        "totalTokens": 6_298_967,
                    },
                },
            },
        }
        solver = object.__new__(CodexSolver)
        solver._proc = SimpleNamespace(
            stdout=FakeStdout([(json.dumps(notification) + "\n").encode()]),
        )
        solver._pending_responses = {}
        solver._turn_done = asyncio.Event()
        solver._compact_done = asyncio.Event()
        solver.settings = Settings(_env_file=None)
        solver.cost_tracker = CostTracker()
        solver.agent_name = "hard/codex/gpt-5.6-sol/xhigh"
        solver.model_id = "gpt-5.6-sol"
        solver.model_spec = "codex/gpt-5.6-sol/xhigh"
        solver._latest_raw_tokens = 0
        solver._turn_start_raw_tokens = 0
        solver._cost_usd = 0.0
        solver.tracer = _Tracer()
        budget_requests: list[str] = []
        checkpoint_requests: list[str] = []
        solver._request_budget_interrupt = lambda reason, turn_id=None: budget_requests.append(reason)
        solver._request_checkpoint_interrupt = (
            lambda reason, turn_id=None: checkpoint_requests.append(reason)
        )

        await solver._read_loop()

        self.assertEqual(budget_requests, [])
        self.assertEqual(len(checkpoint_requests), 1)
        self.assertIn("turn slice checkpoint", checkpoint_requests[0])

    async def test_codex_compacts_only_between_turns(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._thread_id = "thread-compact"
        solver._compact_done = asyncio.Event()
        solver._latest_raw_tokens = 1_500_000
        solver.agent_name = "hard/codex/test"
        solver.tracer = _Tracer()
        calls: list[tuple[str, dict]] = []

        async def fake_rpc(method: str, params: dict) -> dict:
            calls.append((method, params))
            solver._compact_done.set()
            return {"result": {}}

        solver._rpc = fake_rpc
        compacted = await solver._compact_between_turns()

        self.assertTrue(compacted)
        self.assertEqual(
            calls,
            [("thread/compact/start", {"threadId": "thread-compact"})],
        )

    async def test_codex_accepts_current_compaction_item_notification(self) -> None:
        class FakeStdout:
            def __init__(self, lines: list[bytes]) -> None:
                self.lines = lines

            async def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        notification = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-compact",
                "turnId": "turn-compact",
                "completedAtMs": 1,
                "item": {"id": "item-compact", "type": "contextCompaction"},
            },
        }
        solver = object.__new__(CodexSolver)
        solver._proc = SimpleNamespace(
            stdout=FakeStdout([(json.dumps(notification) + "\n").encode()]),
        )
        solver._pending_responses = {}
        solver._turn_done = asyncio.Event()
        solver._compact_done = asyncio.Event()
        solver._latest_raw_tokens = 42
        solver.tracer = _Tracer()

        await solver._read_loop()

        self.assertTrue(solver._compact_done.is_set())
        self.assertIn(("compact_complete", {"tokens": 42}), solver.tracer.events)

    async def test_codex_compaction_failure_is_reported(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._thread_id = "thread-compact"
        solver._compact_done = asyncio.Event()
        solver._latest_raw_tokens = 1_500_000
        solver.agent_name = "hard/codex/test"
        solver.tracer = _Tracer()

        async def failing_rpc(_method: str, _params: dict) -> dict:
            raise RuntimeError("compact unavailable")

        solver._rpc = failing_rpc

        compacted = await solver._compact_between_turns()

        self.assertFalse(compacted)
        self.assertIn(
            ("compact_failed", {"error": "compact unavailable"}),
            solver.tracer.events,
        )

    async def test_productive_checkpoint_resumes_without_bump_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=2,
                solver_turn_timeout_seconds=30,
                solver_max_runtime_seconds=120,
                solver_max_steps=100,
                solver_max_tokens=0,
                solver_max_raw_tokens=0,
                solver_max_estimated_cost_usd=0,
            )
            swarm = ChallengeSwarm(
                challenge_dir=workspace,
                meta=ChallengeMeta(name="checkpoint", category="pwn", flag_format="TEAM{...}"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/gpt-5.6-sol/xhigh"],
                no_submit=True,
            )
            solver = _CheckpointSolver(workspace)
            result, _ = await swarm._run_solver_loop(solver, "codex/gpt-5.6-sol/xhigh")

            self.assertEqual(result.status, CANDIDATE_FOUND)
            self.assertEqual(solver.calls, 2)
            self.assertEqual(solver.bump_count, 0)

    async def test_codex_usage_notification_requests_live_interrupt(self) -> None:
        class FakeStdout:
            def __init__(self, lines: list[bytes]) -> None:
                self.lines = lines

            async def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        notification = {
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn-live",
                "tokenUsage": {
                    "last": {"inputTokens": 8, "outputTokens": 4, "cachedInputTokens": 0},
                    "total": {"inputTokens": 8, "outputTokens": 4, "cachedInputTokens": 0, "totalTokens": 12},
                },
            },
        }
        solver = object.__new__(CodexSolver)
        solver._proc = SimpleNamespace(
            stdout=FakeStdout([(json.dumps(notification) + "\n").encode()]),
        )
        solver._pending_responses = {}
        solver._turn_done = asyncio.Event()
        solver.settings = SimpleNamespace(
            solver_max_tokens=10,
            solver_max_estimated_cost_usd=0,
        )
        solver.cost_tracker = CostTracker()
        solver.agent_name = "live/codex/gpt-5.6-luna/medium"
        solver.model_id = "gpt-5.6-luna"
        solver.model_spec = "codex/gpt-5.6-sol/xhigh"
        solver._latest_raw_tokens = 0
        solver._turn_start_raw_tokens = 0
        solver._compact_done = asyncio.Event()
        solver._cost_usd = 0.0
        solver.tracer = _Tracer()
        requested: list[tuple[str, str]] = []
        solver._request_budget_interrupt = lambda reason, turn_id=None: requested.append(
            (reason, turn_id)
        )

        await solver._read_loop()

        self.assertEqual(requested[0][1], "turn-live")
        self.assertIn("12/10", requested[0][0])

    async def test_codex_interrupt_uses_active_thread_and_turn(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._thread_id = "thread-1"
        solver.agent_name = "challenge/codex/test"
        solver.tracer = _Tracer()
        calls: list[tuple[str, dict]] = []

        async def fake_rpc(method: str, params: dict) -> dict:
            calls.append((method, params))
            return {"result": {}}

        solver._rpc = fake_rpc
        await solver._interrupt_turn("turn-7", "token budget exhausted")

        self.assertEqual(
            calls,
            [("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-7"})],
        )
        self.assertEqual(solver.tracer.events[-1][0], "budget_interrupt_sent")

    async def test_codex_step_cap_rejects_tool_and_interrupts_turn(self) -> None:
        solver = object.__new__(CodexSolver)
        solver.settings = SimpleNamespace(solver_max_steps=1)
        solver.model_spec = "codex/test"
        solver._step_count = 1
        solver._thread_id = "thread-1"
        solver._current_turn_id = "turn-1"
        solver._interrupt_requested = False
        solver._budget_stop_reason = ""
        solver._interrupt_task = None
        solver.agent_name = "challenge/codex/test"
        solver.tracer = _Tracer()
        solver.loop_detector = LoopDetector()
        responses: list[dict] = []
        rpc_calls: list[tuple[str, dict]] = []
        executed = False

        async def fake_response(_request_id: int, result: dict) -> None:
            responses.append(result)

        async def fake_exec(_name: str, _args: dict):
            nonlocal executed
            executed = True
            return "unexpected"

        async def fake_rpc(method: str, params: dict) -> dict:
            rpc_calls.append((method, params))
            return {"result": {}}

        solver._respond_to_request = fake_response
        solver._exec_tool = fake_exec
        solver._rpc = fake_rpc

        await solver._handle_tool_call(
            9,
            {"tool": "bash", "arguments": {"command": "id"}, "turnId": "turn-1"},
        )
        await solver._interrupt_task

        self.assertFalse(executed)
        self.assertFalse(responses[0]["success"])
        self.assertEqual(rpc_calls[0][0], "turn/interrupt")
        self.assertIn("step budget", solver._budget_stop_reason)

    async def test_targeted_findings_and_scout_handoff(self) -> None:
        bus = ChallengeMessageBus()
        await bus.post("coordinator", "only terra", target="codex/gpt-5.6-terra/high")
        await bus.post("coordinator", "everyone")

        terra = await bus.check("codex/gpt-5.6-terra/high")
        luna = await bus.check("codex/gpt-5.6-luna/medium")
        self.assertEqual([finding.content for finding in terra], ["only terra", "everyone"])
        self.assertEqual([finding.content for finding in luna], ["everyone"])

        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="handoff", category="pwn"),
            ctfd=SimpleNamespace(),
            cost_tracker=CostTracker(),
            settings=SimpleNamespace(solver_handoff_wait_seconds=60),
            model_specs=["codex/gpt-5.6-luna/medium", "codex/gpt-5.6-terra/high"],
        )
        notify = swarm._make_notify_fn("codex/gpt-5.6-luna/medium")
        await notify("triage ready")
        self.assertTrue(swarm._triage_ready.is_set())
        self.assertFalse(swarm._solution_ready.is_set())
        unread = await swarm.message_bus.check("codex/gpt-5.6-terra/high")
        self.assertEqual([finding.content for finding in unread], ["triage ready"])

        analyst_notify = swarm._make_notify_fn("codex/gpt-5.6-terra/high")
        await analyst_notify("solution ready")
        self.assertTrue(swarm._solution_ready.is_set())

    async def test_sol_lead_creates_only_bounded_live_delegates(self) -> None:
        settings = Settings(_env_file=None)
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="adaptive", category="pwn"),
            ctfd=SimpleNamespace(is_configured=False),
            cost_tracker=CostTracker(),
            settings=settings,
            model_specs=list(DEFAULT_MODELS),
            no_submit=True,
        )
        worker_gate = asyncio.Event()

        async def fake_run(_model_spec: str, task_directive: str = "") -> None:
            self.assertIn("Required deliverable", task_directive)
            self.assertIn("not visible to SOL", task_directive)
            self.assertIn("/challenge/shared/delegates/", task_directive)
            await worker_gate.wait()

        swarm._run_solver = fake_run  # type: ignore[method-assign]
        first = await swarm._spawn_delegate(
            "Determine whether the parser accepts a negative length",
            "A minimal reproducer and observed result",
        )
        duplicate = await swarm._spawn_delegate(
            "Determine if the parser accepts a negative length",
            "A reproducer with the observed parser result",
        )
        second = await swarm._spawn_delegate(
            "Recover the exact offset to the saved return address",
            "Cyclic-pattern offset with debugger evidence",
        )
        deferred = await swarm._spawn_delegate(
            "Check a third unrelated hypothesis",
            "One supported or refuted conclusion",
        )

        self.assertIn("delegate-01", first)
        self.assertIn("overlaps", duplicate)
        self.assertIn("delegate-02", second)
        self.assertIn("DEFERRED", deferred)
        self.assertEqual(len(swarm.delegate_tasks), 2)
        self.assertEqual(len(swarm.model_specs), 3)
        self.assertTrue(
            all(solver_role(spec).key == "delegate" for spec in swarm.model_specs[1:])
        )

        worker_gate.set()
        await asyncio.gather(*swarm.delegate_tasks.values())

    async def test_audited_delegate_handoff_stops_without_bump(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, workspace_root=root)
            lead = "codex/gpt-5.6-sol/xhigh"
            source = "codex/gpt-5.6-luna/low/delegate-01"
            swarm = ChallengeSwarm(
                challenge_dir=".",
                meta=ChallengeMeta(name="handoff-done", category="pwn"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=[lead, source],
            )
            handoff = Path(root) / "delegate-01.md"
            handoff.write_text(
                "## Conclusion\nREFUTED\n## Evidence\nObserved rejection.\n"
                "## Reproduction\n`python3 repro.py`\n"
                "## Assumptions and conflicts\nNone.\n",
                encoding="utf-8",
            )
            swarm.delegate_requests[source] = {
                "task": "Test one bounded hypothesis",
                "deliverable": "A supported or refuted result",
                "handoff": "/challenge/shared/delegates/delegate-01.md",
                "host_handoff": str(handoff),
            }
            lead_solver = _GaveUpSolver(root)
            swarm.solvers[lead] = lead_solver
            solver = _GaveUpSolver(root)

            result, _ = await swarm._run_solver_loop(solver, source)

            self.assertEqual(result.status, HANDOFF_COMPLETE)
            self.assertEqual(result.attempt, 1)
            self.assertEqual(solver.bump_count, 0)
            self.assertEqual(lead_solver.bump_count, 1)
            self.assertIn("delegate-01.md", result.stop_reason)
            unread = await swarm.message_bus.check(lead)
            self.assertTrue(
                any("DELEGATE HANDOFF READY" in finding.content for finding in unread)
            )

    async def test_cancelling_solver_loop_reaps_active_turn(self) -> None:
        class BlockingSolver(_GaveUpSolver):
            def __init__(self, workspace: str) -> None:
                super().__init__(workspace)
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def run_until_done_or_gave_up(self) -> SolverResult:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, workspace_root=root)
            swarm = ChallengeSwarm(
                challenge_dir=".",
                meta=ChallengeMeta(name="cancel-active", category="pwn"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/gpt-5.6-sol/xhigh"],
            )
            solver = BlockingSolver(root)
            loop_task = asyncio.create_task(
                swarm._run_solver_loop(solver, "codex/gpt-5.6-sol/xhigh")
            )
            await asyncio.wait_for(solver.started.wait(), timeout=1)

            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task

            self.assertTrue(solver.cancelled.is_set())

    async def test_interrupted_delegate_routes_state_and_repairs_unsafe_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, workspace_root=root)
            lead = "codex/gpt-5.6-sol/xhigh"
            source = "codex/gpt-5.6-luna/low/delegate-01"
            swarm = ChallengeSwarm(
                challenge_dir=".",
                meta=ChallengeMeta(name="budget-handoff", category="reversing"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=[lead, source],
                no_submit=True,
            )
            shared = Path(challenge_shared_path(settings, "budget-handoff"))
            source_handoff = shared / "delegates" / "delegate-01.md"
            source_handoff.parent.mkdir(parents=True)
            source_handoff.write_text("partial allocator guess", encoding="utf-8")
            swarm.delegate_requests[source] = {
                "task": "Measure the allocator stride",
                "deliverable": "Observed addresses and a reproducer",
                "handoff": "/challenge/shared/delegates/delegate-01.md",
                "host_handoff": str(source_handoff),
            }
            directives: list[str] = []

            async def fake_run(_model_spec: str, task_directive: str = "") -> None:
                directives.append(task_directive)

            swarm._run_solver = fake_run  # type: ignore[method-assign]
            result = SolverResult(
                flag=None,
                status=BUDGET_EXHAUSTED,
                findings_summary="Static header may be 0x20; dynamic check unfinished.",
                step_count=17,
                cost_usd=0.01,
                log_path="",
                stop_reason="effective token budget exhausted",
                attempt=2,
            )

            await swarm._handle_delegate_budget_stop(source, result)
            await asyncio.gather(*swarm.delegate_tasks.values())

            unread = await swarm.message_bus.check(lead)
            self.assertEqual(len(unread), 1)
            self.assertIn("BUDGET STOP HANDOFF", unread[0].content)
            self.assertIn("UNSAFE", unread[0].content)
            self.assertIn("postprocess=started", unread[0].content)
            summary = shared / "recovery" / "delegate-01-budget-stop.md"
            self.assertTrue(summary.is_file())
            self.assertIn("dynamic check unfinished", summary.read_text(encoding="utf-8"))
            self.assertEqual(swarm._postprocess_count, 1)
            self.assertEqual(len(directives), 1)
            self.assertIn("one-shot postprocessor", directives[0])

            postprocess_spec = next(
                spec for spec in swarm.delegate_requests if "postprocess" in spec
            )
            await swarm._handle_delegate_budget_stop(postprocess_spec, result)
            self.assertEqual(swarm._postprocess_count, 1)

    async def test_valid_budget_stop_handoff_is_sent_without_extra_worker(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, workspace_root=root)
            lead = "codex/gpt-5.6-sol/xhigh"
            source = "codex/gpt-5.6-luna/low/delegate-01"
            swarm = ChallengeSwarm(
                challenge_dir=".",
                meta=ChallengeMeta(name="valid-handoff", category="pwn"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=[lead, source],
            )
            shared = Path(challenge_shared_path(settings, "valid-handoff"))
            handoff = shared / "delegates" / "delegate-01.md"
            handoff.parent.mkdir(parents=True)
            handoff.write_text(
                "## Conclusion\nSUPPORTED\n## Evidence\nObserved.\n"
                "## Reproduction\n`python3 repro.py`\n"
                "## Assumptions and conflicts\nNone.\n",
                encoding="utf-8",
            )
            swarm.delegate_requests[source] = {
                "task": "Verify one offset",
                "deliverable": "Observed offset",
                "handoff": "/challenge/shared/delegates/delegate-01.md",
                "host_handoff": str(handoff),
            }
            result = SolverResult(
                flag=None,
                status=BUDGET_EXHAUSTED,
                findings_summary="Offset observed.",
                step_count=4,
                cost_usd=0.0,
                log_path="",
                stop_reason="raw token safety ceiling exhausted",
            )

            await swarm._handle_delegate_budget_stop(source, result)

            unread = await swarm.message_bus.check(lead)
            self.assertIn("audit=passed", unread[0].content)
            self.assertIn("postprocess=not needed", unread[0].content)
            self.assertEqual(swarm.delegate_tasks, {})

    async def test_attempt_budget_stops_and_writes_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=1,
                solver_turn_timeout_seconds=30,
                solver_max_runtime_seconds=120,
                solver_max_steps=100,
                solver_max_tokens=0,
                solver_max_estimated_cost_usd=0,
            )
            swarm = ChallengeSwarm(
                challenge_dir=workspace,
                meta=ChallengeMeta(name="budget", category="pwn", flag_format="TEAM{...}"),
                ctfd=SimpleNamespace(),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/test"],
            )
            solver = _GaveUpSolver(workspace)
            result, _ = await swarm._run_solver_loop(solver, "codex/test")

            self.assertEqual(result.status, BUDGET_EXHAUSTED)
            self.assertEqual(result.attempt, 1)
            self.assertIn("attempt budget", result.stop_reason)
            self.assertEqual(solver.bump_count, 0)
            self.assertTrue((Path(workspace) / ".ctf-agent-state.json").exists())

    async def test_submission_format_and_total_limit_are_enforced(self) -> None:
        class FakeCTFd:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def submit_flag(self, _challenge: str, flag: str):
                self.calls.append(flag)
                return SimpleNamespace(status="incorrect", display="INCORRECT")

        ctfd = FakeCTFd()
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="submit", category="misc", flag_format="cce2026{...}"),
            ctfd=ctfd,
            cost_tracker=CostTracker(),
            settings=SimpleNamespace(max_flag_submissions_per_challenge=1),
            model_specs=["codex/test"],
        )

        message, confirmed = await swarm.try_submit_flag("flag{wrong}", "codex/test")
        self.assertIn("does not match", message)
        self.assertFalse(confirmed)
        self.assertEqual(ctfd.calls, [])

        await swarm.try_submit_flag("cce2026{first}", "codex/test")
        message, confirmed = await swarm.try_submit_flag("cce2026{second}", "codex/test")
        self.assertIn("SUBMISSION LIMIT", message)
        self.assertFalse(confirmed)
        self.assertEqual(ctfd.calls, ["cce2026{first}"])

    async def test_standalone_candidate_pauses_for_operator_review(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=1,
                solver_turn_timeout_seconds=30,
                solver_max_runtime_seconds=120,
                solver_max_steps=100,
                solver_max_tokens=0,
                solver_max_estimated_cost_usd=0,
            )
            swarm = ChallengeSwarm(
                challenge_dir=workspace,
                meta=ChallengeMeta(name="candidate", category="reversing", flag_format="TEAM{...}"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/test"],
                no_submit=True,
            )
            result, _ = await swarm._run_solver_loop(
                _CandidateSolver(workspace),
                "codex/test",
            )

            self.assertEqual(result.status, CANDIDATE_FOUND)
            self.assertFalse(swarm.cancel_event.is_set())
            self.assertIsNone(swarm.winner)
            self.assertEqual(swarm.candidates["codex/test"].flag, "TEAM{guess}")

    async def test_standalone_operator_submission_records_candidate_not_solve(self) -> None:
        deps = SimpleNamespace(
            ctfd=SimpleNamespace(is_configured=False),
            candidates={},
            results={},
            challenge_metas={
                "local": SimpleNamespace(flag_format="TEAM{...}"),
            },
        )

        message = await coordinator_submit_flag(deps, "local", "TEAM{guess}")

        self.assertIn("LOCAL CANDIDATE", message)
        self.assertEqual(deps.candidates["local"]["flag"], "TEAM{guess}")
        self.assertNotIn("local", deps.results)

        confirmed = await do_review_candidate(deps, "local", "TEAM{guess}", True)
        self.assertIn("LOCAL CONFIRMED", confirmed)
        self.assertEqual(deps.results["local"]["flag"], "TEAM{guess}")
        self.assertNotIn("local", deps.candidates)

        deps.challenge_metas["local-retry"] = SimpleNamespace(flag_format="TEAM{...}")
        await coordinator_submit_flag(deps, "local-retry", "TEAM{wrong}")
        rejected = await do_review_candidate(deps, "local-retry", "TEAM{wrong}", False)
        self.assertIn("LOCAL REJECTED", rejected)
        self.assertNotIn("local-retry", deps.results)
        self.assertNotIn("local-retry", deps.candidates)


if __name__ == "__main__":
    unittest.main()
