"""Dashboard and control API for a running coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import secrets
import shutil
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import web

from backend.artifacts import challenge_approach_notes, challenge_workspace_path
from backend.budgets import (
    solver_token_limits,
    solver_turn_idle_timeout_limit,
    token_metrics,
)
from backend.challenge_profiles import external_skill_path, solver_role
from backend.codex_usage import CodexUsageMonitor
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.experience import experience_root, experience_summary
from backend.model_specs import provider_from_spec
from backend.prompts import ChallengeMeta
from backend.runtime_clock import RuntimeClock
from backend.runtime_settings import (
    RuntimeSettings,
    apply_runtime_settings,
    reset_runtime_settings,
    runtime_settings_from,
    save_runtime_settings,
)
from backend.runtime_state import persist_deps_state
from backend.solver_base import solver_agent_name
from backend.tracing import challenge_trace_paths
from backend.writeups import (
    begin_writeup_generation,
    fail_writeup_generation,
    finalize_writeup,
    interrupted_writeup_status,
    read_writeup,
    seed_writeup_from_solver_evidence,
    writeup_review_verdict,
    writeup_status,
)

if TYPE_CHECKING:
    from backend.deps import CoordinatorDeps
    from backend.poller import CTFdPoller


STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)
RESET_CONFIRMATION = "초기화"
RESET_EXPERIENCE_CONFIRMATION = "경험 초기화"
RUNTIME_REVISION = 20


def _runtime_source_fingerprint(project_root: Path | None = None) -> str:
    """Fingerprint restart-sensitive source/config without exposing file contents."""
    root = project_root or Path(__file__).resolve().parents[2]
    paths = list((root / "backend").rglob("*.py"))
    paths.extend(path for path in (root / ".env", root / "pyproject.toml") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        try:
            stat = path.stat()
            relative = path.relative_to(root)
        except (OSError, ValueError):
            continue
        digest.update(f"{relative.as_posix()}:{stat.st_size}:{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def _is_link_like(path: Path) -> bool:
    """Return whether a path redirects deletion outside its configured root."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _validate_runtime_root(root: Path, project_root: Path) -> None:
    """Reject broad or source-bearing paths before any destructive operation."""
    anchor = Path(root.anchor).resolve()
    home = Path.home().resolve()
    if root in {anchor, home, project_root} or project_root.is_relative_to(root):
        raise ValueError(f"unsafe runtime path: {root}")
    if root.exists() and _is_link_like(root):
        raise ValueError(f"runtime path cannot be a link or junction: {root}")

    protected = (
        project_root / ".git",
        project_root / ".env",
        project_root / ".venv",
        project_root / "backend",
        project_root / "docs",
        project_root / "tests",
        project_root / "pyproject.toml",
    )
    if any(root == path or root.is_relative_to(path) or path.is_relative_to(root) for path in protected):
        raise ValueError(f"runtime path overlaps protected project files: {root}")


def _remove_runtime_child(child: Path) -> None:
    """Remove one runtime entry without following links."""
    if _is_link_like(child):
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            child.rmdir()
    elif child.is_dir():
        shutil.rmtree(child)
    else:
        child.unlink()


def _clear_runtime_root(root: Path) -> tuple[int, list[Path]]:
    """Remove runtime children while retaining files locked by this process.

    Windows does not allow deleting the stdout/stderr files currently opened by
    the coordinator. Report those files and continue clearing sibling entries.
    """
    root.mkdir(parents=True, exist_ok=True)
    removed = 0
    retained: list[Path] = []
    for child in root.iterdir():
        try:
            _remove_runtime_child(child)
        except FileNotFoundError:
            continue
        except PermissionError:
            retained.append(child)
            continue
        removed += 1
    return removed, retained


def _drain_queue(queue: asyncio.Queue | None) -> None:
    if queue is None:
        return
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


@contextmanager
def _staging_directory(root: Path):
    """Create an upload directory that inherits the challenge root's Windows ACL.

    ``tempfile.TemporaryDirectory`` uses mode 0o700. Modern Python translates
    that mode into a protected Windows ACL, which can lock a sandboxed child
    process out after the directory is renamed.
    """
    while True:
        path = root / f".upload-{secrets.token_hex(8)}"
        try:
            path.mkdir()
            break
        except FileExistsError:
            continue
    try:
        yield path
    finally:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _usage_payload(
    cost_tracker: CostTracker,
    agent_name: str,
    settings: object,
    model_spec: str,
) -> dict[str, Any]:
    agent = cost_tracker.by_agent.get(agent_name)
    if not agent:
        limits = solver_token_limits(settings, model_spec)
        return {
            "cost_usd": 0.0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "effective_tokens": 0,
            "effective_token_limit": limits.effective_tokens,
            "raw_token_limit": limits.raw_tokens,
            "duration_seconds": 0.0,
        }
    metrics = token_metrics(
        agent.usage.input_tokens,
        agent.usage.output_tokens,
        agent.usage.cache_read_tokens,
        getattr(settings, "solver_cached_token_weight", 0.10),
    )
    limits = solver_token_limits(settings, model_spec)
    return {
        "cost_usd": round(agent.cost_usd, 6),
        "input_tokens": agent.usage.input_tokens,
        "cached_tokens": agent.usage.cache_read_tokens,
        "output_tokens": agent.usage.output_tokens,
        "effective_tokens": metrics.effective_tokens,
        "effective_token_limit": limits.effective_tokens,
        "raw_token_limit": limits.raw_tokens,
        "duration_seconds": round(agent.duration_seconds, 1),
    }


def _resource_totals(resources: list[dict[str, Any]]) -> dict[str, Any]:
    available = [resource for resource in resources if resource.get("available")]
    return {
        "container_count": len(resources),
        "available_count": len(available),
        "stale_count": sum(bool(resource.get("stale")) for resource in resources),
        "cpu_percent": round(
            sum(float(resource.get("cpu_percent", 0) or 0) for resource in available),
            2,
        ),
        "cpu_limit": round(
            sum(float(resource.get("cpu_limit", 0) or 0) for resource in resources),
            2,
        ),
        "memory_bytes": sum(
            int(resource.get("memory_bytes", 0) or 0) for resource in available
        ),
        "memory_limit_bytes": sum(
            int(resource.get("memory_limit_bytes", 0) or 0) for resource in resources
        ),
        "pids": sum(int(resource.get("pids", 0) or 0) for resource in available),
        "network_rx_bytes": sum(
            int(resource.get("network_rx_bytes", 0) or 0) for resource in available
        ),
        "network_tx_bytes": sum(
            int(resource.get("network_tx_bytes", 0) or 0) for resource in available
        ),
        "block_read_bytes": sum(
            int(resource.get("block_read_bytes", 0) or 0) for resource in available
        ),
        "block_write_bytes": sum(
            int(resource.get("block_write_bytes", 0) or 0) for resource in available
        ),
    }


