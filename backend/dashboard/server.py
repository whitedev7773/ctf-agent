"""Loopback-only dashboard and control API for a running coordinator."""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import web

from backend.ctfd import CTFdClient
from backend.prompts import ChallengeMeta
from backend.solver_base import solver_agent_name

if TYPE_CHECKING:
    from backend.cost_tracker import CostTracker
    from backend.deps import CoordinatorDeps
    from backend.poller import CTFdPoller


STATIC_DIR = Path(__file__).parent / "static"


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


def _usage_payload(cost_tracker: CostTracker, agent_name: str) -> dict[str, Any]:
    agent = cost_tracker.by_agent.get(agent_name)
    if not agent:
        return {
            "cost_usd": 0.0,
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "duration_seconds": 0.0,
        }
    return {
        "cost_usd": round(agent.cost_usd, 6),
        "input_tokens": agent.usage.input_tokens,
        "cached_tokens": agent.usage.cache_read_tokens,
        "output_tokens": agent.usage.output_tokens,
        "duration_seconds": round(agent.duration_seconds, 1),
    }


class DashboardServer:
    """Serve dashboard assets and same-process coordinator controls."""

    def __init__(
        self,
        deps: CoordinatorDeps,
        poller: CTFdPoller,
        cost_tracker: CostTracker,
        port: int = 9400,
    ) -> None:
        self.deps = deps
        self.poller = poller
        self.cost_tracker = cost_tracker
        self.port = port
        self.actual_port = port
        self.started_at = time.monotonic()
        self.csrf_token = secrets.token_urlsafe(32)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._command_lock = asyncio.Lock()

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
                web.get("/api/trace", self._trace),
                web.post("/api/settings/ctfd", self._configure_ctfd),
                web.post("/api/challenges/local", self._create_local_challenge),
                web.post("/api/operator/message", self._operator_message),
                web.post("/api/control/spawn", self._spawn),
                web.post("/api/control/stop", self._stop_swarm),
                web.post("/api/control/broadcast", self._broadcast),
                web.post("/api/control/submit", self._submit),
                # Backward-compatible endpoint used by the ctf-msg command.
                web.post("/msg", self._legacy_message),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await self._site.start()

        server = getattr(self._site, "_server", None)
        sockets = getattr(server, "sockets", None)
        if sockets:
            self.actual_port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
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

    async def _session(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "csrf_token": self.csrf_token,
                "port": self.actual_port,
                "no_submit": self.deps.no_submit,
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
        known = (
            self.poller.known_challenges
            | set(self.deps.challenge_metas)
            | set(self.deps.swarms)
            | set(self.deps.results)
        )
        solved = self.poller.known_solved | set(self.deps.results)
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

        for name in sorted(known, key=str.casefold):
            meta = self.deps.challenge_metas.get(name)
            swarm = self.deps.swarms.get(name)
            result = self.deps.results.get(name, {})
            is_active = name in active_names and swarm is not None and not swarm.cancel_event.is_set()
            is_solved = name in solved

            agents: list[dict[str, Any]] = []
            if swarm:
                for spec in swarm.model_specs:
                    solver = swarm.solvers.get(spec)
                    outcome = swarm.outcomes.get(spec)
                    running = solver is not None and is_active and outcome is None
                    raw_steps = getattr(solver, "_step_count", 0) if solver else 0
                    if isinstance(raw_steps, list):
                        raw_steps = raw_steps[0] if raw_steps else 0
                    usage = _usage_payload(
                        self.cost_tracker,
                        solver_agent_name(name, spec),
                    )
                    agents.append(
                        {
                            "model_spec": spec,
                            "status": "running" if running else (
                                "won" if swarm.winner and swarm.winner.flag else (
                                    outcome.status if outcome else "finished"
                                )
                            ),
                            "steps": raw_steps,
                            "findings": swarm.findings.get(spec, ""),
                            "trace": Path(getattr(getattr(solver, "tracer", None), "path", "")).name,
                            "stop_reason": outcome.stop_reason if outcome else "",
                            "attempt": outcome.attempt if outcome else 0,
                            "workspace_path": (
                                outcome.workspace_path if outcome and outcome.workspace_path
                                else getattr(getattr(solver, "sandbox", None), "workspace_dir", "")
                            ),
                            **usage,
                        }
                    )
                    total_agents += 1
                    active_agents += int(running)

            status = "solved" if is_solved else "active" if is_active else "idle"
            challenges.append(
                {
                    "name": name,
                    "category": getattr(meta, "category", "") or "Unknown",
                    "value": getattr(meta, "value", 0) or 0,
                    "solves": getattr(meta, "solves", 0) or 0,
                    "status": status,
                    "active": is_active,
                    "solved": is_solved,
                    "flag": result.get("flag"),
                    "agents": agents,
                    "cost_usd": round(sum(a["cost_usd"] for a in agents), 6),
                }
            )

        ctfd = getattr(self.deps, "ctfd", None)
        configured = bool(ctfd and ctfd.is_configured)
        force_no_submit = bool(getattr(self.deps, "force_no_submit", False))
        submission_mode = "dry_run" if force_no_submit else "live" if configured else "standalone"

        return {
            "updated_at": datetime.now(UTC).isoformat(),
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
            "runtime_policy": {
                "max_attempts": getattr(self.deps.settings, "max_attempts_per_challenge", 3),
                "turn_timeout_seconds": getattr(self.deps.settings, "solver_turn_timeout_seconds", 1800),
                "max_runtime_seconds": getattr(self.deps.settings, "solver_max_runtime_seconds", 7200),
                "max_steps": getattr(self.deps.settings, "solver_max_steps", 240),
                "max_tokens": getattr(self.deps.settings, "solver_max_tokens", 1_000_000),
                "max_submissions": getattr(self.deps.settings, "max_flag_submissions_per_challenge", 8),
                "workspace_root": str(getattr(self.deps.settings, "workspace_root", "workspace")),
            },
            "stats": {
                "total": len(known),
                "solved": len(solved),
                "active_swarms": len(active_names),
                "active_agents": active_agents,
                "total_agents": total_agents,
                "cost_usd": round(self.cost_tracker.total_cost_usd, 6),
                "tokens": self.cost_tracker.total_tokens,
            },
            "challenges": challenges,
        }

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response(self._snapshot())

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
        self.deps.challenge_dirs[name] = str(destination_dir)
        self.deps.challenge_metas[name] = meta
        attachment_copy = f" 첨부 파일 {file_count}개를 저장했습니다." if file_count else ""
        return web.json_response(
            {
                "ok": True,
                "message": f"로컬 문제 '{name}'을 등록했습니다.{attachment_copy}",
                "challenge": name,
                "file_count": file_count,
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
