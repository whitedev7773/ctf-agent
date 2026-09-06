"""Codex account usage normalization and caching tests."""

from __future__ import annotations

import unittest
from typing import Any

from backend.codex_usage import CodexUsageMonitor, normalize_rate_limits

SAMPLE_RESPONSE: dict[str, Any] = {
    "rateLimits": {"primary": None, "secondary": None},
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "planType": "plus",
            "primary": {
                "usedPercent": 34,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_000,
            },
            "secondary": {
                "usedPercent": 71,
                "windowDurationMins": 10_080,
                "resetsAt": 1_800_500_000,
            },
            "credits": {"hasCredits": True, "unlimited": False, "balance": "4.50"},
        }
    },
    "rateLimitResetCredits": {"availableCount": 2, "credits": None},
}


class CodexUsageNormalizationTests(unittest.TestCase):
    def test_normalizes_five_hour_and_weekly_windows(self) -> None:
        payload = normalize_rate_limits(SAMPLE_RESPONSE)

        self.assertTrue(payload["available"])
        self.assertEqual(payload["plan_type"], "plus")
        self.assertEqual(payload["available_resets"], 2)
        self.assertEqual(payload["credits"]["balance"], "4.50")
        self.assertEqual(
            [(window["kind"], window["used_percent"]) for window in payload["windows"]],
            [("five_hour", 34), ("weekly", 71)],
        )
        self.assertEqual(payload["windows"][0]["remaining_percent"], 66)
        self.assertEqual(payload["windows"][1]["resets_at"], 1_800_500_000)

    def test_falls_back_to_legacy_single_bucket(self) -> None:
        payload = normalize_rate_limits(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 12, "windowDurationMins": 60},
                }
            }
        )

        self.assertEqual(payload["windows"][0]["label"], "1시간")
        self.assertIsNone(payload["windows"][0]["resets_at"])


class FakeCodexUsageMonitor(CodexUsageMonitor):
    def __init__(self) -> None:
        super().__init__(cache_ttl_seconds=60)
        self.read_count = 0

    async def _ensure_started(self) -> None:
        return None

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        self.assert_request(method, params, timeout)
        self.read_count += 1
        return {"result": SAMPLE_RESPONSE}

    @staticmethod
    def assert_request(method: str, params: dict[str, Any] | None, timeout: float) -> None:
        if method != "account/rateLimits/read" or params is not None or timeout != 15:
            raise AssertionError("unexpected Codex usage request")


class CodexUsageMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_cached_and_force_refreshes(self) -> None:
        monitor = FakeCodexUsageMonitor()

        first = await monitor.snapshot()
        second = await monitor.snapshot()
        forced = await monitor.snapshot(force=True)

        self.assertTrue(first["available"])
        self.assertIs(first, second)
        self.assertTrue(forced["available"])
        self.assertEqual(monitor.read_count, 2)