class DashboardServer:
    """Serve dashboard assets and same-process coordinator controls."""

    def __init__(
        self,
        deps: CoordinatorDeps,
        poller: CTFdPoller,
        cost_tracker: CostTracker,
        port: int = 9400,
        host: str = "127.0.0.1",
    ) -> None:
        self.deps = deps
        self.poller = poller
        self.cost_tracker = cost_tracker
        self.port = port
        self.host = host
        self.actual_port = port
        self.started_at = time.monotonic()
        self._startup_source_fingerprint = _runtime_source_fingerprint()
        self.csrf_token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._command_lock = asyncio.Lock()
        self._writeup_lock = asyncio.Lock()
        self._writeup_tasks: dict[str, asyncio.Task[None]] = {}
        self._writeup_solvers: dict[str, Any] = {}
        self._writeup_clocks: dict[str, RuntimeClock] = {}
        self._writeup_phases: dict[str, str] = {}
        self.codex_usage_monitor = CodexUsageMonitor(
            getattr(self.deps.settings, "codex_cli_path", ""),
        )
        self.deps.request_writeup_generation = self.start_writeup_generation

    async def start(self) -> None:
        app = web.Application(client_max_size=256 * 1024 * 1024, middlewares=[self._headers])
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/assets/dashboard.css", self._css),
                web.get("/assets/dashboard.js", self._js),
                web.get("/api/health", self._health),
                web.get("/api/session", self._session),
                web.get("/api/status", self._status),
                web.get("/api/codex/usage", self._codex_usage),
                web.get("/api/resources", self._resources),
                web.get("/api/writeup", self._writeup),
                web.get("/api/writeup/archive", self._writeup_archive),
                web.get("/api/artifact", self._artifact),
                web.get("/api/trace", self._trace),
                web.get("/api/settings/runtime", self._runtime_settings),
                web.post("/api/settings/ctfd", self._configure_ctfd),
                web.post("/api/settings/runtime", self._configure_runtime),
                web.post("/api/settings/runtime/reset", self._reset_runtime_settings),
                web.post("/api/challenges/local", self._create_local_challenge),
                web.post("/api/challenges/delete", self._delete_challenge),
                web.post("/api/operator/message", self._operator_message),
                web.post("/api/control/spawn", self._spawn),
                web.post("/api/control/stop", self._stop_swarm),
                web.post("/api/control/broadcast", self._broadcast),
                web.post("/api/control/submit", self._submit),
                web.post("/api/control/review-candidate", self._review_candidate),
                web.post("/api/control/request-writeup", self._request_writeup),
                web.post("/api/control/reset-runtime", self._reset_runtime),
                web.post("/api/control/reset-experience", self._reset_experience),
                # Backward-compatible endpoint used by the ctf-msg command.
                web.post("/msg", self._legacy_message),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if sockets:
            self.actual_port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        tasks = list(self._writeup_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.codex_usage_monitor.stop()
        if self._runner:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    @web.middleware
    async def _headers(self, request: web.Request, handler):
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _css(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "dashboard.css")

    async def _js(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "dashboard.js")

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _codex_usage(self, request: web.Request) -> web.Response:
        force = request.query.get("refresh") == "1"
        return web.json_response(await self.codex_usage_monitor.snapshot(force=force))

    async def _session(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "csrf_token": self.csrf_token,
                "port": self.actual_port,
                "no_submit": self.deps.no_submit,
                "capabilities": {
                    "reset_runtime": True,
                    "reset_experience": True,
                    "request_writeup": True,
                    "delete_challenge": True,
                    "runtime_settings": True,
                    "codex_usage": True,
                },
            }
        )

    def _require_csrf(self, request: web.Request) -> None:
        token = request.headers.get("X-CTF-Dashboard-Token", "")
        if not secrets.compare_digest(token, self.csrf_token):
            raise web.HTTPForbidden(text="invalid dashboard token")

    async def _json_body(self, request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="JSON body required") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return data

    def _snapshot(self) -> dict[str, Any]:
        candidates = getattr(self.deps, "candidates", {})
        dismissed = getattr(self.deps, "dismissed_challenges", set())
        known = (
            self.poller.known_challenges
            | set(self.deps.challenge_metas)
            | set(self.deps.swarms)
            | set(self.deps.results)
            | set(candidates)
        ) - dismissed
        solved = (self.poller.known_solved | set(self.deps.results)) & known
        active_names = {
            name
            for name, task in self.deps.swarm_tasks.items()
            if not task.done()
            and (swarm := self.deps.swarms.get(name)) is not None
            and not swarm.cancel_event.is_set()
        }

        challenges: list[dict[str, Any]] = []
        total_agents = 0
        active_agents = 0
        live_resources: list[dict[str, Any]] = []
        documented_count = 0

        for name in sorted(known, key=str.casefold):
            meta = self.deps.challenge_metas.get(name)
            swarm = self.deps.swarms.get(name)
            result = self.deps.results.get(name, {})
            candidate = candidates.get(name, {})
            is_active = name in active_names and swarm is not None and not swarm.cancel_event.is_set()
            is_solved = name in solved

            agents: list[dict[str, Any]] = []
            if swarm:
                for spec in swarm.model_specs:
                    solver = swarm.solvers.get(spec)
                    outcome = swarm.outcomes.get(spec)
                    waiting = spec in swarm.waiting_models and is_active
                    running = solver is not None and is_active and outcome is None
                    budget_stop_pending = (
                        getattr(solver, "_budget_stop_reason", "") if solver else ""
                    )
                    checkpoint_pending = (
                        getattr(solver, "_checkpoint_stop_reason", "") if solver else ""
                    )
                    resume_pending = (
                        getattr(solver, "_resume_stop_reason", "") if solver else ""
                    )
                    idle_reader = getattr(solver, "activity_idle_seconds", None)
                    idle_seconds = round(float(idle_reader()), 1) if callable(idle_reader) else 0.0
                    raw_steps = getattr(solver, "_step_count", 0) if solver else 0
                    if isinstance(raw_steps, list):
                        raw_steps = raw_steps[0] if raw_steps else 0
                    swarm_settings = getattr(swarm, "settings", self.deps.settings)
                    usage = _usage_payload(
                        self.cost_tracker,
                        solver_agent_name(name, spec),
                        swarm_settings,
                        spec,
                    )
                    clock = getattr(swarm, "agent_clocks", {}).get(spec)
                    if clock is not None:
                        usage["duration_seconds"] = round(clock.elapsed_seconds, 1)
                    sandbox = getattr(solver, "sandbox", None) if solver else None
                    resource_reader = getattr(sandbox, "resource_snapshot", None)
                    resource = resource_reader() if callable(resource_reader) else {}
                    agents.append(
                        {
                            "model_spec": spec,
                            "role": solver_role(spec).key,
                            "role_title": solver_role(spec).title,
                            "skill_path": external_skill_path(getattr(meta, "category", "")),
                            "status": "waiting" if waiting else "compacting" if (
                                running and checkpoint_pending
                            ) else "stopping" if (
                                running and budget_stop_pending
                            ) else "redirecting" if (
                                running and resume_pending
                            ) else "running" if running else (
                                "won" if outcome is not None and outcome is swarm.winner else (
                                    outcome.status if outcome else "finished"
                                )
                            ),
                            "steps": raw_steps,
                            "findings": swarm.findings.get(spec, ""),
                            "trace": Path(getattr(getattr(solver, "tracer", None), "path", "")).name,
                            "stop_reason": (
                                outcome.stop_reason if outcome else (
                                    budget_stop_pending or checkpoint_pending or resume_pending
                                )
                            ),
                            "attempt": outcome.attempt if outcome else 0,
                            "idle_seconds": idle_seconds if running else 0.0,
                            "idle_limit_seconds": solver_turn_idle_timeout_limit(
                                swarm_settings,
                                spec,
                            ),
                            "tool_call_active": bool(
                                getattr(solver, "tool_call_active", False)
                            ),
                            "workspace_path": (
                                outcome.workspace_path if outcome and outcome.workspace_path
                                else getattr(getattr(solver, "sandbox", None), "workspace_dir", "")
                            ),
                            "resource": resource,
                            **usage,
                        }
                    )
                    total_agents += 1
                    active_agents += int(running or waiting)

            writeup_solver = self._writeup_solvers.get(name)
            writeup_task = self._writeup_tasks.get(name)
            if writeup_solver is not None and writeup_task is not None and not writeup_task.done():
                phase = self._writeup_phases.get(name, "writing")
                role = (
                    "writeup_review"
                    if phase == "reviewing"
                    else "writeup_revision"
                    if phase == "revising"
                    else "writeup"
                )
                spec = f"{writeup_solver.model_spec}/{role.replace('_', '-')}"
                resource_reader = getattr(writeup_solver.sandbox, "resource_snapshot", None)
                resource = resource_reader() if callable(resource_reader) else {}
                agents.append(
                    {
                        "model_spec": spec,
                        "role": role,
                        "role_title": (
                            "Luna writeup reviewer"
                            if phase == "reviewing"
                            else "Terra writeup reviser"
                            if phase == "revising"
                            else "Terra writeup writer"
                        ),
                        "skill_path": "",
                        "status": "generating",
                        "steps": getattr(writeup_solver, "_step_count", 0),
                        "findings": getattr(
                            writeup_solver,
                            "activity_summary",
                            "생성된 라이트업을 검수하는 중"
                            if phase == "reviewing"
                            else "검수 반려 항목을 수정하는 중"
                            if phase == "revising"
                            else "풀이 내역으로 라이트업을 작성하는 중",
                        ),
                        "trace": Path(
                            getattr(getattr(writeup_solver, "tracer", None), "path", "")
                        ).name,
                        "stop_reason": "",
                        "attempt": 1,
                        "workspace_path": getattr(writeup_solver.sandbox, "workspace_dir", ""),
                        "resource": resource,
                        **_usage_payload(
                            self.cost_tracker,
                            writeup_solver.agent_name,
                            self.deps.settings,
                            writeup_solver.model_spec,
                        ),
                        "duration_seconds": round(
                            self._writeup_clocks[name].elapsed_seconds, 1
                        ) if name in self._writeup_clocks else 0.0,
                    }
                )
                total_agents += 1
                active_agents += 1

            agent_resources = [agent["resource"] for agent in agents if agent.get("resource")]
            challenge_resources = _resource_totals(agent_resources)
            live_resources.extend(
                resource
                for resource in agent_resources
                if resource.get("status") != "stopped"
            )

            status = (
                "solved" if is_solved
                else "active" if is_active
                else "candidate" if candidate
                else "idle"
            )
            approach_notes = challenge_approach_notes(
                self.deps.settings,
                name,
                live_findings=getattr(swarm, "findings", {}) if swarm else {},
            )
            writeup = self._current_writeup_status(name, solved=is_solved)
            documented_count += int(bool(writeup.get("documented")))
            challenges.append(
                {
                    "name": name,
                    "category": getattr(meta, "category", "") or "Unknown",
                    "value": getattr(meta, "value", 0) or 0,
                    "solves": getattr(meta, "solves", 0) or 0,
                    "status": status,
                    "active": is_active,
                    "elapsed_seconds": round(
                        swarm.runtime_clock.elapsed_seconds
                        if swarm is not None and hasattr(swarm, "runtime_clock")
                        else max((agent["duration_seconds"] for agent in agents), default=0.0),
                        1,
                    ),
                    "solved": is_solved,
                    "flag": result.get("flag"),
                    "candidate": candidate.get("flag"),
                    "candidates": candidate.get("flags", []),
                    "candidate_review_required": bool(candidate.get("review_required")),
                    "approach_notes": approach_notes,
                    "writeup": writeup,
                    "documented": bool(writeup.get("documented")),
                    "resources": challenge_resources,
                    "agents": agents,
                    "cost_usd": round(sum(a["cost_usd"] for a in agents), 6),
                }
            )

        ctfd = getattr(self.deps, "ctfd", None)
        configured = bool(ctfd and ctfd.is_configured)
        force_no_submit = bool(getattr(self.deps, "force_no_submit", False))
        submission_mode = "dry_run" if force_no_submit else "live" if configured else "standalone"
        effective_total = sum(
            token_metrics(
                agent.usage.input_tokens,
                agent.usage.output_tokens,
                agent.usage.cache_read_tokens,
                getattr(self.deps.settings, "solver_cached_token_weight", 0.10),
            ).effective_tokens
            for agent in self.cost_tracker.by_agent.values()
        )

        current_source_fingerprint = _runtime_source_fingerprint()
        resource_totals = _resource_totals(live_resources)
        runtime_settings = runtime_settings_from(
            self.deps.settings,
            self.deps.model_specs,
            self.deps.max_concurrent_challenges,
        )
        return {
            "updated_at": datetime.now(UTC).isoformat(),
            "runtime_revision": RUNTIME_REVISION,
            "restart_required": current_source_fingerprint != self._startup_source_fingerprint,
            "uptime_seconds": round(time.monotonic() - self.started_at),
            "no_submit": self.deps.no_submit,
            "submission_mode": submission_mode,
            "ctfd": {
                "configured": configured,
                "connected": configured and not bool(getattr(self.poller, "last_error", "")),
                "url": ctfd.base_url if configured else "",
                "token_configured": bool(getattr(ctfd, "token", "")) if configured else False,
                "error": getattr(self.poller, "last_error", ""),
            },
            "models": self.deps.model_specs,
            "max_concurrent_challenges": self.deps.max_concurrent_challenges,
            "runtime_settings": runtime_settings.model_dump(),
            "runtime_policy": {
                "max_attempts": getattr(self.deps.settings, "max_attempts_per_challenge", 8),
                "turn_timeout_seconds": getattr(self.deps.settings, "solver_turn_timeout_seconds", 1800),
                "turn_idle_timeout_seconds": getattr(
                    self.deps.settings,
                    "solver_turn_idle_timeout_seconds",
                    300,
                ),
                "max_runtime_seconds": getattr(self.deps.settings, "solver_max_runtime_seconds", 10800),
                "max_steps": getattr(self.deps.settings, "solver_max_steps", 300),
                "max_tokens": getattr(self.deps.settings, "solver_max_tokens", 1_500_000),
                "max_raw_tokens": getattr(self.deps.settings, "solver_max_raw_tokens", 12_000_000),
                "cached_token_weight": getattr(self.deps.settings, "solver_cached_token_weight", 0.10),
                "turn_slice_tokens": getattr(self.deps.settings, "solver_turn_slice_tokens", 1_500_000),
                "max_submissions": getattr(self.deps.settings, "max_flag_submissions_per_challenge", 8),
                "in_turn_budget_interrupt": True,
                "runtime_state_persistence": True,
                "adaptive_delegation": bool(
                    getattr(self.deps.settings, "dynamic_delegation_enabled", True)
                ),
                "delegate_model": getattr(
                    self.deps.settings,
                    "delegate_model_spec",
                    "codex/gpt-5.6-luna/low",
                ),
                "delegate_max_agents": getattr(self.deps.settings, "delegate_max_agents", 4),
                "delegate_max_concurrent": getattr(
                    self.deps.settings,
                    "delegate_max_concurrent",
                    2,
                ),
                "delegate_max_tokens": getattr(self.deps.settings, "delegate_max_tokens", 250_000),
                "delegate_max_raw_tokens": getattr(
                    self.deps.settings,
                    "delegate_max_raw_tokens",
                    1_200_000,
                ),
                "delegate_max_attempts": getattr(
                    self.deps.settings,
                    "delegate_max_attempts",
                    4,
                ),
                "delegate_max_runtime_seconds": getattr(
                    self.deps.settings,
                    "delegate_max_runtime_seconds",
                    1800,
                ),
                "delegate_max_steps": getattr(self.deps.settings, "delegate_max_steps", 96),
                "delegate_turn_slice_tokens": getattr(
                    self.deps.settings,
                    "delegate_turn_slice_tokens",
                    300_000,
                ),
                "delegate_postprocess_on_budget_stop": bool(
                    getattr(
                        self.deps.settings,
                        "delegate_postprocess_on_budget_stop",
                        True,
                    )
                ),
                "delegate_postprocess_max_agents": getattr(
                    self.deps.settings,
                    "delegate_postprocess_max_agents",
                    1,
                ),
                "delegate_postprocess_max_tokens": getattr(
                    self.deps.settings,
                    "delegate_postprocess_max_tokens",
                    80_000,
                ),
                "delegate_postprocess_max_raw_tokens": getattr(
                    self.deps.settings,
                    "delegate_postprocess_max_raw_tokens",
                    400_000,
                ),
                "workspace_root": str(getattr(self.deps.settings, "workspace_root", "workspace")),
                "experience_root": str(
                    getattr(self.deps.settings, "experience_root", "experience")
                ),
                "logs_root": str(getattr(self.deps.settings, "logs_root", "logs")),
            },
            "resources": resource_totals,
            "experience": experience_summary(self.deps.settings),
            "stats": {
                "total": len(known),
                "solved": len(solved),
                "candidates": len(set(candidates) - solved),
                "active_swarms": len(active_names),
                "active_agents": active_agents,
                "total_agents": total_agents,
                "documented": documented_count,
                "cost_usd": round(self.cost_tracker.total_cost_usd, 6),
                "tokens": self.cost_tracker.total_tokens,
                "effective_tokens": effective_total,
            },
            "challenges": challenges,
        }

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response(self._snapshot())

    async def _resources(self, _request: web.Request) -> web.Response:
        challenges: list[dict[str, Any]] = []
        live: list[dict[str, Any]] = []
        names = set(self.deps.swarms) | set(self._writeup_solvers)
        for name in names:
            swarm = self.deps.swarms.get(name)
            agents: list[dict[str, Any]] = []
            if swarm:
                for spec in swarm.model_specs:
                    solver = swarm.solvers.get(spec)
                    sandbox = getattr(solver, "sandbox", None) if solver else None
                    reader = getattr(sandbox, "resource_snapshot", None)
                    resource = reader() if callable(reader) else {}
                    if not resource:
                        continue
                    agents.append(
                        {
                            "model_spec": spec,
                            "role": solver_role(spec).key,
                            "resource": resource,
                        }
                    )
                    if resource.get("status") != "stopped":
                        live.append(resource)
            writeup_solver = self._writeup_solvers.get(name)
            if writeup_solver is not None:
                phase = self._writeup_phases.get(name, "writing")
                reader = getattr(writeup_solver.sandbox, "resource_snapshot", None)
                resource = reader() if callable(reader) else {}
                if resource:
                    agents.append(
                        {
                            "model_spec": f"{writeup_solver.model_spec}/{'writeup-review' if phase == 'reviewing' else 'writeup-revision' if phase == 'revising' else 'writeup'}",
                            "role": "writeup_review" if phase == "reviewing" else "writeup_revision" if phase == "revising" else "writeup",
                            "resource": resource,
                        }
                    )
                    if resource.get("status") != "stopped":
                        live.append(resource)
            if agents:
                challenges.append(
                    {
                        "name": name,
                        "resources": _resource_totals(
                            [agent["resource"] for agent in agents]
                        ),
                        "agents": agents,
                    }
                )
        return web.json_response(
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "resources": _resource_totals(live),
                "challenges": challenges,
            }
        )

    async def _writeup(self, request: web.Request) -> web.Response:
        name = request.query.get("challenge", "").strip()
        if name not in self.deps.challenge_metas and name not in self.deps.results:
            raise web.HTTPNotFound(text="challenge not found")
        try:
            content, _status, _ = read_writeup(self.deps.settings, name)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        status = self._current_writeup_status(name, solved=True)
        return web.json_response({"challenge": name, "content": content, "writeup": status})

    async def _writeup_archive(self, request: web.Request) -> web.Response:
        """Download a self-contained Markdown and selected-evidence bundle."""
        name = request.query.get("challenge", "").strip()
        if name not in self.deps.challenge_metas and name not in self.deps.results:
            raise web.HTTPNotFound(text="challenge not found")
        try:
            content, status, root = read_writeup(self.deps.settings, name)
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc

        writeup_path = (root / str(status.get("writeup_path", ""))).resolve()
        selected: list[tuple[Path, str]] = []
        used_names: set[str] = set()
        rewritten = content
        for index, item in enumerate(status.get("screenshots") or [], 1):
            relative = str(item.get("path", "")).strip()
            source = (root / relative).resolve()
            if (
                not relative
                or not source.is_relative_to(root)
                or source.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
                or not source.is_file()
            ):
                continue
            archive_name = source.name
            if archive_name.casefold() in used_names:
                archive_name = f"{source.stem}-{index}{source.suffix.lower()}"
            used_names.add(archive_name.casefold())
            archive_path = f"evidence/{archive_name}"
            selected.append((source, archive_path))
            try:
                markdown_path = source.relative_to(writeup_path.parent).as_posix()
            except ValueError:
                parent_depth = len(writeup_path.parent.relative_to(root).parts)
                markdown_path = Path(
                    *([".."] * parent_depth),
                    *source.relative_to(root).parts,
                ).as_posix()
            rewritten = rewritten.replace(f"]({markdown_path})", f"]({archive_path})")

        buffer = io.BytesIO()
        # Solver containers can preserve Unix-epoch mtimes on generated artifacts.
        # The ZIP format cannot represent dates before 1980; without relaxed
        # timestamp handling, ZipFile.write() raises ValueError and the dashboard
        # returns HTTP 500 for an otherwise valid writeup bundle.
        with zipfile.ZipFile(
            buffer,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            strict_timestamps=False,
        ) as archive:
            archive.writestr("WRITEUP.md", rewritten.encode("utf-8"))
            review_path = root / "_shared" / "writeup" / "REVIEW.md"
            if review_path.is_file():
                archive.write(review_path, "REVIEW.md")
            for source, archive_path in selected:
                archive.write(source, archive_path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "challenge"
        return web.Response(
            body=buffer.getvalue(),
            content_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}-writeup.zip"'},
        )

    def _current_writeup_status(self, name: str, *, solved: bool) -> dict[str, Any]:
        status = writeup_status(self.deps.settings, name, solved=solved)
        task = self._writeup_tasks.get(name)
        if task is not None and not task.done():
            status = dict(status)
            status["status"] = "generating"
            status["documented"] = False
            status["active"] = True
            solver = self._writeup_solvers.get(name)
            phase = self._writeup_phases.get(name, "writing")
            status["phase"] = phase
            status["phase_label"] = (
                "Luna-Medium 검수"
                if phase == "reviewing"
                else "Terra-Medium 수정"
                if phase == "revising"
                else "Terra-Medium 작성"
            )
            status["activity"] = getattr(
                solver,
                "activity_summary",
                "생성된 라이트업을 검수하는 중"
                if phase == "reviewing"
                else "검수 반려 항목을 수정하는 중"
                if phase == "revising"
                else "풀이 내역으로 라이트업을 작성하는 중",
            )
            idle_reader = getattr(solver, "activity_idle_seconds", None)
            status["steps"] = getattr(solver, "_step_count", 0)
            if callable(idle_reader):
                idle = max(0.0, float(idle_reader()))
                status["idle_seconds"] = round(idle, 1)
                status["last_activity_at"] = datetime.fromtimestamp(
                    time.time() - idle, UTC,
                ).isoformat()
            status["idle_timeout_seconds"] = max(
                1, int(getattr(self.deps.settings, "writeup_idle_timeout_seconds", 300)),
            )
            resource_reader = getattr(getattr(solver, "sandbox", None), "resource_snapshot", None)
            if callable(resource_reader):
                status["resource"] = resource_reader()
            return status
        return interrupted_writeup_status(status)

    def _writeup_model_specs(self) -> tuple[str, str]:
        writer = str(
            getattr(self.deps.settings, "writeup_model_spec", "codex/gpt-5.6-terra/medium")
        ).strip()
        reviewer = str(
            getattr(
                self.deps.settings,
                "writeup_review_model_spec",
                "codex/gpt-5.6-luna/medium",
            )
        ).strip()
        if provider_from_spec(writer) != "codex" or provider_from_spec(reviewer) != "codex":
            return "", ""
        return writer, reviewer

    def _create_writeup_solver(
        self,
        name: str,
        meta: ChallengeMeta,
        challenge_dir: str,
        model_spec: str,
        flag: str,
        task_mode: str = "writeup",
    ):
        from backend.agents.codex_solver import CodexSolver

        return CodexSolver(
            model_spec=model_spec,
            challenge_dir=challenge_dir,
            meta=meta,
            ctfd=self.deps.ctfd,
            cost_tracker=self.cost_tracker,
            settings=self.deps.settings,
            no_submit=True,
            task_mode=task_mode,
            verified_flag=flag,
        )

    async def _wait_for_writeup(self, solver: Any) -> Any:
        """Watch documentation jobs independently of the solve swarm's watchdog."""
        idle_limit = max(
            1, int(getattr(self.deps.settings, "writeup_idle_timeout_seconds", 300)),
        )
        job = asyncio.create_task(solver.run_until_done_or_gave_up())
        try:
            while True:
                done, _ = await asyncio.wait({job}, timeout=1)
                if done:
                    return job.result()
                reader = getattr(solver, "_reader_task", None)
                if reader is not None and reader.done():
                    if not reader.cancelled() and reader.exception() is not None:
                        raise RuntimeError("라이트업 응답 수신기가 오류로 종료되었습니다") from reader.exception()
                    raise RuntimeError("라이트업 응답 연결이 종료되었습니다")
                idle_reader = getattr(solver, "activity_idle_seconds", None)
                tool_active = bool(getattr(solver, "tool_call_active", False))
                if callable(idle_reader) and not tool_active and idle_reader() >= idle_limit:
                    raise RuntimeError(
                        f"라이트업 생성기에서 {idle_limit}초 동안 새 활동이 없어 중단했습니다. "
                        "보존된 자료로 다시 요청할 수 있습니다"
                    )
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def _run_writeup_generation(
        self,
        name: str,
        meta: ChallengeMeta,
        model_spec: str,
        review_model_spec: str,
        flag: str,
        *,
        targeted_revision: bool = False,
    ) -> None:
        challenge_dir = self.deps.challenge_dirs.get(name, self.deps.challenges_root)
        try:
            timeout = max(
                60,
                int(getattr(self.deps.settings, "writeup_generation_timeout_seconds", 1800)),
            )
            results = []
            # The configured limit applies to the complete writer + reviewer
            # pipeline. Previously each stage received the full allowance, so a
            # nominal 30-minute job could occupy resources for roughly an hour.
            async with asyncio.timeout(timeout):
                async def run_stage(phase: str, spec: str, task_mode: str) -> None:
                    self._writeup_phases[name] = phase
                    solver = self._create_writeup_solver(
                        name, meta, challenge_dir, spec, flag, task_mode=task_mode
                    )
                    self._writeup_solvers[name] = solver
                    clock = RuntimeClock()
                    clock.start()
                    self._writeup_clocks[name] = clock
                    try:
                        results.append(await self._wait_for_writeup(solver))
                    finally:
                        clock.stop()
                        try:
                            await asyncio.wait_for(solver.stop(), timeout=15)
                        except Exception as exc:
                            logger.warning(
                                "Could not stop %s stage for %s: %s", phase, name, exc
                            )
                        if self._writeup_solvers.get(name) is solver:
                            self._writeup_solvers.pop(name, None)
                            self._writeup_clocks.pop(name, None)

                stages = (
                    [
                        ("revising", model_spec, "writeup_revision"),
                        ("reviewing", review_model_spec, "writeup_review"),
                    ]
                    if targeted_revision
                    else [
                        ("writing", model_spec, "writeup"),
                        ("reviewing", review_model_spec, "writeup_review"),
                    ]
                )
                for phase, spec, task_mode in stages:
                    await run_stage(phase, spec, task_mode)
                if (
                    not targeted_revision
                    and writeup_review_verdict(self.deps.settings, name) == "rejected"
                ):
                    for phase, spec, task_mode in (
                        ("revising", model_spec, "writeup_revision"),
                        ("reviewing", review_model_spec, "writeup_review"),
                    ):
                        await run_stage(phase, spec, task_mode)
            status = finalize_writeup(
                self.deps.settings,
                name,
                getattr(meta, "category", "Unknown"),
                flag,
                prefer_canonical=True,
                require_screenshots=True,
                require_review=True,
            )
            if not status.get("documented") and any(
                result.status in {"error", "quota_error"} for result in results
            ):
                status = fail_writeup_generation(
                    self.deps.settings,
                    name,
                    "AI 라이트업 생성기가 오류로 종료되었습니다. 다시 요청할 수 있습니다",
                )
        except asyncio.CancelledError:
            fail_writeup_generation(
                self.deps.settings,
                name,
                "라이트업 생성 작업이 서버 종료 또는 초기화로 중단되었습니다",
            )
            raise
        except TimeoutError:
            status = fail_writeup_generation(
                self.deps.settings,
                name,
                "라이트업 생성 제한 시간을 초과했습니다. 다시 요청할 수 있습니다",
            )
        except Exception as exc:
            logger.warning("Writeup generation failed for %s: %s", name, exc, exc_info=True)
            status = fail_writeup_generation(
                self.deps.settings,
                name,
                f"라이트업 생성 실패: {type(exc).__name__}: {exc}",
            )
        finally:
            self._writeup_solvers.pop(name, None)
            self._writeup_phases.pop(name, None)

        if name in self.deps.results:
            self.deps.results[name]["writeup"] = status
        persist_deps_state(self.deps)

    async def start_writeup_generation(self, name: str) -> dict[str, Any]:
        """Schedule the same AI pipeline for automatic and manual requests."""
        async with self._writeup_lock:
            existing = self._writeup_tasks.get(name)
            if existing is not None and not existing.done():
                return self._current_writeup_status(name, solved=True)

            model_spec, review_model_spec = self._writeup_model_specs()
            if not model_spec or not review_model_spec:
                raise RuntimeError("writeup generation and review require configured Codex models")

            previous_status = self._current_writeup_status(name, solved=True)
            history = previous_status.get("history", [])
            latest_event = ""
            if isinstance(history, list) and history and isinstance(history[-1], dict):
                latest_event = str(history[-1].get("event", ""))
            targeted_revision = bool(
                previous_status.get("status") == "needs_attention"
                and previous_status.get("writeup_path")
                and previous_status.get("review_path")
                and (
                    latest_event == "needs_attention"
                    or previous_status.get("targeted_revision") is True
                )
            )

            meta = self.deps.challenge_metas.get(name)
            if meta is None:
                meta = ChallengeMeta(name=name, category="Unknown")
            result = self.deps.results.get(name, {})
            flag = str(result.get("flag", ""))
            seed_writeup_from_solver_evidence(
                self.deps.settings,
                name,
                getattr(meta, "category", "Unknown"),
                flag,
            )
            status = begin_writeup_generation(
                self.deps.settings,
                name,
                model_spec,
                review_model_spec,
                targeted_revision=targeted_revision,
                revision_scope=list(previous_status.get("issues") or [])
                if targeted_revision
                else None,
            )
            task = asyncio.create_task(
                self._run_writeup_generation(
                    name,
                    meta,
                    model_spec,
                    review_model_spec,
                    flag,
                    targeted_revision=targeted_revision,
                ),
                name=f"writeup-{name}",
            )
            self._writeup_tasks[name] = task
            task.add_done_callback(
                lambda completed, challenge=name: (
                    self._writeup_tasks.pop(challenge, None)
                    if self._writeup_tasks.get(challenge) is completed
                    else None
                )
            )
            return status

    async def _request_writeup(self, request: web.Request) -> web.Response:
        """Start a real asynchronous AI regeneration from preserved artifacts."""
        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        known = self.poller.known_challenges | set(self.deps.challenge_metas) | set(self.deps.results)
        if not name or name not in known:
            raise web.HTTPNotFound(text="challenge not found")
        solved = self.poller.known_solved | set(self.deps.results)
        if name not in solved:
            raise web.HTTPConflict(text="writeup can only be requested for a solved challenge")

        existing = self._writeup_tasks.get(name)
        if existing is not None and not existing.done():
            raise web.HTTPConflict(text="writeup generation is already running")

        try:
            status = await self.start_writeup_generation(name)
        except RuntimeError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

        message = (
            f"{name} 라이트업의 부족한 항목 수정과 재검수를 시작했습니다."
            if status.get("targeted_revision")
            else f"{name} 라이트업 AI 재생성을 시작했습니다."
        )
        return web.json_response(
            {"ok": True, "message": message, "challenge": name, "writeup": status},
            status=202,
        )

    async def _artifact(self, request: web.Request) -> web.StreamResponse:
        name = request.query.get("challenge", "").strip()
        relative = request.query.get("path", "").strip()
        if name not in self.deps.challenge_metas and name not in self.deps.results:
            raise web.HTTPNotFound(text="challenge not found")
        root = Path(challenge_workspace_path(self.deps.settings, name)).resolve()
        path = (root / relative).resolve()
        if (
            not relative
            or not path.is_relative_to(root)
            or path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}
            or not path.is_file()
        ):
            raise web.HTTPNotFound(text="artifact not found")
        return web.FileResponse(path)

    async def _trace(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_read_solver_trace

        name = request.query.get("challenge", "").strip()
        model = request.query.get("model", "")
        try:
            last_n = min(200, max(1, int(request.query.get("last_n", "40"))))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="last_n must be an integer") from exc
        if not name or not model:
            raise web.HTTPBadRequest(text="challenge and model query parameters required")
        trace = await do_read_solver_trace(self.deps, name, model, last_n)
        return web.json_response({"challenge": name, "model": model, "trace": trace})

    def _runtime_settings_payload(self) -> dict[str, Any]:
        return runtime_settings_from(
            self.deps.settings,
            self.deps.model_specs,
            self.deps.max_concurrent_challenges,
        ).model_dump()

    async def _runtime_settings(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "settings": self._runtime_settings_payload(),
                "applies_to": "future_swarms",
            }
        )

    def _apply_runtime_settings(self, runtime: RuntimeSettings) -> None:
        apply_runtime_settings(self.deps.settings, runtime)
        self.deps.model_specs[:] = runtime.models
        self.deps.max_concurrent_challenges = runtime.max_concurrent_challenges

    async def _configure_runtime(self, request: web.Request) -> web.Response:
        """Validate and persist non-secret policy for future solver swarms."""
        self._require_csrf(request)
        data = await self._json_body(request)
        current = self._runtime_settings_payload()
        current.update(data)
        try:
            runtime = RuntimeSettings.model_validate(current)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

        async with self._command_lock:
            try:
                save_runtime_settings(runtime, self.deps.challenges_root)
            except OSError as exc:
                raise web.HTTPInternalServerError(
                    text=f"실행 설정을 저장하지 못했습니다: {exc}"
                ) from exc
            self._apply_runtime_settings(runtime)

        active = sum(not task.done() for task in self.deps.swarm_tasks.values())
        suffix = (
            f" 실행 중인 swarm {active}개는 기존 설정을 유지합니다."
            if active
            else ""
        )
        return web.json_response(
            {
                "ok": True,
                "message": f"실행 설정을 저장했습니다. 새 swarm부터 적용됩니다.{suffix}",
                "settings": runtime.model_dump(),
                "active_swarms_unchanged": active,
            }
        )

    async def _reset_runtime_settings(self, request: web.Request) -> web.Response:
        """Restore the non-secret dashboard policy to project defaults."""
        self._require_csrf(request)
        await self._json_body(request)
        async with self._command_lock:
            try:
                runtime = reset_runtime_settings(self.deps.challenges_root)
            except OSError as exc:
                raise web.HTTPInternalServerError(
                    text=f"기본 실행 설정을 저장하지 못했습니다: {exc}"
                ) from exc
            self._apply_runtime_settings(runtime)
        active = sum(not task.done() for task in self.deps.swarm_tasks.values())
        return web.json_response(
            {
                "ok": True,
                "message": "실행 설정을 기본값으로 복원했습니다. 새 swarm부터 적용됩니다.",
                "settings": runtime.model_dump(),
                "active_swarms_unchanged": active,
            }
        )

    async def _configure_ctfd(self, request: web.Request) -> web.Response:
        """Validate and apply runtime CTFd settings without exposing secrets back."""
        self._require_csrf(request)
        data = await self._json_body(request)
        url = str(data.get("url", "")).strip()
        token = str(data.get("token", "")).strip()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))

        async with self._command_lock:
            if not url:
                await self.deps.ctfd.configure("")
                self.deps.settings.ctfd_url = ""
                self.deps.settings.ctfd_token = ""
                self.deps.settings.ctfd_user = ""
                self.deps.settings.ctfd_pass = ""
                self.deps.no_submit = True
                await self.poller.reseed()
                return web.json_response(
                    {"ok": True, "message": "CTFd 연결을 해제하고 독립 모드로 전환했습니다."}
                )

            try:
                normalized = CTFdClient.normalize_url(url)
            except ValueError as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc

            candidate = CTFdClient(
                base_url=normalized,
                token=token,
                username=username,
                password=password,
            )
            try:
                stubs = await candidate.fetch_challenge_stubs()
            except Exception as exc:
                raise web.HTTPBadGateway(text=f"CTFd 연결 확인 실패: {exc}") from exc
            finally:
                await candidate.close()

            await self.deps.ctfd.configure(normalized, token, username, password)
            self.deps.settings.ctfd_url = normalized
            self.deps.settings.ctfd_token = token
            self.deps.settings.ctfd_user = username
            self.deps.settings.ctfd_pass = password
            self.deps.no_submit = bool(getattr(self.deps, "force_no_submit", False))
            await self.poller.reseed()

        return web.json_response(
            {
                "ok": True,
                "message": f"CTFd에 연결했습니다. 문제 {len(stubs)}개를 확인했습니다.",
            }
        )

    async def _create_local_challenge(self, request: web.Request) -> web.Response:
        """Create one standalone challenge and save optional attachments locally."""
        self._require_csrf(request)
        if not request.content_type.startswith("multipart/"):
            raise web.HTTPUnsupportedMediaType(text="multipart/form-data required")

        root = Path(self.deps.challenges_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        fields: dict[str, str] = {}
        file_count = 0

        with _staging_directory(root) as temp_dir:
            dist_dir = temp_dir / "distfiles"
            reader = await request.multipart()
            async for part in reader:
                if part.filename:
                    filename = Path(part.filename).name
                    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ")
                    if not filename:
                        raise web.HTTPBadRequest(text="invalid attachment filename")
                    dist_dir.mkdir(exist_ok=True)
                    destination = dist_dir / filename
                    if destination.exists():
                        raise web.HTTPConflict(text=f"duplicate attachment: {filename}")
                    with destination.open("wb") as output:
                        while chunk := await part.read_chunk():
                            output.write(chunk)
                    file_count += 1
                else:
                    fields[part.name or ""] = (await part.text()).strip()

            name = fields.get("name", "")[:200]
            if not name:
                raise web.HTTPBadRequest(text="challenge name required")
            flag_format = fields.get("flag_format", "")[:500]
            if not flag_format:
                raise web.HTTPBadRequest(text="flag format required")
            if name in self.deps.challenge_dirs:
                raise web.HTTPConflict(text=f"challenge already exists: {name}")

            slug = re.sub(r'[<>:"/\\|?*.\x00-\x1f]', "", name.lower())
            slug = re.sub(r"[\s_]+", "-", slug)
            slug = re.sub(r"-+", "-", slug).strip("-") or "challenge"
            destination_dir = (root / slug).resolve()
            if destination_dir.parent != root or destination_dir.exists():
                raise web.HTTPConflict(text=f"challenge directory already exists: {slug}")

            try:
                value = max(0, int(fields.get("value", "0") or 0))
            except ValueError as exc:
                raise web.HTTPBadRequest(text="value must be an integer") from exc

            metadata = {
                "name": name,
                "category": fields.get("category", "")[:100],
                "description": fields.get("description", "")[:20000],
                "value": value,
                "connection_info": fields.get("connection_info", "")[:2000],
                "flag_format": flag_format,
                "tags": [],
                "solves": 0,
            }
            (temp_dir / "metadata.yml").write_text(
                yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            temp_dir.replace(destination_dir)

        meta = ChallengeMeta.from_yaml(destination_dir / "metadata.yml")
        self.deps.dismissed_challenges.discard(name)
        self.deps.challenge_dirs[name] = str(destination_dir)
        self.deps.challenge_metas[name] = meta
        persist_deps_state(self.deps)
        attachment_copy = f" 첨부 파일 {file_count}개를 저장했습니다." if file_count else ""
        return web.json_response(
            {
                "ok": True,
                "message": f"로컬 문제 '{name}'을 등록했습니다.{attachment_copy}",
                "challenge": name,
                "file_count": file_count,
            }
        )

    async def _delete_challenge(self, request: web.Request) -> web.Response:
        """Stop one challenge and permanently remove its local runtime data."""
        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        confirmation = str(data.get("confirmation", ""))
        known = (
            self.poller.known_challenges
            | set(self.deps.challenge_metas)
            | set(self.deps.swarms)
            | set(self.deps.results)
            | set(self.deps.candidates)
        )
        if not name or name not in known:
            raise web.HTTPNotFound(text="challenge not found")
        if confirmation != name:
            raise web.HTTPBadRequest(text="문제명을 정확히 입력해야 합니다.")

        project_root = Path.cwd().resolve()
        challenges_root = Path(self.deps.challenges_root).expanduser().resolve()
        workspace_root = Path(
            getattr(self.deps.settings, "workspace_root", "workspace")
        ).expanduser().resolve()
        logs_root = Path(
            getattr(self.deps.settings, "logs_root", "logs")
        ).expanduser().resolve()
        try:
            for root in (challenges_root, workspace_root, logs_root):
                _validate_runtime_root(root, project_root)
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

        challenge_dir_value = self.deps.challenge_dirs.get(name)
        challenge_dir = (
            Path(challenge_dir_value).expanduser().resolve()
            if challenge_dir_value
            else None
        )
        if challenge_dir is not None and (
            challenge_dir == challenges_root
            or not challenge_dir.is_relative_to(challenges_root)
        ):
            raise web.HTTPConflict(text=f"unsafe challenge path: {challenge_dir}")

        # This helper creates the deterministic directory when absent. That is
        # harmless here because it is immediately included in the deletion set.
        workspace_dir = Path(
            challenge_workspace_path(self.deps.settings, name)
        ).resolve()
        if workspace_dir.parent != workspace_root:
            raise web.HTTPConflict(text=f"unsafe challenge workspace: {workspace_dir}")

        removed_entries = 0
        retained: list[Path] = []
        async with self._command_lock:
            writeup_task = self._writeup_tasks.get(name)
            if writeup_task is not None and not writeup_task.done():
                writeup_task.cancel()
                await asyncio.gather(writeup_task, return_exceptions=True)

            swarm = self.deps.swarms.get(name)
            if swarm is not None:
                swarm.kill()
            swarm_task = self.deps.swarm_tasks.get(name)
            if swarm_task is not None and not swarm_task.done():
                swarm_task.cancel()
                await asyncio.gather(swarm_task, return_exceptions=True)

            targets = [workspace_dir]
            if challenge_dir is not None:
                targets.append(challenge_dir)
            targets.extend(challenge_trace_paths(logs_root, name))
            for target in targets:
                if not target.exists():
                    continue
                try:
                    await asyncio.to_thread(_remove_runtime_child, target)
                except FileNotFoundError:
                    continue
                except PermissionError:
                    retained.append(target)
                else:
                    removed_entries += 1

            self._writeup_tasks.pop(name, None)
            self._writeup_solvers.pop(name, None)
            self.deps.swarms.pop(name, None)
            self.deps.swarm_tasks.pop(name, None)
            self.deps.results.pop(name, None)
            self.deps.candidates.pop(name, None)
            self.deps.challenge_dirs.pop(name, None)
            self.deps.challenge_metas.pop(name, None)
            self.deps.dismissed_challenges.add(name)
            agent_prefix = f"{name}/"
            for agent_name in list(self.cost_tracker.by_agent):
                if agent_name.startswith(agent_prefix):
                    self.cost_tracker.by_agent.pop(agent_name, None)
            persist_deps_state(self.deps)

        message = f"문제 '{name}'과 관련 로컬 데이터 {removed_entries}개를 삭제했습니다."
        if retained:
            message += f" 사용 중인 항목 {len(retained)}개는 남아 있습니다."
        return web.json_response(
            {
                "ok": True,
                "challenge": name,
                "removed_entries": removed_entries,
                "retained_locked_entries": len(retained),
                "experience_preserved": True,
                "message": message,
            }
        )

    async def _operator_message(self, request: web.Request) -> web.Response:
        self._require_csrf(request)
        data = await self._json_body(request)
        message = str(data.get("message", "")).strip()
        if not message:
            raise web.HTTPBadRequest(text="message required")
        self.deps.operator_inbox.put_nowait(message[:4000])
        return web.json_response({"ok": True, "message": "Message queued"})

    async def _legacy_message(self, request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="application/json required")
        data = await self._json_body(request)
        message = str(data.get("message", "")).strip()
        if not message:
            raise web.HTTPBadRequest(text="message required")
        self.deps.operator_inbox.put_nowait(message[:4000])
        return web.json_response({"ok": True, "queued": message[:200]})

    async def _spawn(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_spawn_swarm

        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text="challenge required")
        async with self._command_lock:
            message = await do_spawn_swarm(self.deps, name)
        return web.json_response({"ok": True, "message": message})

    async def _stop_swarm(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_kill_swarm

        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text="challenge required")
        async with self._command_lock:
            message = await do_kill_swarm(self.deps, name)
        return web.json_response({"ok": True, "message": message})

    async def _broadcast(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_broadcast

        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        message_text = str(data.get("message", "")).strip()
        if not name or not message_text:
            raise web.HTTPBadRequest(text="challenge and message required")
        message = await do_broadcast(self.deps, name, message_text[:4000])
        return web.json_response({"ok": True, "message": message})

    async def _submit(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_submit_flag

        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        flag = str(data.get("flag", "")).strip()
        if not name or not flag:
            raise web.HTTPBadRequest(text="challenge and flag required")
        async with self._command_lock:
            message = await do_submit_flag(self.deps, name, flag[:2000])
        return web.json_response({"ok": True, "message": message})

    async def _review_candidate(self, request: web.Request) -> web.Response:
        from backend.agents.coordinator_core import do_review_candidate

        self._require_csrf(request)
        data = await self._json_body(request)
        name = str(data.get("challenge", "")).strip()
        flag = str(data.get("flag", "")).strip()
        accepted = data.get("accepted")
        if not name or not flag or not isinstance(accepted, bool):
            raise web.HTTPBadRequest(text="challenge, flag, and boolean accepted are required")
        async with self._command_lock:
            message = await do_review_candidate(self.deps, name, flag[:2000], accepted)
        return web.json_response({"ok": True, "message": message})

    async def _reset_runtime(self, request: web.Request) -> web.Response:
        """Stop all work and clear only configured runtime state and directories."""
        self._require_csrf(request)
        data = await self._json_body(request)
        if data.get("confirmation") != RESET_CONFIRMATION:
            raise web.HTTPBadRequest(text=f"'{RESET_CONFIRMATION}'를 정확히 입력해야 합니다.")

        project_root = Path.cwd().resolve()
        roots = {
            Path(self.deps.challenges_root).expanduser().resolve(),
            Path(getattr(self.deps.settings, "workspace_root", "workspace")).expanduser().resolve(),
            Path(getattr(self.deps.settings, "logs_root", "logs")).expanduser().resolve(),
        }
        permanent_experience = Path(
            getattr(self.deps.settings, "experience_root", "experience")
        ).expanduser().resolve()
        try:
            for root in roots:
                _validate_runtime_root(root, project_root)
                if (
                    root == permanent_experience
                    or root.is_relative_to(permanent_experience)
                    or permanent_experience.is_relative_to(root)
                ):
                    raise ValueError("runtime path overlaps permanent experience storage")
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

        async with self._command_lock:
            writeup_tasks = list(self._writeup_tasks.values())
            for task in writeup_tasks:
                if not task.done():
                    task.cancel()
            if writeup_tasks:
                await asyncio.gather(*writeup_tasks, return_exceptions=True)

            for swarm in list(self.deps.swarms.values()):
                swarm.kill()
            tasks = list(self.deps.swarm_tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            await self.deps.ctfd.configure("")
            self.deps.settings.ctfd_url = ""
            self.deps.settings.ctfd_token = ""
            self.deps.settings.ctfd_user = ""
            self.deps.settings.ctfd_pass = ""
            self.deps.no_submit = True
            await self.poller.reseed()
            drain_events = getattr(self.poller, "drain_events", None)
            if drain_events:
                drain_events()

            self.deps.swarms.clear()
            self.deps.swarm_tasks.clear()
            self.deps.results.clear()
            self.deps.candidates.clear()
            self.deps.dismissed_challenges.clear()
            self.deps.challenge_dirs.clear()
            self.deps.challenge_metas.clear()
            self.cost_tracker.by_agent.clear()
            _drain_queue(getattr(self.deps, "coordinator_inbox", None))
            _drain_queue(getattr(self.deps, "operator_inbox", None))

            removed = 0
            retained: list[Path] = []
            for root in sorted(roots, key=str):
                root_removed, root_retained = await asyncio.to_thread(_clear_runtime_root, root)
                removed += root_removed
                retained.extend(root_retained)

        return web.json_response(
            {
                "ok": True,
                "removed_entries": removed,
                "retained_locked_entries": len(retained),
                "message": (
                    "풀이 기록과 실행 환경을 초기화했습니다. "
                    f"런타임 항목 {removed}개를 제거하고 독립 모드로 전환했습니다."
                ),
            }
        )

    async def _reset_experience(self, request: web.Request) -> web.Response:
        """Clear only the curated cross-challenge experience repository."""
        self._require_csrf(request)
        data = await self._json_body(request)
        if data.get("confirmation") != RESET_EXPERIENCE_CONFIRMATION:
            raise web.HTTPBadRequest(
                text=f"Enter '{RESET_EXPERIENCE_CONFIRMATION}' exactly."
            )

        root = experience_root(self.deps.settings).resolve()
        project_root = Path.cwd().resolve()
        runtime_roots = {
            Path(self.deps.challenges_root).expanduser().resolve(),
            Path(getattr(self.deps.settings, "workspace_root", "workspace")).expanduser().resolve(),
            Path(getattr(self.deps.settings, "logs_root", "logs")).expanduser().resolve(),
        }
        try:
            _validate_runtime_root(root, project_root)
            if any(
                root == runtime_root
                or root.is_relative_to(runtime_root)
                or runtime_root.is_relative_to(root)
                for runtime_root in runtime_roots
            ):
                raise ValueError("experience path overlaps a runtime directory")
        except ValueError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc

        async with self._command_lock:
            removed, retained = await asyncio.to_thread(_clear_runtime_root, root)
        return web.json_response(
            {
                "ok": True,
                "removed_entries": removed,
                "retained_locked_entries": len(retained),
                "message": f"Stored solver experience was cleared ({removed} entries removed).",
            }
        )
