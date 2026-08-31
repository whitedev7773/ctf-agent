from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.agents.swarm import ChallengeSwarm
from backend.artifacts import solver_workspace_path, write_checkpoint
from backend.challenge_profiles import category_playbook, solver_lane
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.flag_format import flag_matches_format
from backend.prompts import ChallengeMeta, build_prompt
from backend.solver_base import BUDGET_EXHAUSTED, GAVE_UP, SolverResult


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
        self.assertIn("survives solver/container restarts", prompt)
        self.assertIn("lattice", category_playbook("crypto"))
        self.assertIn("Deep exploitation lane", solver_lane("codex/gpt-5.6-sol/xhigh"))

    def test_desktop_and_billing_safety_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.max_concurrent_challenges, 1)
        self.assertEqual(settings.container_memory_limit, "4g")
        self.assertFalse(settings.enable_api_fallback)


class _Tracer:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def event(self, name: str, **data) -> None:
        self.events.append((name, data))


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


class RuntimeBudgetTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
