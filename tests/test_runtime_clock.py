"""Elapsed time stays independent of dashboard requests and browser sessions."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agents.swarm import ChallengeSwarm
from backend.runtime_clock import RuntimeClock


class RuntimeClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_elapsed_without_readers_and_frozen_after_stop(self):
        clock = RuntimeClock()
        self.assertEqual(clock.elapsed_seconds, 0)
        with patch("backend.runtime_clock.time.monotonic", return_value=0):
            clock.start()
        with patch("backend.runtime_clock.time.monotonic", return_value=120):
            self.assertEqual(clock.elapsed_seconds, 120)
            clock.stop()
        with patch("backend.runtime_clock.time.monotonic", return_value=900):
            self.assertEqual(clock.elapsed_seconds, 120)
            clock.stop()
            clock.start()
            self.assertEqual(clock.elapsed_seconds, 0)

    async def test_lifecycle_freezes_on_completion_error_and_cancellation(self):
        for error in (None, RuntimeError("failed"), asyncio.CancelledError()):
            with self.subTest(error=error):
                owner = SimpleNamespace(
                    runtime_clock=RuntimeClock(),
                    agent_clocks={},
                    _run_timed=AsyncMock(return_value=None, side_effect=error),
                    _run_solver_timed=AsyncMock(return_value=None, side_effect=error),
                )
                for call in (ChallengeSwarm.run(owner), ChallengeSwarm._run_solver(owner, "test")):
                    with patch("backend.runtime_clock.time.monotonic", side_effect=[10, 25]):
                        if error is None:
                            await call
                        else:
                            with self.assertRaises(type(error)):
                                await call
                self.assertEqual(owner.runtime_clock.elapsed_seconds, 15)
                self.assertEqual(owner.agent_clocks["test"].elapsed_seconds, 15)
