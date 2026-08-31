"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from backend.agents.solver import Solver
from backend.artifacts import write_checkpoint
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
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
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

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    outcomes: dict[str, SolverResult] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    _total_submit_count: int = 0
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - openai/*, bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
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
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {message}"
                )
        return _notify

    def _create_pydantic_solver(self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None) -> Solver:
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

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            self.outcomes[model_spec] = result
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            await solver.stop()

    async def _run_solver_loop(self, solver, model_spec: str) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        attempt = 0
        started_at = time.monotonic()
        max_attempts = max(1, int(getattr(self.settings, "max_attempts_per_challenge", 3)))
        turn_timeout = max(30, int(getattr(self.settings, "solver_turn_timeout_seconds", 1800)))
        max_runtime = max(turn_timeout, int(getattr(self.settings, "solver_max_runtime_seconds", 7200)))
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            elapsed = time.monotonic() - started_at
            if attempt >= max_attempts:
                result = self._budget_result(
                    solver, model_spec, result, attempt,
                    f"attempt budget exhausted ({attempt}/{max_attempts})",
                )
                break
            if elapsed >= max_runtime:
                result = self._budget_result(
                    solver, model_spec, result, attempt,
                    f"runtime budget exhausted ({int(elapsed)}s/{max_runtime}s)",
                )
                break

            attempt += 1
            allowed = min(turn_timeout, max(1.0, max_runtime - elapsed))
            run_task = asyncio.create_task(
                solver.run_until_done_or_gave_up(),
                name=f"turn-{self.meta.name}-{model_spec}-{attempt}",
            )
            done, _ = await asyncio.wait({run_task}, timeout=allowed)
            if not done:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
                result = self._budget_result(
                    solver, model_spec, result, attempt,
                    f"turn timeout exceeded ({int(allowed)}s)",
                )
                self._checkpoint(solver, model_spec, result, attempt)
                break
            result = run_task.result()
            result = replace(
                result,
                attempt=attempt,
                workspace_path=getattr(getattr(solver, "sandbox", None), "workspace_dir", ""),
            )
            self._checkpoint(solver, model_spec, result, attempt)

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                self.cancel_event.set()
                self.winner = result
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver

            if result.status == CANCELLED:
                break

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
                    solver = self._create_pydantic_solver(fallback_spec, sandbox=existing_sandbox, owns_sandbox=True)
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

            if result.status in (GAVE_UP, ERROR):
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
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

        return result, solver

    @staticmethod
    def _step_count(solver) -> int:
        raw = getattr(solver, "_step_count", 0)
        if isinstance(raw, list):
            raw = raw[0] if raw else 0
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _agent_usage(self, model_spec: str):
        return self.cost_tracker.by_agent.get(solver_agent_name(self.meta.name, model_spec))

    def _budget_reason(self, solver, model_spec: str, attempt: int, started_at: float) -> str:
        max_attempts = max(1, int(getattr(self.settings, "max_attempts_per_challenge", 3)))
        max_steps = max(1, int(getattr(self.settings, "solver_max_steps", 240)))
        max_runtime = max(1, int(getattr(self.settings, "solver_max_runtime_seconds", 7200)))
        max_tokens = max(0, int(getattr(self.settings, "solver_max_tokens", 1_000_000)))
        max_cost = max(0.0, float(getattr(self.settings, "solver_max_estimated_cost_usd", 0.0)))
        usage = self._agent_usage(model_spec)
        tokens = usage.usage.total_tokens if usage else 0
        cost = usage.cost_usd if usage else 0.0
        elapsed = time.monotonic() - started_at

        if self._step_count(solver) >= max_steps:
            return f"step budget exhausted ({self._step_count(solver)}/{max_steps})"
        if max_tokens and tokens >= max_tokens:
            return f"token budget exhausted ({tokens}/{max_tokens})"
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
        """Run all solvers in parallel. Returns the winner's result or None."""
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        ]

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self.cancel_event.set()
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self.cancel_event.set()
            return self.winner
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return None

    def kill(self) -> None:
        """Cancel all agents for this challenge."""
        self.cancel_event.set()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": "running" if spec in self.solvers and spec not in self.outcomes and not self.cancel_event.is_set()
                             else ("won" if self.winner and self.winner.flag else self.outcomes.get(spec).status if spec in self.outcomes else "finished"),
                    "stop_reason": self.outcomes.get(spec).stop_reason if spec in self.outcomes else "",
                    "workspace_path": getattr(getattr(self.solvers.get(spec), "sandbox", None), "workspace_dir", ""),
                }
                for spec in self.model_specs
            },
        }
