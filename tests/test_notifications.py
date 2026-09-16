from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.notifications import DiscordWebhookNotifier, notify_discord, spoiler


class _Response:
    def raise_for_status(self) -> None:
        return None


class _Client:
    post = AsyncMock(return_value=_Response())

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


class DiscordWebhookNotifierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _Client.post.reset_mock()
        _Client.post.side_effect = None
        _Client.post.return_value = _Response()

    async def test_disabled_notifier_is_a_noop(self) -> None:
        notifier = DiscordWebhookNotifier("")
        self.assertFalse(
            await notifier.send("challenge_added", "intro", description="new")
        )

    async def test_payload_disables_mentions_and_deduplicates(self) -> None:
        notifier = DiscordWebhookNotifier("https://discord.com/api/webhooks/1/token")
        with patch("backend.notifications.httpx.AsyncClient", _Client):
            first = await notifier.send(
                "candidate_review",
                "intro",
                description="review",
                fields=[("후보", spoiler("TEAM{guess}"))],
                dedupe_key="candidate:intro:TEAM{guess}",
            )
            second = await notifier.send(
                "candidate_review",
                "intro",
                description="review",
                dedupe_key="candidate:intro:TEAM{guess}",
            )

        self.assertTrue(first)
        self.assertFalse(second)
        _Client.post.assert_awaited_once()
        payload = _Client.post.await_args.kwargs["json"]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(payload["embeds"][0]["fields"][0]["value"], "||TEAM{guess}||")

    async def test_failure_is_retryable_and_does_not_escape(self) -> None:
        notifier = DiscordWebhookNotifier("https://discord.com/api/webhooks/1/token")
        deps = SimpleNamespace(notifier=notifier)
        _Client.post.side_effect = [RuntimeError("offline"), _Response()]
        with patch("backend.notifications.httpx.AsyncClient", _Client):
            self.assertFalse(
                await notify_discord(
                    deps,
                    "solve_completed",
                    "intro",
                    description="done",
                )
            )
            self.assertTrue(
                await notify_discord(
                    deps,
                    "solve_completed",
                    "intro",
                    description="done",
                )
            )
        self.assertEqual(_Client.post.await_count, 2)

    async def test_writeup_alert_includes_problem_and_usage_context(self) -> None:
        notifier = DiscordWebhookNotifier("https://discord.com/api/webhooks/1/token")
        deps = SimpleNamespace(
            notifier=notifier,
            challenge_metas={
                "intro": SimpleNamespace(category="Web", value=500, solves=3),
            },
            cost_tracker=SimpleNamespace(
                by_agent={
                    "intro/codex/gpt-5.6-sol": SimpleNamespace(
                        model_name="gpt-5.6-sol",
                        cost_usd=0.12345,
                        duration_seconds=65,
                    ),
                },
            ),
        )
        with patch("backend.notifications.httpx.AsyncClient", _Client):
            delivered = await notify_discord(
                deps,
                "writeup_completed",
                "intro",
                description="done",
                fields=[("문서", "_shared/writeup/WRITEUP.md")],
            )

        self.assertTrue(delivered)
        payload = _Client.post.await_args.kwargs["json"]
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "라이트업 생성 완료")
        self.assertIn("timestamp", embed)
        self.assertEqual(embed["footer"], {"text": "CTF Agent · writeup_completed"})
        fields = {item["name"]: item["value"] for item in embed["fields"]}
        self.assertEqual(fields["카테고리"], "Web")
        self.assertEqual(fields["점수"], "500")
        self.assertEqual(fields["풀이 수"], "3")
        self.assertEqual(fields["사용 모델"], "gpt-5.6-sol")
        self.assertEqual(fields["누적 비용"], "$0.1235")
        self.assertEqual(fields["누적 실행 시간"], "1분 5초")
