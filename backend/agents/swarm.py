"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from backend.agents.solver import Solver
from backend.artifacts import (
    challenge_shared_path,
    handoff_quality_issues,
    verify_handoff_reproducer,
    workspace_progress_signature,
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
from backend.challenge_profiles import external_skill_path, solver_role
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.flag_format import flag_matches_format
from backend.message_bus import ChallengeMessageBus
from backend.model_specs import provider_from_spec, quota_fallback_spec
from backend.models import DEFAULT_MODELS
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    BUDGET_EXHAUSTED,
    CANCELLED,
    CANDIDATE_FOUND,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    HANDOFF_COMPLETE,
    PROGRESS_CHECKPOINT,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
    solver_agent_name,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


def _quota_fallback_spec(model_spec: str) -> str | None:
    return quota_fallback_spec(model_spec)


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None
    feedback_directive: str = ""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, SolverResult] = field(default_factory=dict)
    candidates: dict[str, SolverResult] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(
        default_factory=dict
    )  # per-model last submit timestamp
    _total_submit_count: int = 0
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)
    waiting_models: set[str] = field(default_factory=set)
    delegate_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    delegate_requests: dict[str, dict[str, str]] = field(default_factory=dict)
    _delegate_count: int = 0
    _postprocess_count: int = 0
    _delegate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _triage_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _solution_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _primary_tasks: set[asyncio.Task] = field(default_factory=set)

    def __post_init__(self) -> None:
        # A swarm may append live delegate aliases. Never mutate the coordinator's
        # configured primary roster or leak one challenge's workers into another.
        self.model_specs = list(self.model_specs)

    def _create_solver(self, model_spec: str, task_directive: str = ""):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - openai/*, bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag):
            return self.try_submit_flag(flag, model_spec)

        _notify = self._make_notify_fn(model_spec)

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver

            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver

            can_delegate = solver_role(model_spec).key == "lead" and bool(
                getattr(self.settings, "dynamic_delegation_enabled", True)
            )
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                delegate_task_fn=self._spawn_delegate if can_delegate else None,
                delegate_status_fn=self._delegate_status if can_delegate else None,
                task_directive=task_directive,
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Publish a milestone to the coordinator and sibling solvers."""

        async def _notify(message: str) -> None:
            await self.message_bus.post(model_spec, message)
            role_key = solver_role(model_spec).key
            if role_key == "scout":
                self._triage_ready.set()
            elif role_key == "analyst":
                self._solution_ready.set()
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(f"[{self.meta.name}/{model_spec}] {message}")

        return _notify

    def _create_pydantic_solver(
        self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None
    ) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            ctfd=self.ctfd,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    @staticmethod
    def _delegate_tasks_overlap(first: str, second: str) -> bool:
        """Catch cheap paraphrases so scarce worker slots are not duplicated."""

        def normalize(value: str) -> str:
            return " ".join(re.findall(r"[a-z0-9_]+", value.casefold()))

        left, right = normalize(first), normalize(second)
        if not left or not right:
            return False
        if left == right or difflib.SequenceMatcher(None, left, right).ratio() >= 0.82:
            return True
        left_words, right_words = set(left.split()), set(right.split())
        union = left_words | right_words
        return bool(union) and len(left_words & right_words) / len(union) >= 0.72

    def _delegate_model_for(self, task_type: str, difficulty: str) -> str:
        task_type = task_type.casefold()
        if task_type in {"verification", "reproduction", "independent_verification"}:
            setting = "delegate_verifier_model_spec"
            fallback = "codex/gpt-5.6-terra/medium"
        elif difficulty.casefold() == "hard" or task_type in {
            "crypto_analysis",
            "vm_analysis",
            "heap_exploitation",
            "protocol_inference",
        }:
            setting = "delegate_hard_model_spec"
            fallback = "codex/gpt-5.6-sol/high"
        else:
            setting = "delegate_model_spec"
            fallback = "codex/gpt-5.6-luna/low"
        return str(getattr(self.settings, setting, fallback)).strip()

    async def _spawn_delegate(
        self,
        task: str,
        deliverable: str,
        *,
        hypothesis_id: str = "",
        dependency_key: str = "",
        task_type: str = "extraction",
        difficulty: str = "easy",
        expected_seconds: int = 300,
        independent: bool = True,
    ) -> str:
        """Launch one bounded Luna worker without blocking the SOL lead."""
        task = " ".join(task.split())[:3000]
        deliverable = " ".join(deliverable.split())[:1500]
        hypothesis_id = hypothesis_id.strip()[:200]
        dependency_key = dependency_key.strip()[:300]
        task_type = task_type.strip().casefold()[:100] or "extraction"
        difficulty = difficulty.strip().casefold()[:20] or "easy"
        expected_seconds = max(1, min(int(expected_seconds), 3600))
        if len(task) < 8 or len(deliverable) < 4:
            return "DELEGATION REJECTED: provide one precise task and an explicit deliverable."
        if not bool(getattr(self.settings, "dynamic_delegation_enabled", True)):
            return "DELEGATION DISABLED by runtime policy."

        async with self._delegate_lock:
            def overlaps(request: dict) -> bool:
                same_dependency = bool(
                    dependency_key and request.get("dependency_key") == dependency_key
                )
                same_hypothesis = bool(
                    hypothesis_id
                    and request.get("hypothesis_id") == hypothesis_id
                    and request.get("task_type") == task_type
                )
                lexical_fallback = not (dependency_key or hypothesis_id) and self._delegate_tasks_overlap(
                    request["task"], task
                )
                return same_dependency or same_hypothesis or lexical_fallback

            if any(overlaps(request) for request in self.delegate_requests.values()):
                return "DELEGATION REJECTED: this task overlaps an existing worker assignment."
            active = sum(1 for worker in self.delegate_tasks.values() if not worker.done())
            max_concurrent = max(
                0,
                int(getattr(self.settings, "delegate_max_concurrent", 2)),
            )
            max_agents = max(0, int(getattr(self.settings, "delegate_max_agents", 4)))
            if not max_concurrent or not max_agents:
                return "DELEGATION DISABLED: worker limits are zero."
            if active >= max_concurrent:
                return (
                    f"DELEGATION DEFERRED: {active}/{max_concurrent} workers are active. "
                    "Continue the lead path and check delegates before requesting another."
                )
            if self._delegate_count >= max_agents:
                return f"DELEGATION LIMIT: all {max_agents} worker slots were already used."

            configured = self._delegate_model_for(task_type, difficulty)
            parts = configured.split("/")
            if len(parts) < 2 or parts[0] != "codex":
                return "DELEGATION CONFIG ERROR: delegate_model_spec must use the codex provider."
            base_spec = "/".join(parts[:3]) if len(parts) >= 3 else configured + "/low"
            self._delegate_count += 1
            delegate_id = f"delegate-{self._delegate_count:02d}"
            model_spec = f"{base_spec}/{delegate_id}"
            handoff = f"/challenge/shared/delegates/{delegate_id}.md"
            host_handoff = str(
                Path(challenge_shared_path(self.settings, self.meta.name))
                / "delegates"
                / f"{delegate_id}.md"
            )
            directive = (
                f"Delegate ID: {delegate_id}\n"
                f"Assigned task: {task}\n"
                f"Required deliverable: {deliverable}\n"
                f"Hypothesis: {hypothesis_id or 'unassigned'}\n"
                f"Dependency: {dependency_key or 'independent'}\n"
                f"Task type/difficulty: {task_type}/{difficulty}\n"
                f"Expected duration: {expected_seconds}s\n"
                f"Your private `/challenge/workspace/` is not visible to SOL. Put every script, "
                f"capture, or machine-readable artifact needed for reproduction under "
                f"`/challenge/shared/delegates/{delegate_id}/`, and reference only those shared "
                f"paths in the handoff. "
                f"Write the result to `{handoff}` using exactly these sections: "
                "`## Conclusion` (SUPPORTED/REFUTED/INCONCLUSIVE), `## Evidence`, "
                "`## Reproduction`, and `## Assumptions and conflicts`. Include commands or scripts "
                "and observed output. Under `## Reproduction`, include a fenced YAML block with "
                "`reproducer.command`, `reproducer.expect.exit_code`, and "
                "`reproducer.expect.stdout_contains`; the runtime executes it in a clean tool call. "
                "Recompute critical arithmetic and name contradictory shared "
                "claims rather than silently choosing one. Call `notify_coordinator` "
                "with a one-paragraph summary and the handoff path, then stop."
            )
            self.delegate_requests[model_spec] = {
                "task": task,
                "deliverable": deliverable,
                "handoff": handoff,
                "host_handoff": host_handoff,
                "hypothesis_id": hypothesis_id,
                "dependency_key": dependency_key,
                "task_type": task_type,
                "difficulty": difficulty,
                "expected_seconds": expected_seconds,
                "independent": independent,
            }
            self.model_specs.append(model_spec)
            worker = asyncio.create_task(
                self._run_solver(model_spec, task_directive=directive),
                name=f"solver-{self.meta.name}-{delegate_id}",
            )
            self.delegate_tasks[model_spec] = worker

        logger.info(
            "[%s] SOL lead launched %s for: %s",
            self.meta.name,
            delegate_id,
            task[:160],
        )
        return (
            f"DELEGATE STARTED: {delegate_id} using {base_spec}. "
            f"Handoff: {handoff}. Continue your own critical path."
        )

    def _lead_model_spec(self) -> str | None:
        return next(
            (spec for spec in self.model_specs if solver_role(spec).key == "lead"),
            None,
        )

    async def _spawn_budget_postprocessor(
        self,
        source_spec: str,
        summary_path: str,
        source_handoff: str,
    ) -> str:
        """Reserve one tiny worker to repair an unsafe interrupted handoff."""
        if not bool(getattr(self.settings, "delegate_postprocess_on_budget_stop", True)):
            return "disabled"

        async with self._delegate_lock:
            max_postprocessors = max(
                0,
                int(getattr(self.settings, "delegate_postprocess_max_agents", 1)),
            )
            if not max_postprocessors or self._postprocess_count >= max_postprocessors:
                return "limit reached"
            active = sum(
                1
                for spec, worker in self.delegate_tasks.items()
                if spec != source_spec and not worker.done()
            )
            max_concurrent = max(
                0,
                int(getattr(self.settings, "delegate_max_concurrent", 2)),
            )
            if not max_concurrent or active >= max_concurrent:
                return f"deferred; {active}/{max_concurrent} other workers active"

            configured = str(
                getattr(self.settings, "delegate_model_spec", "codex/gpt-5.6-luna/low")
            ).strip()
            parts = configured.split("/")
            if len(parts) < 2 or parts[0] != "codex":
                return "configuration error"
            base_spec = "/".join(parts[:3]) if len(parts) >= 3 else configured + "/low"
            self._delegate_count += 1
            self._postprocess_count += 1
            delegate_id = f"delegate-{self._delegate_count:02d}-postprocess"
            model_spec = f"{base_spec}/{delegate_id}"
            handoff = f"/challenge/shared/recovery/{delegate_id}.md"
            host_handoff = str(
                Path(challenge_shared_path(self.settings, self.meta.name))
                / "recovery"
                / f"{delegate_id}.md"
            )
            task = f"Consolidate interrupted evidence from {source_spec.rsplit('/', 1)[-1]}"
            deliverable = "A contradiction-checked handoff that tells SOL exactly what is reusable"
            directive = (
                f"Delegate ID: {delegate_id}\n"
                "This is a one-shot postprocessor, not a new solve lane. Do not perform broad triage "
                "or continue the full exploit. "
                f"Read `{summary_path}` and `{source_handoff}` when present. Check claims against "
                "the already shared source/evidence, repair arithmetic contradictions, and write "
                f"`{handoff}` with `## Conclusion` (SUPPORTED/REFUTED/INCONCLUSIVE), `## Evidence`, "
                "`## Reproduction`, and `## Assumptions and conflicts`. State the single next action "
                "for SOL, call `notify_coordinator`, then stop."
            )
            self.delegate_requests[model_spec] = {
                "task": task,
                "deliverable": deliverable,
                "handoff": handoff,
                "host_handoff": host_handoff,
                "kind": "postprocess",
                "source": source_spec,
            }
            self.model_specs.append(model_spec)
            worker = asyncio.create_task(
                self._run_solver(model_spec, task_directive=directive),
                name=f"solver-{self.meta.name}-{delegate_id}",
            )
            self.delegate_tasks[model_spec] = worker
        return f"started {delegate_id}; handoff={handoff}"

    async def _handle_delegate_budget_stop(
        self,
        model_spec: str,
        result: SolverResult,
    ) -> None:
        """Persist and route interrupted delegate state to SOL before cleanup."""
        request = self.delegate_requests.get(model_spec, {})
        source_id = model_spec.rsplit("/", 1)[-1]
        source_handoff = request.get(
            "handoff",
            f"/challenge/shared/delegates/{source_id}.md",
        )
        host_handoff = Path(request.get("host_handoff", ""))
        issues = (
            handoff_quality_issues(host_handoff) if host_handoff.is_file() else ["handoff missing"]
        )
        audit = "passed" if not issues else "UNSAFE: " + ", ".join(issues)
        recovery_dir = Path(challenge_shared_path(self.settings, self.meta.name)) / "recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        host_summary = recovery_dir / f"{source_id}-budget-stop.md"
        container_summary = f"/challenge/shared/recovery/{source_id}-budget-stop.md"
        findings = " ".join((result.findings_summary or "").split())[:4000]
        summary = (
            f"# Budget stop: {source_id}\n\n"
            f"- Source agent: `{model_spec}`\n"
            f"- Stop reason: {result.stop_reason or 'budget exhausted'}\n"
            f"- Attempt: {result.attempt}\n"
            f"- Tool steps: {result.step_count}\n"
            f"- Original task: {request.get('task', 'unknown')}\n"
            f"- Requested deliverable: {request.get('deliverable', 'unknown')}\n"
            f"- Original handoff: `{source_handoff}`\n"
            f"- Handoff audit: {audit}\n\n"
            "## Last findings\n"
            f"{findings or 'No concise findings were returned before interruption.'}\n"
        )
        try:
            temporary = host_summary.with_suffix(".tmp")
            temporary.write_text(summary, encoding="utf-8")
            temporary.replace(host_summary)
        except OSError as exc:
            logger.warning("Could not write budget-stop handoff %s: %s", host_summary, exc)

        postprocess = "not needed; source handoff passed audit"
        if issues and request.get("kind") != "postprocess":
            postprocess = await self._spawn_budget_postprocessor(
                model_spec,
                container_summary,
                source_handoff,
            )
        message = (
            f"BUDGET STOP HANDOFF: {source_id}; reason={result.stop_reason or 'budget exhausted'}; "
            f"audit={audit}; findings={findings or 'none'}; summary={container_summary}; "
            f"postprocess={postprocess}. Integrate only audit-passed evidence."
        )[:6000]
        lead_spec = self._lead_model_spec()
        await self.message_bus.post(
            "runtime-budget-guard",
            message,
            target=lead_spec,
        )
        if self.coordinator_inbox:
            self.coordinator_inbox.put_nowait(f"[{self.meta.name}] {message}")

    async def _delegate_status(self) -> str:
        """Return a compact lead-facing view of dynamically created workers."""
        if not self.delegate_requests:
            return "No delegates have been created."
        lines: list[str] = []
        for spec, request in self.delegate_requests.items():
            worker = self.delegate_tasks.get(spec)
            outcome = self.outcomes.get(spec)
            status = (
                outcome.status
                if outcome
                else "running"
                if worker and not worker.done()
                else "starting"
            )
            finding = self.findings.get(spec, "").strip().replace("\n", " ")[:700]
            host_handoff = Path(request.get("host_handoff", ""))
            if host_handoff.is_file():
                issues = handoff_quality_issues(host_handoff)
                audit = "passed" if not issues else "UNSAFE: " + ", ".join(issues)
            elif worker and not worker.done():
                audit = "pending"
            else:
                audit = "UNSAFE: handoff missing"
            lines.append(
                f"- {spec.rsplit('/', 1)[-1]}: {status}; handoff={request['handoff']}; "
                f"audit={audit}; finding={finding or 'none yet'}"
            )
        return "\n".join(lines)

    def _audited_delegate_handoff(self, model_spec: str) -> str | None:
        """Return a completed delegate handoff path only after schema audit."""
        if solver_role(model_spec).key != "delegate":
            return None
        request = self.delegate_requests.get(model_spec)
        if not request:
            return None
        host_handoff = Path(request.get("host_handoff", ""))
        if not host_handoff.is_file() or handoff_quality_issues(host_handoff):
            return None
        return request.get("handoff") or str(host_handoff)

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            if self.confirmed_flag:
                return f"ALREADY SOLVED — flag already confirmed: {self.confirmed_flag}", True

            normalized = flag.strip()

            if not flag_matches_format(normalized, self.meta.flag_format):
                return (
                    f'REJECTED — candidate does not match flag format "{self.meta.flag_format}".',
                    False,
                )

            submission_limit = max(
                1,
                int(getattr(self.settings, "max_flag_submissions_per_challenge", 8)),
            )
            if self._total_submit_count >= submission_limit:
                return (
                    f"SUBMISSION LIMIT — {submission_limit} candidates were already sent for this challenge. "
                    "Ask the operator to verify evidence before any further submission.",
                    False,
                )

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)
            self._total_submit_count += 1

            from backend.tools.core import do_submit_flag

            display, is_confirmed = await do_submit_flag(self.ctfd, self.meta.name, flag)
            if is_confirmed:
                self.confirmed_flag = normalized
            else:
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, is_confirmed

    async def _run_solver(
        self,
        model_spec: str,
        task_directive: str = "",
    ) -> SolverResult | None:
        role = solver_role(model_spec)
        has_scout = any(solver_role(spec).key == "scout" for spec in self.model_specs)
        has_analyst = any(solver_role(spec).key == "analyst" for spec in self.model_specs)
        wait_seconds = max(
            0.0,
            float(getattr(self.settings, "solver_handoff_wait_seconds", 60))
            * role.handoff_wait_factor,
        )
        solver = None

        try:
            wait_event = None
            wait_label = ""
            if role.key == "verifier" and has_analyst:
                wait_event = self._solution_ready
                wait_label = "ANALYST solution"
            elif role.key != "scout" and has_scout:
                wait_event = self._triage_ready
                wait_label = "SCOUT triage"

            if wait_event is not None and wait_seconds:
                self.waiting_models.add(model_spec)
                try:
                    await asyncio.wait_for(wait_event.wait(), timeout=wait_seconds)
                except TimeoutError:
                    logger.info(
                        "[%s/%s] %s handoff wait expired after %.0fs; starting with available evidence",
                        self.meta.name,
                        model_spec,
                        wait_label,
                        wait_seconds,
                    )
                finally:
                    self.waiting_models.discard(model_spec)

            if self.cancel_event.is_set():
                return None

            directive_parts = [self.feedback_directive.strip(), task_directive.strip()]
            directive = "\n\n".join(part for part in directive_parts if part)
            solver = self._create_solver(model_spec, task_directive=directive)
            self.solvers[model_spec] = solver
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            self.outcomes[model_spec] = result
            if result.status == BUDGET_EXHAUSTED and role.key == "delegate":
                await self._handle_delegate_budget_stop(model_spec, result)
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            self.waiting_models.discard(model_spec)
            if role.key == "scout":
                # A failed or quota-limited scout must never deadlock deeper lanes.
                self._triage_ready.set()
            elif role.key == "analyst":
                # A failed or quota-limited analyst must never deadlock VERIFIER.
                self._solution_ready.set()
            if solver is not None:
                await solver.stop()

    async def _run_solver_loop(
        self, solver, model_spec: str
    ) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        attempt = 0
        started_at = time.monotonic()
        max_attempts = solver_token_limits(self.settings, model_spec).attempts
        turn_timeout = solver_turn_timeout_limit(self.settings, model_spec)
        idle_timeout = solver_turn_idle_timeout_limit(self.settings, model_spec)
        max_runtime = max(turn_timeout, solver_runtime_limit(self.settings, model_spec))
        result = SolverResult(
            flag=None,
            status=CANCELLED,
            findings_summary="",
            step_count=0,
            cost_usd=0.0,
            log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            elapsed = time.monotonic() - started_at
            if attempt >= max_attempts:
                result = self._budget_result(
                    solver,
                    model_spec,
                    result,
                    attempt,
                    f"attempt budget exhausted ({attempt}/{max_attempts})",
                )
                break
            if elapsed >= max_runtime:
                result = self._budget_result(
                    solver,
                    model_spec,
                    result,
                    attempt,
                    f"runtime budget exhausted ({int(elapsed)}s/{max_runtime}s)",
                )
                break

            attempt += 1
            progress_before = self._progress_signature(solver)
            allowed = min(turn_timeout, max(1.0, max_runtime - elapsed))
            run_task = asyncio.create_task(
                solver.run_until_done_or_gave_up(),
                name=f"turn-{self.meta.name}-{model_spec}-{attempt}",
            )
            turn_deadline = time.monotonic() + allowed
            idle_interrupt_requested = False
            idle_interrupt_deadline = 0.0
            try:
                while True:
                    remaining = turn_deadline - time.monotonic()
                    if remaining <= 0:
                        done = set()
                        break
                    done, _ = await asyncio.wait(
                        {run_task},
                        timeout=min(1.0, remaining),
                    )
                    if done:
                        break

                    idle_reader = getattr(solver, "activity_idle_seconds", None)
                    tool_active = bool(getattr(solver, "tool_call_active", False))
                    idle_seconds = float(idle_reader()) if callable(idle_reader) else 0.0
                    if (
                        idle_timeout
                        and callable(idle_reader)
                        and not tool_active
                        and idle_seconds >= idle_timeout
                        and not idle_interrupt_requested
                    ):
                        reason = (
                            f"idle watchdog observed {int(idle_seconds)}s without model/tool "
                            f"activity (limit {idle_timeout}s)"
                        )
                        interrupter = getattr(solver, "request_resume_interrupt", None)
                        if callable(interrupter) and interrupter(reason):
                            idle_interrupt_requested = True
                            idle_interrupt_deadline = min(
                                turn_deadline,
                                time.monotonic() + 30.0,
                            )
                            logger.warning("[%s/%s] %s", self.meta.name, model_spec, reason)
                        else:
                            idle_interrupt_requested = True
                            idle_interrupt_deadline = time.monotonic()
                    if (
                        idle_interrupt_deadline
                        and time.monotonic() >= idle_interrupt_deadline
                        and not run_task.done()
                    ):
                        done = set()
                        break
            except asyncio.CancelledError:
                # asyncio.wait() does not propagate cancellation to its children.
                # Reap the active model turn so swarm cancellation cannot leave a
                # pending task behind.
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                raise
            if not done:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                reason = (
                    "idle watchdog interrupt did not complete within 30s"
                    if idle_interrupt_requested
                    else f"turn timeout exceeded ({int(allowed)}s)"
                )
                result = self._budget_result(
                    solver,
                    model_spec,
                    result,
                    attempt,
                    reason,
                )
                self._checkpoint(solver, model_spec, result, attempt)
                break
            result = run_task.result()
            result = replace(
                result,
                attempt=attempt,
                workspace_path=getattr(getattr(solver, "sandbox", None), "workspace_dir", ""),
            )
            progress_after = self._progress_signature(solver)
            self._checkpoint(solver, model_spec, result, attempt)

            # Only broadcast useful findings — skip errors and broken solvers
            if (
                result.status not in (ERROR, QUOTA_ERROR)
                and not (result.step_count == 0 and result.cost_usd == 0)
                and result.findings_summary
                and not result.findings_summary.startswith(("Error:", "Turn failed:"))
            ):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                self.cancel_event.set()
                self.winner = result
                logger.info(f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}")
                return result, solver

            if result.status == CANDIDATE_FOUND:
                self.candidates[model_spec] = result
                logger.info(
                    "[%s/%s] Recorded unverified candidate: %s",
                    self.meta.name,
                    model_spec,
                    result.flag,
                )
                if self.no_submit and not getattr(self.ctfd, "is_configured", False):
                    if self.coordinator_inbox:
                        self.coordinator_inbox.put_nowait(
                            f"LOCAL CANDIDATE REVIEW REQUIRED: {self.meta.name} "
                            f"from {model_spec}: {result.flag}"
                        )
                    return result, cast(SolverProtocol, solver)

            if result.status in (GAVE_UP, PROGRESS_CHECKPOINT, BUDGET_EXHAUSTED):
                handoff = self._audited_delegate_handoff(model_spec)
                if handoff:
                    request = self.delegate_requests.get(model_spec, {})
                    verified, verification = await verify_handoff_reproducer(
                        solver.sandbox,
                        request.get("host_handoff", ""),
                    )
                    if not verified:
                        solver.bump(
                            "Delegate handoff format passed but clean reproduction failed: "
                            f"{verification}. Repair the shared reproducer and its observed marker."
                        )
                        logger.warning(
                            "[%s/%s] Delegate reproducer rejected: %s",
                            self.meta.name,
                            model_spec,
                            verification,
                        )
                        continue
                    result = replace(
                        result,
                        status=HANDOFF_COMPLETE,
                        stop_reason=(
                            f"verified delegate handoff completed: {handoff}; {verification}"
                        ),
                    )
                    logger.info(
                        "[%s/%s] Delegate handoff passed audit; stopping without bump",
                        self.meta.name,
                        model_spec,
                    )
                    message = (
                        f"DELEGATE HANDOFF READY: {model_spec.rsplit('/', 1)[-1]}; "
                        f"audit=SUPPORTED_VERIFIED; {verification}; handoff={handoff}. "
                        "Integrate this evidence "
                        "before repeating the delegated experiment."
                    )
                    lead_spec = self._lead_model_spec()
                    await self.message_bus.post(
                        "runtime-delegate",
                        message,
                        target=lead_spec,
                        kind="reproduction",
                        claim=message,
                        confidence=1.0,
                        evidence_ids=[
                            f"runtime:{model_spec.rsplit('/', 1)[-1]}:reproducer"
                        ],
                        hypothesis_id=request.get("hypothesis_id") or None,
                        tags=[request.get("task_type", "delegate")],
                        urgency="urgent",
                    )
                    lead_solver = self.solvers.get(lead_spec) if lead_spec else None
                    if lead_solver is not None:
                        lead_solver.bump(message)
                    if self.coordinator_inbox:
                        self.coordinator_inbox.put_nowait(f"[{self.meta.name}] {message}")
                    return result, cast(SolverProtocol, solver)

            if result.status == CANCELLED:
                break

            if result.status == BUDGET_EXHAUSTED:
                break

            if result.status == PROGRESS_CHECKPOINT:
                if result.stop_reason.startswith("resume interrupt:"):
                    if "idle watchdog" in result.stop_reason:
                        insights = self._gather_sibling_insights(model_spec)
                        solver.bump(
                            f"{result.stop_reason}. The prior turn stalled without a tool call. "
                            "Resume from the saved evidence, choose the cheapest discriminating "
                            "experiment, and avoid broad dumps. "
                            f"Sibling evidence: {insights}"
                        )
                    logger.info(
                        "[%s/%s] Guidance/idle interrupt completed; resuming immediately",
                        self.meta.name,
                        model_spec,
                    )
                    continue
                if result.stop_reason.startswith("compaction recovery:"):
                    logger.warning(
                        "[%s/%s] Compaction recovery created a fresh thread; resuming immediately",
                        self.meta.name,
                        model_spec,
                    )
                    continue
                if progress_after == progress_before:
                    result = self._budget_result(
                        solver,
                        model_spec,
                        result,
                        attempt,
                        "turn slice produced no new workspace or shared artifact; "
                        "automatic token escalation denied",
                    )
                    self._checkpoint(solver, model_spec, result, attempt)
                    break
                logger.info(
                    "[%s/%s] Productive checkpoint compacted; resuming without cooldown",
                    self.meta.name,
                    model_spec,
                )
                continue

            budget_reason = self._budget_reason(solver, model_spec, attempt, started_at)
            if budget_reason:
                result = self._budget_result(solver, model_spec, result, attempt, budget_reason)
                self._checkpoint(solver, model_spec, result, attempt)
                break

            # Quota exhaustion: fall back to API-backed Pydantic AI solver
            if result.status == QUOTA_ERROR:
                fallback_spec = (
                    _quota_fallback_spec(model_spec)
                    if getattr(self.settings, "enable_api_fallback", False)
                    else None
                )
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    solver.sandbox = None  # type: ignore[assignment]
                    await solver.stop()
                    solver = self._create_pydantic_solver(
                        fallback_spec, sandbox=existing_sandbox, owns_sandbox=True
                    )
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue
                if _quota_fallback_spec(model_spec):
                    logger.warning(
                        "[%s/%s] API fallback is disabled; set ENABLE_API_FALLBACK=true to opt in",
                        self.meta.name,
                        model_spec,
                    )
                break

            if result.status in (CANDIDATE_FOUND, GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Re-plan immediately; wall-clock backoff is reserved for submissions/APIs.
                insights = self._gather_sibling_insights(model_spec)
                state_store = getattr(solver, "reasoning_state_store", None)
                if state_store is not None:
                    state = state_store.load()
                    refuted = [
                        f"{item.id}: {item.statement}"
                        for item in state.hypotheses
                        if item.status == "refuted"
                    ][-5:]
                    insights = (
                        f"Confirmed facts: {state.confirmed_facts[-8:]}\n"
                        f"Refuted hypotheses: {refuted}\n"
                        f"Current blocker: {state.current_blocker or 'not recorded'}\n"
                        f"Recent failed experiments: {state.failed_experiments[-3:]}\n"
                        f"Available sibling evidence: {insights}\n"
                        "Generate three materially different hypotheses, each with one cheap "
                        "discriminating experiment, then activate the best one."
                    )
                solver.bump(insights)
                logger.info(f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming")
                continue

        return result, solver

    @staticmethod
    def _step_count(solver) -> int:
        raw = getattr(solver, "_step_count", 0)
        if isinstance(raw, list):
            raw = raw[0] if raw else 0
        try:
            return max(0, int(raw))
        except TypeError, ValueError:
            return 0

    def _agent_usage(self, model_spec: str):
        return self.cost_tracker.by_agent.get(solver_agent_name(self.meta.name, model_spec))

    @staticmethod
    def _progress_signature(solver) -> str:
        state_store = getattr(solver, "reasoning_state_store", None)
        if state_store is not None:
            return state_store.semantic_signature()
        sandbox = getattr(solver, "sandbox", None)
        return workspace_progress_signature(
            getattr(sandbox, "workspace_dir", ""),
            getattr(sandbox, "shared_workspace_dir", ""),
        )

    def _budget_reason(self, solver, model_spec: str, attempt: int, started_at: float) -> str:
        limits = solver_token_limits(self.settings, model_spec)
        max_attempts = limits.attempts
        max_steps = solver_step_limit(self.settings, model_spec)
        max_runtime = solver_runtime_limit(self.settings, model_spec)
        max_cost = max(0.0, float(getattr(self.settings, "solver_max_estimated_cost_usd", 0.0)))
        usage = self._agent_usage(model_spec)
        cached_weight = getattr(self.settings, "solver_cached_token_weight", 0.10)
        metrics = token_metrics(
            usage.usage.input_tokens if usage else 0,
            usage.usage.output_tokens if usage else 0,
            usage.usage.cache_read_tokens if usage else 0,
            cached_weight,
        )
        cost = usage.cost_usd if usage else 0.0
        elapsed = time.monotonic() - started_at

        if self._step_count(solver) >= max_steps:
            return f"step budget exhausted ({self._step_count(solver)}/{max_steps})"
        if limits.effective_tokens and metrics.effective_tokens >= limits.effective_tokens:
            return (
                f"effective token budget exhausted ({metrics.effective_tokens}/"
                f"{limits.effective_tokens}; raw={metrics.raw_tokens})"
            )
        if limits.raw_tokens and metrics.raw_tokens >= limits.raw_tokens:
            return f"raw token safety ceiling exhausted ({metrics.raw_tokens}/{limits.raw_tokens})"
        if max_cost and cost >= max_cost:
            return f"estimated cost budget exhausted (${cost:.2f}/${max_cost:.2f})"
        if elapsed >= max_runtime:
            return f"runtime budget exhausted ({int(elapsed)}s/{max_runtime}s)"
        if attempt >= max_attempts:
            return f"attempt budget exhausted ({attempt}/{max_attempts})"
        return ""

    def _budget_result(
        self,
        solver,
        model_spec: str,
        previous: SolverResult,
        attempt: int,
        reason: str,
    ) -> SolverResult:
        usage = self._agent_usage(model_spec)
        workspace = getattr(getattr(solver, "sandbox", None), "workspace_dir", "")
        logger.warning("[%s/%s] %s", self.meta.name, model_spec, reason)
        tracer = getattr(solver, "tracer", None)
        if tracer:
            tracer.event("budget_exhausted", reason=reason, attempt=attempt)
        return replace(
            previous,
            status=BUDGET_EXHAUSTED,
            step_count=self._step_count(solver),
            cost_usd=usage.cost_usd if usage else previous.cost_usd,
            stop_reason=reason,
            workspace_path=workspace,
            attempt=attempt,
        )

    def _checkpoint(self, solver, model_spec: str, result: SolverResult, attempt: int) -> None:
        workspace = getattr(getattr(solver, "sandbox", None), "workspace_dir", "")
        if not workspace:
            return
        usage = self._agent_usage(model_spec)
        try:
            write_checkpoint(
                workspace,
                challenge=self.meta.name,
                model_spec=model_spec,
                status=result.status,
                attempt=attempt,
                steps=self._step_count(solver),
                tokens=usage.usage.total_tokens if usage else 0,
                estimated_cost_usd=usage.cost_usd if usage else result.cost_usd,
                findings=result.findings_summary,
                stop_reason=result.stop_reason,
            )
        except OSError as exc:
            logger.warning("Could not write solver checkpoint in %s: %s", workspace, exc)

    async def run(self) -> SolverResult | None:
        """Return a verified winner, or a standalone candidate awaiting human review."""
        benchmark_started = time.monotonic()
        benchmark_started_wall = time.time()

        def record_benchmark(result: SolverResult | None) -> None:
            destination = str(getattr(self.settings, "benchmark_results_path", "") or "")
            if not destination:
                return
            try:
                from backend.benchmark import append_trial, trial_from_swarm

                append_trial(
                    destination,
                    trial_from_swarm(
                        self,
                        result,
                        time.monotonic() - benchmark_started,
                        benchmark_started_wall,
                    ),
                )
            except Exception as exc:
                logger.warning("Could not record benchmark trial: %s", exc)

        tasks: set[asyncio.Task] = {
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in list(self.model_specs)
        }
        self._primary_tasks = set(tasks)
        tracked = set(tasks)

        async def cancel_workers(workers: set[asyncio.Task]) -> None:
            all_workers = workers | set(self.delegate_tasks.values())
            for worker in all_workers:
                if not worker.done():
                    worker.cancel()
            if all_workers:
                await asyncio.gather(*all_workers, return_exceptions=True)

        try:
            while tasks:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=0.25,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                tasks = set(pending)
                for worker in self.delegate_tasks.values():
                    if worker not in tracked:
                        tracked.add(worker)
                        tasks.add(worker)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self.cancel_event.set()
                        await cancel_workers(tasks)
                        record_benchmark(result)
                        return result
                    if (
                        result
                        and result.status == CANDIDATE_FOUND
                        and self.no_submit
                        and not getattr(self.ctfd, "is_configured", False)
                    ):
                        self.cancel_event.set()
                        await cancel_workers(tasks)
                        record_benchmark(result)
                        return result

            self.cancel_event.set()
            record_benchmark(self.winner)
            return self.winner
        except asyncio.CancelledError:
            self.cancel_event.set()
            await cancel_workers(tasks)
            raise
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            await cancel_workers(tasks)
            record_benchmark(None)
            return None

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
        self.cancel_event.set()
        for worker in (*self._primary_tasks, *self.delegate_tasks.values()):
            if not worker.done():
                worker.cancel()

    def _agent_status(self, spec: str) -> str:
        if spec in self.waiting_models and not self.cancel_event.is_set():
            return "waiting"
        outcome = self.outcomes.get(spec)
        if spec in self.solvers and outcome is None and not self.cancel_event.is_set():
            return "running"
        if outcome is not None and outcome is self.winner:
            return "won"
        return outcome.status if outcome is not None else "finished"

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        reasoning = {}
        for solver in self.solvers.values():
            store = getattr(solver, "reasoning_state_store", None)
            if store is None:
                continue
            state = store.load()
            reasoning = {
                "phase": state.phase,
                "semantic_revision": state.semantic_revision,
                "active_hypothesis": state.active_hypothesis,
                "current_blocker": state.current_blocker,
                "next_experiment": state.next_experiment,
                "evidence_count": len(state.evidence),
                "contradictions": state.contradictions[-5:],
                "hypotheses": [
                    {
                        "id": item.id,
                        "statement": item.statement,
                        "status": item.status,
                        "confidence": item.confidence,
                    }
                    for item in state.hypotheses
                ],
            }
            break
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "candidates": {
                spec: result.flag for spec, result in self.candidates.items() if result.flag
            },
            "reasoning": reasoning,
            "agents": {
                spec: {
                    "role": solver_role(spec).key,
                    "role_title": solver_role(spec).title,
                    "skill_path": external_skill_path(self.meta.category),
                    "findings": self.findings.get(spec, ""),
                    "status": self._agent_status(spec),
                    "stop_reason": (
                        self.outcomes[spec].stop_reason if spec in self.outcomes else ""
                    ),
                    "workspace_path": getattr(
                        getattr(self.solvers.get(spec), "sandbox", None), "workspace_dir", ""
                    ),
                }
                for spec in self.model_specs
            },
        }
