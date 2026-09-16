"""Best-effort server notifications for coordinator lifecycle events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_COLORS = {
    "challenge_added": 0x3498DB,
    "solve_completed": 0x2ECC71,
    "candidate_review": 0xF1C40F,
}


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
    return await notifier.send(
        event,
        challenge_name,
        description=description,
        fields=fields,
        dedupe_key=dedupe_key,
    )


def spoiler(value: str) -> str:
    """Render secrets compactly while neutralizing Discord's spoiler delimiter."""
    return f"||{value.strip().replace('||', '❘❘')[:1000]}||"
