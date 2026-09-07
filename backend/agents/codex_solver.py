"""Codex solver — drives `codex app-server` via JSON-RPC 2.0 over stdio.

Protocol shapes verified against the Codex App Server v2 schema (August 2026):
- thread/start returns {thread: {id, ...}, ...}
- turn/start takes {threadId, input: UserInput[]}
- Dynamic tool calls arrive as item/tool/call server requests with DynamicToolCallParams
  {tool, arguments, callId, threadId, turnId}
- Client responds with DynamicToolCallResponse {contentItems: [{type, text}], success}
- Token usage via thread/tokenUsage/updated notification
- Active budget enforcement via turn/interrupt with {threadId, turnId}
- Turn completion via turn/completed notification with {threadId, turn: Turn}
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import time
from typing import Any

from backend.artifacts import (
    challenge_shared_path,
    solver_workspace_path,
    workspace_resume_manifest,
)
from backend.budgets import solver_step_limit, solver_token_limits, token_metrics
from backend.challenge_profiles import external_skill_path, solver_role
from backend.codex_cli import prepare_codex_cli
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.experience import experience_root
from backend.loop_detect import LoopDetector
from backend.model_specs import effort_from_spec
from backend.models import model_id_from_spec, supports_vision
from backend.output_types import assess_solver_output, solver_output_json_schema
from backend.prompts import (
    ChallengeMeta,
    build_prompt,
    build_writeup_prompt,
    build_writeup_review_prompt,
    list_distfiles,
)
from backend.sandbox import DockerSandbox
from backend.solver_base import (
    BUDGET_EXHAUSTED,
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    PROGRESS_CHECKPOINT,
    QUOTA_ERROR,
    SolverResult,
    solver_agent_name,
)
from backend.tools.core import (
    do_bash,
    do_list_files,
    do_read_file,
    do_view_image,
    do_web_fetch,
    do_webhook_create,
    do_webhook_get_requests,
    do_write_file,
)
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

_rpc_counter = itertools.count(1)

# Per-model reasoning effort (only for models that support it)
LEGACY_REASONING_EFFORT: dict[str, str] = {
    "gpt-5.3-codex": "xhigh",
}


def _next_id() -> int:
    return next(_rpc_counter)


# DynamicToolSpec[] for thread/start
SANDBOX_TOOLS = [
    {
        "name": "bash",
        "description": "Execute a bash command in the Docker sandbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 60},
                "max_output_chars": {
                    "type": "integer",
                    "default": 12000,
                    "description": "Bound returned output; prefer targeted commands over raising this.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the sandbox container.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write a file into the sandbox container.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    },
    {
        "name": "list_files",
        "description": "List files in a directory in the sandbox.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "default": "/challenge/distfiles"}}},
    },
    {
        "name": "submit_flag",
        "description": "Submit a flag to CTFd. Returns CORRECT, ALREADY SOLVED, or INCORRECT.",
        "inputSchema": {"type": "object", "properties": {"flag": {"type": "string"}}, "required": ["flag"]},
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL from the host network.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}, "method": {"type": "string", "default": "GET"}, "body": {"type": "string", "default": ""}}, "required": ["url"]},
    },
    {
        "name": "webhook_create",
        "description": "Create a webhook.site token for out-of-band HTTP callbacks (XSS, SSRF, bot challenges).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "webhook_get_requests",
        "description": "Retrieve HTTP requests received by a webhook.site token.",
        "inputSchema": {"type": "object", "properties": {"uuid": {"type": "string"}}, "required": ["uuid"]},
    },
    {
        "name": "view_image",
        "description": "View an image file from the sandbox for visual/steg analysis.",
        "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]},
    },
    {
        "name": "notify_coordinator",
        "description": "Send a strategic message to the coordinator (e.g. flag format discovery, shared vulnerability, request for help).",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
]

DELEGATION_TOOLS = [
    {
        "name": "delegate_task",
        "description": (
            "Launch one low-budget Luna worker for a narrow independent subproblem. "
            "The lead must continue its own critical path while the worker runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "One falsifiable technical question; never the whole challenge.",
                },
                "deliverable": {
                    "type": "string",
                    "description": "Exact evidence, script, value, or negative result needed by the lead.",
                },
            },
            "required": ["task", "deliverable"],
        },
    },
    {
        "name": "check_delegates",
        "description": "Return bounded status, findings, and handoff paths for all delegated workers.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class CodexSolver:
    """Codex solver speaking the actual app-server JSON-RPC 2.0 protocol."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        ctfd: CTFdClient,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        no_submit: bool = False,
        submit_fn=None,
        message_bus=None,
        notify_coordinator=None,
        delegate_task_fn=None,
        delegate_status_fn=None,
        task_directive: str = "",
        task_mode: str = "solve",
        verified_flag: str = "",
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.message_bus = message_bus
        self.notify_coordinator = notify_coordinator
        self.delegate_task_fn = delegate_task_fn
        self.delegate_status_fn = delegate_status_fn
        self.task_directive = task_directive.strip()[:6000]
        if task_mode not in {"solve", "writeup", "writeup_review"}:
            raise ValueError(f"unsupported Codex task mode: {task_mode}")
        self.task_mode = task_mode
        self.verified_flag = verified_flag.strip()[:2000]
        self.ctfd = ctfd
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.no_submit = no_submit
        self.submit_fn = submit_fn

        self.sandbox = DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            cpu_limit=getattr(settings, "container_cpu_limit", 2.0),
            max_exec_timeout_s=getattr(settings, "max_command_timeout_seconds", 600),
            workspace_dir=solver_workspace_path(
                settings,
                meta.name,
                f"{model_spec}/{task_mode.replace('_', '-')}"
                if task_mode != "solve"
                else model_spec,
            ),
            shared_workspace_dir=challenge_shared_path(settings, meta.name),
            experience_dir=str(experience_root(settings)),
            keep_workspace=True,
            resource_sample_interval_s=getattr(
                settings,
                "resource_sample_interval_seconds",
                2.0,
            ),
        )
        self.use_vision = supports_vision(model_spec)
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(
            meta.name,
            self.model_spec,
            log_dir=getattr(settings, "logs_root", "logs"),
        )
        accounting_spec = (
            f"{self.model_spec}/{task_mode.replace('_', '-')}"
            if task_mode != "solve"
            else self.model_spec
        )
        self.agent_name = solver_agent_name(meta.name, accounting_spec)

        self._proc: asyncio.subprocess.Process | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._step_count = 0
        self._flag: str | None = None
        self._confirmed = False
        self._findings = ""
        self._cost_usd = 0.0
        self._bump_insights: str | None = None
        self._resume_after_checkpoint = False
        self._structured_output: dict | None = None
        self._turn_error: str | None = None
        self._budget_stop_reason = ""
        self._checkpoint_stop_reason = ""
        self._resume_stop_reason = ""
        self._interrupt_requested = False
        self._interrupt_task: asyncio.Task[None] | None = None
        self._turn_active = False
        self._active_tool_calls = 0
        self._last_activity_at = time.monotonic()
        self._activity_summary = (
            "풀이 내역을 바탕으로 라이트업 구성을 준비하는 중"
            if task_mode == "writeup"
            else "생성된 라이트업의 검수 기준을 준비하는 중"
            if task_mode == "writeup_review"
            else ""
        )
        self._latest_raw_tokens = 0
        self._turn_start_raw_tokens = 0
        self._pending_responses: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._turn_done: asyncio.Event = asyncio.Event()
        self._compact_done: asyncio.Event = asyncio.Event()
        self._reasoning_effort = (
            effort_from_spec(self.model_spec)
            or LEGACY_REASONING_EFFORT.get(self.model_id)
        )

    async def start(self) -> None:
        await self.sandbox.start()

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        if self.task_mode == "writeup":
            system_prompt = build_writeup_prompt(self.meta, self.verified_flag)
        elif self.task_mode == "writeup_review":
            system_prompt = build_writeup_review_prompt(self.meta, self.verified_flag)
        else:
            system_prompt = build_prompt(
                self.meta, distfile_names, container_arch=container_arch,
                has_named_tools=True,
                model_spec=self.model_spec,
                resume_manifest=workspace_resume_manifest(
                    self.sandbox.workspace_dir,
                    self.sandbox.shared_workspace_dir,
                ),
            )
        if self.task_directive and self.task_mode == "solve":
            system_prompt += "\n\n## Live delegated assignment\n" + self.task_directive
        if self.delegate_task_fn and self.task_mode == "solve":
            system_prompt += (
                "\n\n## Adaptive delegation\n"
                "You are the SOL lead and retain end-to-end ownership. After initial "
                "triage, use `delegate_task` only when a narrow independent question can run "
                "in parallel. For multi-artifact or multi-subsystem challenges, launch up to two "
                "non-overlapping workers before deep linear analysis, ideally within the first ten "
                "tool calls. Continue the critical path yourself, use `check_delegates` at "
                "decision points, and integrate only evidence written to shared handoffs. "
                "Do not delegate broad solving, final candidate verification, or work you can "
                "finish with one cheap experiment."
            )

        codex_executable = await prepare_codex_cli(
            getattr(self.settings, "codex_cli_path", ""),
        )
        self._proc = await asyncio.create_subprocess_exec(
            codex_executable, "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize handshake: send initialize request, then initialized notification
        await self._rpc("initialize", {
            "clientInfo": {"name": "ctf-agent", "version": "2.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self._send_notification("initialized", {})

        # thread/start — system prompt is supplied through baseInstructions
        # Prepend sandbox path reminder to prevent models from using host paths
        if self.task_mode in {"writeup", "writeup_review"}:
            allowed = {"bash", "read_file", "write_file", "list_files", "view_image", "web_fetch"}
            dynamic_tools = [tool for tool in SANDBOX_TOOLS if tool["name"] in allowed]
        else:
            dynamic_tools = list(SANDBOX_TOOLS)
        if self.delegate_task_fn and self.task_mode == "solve":
            dynamic_tools.extend(DELEGATION_TOOLS)
        tool_names = [str(t["name"]) for t in dynamic_tools]
        sandbox_preamble = (
            "IMPORTANT: You are running inside a Docker sandbox. "
            "All files are under /challenge/ — distfiles at /challenge/distfiles/, "
            "workspace at /challenge/workspace/, shared handoffs at /challenge/shared/, "
            "and read-only tactical skills at /challenge/skills/. "
            "Do NOT use paths outside /challenge/. "
            f"Your tools: {', '.join(tool_names)}. Use these for ALL operations.\n\n"
        )
        thread_params = {
            "model": self.model_id,
            "baseInstructions": sandbox_preamble + system_prompt,
            "cwd": "/challenge",
            "approvalPolicy": "on-request",
            "sandbox": "read-only",
            "dynamicTools": dynamic_tools,
        }
        resp = await self._rpc("thread/start", thread_params)
        # ThreadStartResponse: result.thread.id
        self._thread_id = resp.get("result", {}).get("thread", {}).get("id", "")

        self.tracer.event(
            "start",
            challenge=self.meta.name,
            model=self.model_id,
            role=solver_role(self.model_spec).key,
            skill=external_skill_path(self.meta.category),
            reasoning_effort=self._reasoning_effort,
            token_limits=vars(solver_token_limits(self.settings, self.model_spec)),
        )
        logger.info(
            f"[{self.agent_name}] Codex solver started "
            f"(thread={self._thread_id}, effort={self._reasoning_effort or 'default'})"
        )

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self._proc and self._proc.stdin
        msg_id = _next_id()
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending_responses[msg_id] = future

        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=300)
        finally:
            self._pending_responses.pop(msg_id, None)

    async def _respond_to_request(self, request_id: int, result: Any) -> None:
        """Send a JSON-RPC response to a server request (e.g. item/tool/call)."""
        assert self._proc and self._proc.stdin
        resp = {"id": request_id, "result": result}
        self._proc.stdin.write((json.dumps(resp) + "\n").encode())
        await self._proc.stdin.drain()

    async def _send_notification(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        assert self._proc and self._proc.stdin
        msg: dict[str, Any] = {"method": method}
        if params:
            msg["params"] = params
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()

    def _request_budget_interrupt(self, reason: str, turn_id: str | None = None) -> None:
        """Schedule a turn interrupt without blocking the sole JSON-RPC reader."""
        self._request_turn_interrupt(reason, turn_id, budget=True)

    def _request_checkpoint_interrupt(self, reason: str, turn_id: str | None = None) -> None:
        """End an oversized turn so it can be compacted and resumed cheaply."""
        self._request_turn_interrupt(reason, turn_id, budget=False)

    def request_resume_interrupt(self, reason: str) -> bool:
        """Interrupt one live turn but preserve the solver for an immediate retry."""
        if not self._turn_active or self._interrupt_requested:
            return False
        clean_reason = " ".join(reason.split())[:1000]
        self._interrupt_requested = True
        self._resume_stop_reason = f"resume interrupt: {clean_reason}"
        self.tracer.event(
            "resume_interrupt_requested",
            reason=clean_reason,
            step=self._step_count,
            idle_seconds=round(self.activity_idle_seconds(), 1),
        )
        self._interrupt_task = asyncio.create_task(
            self._interrupt_turn(
                self._current_turn_id,
                clean_reason,
                event_prefix="resume",
            ),
            name=f"resume-interrupt-{self.agent_name}",
        )
        return True

    def activity_idle_seconds(self) -> float:
        """Return elapsed wall time since the last app-server or tool activity."""
        return max(0.0, time.monotonic() - self._last_activity_at)

    @property
    def activity_summary(self) -> str:
        return self._activity_summary

    @property
    def tool_call_active(self) -> bool:
        return self._active_tool_calls > 0

    def _mark_activity(self) -> None:
        self._last_activity_at = time.monotonic()

    def _request_turn_interrupt(
        self,
        reason: str,
        turn_id: str | None,
        *,
        budget: bool,
    ) -> None:
        if self._interrupt_requested:
            return
        self._interrupt_requested = True
        if budget:
            self._budget_stop_reason = reason
        else:
            self._checkpoint_stop_reason = reason
        self.tracer.event(
            "budget_interrupt_requested" if budget else "checkpoint_interrupt_requested",
            reason=reason,
            step=self._step_count,
        )
        self._interrupt_task = asyncio.create_task(
            self._interrupt_turn(turn_id or self._current_turn_id, reason),
            name=f"budget-interrupt-{self.agent_name}",
        )

    async def _interrupt_turn(
        self,
        turn_id: str | None,
        reason: str,
        event_prefix: str = "budget",
    ) -> None:
        if not self._thread_id or not turn_id:
            logger.warning(
                "[%s] Could not interrupt over-budget turn: missing turn id",
                self.agent_name,
            )
            return
        try:
            await self._rpc(
                "turn/interrupt",
                {"threadId": self._thread_id, "turnId": turn_id},
            )
            logger.warning("[%s] Turn interrupted: %s", self.agent_name, reason)
            self.tracer.event(f"{event_prefix}_interrupt_sent", reason=reason, turn_id=turn_id)
        except Exception as exc:
            logger.warning("[%s] Turn interrupt failed: %s", self.agent_name, exc)
            self.tracer.event(f"{event_prefix}_interrupt_failed", reason=reason, error=str(exc))

    async def _compact_between_turns(self) -> bool:
        """Compact after an interrupted turn and report whether it completed."""
        self._compact_done.clear()
        try:
            await self._rpc("thread/compact/start", {"threadId": self._thread_id})
            self.tracer.event("compact_requested", tokens=self._latest_raw_tokens)
            try:
                await asyncio.wait_for(self._compact_done.wait(), timeout=120)
            except TimeoutError:
                logger.warning("[%s] Compaction completion timed out", self.agent_name)
                self.tracer.event("compact_timeout", tokens=self._latest_raw_tokens)
                return False
        except Exception as exc:
            logger.warning("[%s] Compaction request failed: %s", self.agent_name, exc)
            self.tracer.event("compact_failed", error=str(exc))
            return False
        return True

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages: responses, notifications, and server requests."""
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                self._turn_done.set()
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._mark_activity()

            msg_id = msg.get("id")
            if msg_id is not None and ("result" in msg or "error" in msg):
                future = self._pending_responses.pop(msg_id, None)
                if future and not future.done():
                    if "error" in msg:
                        err = msg["error"]
                        logger.error(f"[{self.agent_name}] RPC error: {err}")
                        future.set_exception(RuntimeError(f"Codex RPC error: {err}"))
                    else:
                        future.set_result(msg)
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})

            # Server request: dynamic tool call
            if method == "item/tool/call" and msg_id is not None:
                await self._handle_tool_call(msg_id, params)

            # Notification: item completed — assistant text arrives here
            elif method == "item/completed":
                item = params.get("item", params)
                if item.get("type") == "contextCompaction":
                    # Current app-server protocol. ``thread/compacted`` below is
                    # retained for compatibility with older Codex versions.
                    self._compact_done.set()
                    self.tracer.event("compact_complete", tokens=self._latest_raw_tokens)
                elif item.get("type") == "agentMessage":
                    text = item.get("text", "")
                    phase = item.get("phase")  # "commentary" | "final_answer" | null
                    if text:
                        self._findings = text[:2000]
                        if phase == "commentary" and self.task_mode != "solve":
                            summary = text
                            if text.lstrip().startswith("{"):
                                try:
                                    progress = json.loads(text)
                                    summary = str(progress.get("method", ""))
                                except (json.JSONDecodeError, AttributeError, ValueError):
                                    summary = ""
                            summary = " ".join(summary.split())[:180]
                            if summary:
                                self._activity_summary = summary
                        if phase != "commentary" and text.lstrip()[:1] == "{":
                            try:
                                parsed = json.loads(text)
                                if isinstance(parsed, dict) and "type" in parsed:
                                    self._structured_output = parsed
                            except (json.JSONDecodeError, ValueError):
                                pass

            # Notification: turn completed — signals the turn is done
            elif method == "turn/completed":
                turn = params.get("turn", {})
                status = turn.get("status", "")
                if status == "failed":
                    error = turn.get("error", {})
                    if isinstance(error, dict):
                        # Include all error fields for robust quota classification
                        parts = [error.get("message", "unknown error")]
                        codex_info = error.get("codexErrorInfo", {})
                        if isinstance(codex_info, dict):
                            parts.append(str(codex_info))
                        additional = error.get("additionalDetails")
                        if additional:
                            parts.append(str(additional))
                        error_msg = " | ".join(parts)
                    else:
                        error_msg = str(error)
                    self._turn_error = error_msg
                    logger.error(f"[{self.agent_name}] Turn failed: {error_msg}")
                    self.tracer.event("turn_failed", error=error_msg, step=self._step_count)
                    self._findings = f"Turn failed: {error_msg}"
                    self._structured_output = None
                else:
                    self._turn_error = None
                self._turn_done.set()

            elif method == "thread/compacted":
                self._compact_done.set()
                self.tracer.event("compact_complete", tokens=self._latest_raw_tokens)

            # Notification: token usage updated
            # params: {threadId, turnId, tokenUsage: {last: TokenUsageBreakdown, total: TokenUsageBreakdown}}
            elif method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage", {})
                last = token_usage.get("last", {})
                total = token_usage.get("total", {})

                self.cost_tracker.record_tokens(
                    self.agent_name, self.model_id,
                    input_tokens=last.get("inputTokens", 0),
                    output_tokens=last.get("outputTokens", 0),
                    cache_read_tokens=last.get("cachedInputTokens", 0),
                    provider_spec="codex",
                )
                agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
                self._cost_usd = agent_usage.cost_usd if agent_usage else 0.0
                self.tracer.usage(
                    total.get("inputTokens", 0),
                    total.get("outputTokens", 0),
                    total.get("cachedInputTokens", 0),
                    self._cost_usd,
                )

                metrics = token_metrics(
                    total.get("inputTokens", 0),
                    total.get("outputTokens", 0),
                    total.get("cachedInputTokens", 0),
                    getattr(self.settings, "solver_cached_token_weight", 0.10),
                )
                self._latest_raw_tokens = metrics.raw_tokens
                limits = solver_token_limits(self.settings, self.model_spec)
                self.tracer.event(
                    "budget_usage",
                    raw_tokens=metrics.raw_tokens,
                    effective_tokens=metrics.effective_tokens,
                    cached_tokens=metrics.cached_input_tokens,
                    raw_limit=limits.raw_tokens,
                    effective_limit=limits.effective_tokens,
                )
                if (
                    limits.effective_tokens
                    and metrics.effective_tokens >= limits.effective_tokens
                ):
                    self._request_budget_interrupt(
                        "effective token budget exhausted during turn "
                        f"({metrics.effective_tokens}/{limits.effective_tokens}; "
                        f"raw={metrics.raw_tokens}, cached={metrics.cached_input_tokens})",
                        params.get("turnId"),
                    )
                if limits.raw_tokens and metrics.raw_tokens >= limits.raw_tokens:
                    self._request_budget_interrupt(
                        f"raw token safety ceiling exhausted during turn "
                        f"({metrics.raw_tokens}/{limits.raw_tokens})",
                        params.get("turnId"),
                    )
                max_cost = max(
                    0.0,
                    float(getattr(self.settings, "solver_max_estimated_cost_usd", 0.0)),
                )
                if max_cost and self._cost_usd >= max_cost:
                    self._request_budget_interrupt(
                        f"estimated cost budget exhausted during turn "
                        f"(${self._cost_usd:.2f}/${max_cost:.2f})",
                        params.get("turnId"),
                    )
                turn_raw_tokens = metrics.raw_tokens - self._turn_start_raw_tokens
                if (
                    limits.turn_slice_raw_tokens
                    and turn_raw_tokens >= limits.turn_slice_raw_tokens
                ):
                    self._request_checkpoint_interrupt(
                        f"turn slice checkpoint ({turn_raw_tokens}/"
                        f"{limits.turn_slice_raw_tokens} raw tokens)",
                        params.get("turnId"),
                    )

    async def _handle_tool_call(self, request_id: int, params: dict) -> None:
        """Handle item/tool/call server request. Params are DynamicToolCallParams."""
        tool_name = params.get("tool", "")
        try:
            args = params.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
        except Exception:
            args = {}

        self._step_count += 1
        if getattr(self, "task_mode", "solve") != "solve":
            self._activity_summary = self._documentation_tool_activity(tool_name, args)
        self.tracer.tool_call(tool_name, args, self._step_count)

        max_steps = solver_step_limit(self.settings, self.model_spec)
        if self._step_count > max_steps:
            result = f"Step budget exhausted ({self._step_count - 1}/{max_steps}); tool was not executed."
            self.tracer.tool_result(tool_name, result, self._step_count)
            await self._respond_to_request(request_id, {
                "contentItems": [{"type": "inputText", "text": result}],
                "success": False,
            })
            self._request_budget_interrupt(
                f"step budget exhausted during turn ({self._step_count - 1}/{max_steps})",
                params.get("turnId"),
            )
            return

        loop_status = self.loop_detector.check(tool_name, args)
        if loop_status == "break":
            self.tracer.event("loop_break", tool=tool_name, step=self._step_count)
            result = "Loop detected — try a completely different approach."
        else:
            self._active_tool_calls += 1
            try:
                result = await self._exec_tool(tool_name, args)
            finally:
                self._active_tool_calls = max(0, self._active_tool_calls - 1)
                self._mark_activity()
            outcome_status = self.loop_detector.record_result(tool_name, args, result)
            if loop_status == "warn" and isinstance(result, str):
                from backend.loop_detect import LOOP_WARNING_MESSAGE
                result = f"{result}\n\n{LOOP_WARNING_MESSAGE}"
            elif outcome_status == "warn" and isinstance(result, str):
                result = (
                    f"{result}\n\nTwo consecutive emulator boots produced no usable signal. "
                    "The next boot is blocked until a coordinator bump; pivot to static analysis "
                    "or fix the concrete environment fault first."
                )

        # Build content items — handle image tuples from view_image
        if isinstance(result, tuple):
            image_bytes, mime_type = result
            data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode()}"
            content_items = [{"type": "inputImage", "imageUrl": data_url}]
            self.tracer.tool_result(tool_name, f"image:{mime_type}:{len(image_bytes)}b", self._step_count)
        else:
            result_text = str(result)
            self.tracer.tool_result(tool_name, result_text[:500], self._step_count)

            if self._step_count % 5 == 0 and self.message_bus:
                from backend.tools.core import do_check_findings
                findings = await do_check_findings(self.message_bus, self.model_spec)
                if findings and "No new findings" not in findings:
                    result_text = f"{result_text}\n\n---\n{findings}"

            content_items = [{"type": "inputText", "text": result_text}]

        await self._respond_to_request(request_id, {
            "contentItems": content_items,
            "success": True,
        })
        if self._step_count >= max_steps:
            self._request_budget_interrupt(
                f"step budget exhausted during turn ({self._step_count}/{max_steps})",
                params.get("turnId"),
            )

    def _documentation_tool_activity(self, tool_name: str, args: dict) -> str:
        """Map low-level tool calls to a compact operator-facing status."""
        reviewing = self.task_mode == "writeup_review"
        prefix = "검수: " if reviewing else "작성: "
        raw_path = str(args.get("path", args.get("filename", ""))).casefold()
        command = str(args.get("command", "")).casefold()
        if tool_name == "view_image":
            detail = "취약점·해결 스크린샷을 확인하는 중"
        elif tool_name == "write_file" and "review.md" in raw_path:
            detail = "검수 결과와 승인 여부를 기록하는 중"
        elif tool_name == "write_file" and "writeup.md" in raw_path:
            detail = "최종 Markdown을 보강해 저장하는 중"
        elif tool_name in {"read_file", "list_files"}:
            detail = "풀이 기록과 증거 파일을 대조하는 중" if reviewing else "풀이 기록과 증거를 읽는 중"
        elif tool_name == "bash" and ("writeup.md" in command or "review.md" in command):
            detail = "문서 구조와 증거 링크를 검증하는 중" if reviewing else "Markdown과 증거 링크를 작성하는 중"
        elif tool_name == "bash":
            detail = "재현 명령과 핵심 로직을 확인하는 중"
        else:
            detail = "문서 근거를 검토하는 중"
        return prefix + detail

    async def _exec_tool(self, name: str, args: dict) -> str | tuple[bytes, str]:
        if name == "bash":
            try:
                timeout = int(args.get("timeout_seconds", 60) or 60)
            except (TypeError, ValueError):
                timeout = 60
            try:
                max_output_chars = int(args.get("max_output_chars", 12_000) or 12_000)
            except (TypeError, ValueError):
                max_output_chars = 12_000
            return await do_bash(
                self.sandbox,
                args.get("command", ""),
                timeout,
                max_output_chars,
            )
        elif name == "read_file":
            return str(await do_read_file(self.sandbox, args.get("path", "")))
        elif name == "write_file":
            return await do_write_file(self.sandbox, args.get("path", ""), args.get("content", ""))
        elif name == "list_files":
            return await do_list_files(self.sandbox, args.get("path", "/challenge/distfiles"))
        elif name == "submit_flag":
            flag = args.get("flag", "")
            if self.no_submit:
                return f'DRY RUN — would submit "{flag}"'
            if self.submit_fn:
                display, is_confirmed = await self.submit_fn(flag)
            else:
                from backend.tools.core import do_submit_flag
                display, is_confirmed = await do_submit_flag(self.ctfd, self.meta.name, flag)
            if is_confirmed:
                self._confirmed = True
                self._flag = flag
            return display
        elif name == "web_fetch":
            return await do_web_fetch(args.get("url", ""), args.get("method", "GET"), args.get("body", ""))
        elif name == "webhook_create":
            return await do_webhook_create()
        elif name == "webhook_get_requests":
            return await do_webhook_get_requests(args.get("uuid", ""))
        elif name == "view_image":
            return await do_view_image(self.sandbox, args.get("filename", ""), use_vision=self.use_vision)
        elif name == "notify_coordinator":
            if self.notify_coordinator:
                await self.notify_coordinator(args.get("message", ""))
                return "Message sent to coordinator."
            return "No coordinator connected."
        elif name == "delegate_task":
            if self.delegate_task_fn:
                return await self.delegate_task_fn(
                    str(args.get("task", "")),
                    str(args.get("deliverable", "")),
                )
            return "Dynamic delegation is not available to this agent."
        elif name == "check_delegates":
            if self.delegate_status_fn:
                return await self.delegate_status_fn()
            return "No delegated workers are attached to this agent."
        return f"Unknown tool: {name}"

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._proc:
            await self.start()
        assert self._thread_id

        t0 = time.monotonic()
        if self._bump_insights:
            prompt_text = (
                "Your previous attempt did not find the flag. "
                f"Insights from other agents:\n\n{self._bump_insights}\n\n"
                "Try a different approach."
            )
            self._bump_insights = None
        elif self._resume_after_checkpoint:
            prompt_text = (
                "Resume the same productive solve path from the compacted context and saved "
                "workspace artifacts. Read the latest checkpoint and lead STATE/progress file first, "
                "state the single unresolved blocker, and run its cheapest discriminating experiment. "
                "Do not restart triage, bulk-extract again, or repeat completed experiments."
            )
            self._resume_after_checkpoint = False
        elif self.task_mode == "writeup":
            prompt_text = (
                "Generate the final Korean writeup now. Read the preserved solver evidence, write "
                "/challenge/shared/writeup/WRITEUP.md, and include real decisive screenshots when "
                "available or reproducible. Do not re-solve or submit the challenge."
            )
        elif self.task_mode == "writeup_review":
            prompt_text = (
                "Review the generated Korean writeup now. Correct unsupported or incomplete content, verify the "
                "two decisive screenshot roles and embedded solver code, then write REVIEW.md with the final verdict."
            )
        elif self._step_count == 0:
            prompt_text = "Solve this CTF challenge."
        else:
            prompt_text = (
                "Continue from the evidence ledger. Record why the previous route failed or remains "
                "unproven, select one unresolved blocker, and run a different falsifiable experiment. "
                "Do not repeat inventory or broad extraction."
            )

        try:
            self._turn_done.clear()
            self._structured_output = None
            self._turn_error = None
            self._budget_stop_reason = ""
            self._checkpoint_stop_reason = ""
            self._resume_stop_reason = ""
            self._interrupt_requested = False
            self._interrupt_task = None
            self._current_turn_id = None
            self._turn_start_raw_tokens = self._latest_raw_tokens
            turn_params: dict[str, Any] = {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt_text}],
                "outputSchema": solver_output_json_schema(),
            }
            # Current App Server exposes reasoning as `effort` on turn/start.
            if self._reasoning_effort:
                turn_params["effort"] = self._reasoning_effort
            self._mark_activity()
            turn_response = await self._rpc("turn/start", turn_params)
            self._current_turn_id = (
                turn_response.get("result", {}).get("turn", {}).get("id")
            )
            self._turn_active = True
            try:
                await self._turn_done.wait()
            finally:
                self._turn_active = False

            duration = time.monotonic() - t0
            self.tracer.event("turn_complete", duration=round(duration, 1), steps=self._step_count)

            if self._resume_stop_reason:
                self._findings = self._findings or self._resume_stop_reason
                return self._result(PROGRESS_CHECKPOINT, self._resume_stop_reason)

            if self._budget_stop_reason:
                self._findings = self._findings or self._budget_stop_reason
                return self._result(BUDGET_EXHAUSTED, self._budget_stop_reason)

            if self._checkpoint_stop_reason:
                compacted = await self._compact_between_turns()
                if not compacted:
                    reason = (
                        f"{self._checkpoint_stop_reason}; compaction did not complete, "
                        "so starting another turn is unsafe"
                    )
                    self._resume_after_checkpoint = False
                    self._findings = self._findings or reason
                    return self._result(BUDGET_EXHAUSTED, reason)
                self._resume_after_checkpoint = True
                self._findings = self._findings or self._checkpoint_stop_reason
                return self._result(PROGRESS_CHECKPOINT, self._checkpoint_stop_reason)

            if self._turn_error:
                err = self._turn_error.lower()
                # Context overflow is terminal — don't fallback, just error
                if "context_length" in err or "context window" in err:
                    return self._result(ERROR)
                if any(k in err for k in ("quota", "rate", "capacity", "usage")):
                    return self._result(QUOTA_ERROR)
                return self._result(ERROR)

            output = self._structured_output or {
                "type": "incomplete",
                "flag": "",
                "method": self._findings or "No structured progress was returned.",
            }
            assessment = assess_solver_output(
                output_type=str(output.get("type", "incomplete")),
                flag=output.get("flag"),
                method=output.get("method"),
                confirmed_flag=self._flag if self._confirmed else None,
                flag_format=self.meta.flag_format,
            )
            self._confirmed = assessment.status == FLAG_FOUND
            self._flag = assessment.flag
            self._findings = assessment.findings
            return self._result(assessment.status)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
        except Exception as e:
            error_str = str(e)
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=error_str)
            if "quota" in error_str.lower() or "rate" in error_str.lower():
                return self._result(QUOTA_ERROR)
            return self._result(ERROR)

    def bump(self, insights: str) -> None:
        clean = " ".join(insights.split())[:6000]
        if self._bump_insights:
            clean = f"{self._bump_insights}\n\n{clean}"[-6000:]
        self._bump_insights = clean
        self._resume_after_checkpoint = False
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        if self._turn_active:
            self.request_resume_interrupt("new coordinator or delegate guidance arrived")

    def _result(self, status: str, stop_reason: str = "") -> SolverResult:
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed)
        return SolverResult(
            flag=self._flag, status=status,
            findings_summary=self._findings[:2000],
            step_count=self._step_count,
            cost_usd=self._cost_usd, log_path=self.tracer.path,
            stop_reason=stop_reason,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count)
        self.tracer.close()
        if self._interrupt_task and not self._interrupt_task.done():
            self._interrupt_task.cancel()
            await asyncio.gather(self._interrupt_task, return_exceptions=True)
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self.sandbox:
            await self.sandbox.stop()
