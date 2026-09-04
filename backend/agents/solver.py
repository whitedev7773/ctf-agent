"""Per-model solver agent — one model, one container, one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from backend.artifacts import (
    challenge_shared_path,
    solver_workspace_path,
    workspace_resume_manifest,
)
from backend.budgets import solver_token_limits, token_metrics
from backend.challenge_profiles import external_skill_path, solver_role
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import SolverDeps
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
    supports_vision,
)
from backend.output_types import SolverTurnOutput, assess_solver_output
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    BUDGET_EXHAUSTED,
    CANCELLED,
    CORRECT_MARKERS,
    ERROR,
    FLAG_FOUND,
    SolverResult,
    solver_agent_name,
)
from backend.tools.flag import submit_flag
from backend.tools.sandbox import (
    bash,
    check_findings,
    list_files,
    notify_coordinator,
    read_file,
    web_fetch,
    webhook_create,
    webhook_get_requests,
    write_file,
)
from backend.tools.vision import view_image
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)


@dataclass
class TracingToolset(WrapperToolset[SolverDeps]):
    """Wraps a toolset to add per-call tracing and loop detection."""

    tracer: SolverTracer = field(repr=False)
    loop_detector: LoopDetector = field(repr=False)
    step_counter: list[int] = field(repr=False)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[SolverDeps], tool: ToolsetTool[SolverDeps]
    ) -> Any:
        self.step_counter[0] += 1
        step = self.step_counter[0]

        self.tracer.tool_call(name, tool_args, step)

        # Loop detection
        loop_status = self.loop_detector.check(name, tool_args)
        if loop_status == "break":
            logger.warning(f"Loop break on {name} at step {step}")
            self.tracer.event("loop_break", tool=name, step=step)
            # Inject loop warning by returning it as the tool result
            return LOOP_WARNING_MESSAGE

        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)

        result_str = str(result) if result is not None else ""
        outcome_status = self.loop_detector.record_result(name, tool_args, result_str)
        self.tracer.tool_result(name, result_str, step)

        # Inject loop warning alongside result on "warn" level
        if loop_status == "warn":
            result = f"{result}\n\n{LOOP_WARNING_MESSAGE}" if isinstance(result, str) else result
        elif outcome_status == "warn" and isinstance(result, str):
            result = (
                f"{result}\n\nTwo consecutive emulator boots produced no usable signal. "
                "The next boot is blocked until new coordinator guidance."
            )

        # Check for confirmed flag
        if name == "submit_flag" and any(m in result_str for m in CORRECT_MARKERS):
            self.tracer.event("flag_confirmed", tool=name, step=step)

        if step % 5 == 0 and ctx.deps.message_bus and isinstance(result, str):
            from backend.tools.core import do_check_findings
            findings_text = await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)
            if findings_text and "No new findings" not in findings_text:
                result = f"{result}\n\n---\n{findings_text}"
                self.tracer.event("findings_injected", step=step)

        return result


def _build_toolset(deps: SolverDeps) -> FunctionToolset[SolverDeps]:
    """Build the raw toolset for a solver agent."""
    tools = [bash, read_file, write_file, list_files, submit_flag, web_fetch,
             webhook_create, webhook_get_requests, check_findings, notify_coordinator]
    if deps.use_vision:
        tools.append(view_image)
    return FunctionToolset(tools=tools, max_retries=4)


class Solver:
    """A single solver: one model, one container, one challenge."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)

        workspace_dir = solver_workspace_path(settings, meta.name, model_spec)
        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            cpu_limit=getattr(settings, "container_cpu_limit", 2.0),
            max_exec_timeout_s=getattr(settings, "max_command_timeout_seconds", 600),
            workspace_dir=workspace_dir,
            shared_workspace_dir=challenge_shared_path(settings, meta.name),
            keep_workspace=True,
        )
        self.use_vision = supports_vision(model_spec)
        self.deps = SolverDeps(
            sandbox=self.sandbox,
            ctfd=ctfd,
            challenge_dir=challenge_dir,
            challenge_name=meta.name,
            workspace_dir="",
            use_vision=self.use_vision,
            cost_tracker=cost_tracker,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(
            meta.name,
            self.model_spec,
            log_dir=getattr(settings, "logs_root", "logs"),
        )
        self.agent_name = solver_agent_name(meta.name, self.model_spec)
        self._agent: Agent[SolverDeps, SolverTurnOutput] | None = None
        self._messages: list = []
        self._step_count = [0]  # mutable ref shared with TracingToolset
        self._flag: str | None = None
        self._confirmed: bool = False
        self._findings: str = ""

    async def start(self) -> None:
        """Start the sandbox and build the agent."""
        if not self.sandbox._container:
            await self.sandbox.start()
        self.deps.workspace_dir = self.sandbox.workspace_dir

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
            model_spec=self.model_spec,
            resume_manifest=workspace_resume_manifest(
                self.sandbox.workspace_dir,
                self.sandbox.shared_workspace_dir,
            ),
        )

        model = resolve_model(self.model_spec, self.settings)
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(self.deps)
        toolset = TracingToolset(
            wrapped=raw_toolset,
            tracer=self.tracer,
            loop_detector=self.loop_detector,
            step_counter=self._step_count,
        )

        self._agent = Agent(
            model,
            deps_type=SolverDeps,
            system_prompt=system_prompt,
            model_settings=model_settings,
            toolsets=[toolset],
            output_type=SolverTurnOutput,
        )

        self.tracer.event(
            "start",
            challenge=self.meta.name,
            model=self.model_id,
            role=solver_role(self.model_spec).key,
            skill=external_skill_path(self.meta.category),
        )
        logger.info(f"[{self.agent_name}] Solver started")

    async def run_until_done_or_gave_up(self) -> SolverResult:
        """Run the solver loop until flag found, gave up, or cancelled."""
        if not self._agent:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        try:
            from pydantic_ai.usage import UsageLimits
            prior_usage = self.cost_tracker.by_agent.get(self.agent_name)
            metrics = token_metrics(
                prior_usage.usage.input_tokens if prior_usage else 0,
                prior_usage.usage.output_tokens if prior_usage else 0,
                prior_usage.usage.cache_read_tokens if prior_usage else 0,
                getattr(self.settings, "solver_cached_token_weight", 0.10),
            )
            limits = solver_token_limits(self.settings, self.model_spec)
            remaining_tokens = (
                limits.raw_tokens - metrics.raw_tokens if limits.raw_tokens else None
            )
            max_steps = max(1, int(getattr(self.settings, "solver_max_steps", 240)))
            remaining_steps = max_steps - self._step_count[0]
            if (
                limits.effective_tokens
                and metrics.effective_tokens >= limits.effective_tokens
            ):
                reason = (
                    f"effective token budget exhausted before turn "
                    f"({metrics.effective_tokens}/{limits.effective_tokens})"
                )
                self._findings = reason
                return self._result(BUDGET_EXHAUSTED, stop_reason=reason)
            if remaining_tokens is not None and remaining_tokens <= 0:
                reason = (
                    f"raw token safety ceiling exhausted before turn "
                    f"({metrics.raw_tokens}/{limits.raw_tokens})"
                )
                self._findings = reason
                return self._result(BUDGET_EXHAUSTED, stop_reason=reason)
            if remaining_steps <= 0:
                reason = f"step budget exhausted before turn ({self._step_count[0]}/{max_steps})"
                self._findings = reason
                return self._result(BUDGET_EXHAUSTED, stop_reason=reason)
            result = await self._agent.run(
                "Solve this CTF challenge." if not self._messages else "Continue solving.",
                deps=self.deps,
                message_history=self._messages if self._messages else None,
                usage_limits=UsageLimits(
                    request_limit=None,
                    tool_calls_limit=remaining_steps,
                    total_tokens_limit=remaining_tokens,
                ),
            )

            duration = time.monotonic() - t0
            usage = result.usage

            self.cost_tracker.record(
                self.agent_name, usage, self.model_id,
                provider_spec=provider_from_spec(self.model_spec),
                duration_seconds=duration,
            )

            agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
            self.tracer.usage(
                usage.input_tokens, usage.output_tokens,
                usage.cache_read_tokens,
                agent_usage.cost_usd if agent_usage else 0.0,
            )

            self._messages = result.all_messages()

            # Trace model responses from new messages
            from pydantic_ai.messages import ModelResponse, TextPart
            for msg in result.new_messages():
                if isinstance(msg, ModelResponse):
                    text_parts = [p.content for p in msg.parts if isinstance(p, TextPart)]
                    text = " ".join(text_parts)
                    msg_usage = msg.usage
                    self.tracer.model_response(
                        text[:500], self._step_count[0],
                        input_tokens=msg_usage.input_tokens if msg_usage else 0,
                        output_tokens=msg_usage.output_tokens if msg_usage else 0,
                    )

            output = result.output
            assessment = assess_solver_output(
                output_type=output.type,
                flag=output.flag,
                method=output.method,
                confirmed_flag=self.deps.confirmed_flag,
                flag_format=self.meta.flag_format,
            )
            self._confirmed = assessment.status == FLAG_FOUND
            self._flag = assessment.flag
            self._findings = assessment.findings
            return self._result(assessment.status)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except UsageLimitExceeded as exc:
            reason = f"provider usage budget exhausted during turn: {exc}"
            self._findings = reason
            self.tracer.event("budget_exhausted", reason=reason)
            return self._result(BUDGET_EXHAUSTED, stop_reason=reason)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            return self._result(ERROR)

    def bump(self, insights: str) -> None:
        """Inject insights from siblings and prepare to resume."""
        bump_msg = ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "Your previous attempt did not find the flag. Here are insights "
                        "from other agents working on the same challenge:\n\n"
                        f"{insights}\n\n"
                        "Use these insights to try a different approach. "
                        "Do NOT repeat what has already been tried."
                    )
                )
            ]
        )
        self._messages.append(bump_msg)
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with sibling insights")

    def _result(
        self,
        status: str,
        run_steps: int | None = None,
        run_cost: float | None = None,
        stop_reason: str = "",
    ) -> SolverResult:
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed, cost_usd=round(cost, 4))
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count[0],
            cost_usd=run_cost if run_cost is not None else cost,
            log_path=self.tracer.path,
            stop_reason=stop_reason,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()
