from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agents.codex_coordinator import CodexCoordinator
from backend.agents.codex_solver import CodexSolver
from backend.agents.coordinator_core import (
    _generate_or_finalize_writeup,
    _merge_candidate_record,
    do_kill_swarm,
    do_review_candidate,
    do_spawn_swarm,
)
from backend.agents.coordinator_core import do_submit_flag as coordinator_submit_flag
from backend.agents.coordinator_loop import _auto_spawn_one, _unsolved_names, build_deps
from backend.agents.solver import Solver
from backend.agents.swarm import ChallengeSwarm
from backend.artifacts import (
    challenge_approach_notes,
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
    apply_lead_model_override,
    category_playbook,
    external_skill_path,
    normalized_category,
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
from backend.runtime_settings import (
    CTFdSettings,
    RuntimeSettings,
    load_ctfd_settings,
    load_runtime_settings,
    save_ctfd_settings,
    save_runtime_settings,
)
from backend.runtime_state import (
    load_dismissed_challenges,
    load_runtime_state,
    runtime_state_path,
    save_runtime_state,
)
from backend.sandbox import DockerSandbox, configure_semaphore
from backend.solver_base import (
    BUDGET_EXHAUSTED,
    CANCELLED,
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

    def test_resume_manifest_surfaces_authoritative_reasoning_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            reasoning = Path(root) / "reasoning"
            reasoning.mkdir()
            (reasoning / "state.json").write_text(
                json.dumps(
                    {
                        "phase": "HYPOTHESIS_TEST",
                        "active_hypothesis": "H-vm",
                        "current_blocker": "native checkpoint mismatch",
                        "next_experiment": "lift one transition",
                        "failed_experiments": ["additive model refuted"],
                        "hypotheses": [
                            {
                                "id": "H-vm",
                                "statement": "The emulator matches native execution.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            manifest = workspace_resume_manifest(root)
            self.assertIn("REASONING", manifest)
            self.assertIn("native checkpoint mismatch", manifest)
            self.assertIn("additive model refuted", manifest)
            self.assertIn("lift one transition", manifest)

    def test_dashboard_notes_prefer_reasoning_state_over_stale_state_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = SimpleNamespace(workspace_root=root)
            shared = Path(challenge_shared_path(settings, "stale-state"))
            (shared / "lead").mkdir()
            (shared / "lead" / "STATE.md").write_text(
                "## Current blocker\nOld blocker from initial triage.", encoding="utf-8"
            )
            reasoning = shared / "reasoning"
            reasoning.mkdir()
            (reasoning / "state.json").write_text(
                json.dumps(
                    {
                        "phase": "HYPOTHESIS_TEST",
                        "active_hypothesis": "H-new",
                        "current_blocker": "native differential mismatch",
                        "next_experiment": "lift one transition",
                        "hypotheses": [
                            {"id": "H-new", "statement": "Revised measured model."}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            notes = challenge_approach_notes(settings, "stale-state")
            self.assertEqual(notes[0]["source"], "reasoning/state.json")
            self.assertIn("native differential mismatch", notes[0]["text"])

    def test_delegate_handoff_requires_reproducible_evidence_schema(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            handoff = Path(root) / "delegate.md"
            handoff.write_text("I think the offset is 0x20.", encoding="utf-8")
            self.assertIn("missing evidence section", handoff_quality_issues(handoff))

            handoff.write_text(
                "## Conclusion\nSUPPORTED\n"
                "## Evidence\nObserved address delta: 0x130.\n"
                "## Reproduction\n`python3 /challenge/workspace/harness.py`\n"
                "## Scope and limitations\nLocal component test; deployment behavior unverified.\n"
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
        self.assertIn("differentially compare", prompt)
        self.assertIn("UNSAT proves only the exact path", prompt)
        self.assertIn("Do not apply the challenge flag format to stdin", prompt)
        self.assertIn("lattice", category_playbook("crypto"))
        self.assertEqual(normalized_category("AI/ML"), "ai")
        self.assertEqual(normalized_category("malware analysis"), "malware")
        self.assertIn("local harness", category_playbook("ai"))
        self.assertIn("static triage", category_playbook("malware"))
        self.assertEqual(external_skill_path("AI/ML"), "/challenge/skills/ctf-ai-ml/SKILL.md")
        self.assertEqual(external_skill_path("악성코드"), "/challenge/skills/ctf-malware/SKILL.md")
        self.assertIn("Primary solve owner", solver_lane("codex/gpt-5.6-sol/xhigh"))
        self.assertEqual(solver_role("codex/gpt-5.6-sol/xhigh").key, "lead")
        self.assertEqual(
            solver_role("codex/gpt-5.6-luna/low/delegate-01").key,
            "delegate",
        )
        self.assertEqual(solver_role("codex/gpt-5.6-terra/high").key, "analyst")

    def test_lead_model_override_preserves_other_roster_lanes(self) -> None:
        roster = [
            "codex/gpt-5.6-sol/high",
            "codex/gpt-5.6-luna/low",
        ]
        self.assertEqual(
            apply_lead_model_override(roster, "codex/gpt-5.6-sol/xhigh"),
            [
                "codex/gpt-5.6-sol/xhigh",
                "codex/gpt-5.6-luna/low",
            ],
        )
        self.assertEqual(roster[0], "codex/gpt-5.6-sol/high")
        self.assertEqual(external_skill_path("OSINT"), "/challenge/skills/ctf-osint/SKILL.md")
        self.assertEqual(external_skill_path("blockchain"), "/challenge/skills/ctf-misc/SKILL.md")
        analyst_prompt = build_prompt(
            ChallengeMeta(name="heap", category="pwn"),
            ["chall"],
            model_spec="codex/gpt-5.6-terra/high",
        )
        self.assertIn("do not repeat the scout's full skill read", analyst_prompt)

    def test_prompt_prefers_local_source_before_remote_verification(self) -> None:
        prompt = build_prompt(
            ChallengeMeta(
                name="source-service",
                category="web",
                connection_info="https://challenge.example",
            ),
            ["source.zip"],
            model_spec="codex/gpt-5.6-sol/high",
        )
        self.assertIn("LOCAL-FIRST REQUIRED", prompt)
        self.assertIn("do not contact the live service yet", prompt)
        self.assertIn("send only the minimum clean verification request", prompt)
        self.assertNotIn("very first tool call MUST connect", prompt)

        remote_only = build_prompt(
            ChallengeMeta(
                name="remote-only",
                category="web",
                connection_info="https://challenge.example",
            ),
            [],
            model_spec="codex/gpt-5.6-sol/high",
        )
        self.assertIn("very first tool call MUST connect", remote_only)

    def test_desktop_and_billing_safety_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.max_concurrent_challenges, 4)
        self.assertEqual(settings.container_memory_limit, "4g")
        self.assertFalse(settings.enable_api_fallback)
        self.assertEqual(settings.solver_handoff_wait_seconds, 180)
        self.assertEqual(settings.solver_max_tokens, 1_500_000)
        self.assertEqual(settings.solver_max_raw_tokens, 12_000_000)
        self.assertEqual(settings.solver_cached_token_weight, 0.10)
        self.assertEqual(settings.solver_compaction_timeout_seconds, 300)
        self.assertEqual(settings.solver_compaction_max_waits, 2)
        self.assertEqual(DEFAULT_MODELS, ["codex/gpt-5.6-sol/high"])
        self.assertTrue(settings.dynamic_delegation_enabled)
        self.assertEqual(settings.delegate_max_concurrent, 2)
        self.assertEqual(settings.solver_turn_idle_timeout_seconds, 300)
        self.assertEqual(settings.delegate_turn_idle_timeout_seconds, 180)
        self.assertTrue(settings.delegate_postprocess_on_budget_stop)
        self.assertEqual(settings.delegate_postprocess_max_agents, 1)

    def test_dashboard_settings_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None)
            configured = RuntimeSettings(
                models=["codex/gpt-5.6-sol/medium"],
                max_concurrent_challenges=3,
                solver_compaction_timeout_seconds=420,
                solver_compaction_max_waits=3,
                solver_max_estimated_cost_usd=2.5,
            )
            save_ctfd_settings(
                CTFdSettings(
                    url="https://ctf.example.com/",
                    token="saved-token",
                    username="researcher",
                    password="saved-password",
                ),
                Path(root) / "challenges",
            )
            save_runtime_settings(configured, Path(root) / "challenges")

            restarted = Settings(_env_file=None)
            loaded = load_runtime_settings(
                restarted,
                list(DEFAULT_MODELS),
                Path(root) / "challenges",
            )
            connection = load_ctfd_settings(restarted, Path(root) / "challenges")

            self.assertEqual(loaded.models, ["codex/gpt-5.6-sol/medium"])
            self.assertEqual(restarted.max_concurrent_challenges, 3)
            self.assertEqual(restarted.solver_compaction_timeout_seconds, 420)
            self.assertEqual(restarted.solver_compaction_max_waits, 3)
            self.assertEqual(restarted.solver_max_estimated_cost_usd, 2.5)
            self.assertEqual(restarted.openai_api_key, settings.openai_api_key)
            self.assertIsNotNone(connection)
            self.assertEqual(restarted.ctfd_url, "https://ctf.example.com")
            self.assertEqual(restarted.ctfd_token, "saved-token")
            self.assertEqual(restarted.ctfd_user, "researcher")
            self.assertEqual(restarted.ctfd_pass, "saved-password")

            save_ctfd_settings(CTFdSettings(), Path(root) / "challenges")
            disconnected = Settings(
                _env_file=None,
                ctfd_url="https://from-env.example.com",
                ctfd_token="from-env",
            )
            restored_disconnect = load_ctfd_settings(
                disconnected,
                Path(root) / "challenges",
            )
            self.assertIsNotNone(restored_disconnect)
            self.assertEqual(disconnected.ctfd_url, "")
            self.assertEqual(disconnected.ctfd_token, "")

            with self.assertRaisesRegex(ValueError, "absolute http"):
                CTFdSettings(url="ctf.example.com")

    def test_windows_launcher_only_overrides_saved_concurrency_when_requested(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "run-ctf-agent.bat"
        source = launcher.read_text(encoding="utf-8")

        self.assertNotIn(
            'if not defined CTF_AGENT_MAX_CHALLENGES set "CTF_AGENT_MAX_CHALLENGES=',
            source,
        )
        self.assertIn(
            'if defined CTF_AGENT_MAX_CHALLENGES set "CTF_AGENT_MAX_CHALLENGES_ARG=',
            source,
        )

    def test_launchers_build_missing_sandbox_image(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        windows_source = (project_root / "run-ctf-agent.bat").read_text(encoding="utf-8")
        unix_source = (project_root / "run-ctf-agent.sh").read_text(encoding="utf-8")

        for source in (windows_source, unix_source):
            self.assertIn("docker info", source)
            self.assertIn("docker image inspect", source)
            self.assertIn("sandbox/Dockerfile.sandbox", source)
            self.assertIn("docker build", source)

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

    def test_session_read_polling_never_permanently_trips_loop_guard(self) -> None:
        detector = LoopDetector()
        for _ in range(20):
            self.assertIsNone(detector.check("session_read", {"session_id": "debugger"}))
            self.assertIsNone(
                detector.record_result(
                    "session_read",
                    {"session_id": "debugger"},
                    "(no output)",
                )
            )

    def test_docker_memory_limit_parser_accepts_runtime_formats(self) -> None:
        sandbox = DockerSandbox(image="unused", challenge_dir=".", memory_limit="1.5GB")
        self.assertEqual(sandbox._parse_memory_limit(), int(1.5 * 1024**3))
        sandbox.memory_limit = "768m"
        self.assertEqual(sandbox._parse_memory_limit(), 768 * 1024**2)

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
                {"review": {
                    "flag": "GoN{native}",
                    "flags": ["GoN{native}"],
                    "format_hint": "DH{...}",
                    "format_mismatches": ["GoN{native}"],
                    "review_required": True,
                }},
                {"deleted"},
            )
            results, candidates = load_runtime_state(settings)
            self.assertEqual(results["done"]["flag"], "TEAM{done}")
            self.assertTrue(candidates["review"]["review_required"])
            self.assertEqual(candidates["review"]["format_hint"], "DH{...}")
            self.assertEqual(candidates["review"]["format_mismatches"], ["GoN{native}"])
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

        alternate = assess_solver_output(
            output_type="flag_found",
            flag="GoN{native_output}",
            method="unmodified verifier stdout",
            confirmed_flag=None,
            flag_format="DH{...}",
        )
        self.assertEqual(alternate.status, CANDIDATE_FOUND)
        self.assertEqual(alternate.flag, "GoN{native_output}")
        self.assertIn("preserve it verbatim", alternate.findings)

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


class _FakeWorkspaceSandbox:
    def __init__(self, workspace: str) -> None:
        self.workspace_dir = workspace

    async def exec(self, _command: str, timeout_s: int = 120):
        return SimpleNamespace(exit_code=0, stdout="REPRO_OK", stderr="")


class _GaveUpSolver:
    def __init__(self, workspace: str) -> None:
        self.model_spec = "codex/test"
        self.agent_name = "budget/codex/test"
        self.sandbox = _FakeWorkspaceSandbox(workspace)
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


class _RecoveredCheckpointSolver(_CandidateSolver):
    def __init__(self, workspace: str) -> None:
        super().__init__(workspace)
        self.sandbox.shared_workspace_dir = ""
        self.calls = 0

    async def run_until_done_or_gave_up(self) -> SolverResult:
        self.calls += 1
        self._step_count += 1
        if self.calls == 1:
            return SolverResult(
                None,
                PROGRESS_CHECKPOINT,
                "fresh-thread recovery ready",
                self._step_count,
                0.01,
                "trace.jsonl",
                stop_reason="compaction recovery: timed out; switched to fresh thread",
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
    async def test_container_limit_is_held_for_full_sandbox_lifetime(self) -> None:
        class FakeContainer:
            def __init__(self, suffix: str) -> None:
                self.id = suffix * 64

            async def start(self) -> None:
                return None

            async def put_archive(self, _path: str, _data: bytes) -> None:
                return None

            async def show(self) -> dict:
                return {"Id": self.id}

            async def stats(self, **_kwargs) -> list[dict]:
                return [{}]

            async def delete(self, **_kwargs) -> None:
                return None

        class FakeContainers:
            def __init__(self, container: FakeContainer) -> None:
                self.container = container

            async def create(self, _config: dict) -> FakeContainer:
                return self.container

        class FakeDocker:
            def __init__(self, container: FakeContainer) -> None:
                self.containers = FakeContainers(container)

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as root:
            first = DockerSandbox(
                image="unused",
                challenge_dir=root,
                workspace_dir=str(Path(root) / "first"),
                keep_workspace=True,
            )
            second = DockerSandbox(
                image="unused",
                challenge_dir=root,
                workspace_dir=str(Path(root) / "second"),
                keep_workspace=True,
            )
            dockers = iter(
                [FakeDocker(FakeContainer("a")), FakeDocker(FakeContainer("b"))]
            )
            configure_semaphore(1)
            try:
                with patch("backend.sandbox.aiodocker.Docker", side_effect=lambda: next(dockers)):
                    await first.start()
                    second_start = asyncio.create_task(second.start())
                    await asyncio.sleep(0)
                    self.assertFalse(second_start.done())

                    await first.stop()
                    await asyncio.wait_for(second_start, timeout=1)
                    self.assertIsNotNone(second._container)
                    await second.stop()
            finally:
                configure_semaphore(50)

    async def test_idle_turn_is_interrupted_and_resumed_without_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=1,
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

        solver._interrupt_requested = False
        solver._resume_stop_reason = ""
        solver.bump("a second background update")
        self.assertEqual(len(calls), 1)
        self.assertEqual(solver.tracer.events[-1][0], "resume_interrupt_deferred")

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

    async def test_auto_spawn_honors_retry_backoff_and_run_cap(self) -> None:
        now = asyncio.get_running_loop().time()
        deps = SimpleNamespace(
            dismissed_challenges=set(),
            swarms={},
            swarm_tasks={},
            swarm_run_counts={"retry": 1},
            swarm_retry_after={"retry": now + 60},
            settings=SimpleNamespace(coordinator_max_swarm_runs_per_challenge=2),
            max_concurrent_challenges=1,
        )
        with patch(
            "backend.agents.coordinator_core.do_spawn_swarm",
            new=AsyncMock(return_value="spawned"),
        ) as spawn:
            await _auto_spawn_one(deps, "retry")
            spawn.assert_not_awaited()

            deps.swarm_retry_after["retry"] = 0
            await _auto_spawn_one(deps, "retry")
            spawn.assert_awaited_once_with(deps, "retry")

            spawn.reset_mock()
            deps.swarm_run_counts["retry"] = 2
            await _auto_spawn_one(deps, "retry")
            spawn.assert_not_awaited()

    async def test_api_fallback_keeps_original_delegate_lane_identity(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(
                _env_file=None,
                workspace_root=str(Path(root) / "workspace"),
                experience_root=str(Path(root) / "experience"),
                logs_root=str(Path(root) / "logs"),
            )
            lane = "codex/gpt-5.6-luna/low/delegate-01"
            swarm = ChallengeSwarm(
                challenge_dir=root,
                meta=ChallengeMeta(name="fallback", category="misc"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=[lane],
                no_submit=True,
            )
            solver = swarm._create_pydantic_solver(
                "openai/gpt-5.6-sol/high",
                sandbox=SimpleNamespace(),
                owns_sandbox=False,
                lane_model_spec=lane,
            )

            self.assertIsInstance(solver, Solver)
            self.assertEqual(solver.model_spec, "openai/gpt-5.6-sol/high")
            self.assertEqual(solver.lane_model_spec, lane)
            self.assertEqual(solver.deps.model_spec, lane)
            self.assertEqual(solver.agent_name, f"fallback/{lane}")
            self.assertEqual(solver_role(solver.lane_model_spec).key, "delegate")
            solver.tracer.close()

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
            self.assertEqual(deps.swarm_run_counts["snapshot"], 1)
            settings.solver_max_steps = 50
            task = deps.swarm_tasks["snapshot"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertIsNot(swarm.settings, settings)
        self.assertEqual(swarm.settings.solver_max_steps, 300)
        self.assertEqual(swarm.model_specs, ["codex/gpt-5.6-sol/high"])

    async def test_operator_restart_archives_usage_and_starts_with_fresh_budget(self) -> None:
        settings = Settings(_env_file=None)
        gate = asyncio.Event()

        async def fake_run(_swarm: ChallengeSwarm) -> None:
            await gate.wait()

        tracker = CostTracker()
        live_agent = "restart/codex/gpt-5.6-sol/high"
        other_agent = "other/codex/gpt-5.6-sol/high"
        tracker.record_tokens(live_agent, "gpt-5.6-sol", input_tokens=1_500_000)
        tracker.record_tokens(other_agent, "gpt-5.6-sol", input_tokens=123)
        deps = SimpleNamespace(
            swarms={},
            swarm_tasks={},
            swarm_run_counts={"restart": 2},
            swarm_retry_after={"restart": 123.0},
            results={},
            candidates={},
            dismissed_challenges=set(),
            max_concurrent_challenges=1,
            ctfd=SimpleNamespace(is_configured=False),
            challenges_root="challenges",
            challenge_dirs={"restart": "."},
            challenge_metas={"restart": ChallengeMeta(name="restart", category="misc")},
            cost_tracker=tracker,
            settings=settings,
            model_specs=["codex/gpt-5.6-sol/high"],
            no_submit=True,
            coordinator_inbox=asyncio.Queue(),
        )

        with patch("backend.agents.swarm.ChallengeSwarm.run", new=fake_run):
            await do_spawn_swarm(deps, "restart", reset_run_budget=True)
            self.assertNotIn(live_agent, tracker.by_agent)
            archived = [name for name in tracker.by_agent if name.startswith(live_agent + "#restart-")]
            self.assertEqual(len(archived), 1)
            self.assertEqual(tracker.by_agent[archived[0]].usage.input_tokens, 1_500_000)
            self.assertIn(other_agent, tracker.by_agent)
            self.assertEqual(deps.swarm_run_counts["restart"], 1)
            self.assertNotIn("restart", deps.swarm_retry_after)
            task = deps.swarm_tasks["restart"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_new_swarm_uses_per_challenge_lead_model_override(self) -> None:
        settings = Settings(_env_file=None)
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
            challenge_dirs={"xhigh challenge": "."},
            challenge_metas={
                "xhigh challenge": ChallengeMeta(
                    name="xhigh challenge",
                    category="reversing",
                    lead_model_spec="codex/gpt-5.6-sol/xhigh",
                )
            },
            cost_tracker=CostTracker(),
            settings=settings,
            model_specs=["codex/gpt-5.6-sol/high"],
            no_submit=True,
            coordinator_inbox=asyncio.Queue(),
        )

        with patch("backend.agents.swarm.ChallengeSwarm.run", new=fake_run):
            await do_spawn_swarm(deps, "xhigh challenge")
            swarm = deps.swarms["xhigh challenge"]
            self.assertEqual(swarm.model_specs, ["codex/gpt-5.6-sol/xhigh"])
            task = deps.swarm_tasks["xhigh challenge"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_restarted_swarm_receives_persisted_solution_review(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = Settings(_env_file=None, workspace_root=workspace_root)
            review = (
                Path(challenge_shared_path(settings, "reviewed restart"))
                / "review"
                / "CURRENT_SOLUTION_REVIEW.md"
            )
            review.parent.mkdir(parents=True)
            review.write_text(
                "## 판정\nPIVOT\n\n## 다음 권고\n- parser 경계를 먼저 재현합니다.\n",
                encoding="utf-8",
            )
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
                challenge_dirs={"reviewed restart": "."},
                challenge_metas={
                    "reviewed restart": ChallengeMeta(
                        name="reviewed restart", category="misc"
                    )
                },
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=["codex/gpt-5.6-sol/high"],
                no_submit=True,
                coordinator_inbox=asyncio.Queue(),
            )

            with patch("backend.agents.swarm.ChallengeSwarm.run", new=fake_run):
                await do_spawn_swarm(deps, "reviewed restart")
                swarm = deps.swarms["reviewed restart"]
                task = deps.swarm_tasks["reviewed restart"]
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            self.assertIn("CURRENT_SOLUTION_REVIEW.md", swarm.solution_review_directive)
            self.assertIn("PIVOT", swarm.solution_review_directive)

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

    async def test_kill_preserves_output_backed_candidate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as workspace_root:
            settings = Settings(_env_file=None, workspace_root=workspace_root)
            shared = Path(challenge_shared_path(settings, "candidate-before-submit"))
            state_path = shared / "reasoning" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "evidence": [
                            {
                                "id": "EV-output-backed",
                                "kind": "candidate",
                                "claim": "formatted candidate recovered",
                                "observed_excerpt": "model_accepts=True flag = TEAM{recovered}",
                                "confidence": 0.99,
                            },
                            {
                                "id": "EV-note-only",
                                "kind": "static",
                                "claim": "TEAM{must_not_be_promoted}",
                                "observed_excerpt": "TEAM{must_not_be_promoted}",
                                "confidence": 1.0,
                            },
                            {
                                "id": "EV-unsupported",
                                "kind": "candidate",
                                "claim": "TEAM{claim_only}",
                                "observed_excerpt": "",
                                "confidence": 0.99,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            swarm = SimpleNamespace(kill=unittest.mock.Mock())
            deps = SimpleNamespace(
                settings=settings,
                swarms={"candidate-before-submit": swarm},
                candidates={},
                results={},
                challenge_metas={
                    "candidate-before-submit": ChallengeMeta(
                        name="candidate-before-submit",
                        category="reversing",
                        flag_format="TEAM{...}",
                    )
                },
            )

            with patch("backend.agents.coordinator_core.persist_deps_state") as persist:
                message = await do_kill_swarm(deps, "candidate-before-submit")

            swarm.kill.assert_called_once_with("operator requested stop")
            persist.assert_called_once_with(deps)
            self.assertIn("preserved 1 unverified candidate", message)
            self.assertEqual(
                deps.candidates["candidate-before-submit"]["flags"],
                ["TEAM{recovered}"],
            )
            self.assertEqual(
                deps.candidates["candidate-before-submit"]["sources"],
                ["candidate evidence: EV-output-backed"],
            )

    async def test_startup_recovers_stopped_candidate_without_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace_root = Path(root) / "workspace"
            challenges_root = Path(root) / "challenges"
            challenges_root.mkdir()
            settings = Settings(
                _env_file=None,
                workspace_root=str(workspace_root),
                logs_root=str(Path(root) / "logs"),
                experience_root=str(Path(root) / "experience"),
            )
            state_path = (
                Path(challenge_shared_path(settings, "stopped-candidate"))
                / "reasoning"
                / "state.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "evidence": [
                            {
                                "id": "EV-before-crash",
                                "kind": "candidate",
                                "observed_excerpt": "verified model output: TEAM{resume_me}",
                                "confidence": 0.95,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            meta = ChallengeMeta(
                name="stopped-candidate",
                category="reversing",
                flag_format="TEAM{...}",
            )

            _ctfd, _cost, deps = build_deps(
                settings,
                challenges_root=str(challenges_root),
                challenge_dirs={"stopped-candidate": str(Path(root) / "challenge")},
                challenge_metas={"stopped-candidate": meta},
            )
            poller = SimpleNamespace(known_challenges={"stopped-candidate"}, known_solved=set())

            self.assertEqual(
                deps.candidates["stopped-candidate"]["flags"],
                ["TEAM{resume_me}"],
            )
            self.assertNotIn("stopped-candidate", _unsolved_names(deps, poller))

    def test_startup_ignores_empty_challenge_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            challenges_root = Path(root) / "challenges"
            invalid = challenges_root / "partial-upload"
            invalid.mkdir(parents=True)
            (invalid / "metadata.yml").write_text("", encoding="utf-8")
            settings = Settings(
                _env_file=None,
                workspace_root=str(Path(root) / "workspace"),
                logs_root=str(Path(root) / "logs"),
            )

            _ctfd, _cost, deps = build_deps(
                settings,
                challenges_root=str(challenges_root),
            )

            self.assertEqual(deps.challenge_metas, {})
            self.assertEqual(deps.challenge_dirs, {})

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

    async def test_codex_compaction_waits_again_before_timing_out(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._thread_id = "thread-compact"
        solver._compact_done = asyncio.Event()
        solver._latest_raw_tokens = 1_500_000
        solver.settings = SimpleNamespace(
            solver_compaction_timeout_seconds=30,
            solver_compaction_max_waits=2,
        )
        solver.agent_name = "hard/codex/test"
        solver.tracer = _Tracer()

        async def fake_rpc(_method: str, _params: dict) -> dict:
            return {"result": {}}

        wait_calls = 0

        async def fake_wait_for(awaitable, *, timeout: float):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls == 1:
                awaitable.close()
                raise TimeoutError
            solver._compact_done.set()
            return await awaitable

        solver._rpc = fake_rpc
        with patch("backend.agents.codex_solver.asyncio.wait_for", side_effect=fake_wait_for):
            compacted = await solver._compact_between_turns()

        self.assertTrue(compacted)
        self.assertEqual(wait_calls, 2)
        self.assertTrue(any(name == "compact_wait_retry" for name, _ in solver.tracer.events))

    async def test_codex_compaction_failure_recovers_on_fresh_thread(self) -> None:
        solver = object.__new__(CodexSolver)
        solver._thread_id = "thread-old"
        solver._thread_params = {"model": "gpt-test", "cwd": "/challenge"}
        solver._latest_raw_tokens = 2_000_000
        solver._turn_start_raw_tokens = 1_000_000
        solver._resume_after_checkpoint = False
        solver.agent_name = "hard/codex/test"
        solver.tracer = _Tracer()
        calls: list[tuple[str, dict]] = []

        async def fake_rpc(method: str, params: dict) -> dict:
            calls.append((method, params))
            return {"result": {"thread": {"id": "thread-new"}}}

        solver._rpc = fake_rpc
        recovered = await solver._recover_from_compaction_failure("compaction timeout")

        self.assertTrue(recovered)
        self.assertEqual(solver._thread_id, "thread-new")
        self.assertEqual(solver._latest_raw_tokens, 0)
        self.assertTrue(solver._resume_after_checkpoint)
        self.assertEqual(calls, [("thread/start", solver._thread_params)])

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

    async def test_codex_ignores_late_compaction_from_replaced_thread(self) -> None:
        class FakeStdout:
            def __init__(self, lines: list[bytes]) -> None:
                self.lines = lines

            async def readline(self) -> bytes:
                return self.lines.pop(0) if self.lines else b""

        notification = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-old",
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
        solver._compacting_thread_id = "thread-new"
        solver._latest_raw_tokens = 42
        solver.tracer = _Tracer()

        await solver._read_loop()

        self.assertFalse(solver._compact_done.is_set())
        self.assertTrue(
            any(name == "compact_complete_ignored" for name, _ in solver.tracer.events)
        )

    async def test_codex_transport_failure_wakes_pending_rpc_immediately(self) -> None:
        solver = object.__new__(CodexSolver)
        pending = asyncio.get_running_loop().create_future()
        solver._pending_responses = {7: pending}
        solver._turn_done = asyncio.Event()
        solver._transport_failed = asyncio.Event()
        solver._stderr_tail = deque(["fatal startup error"])
        solver._turn_error = None

        solver._fail_transport(RuntimeError("app-server EOF"))

        with self.assertRaisesRegex(RuntimeError, "app-server EOF"):
            await pending
        self.assertTrue(solver._turn_done.is_set())
        self.assertTrue(solver._transport_failed.is_set())
        self.assertIn("fatal startup error", solver._turn_error)

    async def test_codex_coordinator_interrupts_timed_out_turn(self) -> None:
        deps = SimpleNamespace(settings=SimpleNamespace(), model_specs=[])
        coordinator = CodexCoordinator(deps)
        coordinator._proc = SimpleNamespace(returncode=None)
        coordinator._thread_id = "thread-1"
        calls: list[tuple[str, dict]] = []

        async def fake_rpc(method: str, params: dict, **_kwargs) -> dict:
            calls.append((method, params))
            if method == "turn/start":
                return {"result": {"turn": {"id": "turn-1"}}}
            if method == "turn/interrupt":
                coordinator._turn_done.set()
            return {"result": {}}

        coordinator._rpc = fake_rpc
        real_wait_for = asyncio.wait_for
        waits = 0

        async def timeout_once(awaitable, timeout):
            nonlocal waits
            waits += 1
            if waits == 1:
                awaitable.close()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout)

        with patch("backend.agents.codex_coordinator.asyncio.wait_for", new=timeout_once):
            await coordinator.turn("status")

        self.assertEqual([method for method, _ in calls], ["turn/start", "turn/interrupt"])
        self.assertEqual(calls[1][1], {"threadId": "thread-1", "turnId": "turn-1"})

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
                # The productive turn-slice checkpoint must not consume this
                # sole attempt; the following candidate turn must still run.
                max_attempts_per_challenge=1,
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

    async def test_compaction_recovery_resumes_even_without_new_progress_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            settings = SimpleNamespace(
                max_attempts_per_challenge=1,
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
            solver = _RecoveredCheckpointSolver(workspace)
            result, _ = await swarm._run_solver_loop(solver, "codex/gpt-5.6-sol/xhigh")

            self.assertEqual(result.status, CANDIDATE_FOUND)
            self.assertEqual(solver.calls, 2)

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

    async def test_delegate_rejects_lead_private_input_paths(self) -> None:
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="private-input", category="reversing"),
            ctfd=SimpleNamespace(is_configured=False),
            cost_tracker=CostTracker(),
            settings=Settings(_env_file=None),
            model_specs=list(DEFAULT_MODELS),
            no_submit=True,
        )

        message = await swarm._spawn_delegate(
            "Analyze /challenge/workspace/tnt/output.bin for the recovered state",
            "Write a reproducer to /challenge/shared/delegates/delegate-01.md",
        )

        self.assertIn("private to the lead lane", message)
        self.assertEqual(swarm.delegate_tasks, {})

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
                "## Reproduction\n```yaml\nreproducer:\n  command: python3 repro.py\n"
                "  expect:\n    exit_code: 0\n    stdout_contains: REPRO_OK\n```\n"
                "## Scope and limitations\nLocal component rejection only.\n"
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
            self.assertIn("REPRO_OK", result.stop_reason)
            unread = await swarm.message_bus.check(lead)
            self.assertTrue(
                any("SUPPORTED_VERIFIED" in finding.content for finding in unread)
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
            checkpoint = json.loads(
                (Path(root) / ".ctf-agent-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["status"], CANCELLED)
            self.assertIn("cancelled", checkpoint["stop_reason"])

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

    async def test_budget_stop_without_shared_evidence_skips_postprocessor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(_env_file=None, workspace_root=root)
            lead = "codex/gpt-5.6-sol/xhigh"
            source = "codex/gpt-5.6-luna/low/delegate-01"
            swarm = ChallengeSwarm(
                challenge_dir=".",
                meta=ChallengeMeta(name="empty-budget-handoff", category="reversing"),
                ctfd=SimpleNamespace(is_configured=False),
                cost_tracker=CostTracker(),
                settings=settings,
                model_specs=[lead, source],
                no_submit=True,
            )
            swarm.delegate_requests[source] = {
                "task": "Test one bounded branch",
                "deliverable": "Observed output",
                "handoff": "/challenge/shared/delegates/delegate-01.md",
                "host_handoff": str(Path(root) / "missing.md"),
            }
            result = SolverResult(
                flag=None,
                status=BUDGET_EXHAUSTED,
                findings_summary="No result before interruption.",
                step_count=3,
                cost_usd=0.01,
                log_path="",
                stop_reason="effective token budget exhausted",
            )

            await swarm._handle_delegate_budget_stop(source, result)

            unread = await swarm.message_bus.check(lead)
            self.assertIn("no recoverable shared evidence", unread[0].content)
            self.assertEqual(swarm._postprocess_count, 0)
            self.assertEqual(swarm.delegate_tasks, {})

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
                "## Scope and limitations\nLocal component test only.\n"
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
        self.assertIn("FORMAT-HINT MISMATCH", message)
        self.assertFalse(confirmed)
        self.assertEqual(ctfd.calls, ["flag{wrong}"])
        self.assertEqual(swarm.all_candidate_flags(), ["flag{wrong}"])
        swarm.discard_candidate("flag{wrong}")
        self.assertEqual(swarm.all_candidate_flags(), [])

        message, confirmed = await swarm.try_submit_flag("cce2026{first}", "codex/test")
        self.assertIn("SUBMISSION LIMIT", message)
        self.assertFalse(confirmed)
        self.assertEqual(ctfd.calls, ["flag{wrong}"])

    async def test_retryable_submission_preserves_and_retries_exact_candidate(self) -> None:
        class FlakyCTFd:
            def __init__(self) -> None:
                self.calls = 0

            async def submit_flag(self, _challenge: str, flag: str):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("temporary network failure")
                return SimpleNamespace(status="correct", display=f"CORRECT: {flag}")

        ctfd = FlakyCTFd()
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="retry", category="misc", flag_format="TEAM{...}"),
            ctfd=ctfd,
            cost_tracker=CostTracker(),
            settings=SimpleNamespace(max_flag_submissions_per_challenge=1),
            model_specs=["codex/test"],
        )

        message, confirmed = await swarm.try_submit_flag("TEAM{same}", "codex/test")
        self.assertIn("RETRYABLE SUBMISSION ERROR", message)
        self.assertFalse(confirmed)
        self.assertEqual(swarm._total_submit_count, 0)
        self.assertNotIn("TEAM{same}", swarm._submitted_flags)
        self.assertEqual(swarm.all_candidate_flags(), ["TEAM{same}"])

        _message, confirmed = await swarm.try_submit_flag("TEAM{same}", "codex/test")
        self.assertTrue(confirmed)
        self.assertEqual(ctfd.calls, 2)
        self.assertEqual(swarm.confirmed_flag, "TEAM{same}")

    async def test_already_solved_does_not_confirm_submitted_candidate(self) -> None:
        class SolvedCTFd:
            async def submit_flag(self, _challenge: str, _flag: str):
                return SimpleNamespace(
                    status="already_solved",
                    display="ALREADY SOLVED EXTERNALLY",
                )

        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="external", category="misc", flag_format="TEAM{...}"),
            ctfd=SolvedCTFd(),
            cost_tracker=CostTracker(),
            settings=SimpleNamespace(max_flag_submissions_per_challenge=2),
            model_specs=["codex/test"],
        )

        message, confirmed = await swarm.try_submit_flag("TEAM{unverified}", "codex/test")
        self.assertIn("ALREADY SOLVED EXTERNALLY", message)
        self.assertFalse(confirmed)
        self.assertIsNone(swarm.confirmed_flag)
        self.assertEqual(swarm.all_candidate_flags(), ["TEAM{unverified}"])

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

        alternate = await coordinator_submit_flag(deps, "local", "GoN{native}")
        self.assertIn("FORMAT-HINT MISMATCH", alternate)
        self.assertIn("LOCAL CANDIDATE", alternate)
        self.assertEqual(
            deps.candidates["local"]["flags"],
            ["TEAM{guess}", "GoN{native}"],
        )
        self.assertEqual(deps.candidates["local"]["format_mismatches"], ["GoN{native}"])

        confirmed = await do_review_candidate(deps, "local", "TEAM{guess}", True)
        self.assertIn("LOCAL CONFIRMED", confirmed)
        self.assertEqual(deps.results["local"]["flag"], "TEAM{guess}")
        self.assertEqual(deps.results["local"]["accepted_flags"], ["TEAM{guess}"])
        self.assertEqual(deps.candidates["local"]["flags"], ["GoN{native}"])
        self.assertEqual(deps.candidates["local"]["status"], "conflicts_with_solved")

        # A late swarm cleanup may report its old candidate set again. The
        # confirmed value stays suppressed while genuinely different values
        # remain available for explicit operator review.
        _merge_candidate_record(
            deps,
            "local",
            ["TEAM{guess}", "GoN{native}", "TEAM{alternate}"],
            source="late swarm cleanup",
        )
        self.assertEqual(
            deps.candidates["local"]["flags"],
            ["GoN{native}", "TEAM{alternate}"],
        )

        deps.challenge_metas["local-retry"] = SimpleNamespace(flag_format="TEAM{...}")
        with tempfile.TemporaryDirectory() as workspace:
            deps.settings = SimpleNamespace(workspace_root=workspace)
            deps.challenge_dirs = {"local-retry": workspace}
            deps.swarms = {}
            deps.swarm_tasks = {}
            with patch(
                "backend.agents.coordinator_core.do_spawn_swarm",
                new=AsyncMock(return_value="SOL-led swarm spawned with rejection feedback"),
            ) as spawn:
                await coordinator_submit_flag(deps, "local-retry", "TEAM{wrong}")
                rejected = await do_review_candidate(deps, "local-retry", "TEAM{wrong}", False)
            spawn.assert_awaited_once()
            self.assertIn("TEAM{wrong}", spawn.await_args.kwargs["feedback"])
            feedback_file = Path(challenge_shared_path(deps.settings, "local-retry")) / "REJECTED_CANDIDATES.md"
            self.assertIn("TEAM{wrong}", feedback_file.read_text(encoding="utf-8"))
        self.assertIn("LOCAL REJECTED", rejected)
        self.assertIn("new solver swarm", rejected)
        self.assertNotIn("local-retry", deps.results)
        self.assertEqual(deps.candidates["local-retry"]["rejected_flags"], ["TEAM{wrong}"])


if __name__ == "__main__":
    unittest.main()
