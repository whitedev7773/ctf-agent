"""Read account-wide Codex rate limits from the local Codex App Server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from backend.codex_cli import prepare_codex_cli

logger = logging.getLogger(__name__)


class CodexUsageError(RuntimeError):
    """Raised when the Codex App Server cannot provide rate-limit data."""


def _window_kind(name: str, duration_minutes: int | None) -> tuple[str, str]:
    if duration_minutes is not None and 295 <= duration_minutes <= 305:
        return "five_hour", "5시간"
    if duration_minutes is not None and 10_000 <= duration_minutes <= 10_160:
        return "weekly", "주간"
    if duration_minutes:
        if duration_minutes % 1_440 == 0:
            days = duration_minutes // 1_440
            return name, f"{days}일"
        if duration_minutes % 60 == 0:
            hours = duration_minutes // 60
            return name, f"{hours}시간"
        return name, f"{duration_minutes}분"
    return name, "기본" if name == "primary" else "보조"


def normalize_rate_limits(result: dict[str, Any]) -> dict[str, Any]:
    """Convert the versioned app-server response into a stable dashboard payload."""
    buckets = result.get("rateLimitsByLimitId")
    bucket = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        bucket = result.get("rateLimits")
    if not isinstance(bucket, dict):
        raise CodexUsageError("Codex usage response did not include a rate-limit bucket.")

    windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        raw = bucket.get(name)
        if not isinstance(raw, dict) or "usedPercent" not in raw:
            continue
        duration_raw = raw.get("windowDurationMins")
        duration = int(duration_raw) if duration_raw is not None else None
        kind, label = _window_kind(name, duration)
        used = max(0, min(100, int(raw.get("usedPercent", 0))))
        resets_raw = raw.get("resetsAt")
        windows.append(
            {
                "kind": kind,
                "label": label,
                "used_percent": used,
                "remaining_percent": 100 - used,
                "window_minutes": duration,
                "resets_at": int(resets_raw) if resets_raw is not None else None,
            }
        )

    reset_credits = result.get("rateLimitResetCredits")
    available_resets = None
    if isinstance(reset_credits, dict):
        count = reset_credits.get("availableCount")
        available_resets = int(count) if count is not None else None

    credits = bucket.get("credits")
    credit_payload = None
    if isinstance(credits, dict):
        credit_payload = {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }

    return {
        "available": True,
        "fetched_at": datetime.now(UTC).isoformat(),
        "limit_id": bucket.get("limitId") or "codex",
        "limit_name": bucket.get("limitName"),
        "plan_type": bucket.get("planType"),
        "limit_reached_type": bucket.get("rateLimitReachedType"),
        "windows": windows,
        "credits": credit_payload,
        "available_resets": available_resets,
    }


class CodexUsageMonitor:
    """Maintain a lightweight authenticated app-server connection for usage reads."""

    def __init__(self, configured_path: str = "", cache_ttl_seconds: float = 30.0) -> None:
        self.configured_path = configured_path
        self.cache_ttl_seconds = cache_ttl_seconds
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._cache_expires_at = 0.0

    async def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._cached is not None and now < self._cache_expires_at:
            return self._cached

        async with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None and now < self._cache_expires_at:
                return self._cached
            try:
                await self._ensure_started()
                response = await self._rpc("account/rateLimits/read", timeout=15)
                result = response.get("result")
                if not isinstance(result, dict):
                    raise CodexUsageError("Codex usage response was empty.")
                payload = normalize_rate_limits(result)
                self._cached = payload
                self._cache_expires_at = time.monotonic() + self.cache_ttl_seconds
                return payload
            except Exception as exc:
                logger.warning("Could not read Codex account usage: %s", exc)
                await self._stop_process()
                message = " ".join(str(exc).split())[:300]
                payload = {
                    "available": False,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "error": message or "Codex usage is unavailable.",
                    "windows": [],
                }
                self._cached = payload
                self._cache_expires_at = time.monotonic() + min(self.cache_ttl_seconds, 10.0)
                return payload

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_process()

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        executable = await prepare_codex_cli(self.configured_path)
        self._proc = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._rpc(
            "initialize",
            {
                "clientInfo": {"name": "ctf-agent-dashboard", "version": "2.0.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=15,
        )
        await self._send_notification("initialized")

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise CodexUsageError("Codex App Server is not running.")
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params:
            message["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            raise CodexUsageError(str(error.get("message") or "Codex request failed."))
        return response

    async def _send_notification(self, method: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise CodexUsageError("Codex App Server is not running.")
        self._proc.stdin.write((json.dumps({"method": method}) + "\n").encode())
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    raise CodexUsageError("Codex App Server closed the connection.")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message.get("method") == "account/rateLimits/updated":
                    self._cache_expires_at = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)

    async def _stop_process(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (OSError, ProcessLookupError):
                pass
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
