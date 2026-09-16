"""Best-effort server notifications for coordinator lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_COLORS = {
    "challenge_added": 0x3498DB,
    "solve_completed": 0x2ECC71,
    "candidate_review": 0xF1C40F,
    "writeup_started": 0x3498DB,
    "writeup_completed": 0x2ECC71,
    "writeup_needs_attention": 0xF1C40F,
    "writeup_failed": 0xE74C3C,
}


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}시간 {minutes}분 {seconds}초"
    if minutes:
        return f"{minutes}분 {seconds}초"
    return f"{seconds}초"


def _challenge_context_fields(deps: object, challenge_name: str) -> list[tuple[str, str]]:
    """Build useful problem and accounting context without coupling the notifier to deps types."""
    fields: list[tuple[str, str]] = []
    metas = getattr(deps, "challenge_metas", {})
    meta = metas.get(challenge_name) if isinstance(metas, dict) else None
    if meta is not None:
        category = str(getattr(meta, "category", "") or "").strip()
        if category:
            fields.append(("카테고리", category))
        value = int(getattr(meta, "value", 0) or 0)
        fields.append(("점수", str(value)))
        solves = int(getattr(meta, "solves", 0) or 0)
        if solves:
            fields.append(("풀이 수", str(solves)))

    tracker = getattr(deps, "cost_tracker", None)
    usages = getattr(tracker, "by_agent", {})
    if isinstance(usages, dict):
        prefix = f"{challenge_name}/"
        matching = [usage for name, usage in usages.items() if str(name).startswith(prefix)]
        if matching:
            models = sorted({str(getattr(usage, "model_name", "") or "") for usage in matching})
            models = [model for model in models if model]
            if models:
                fields.append(("사용 모델", ", ".join(models)))
            cost = sum(float(getattr(usage, "cost_usd", 0) or 0) for usage in matching)
            duration = sum(float(getattr(usage, "duration_seconds", 0) or 0) for usage in matching)
            fields.append(("누적 비용", f"${cost:.4f}"))
            fields.append(("누적 실행 시간", _format_duration(duration)))
    return fields


class DiscordWebhookNotifier:
    """Send deduplicated Discord webhook embeds without affecting the solve path."""

    def __init__(self, webhook_url: str = "", *, timeout_seconds: float = 10.0) -> None:
        self.webhook_url = webhook_url.strip()
        self.timeout_seconds = timeout_seconds
        self._sent_keys: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def send(
        self,
        event: str,
        challenge_name: str,
        *,
        description: str,
        fields: list[tuple[str, str]] | None = None,
        dedupe_key: str = "",
    ) -> bool:
        if not self.enabled:
            return False

        key = dedupe_key or f"{event}:{challenge_name}"
        async with self._lock:
            if key in self._sent_keys:
                return False
            self._sent_keys.add(key)

        titles = {
            "challenge_added": "새 문제 추가",
            "solve_completed": "풀이 완료",
            "candidate_review": "Flag 후보 검증 요청",
            "writeup_started": "라이트업 생성 시작",
            "writeup_completed": "라이트업 생성 완료",
            "writeup_needs_attention": "라이트업 보완 필요",
            "writeup_failed": "라이트업 생성 실패",
        }
        embed: dict[str, Any] = {
            "title": titles.get(event, "CTF Agent 알림"),
            "description": description[:4096],
            "color": _COLORS.get(event, 0x95A5A6),
            "fields": [
                {"name": name[:256], "value": value[:1024], "inline": True}
                for name, value in (fields or [])
                if name and value
            ][:25],
            "footer": {"text": f"CTF Agent · {event}"},
            "timestamp": datetime.now(UTC).isoformat(),
        }
        payload = {
            "username": "CTF Agent",
            "allowed_mentions": {"parse": []},
            "embeds": [embed],
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.webhook_url, json=payload)
                response.raise_for_status()
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Exception messages from HTTP clients can contain the secret webhook URL.
            logger.warning(
                "Discord webhook notification failed for %s (%s)",
                key,
                type(exc).__name__,
            )
            async with self._lock:
                self._sent_keys.discard(key)
            return False


async def notify_discord(
    deps: object,
    event: str,
    challenge_name: str,
    *,
    description: str,
    fields: list[tuple[str, str]] | None = None,
    dedupe_key: str = "",
) -> bool:
    """Use a coordinator's optional notifier, including lightweight test deps."""
    notifier = getattr(deps, "notifier", None)
    if not isinstance(notifier, DiscordWebhookNotifier):
        return False
    enriched_fields = list(fields or [])
    existing_names = {name for name, _value in enriched_fields}
    enriched_fields.extend(
        (name, value)
        for name, value in _challenge_context_fields(deps, challenge_name)
        if name not in existing_names
        and not (name == "카테고리" and "분류" in existing_names)
    )
    return await notifier.send(
        event,
        challenge_name,
        description=description,
        fields=enriched_fields,
        dedupe_key=dedupe_key,
    )


def spoiler(value: str) -> str:
    """Render secrets compactly while neutralizing Discord's spoiler delimiter."""
    return f"||{value.strip().replace('||', '❘❘')[:1000]}||"
