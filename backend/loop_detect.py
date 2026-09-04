"""Tool signature tracking for loop detection."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field


@dataclass
class LoopDetector:
    """Track recent tool call signatures to detect repetitive loops."""

    window: int = 12
    warn_threshold: int = 3
    break_threshold: int = 5
    _recent: deque[str] = field(init=False)
    _heavy_failures: dict[str, int] = field(init=False)
    _blocked_heavy: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.window)
        self._heavy_failures = {}
        self._blocked_heavy = set()

    @staticmethod
    def _heavy_family(tool_name: str, args: dict | str | None) -> str:
        """Return a semantic family for expensive environment boot commands."""
        raw = str(args.get("command", "")) if isinstance(args, dict) else str(args or "")
        if tool_name.casefold() in {"bash", "shell"} and re.search(
            r"(?:^|[\\/\s])qemu-system-[\w.-]+",
            raw,
            flags=re.IGNORECASE,
        ):
            return "qemu-system"
        return ""

    def check(self, tool_name: str, args: dict | str | None = None) -> str | None:
        """Check if the agent is stuck in a loop.

        Returns:
            None: no loop
            "warn": approaching loop threshold
            "break": exceeded loop threshold, should force-break
        """
        if args:
            raw = json.dumps(args, sort_keys=True) if isinstance(args, dict) else str(args)
            sig = f"{tool_name}:{raw[:500]}"
        else:
            sig = tool_name
        self._recent.append(sig)

        heavy_family = self._heavy_family(tool_name, args)
        if heavy_family in self._blocked_heavy:
            return "break"

        count = sum(1 for s in self._recent if s == sig)
        if count >= self.break_threshold:
            return "break"
        if count >= self.warn_threshold:
            return "warn"
        return None

    def record_result(
        self,
        tool_name: str,
        args: dict | str | None,
        result: object,
    ) -> str | None:
        """Learn whether a costly execution family produced any usable signal.

        Distinct emulator boots are legitimate during exploit development. They
        are blocked only after two consecutive empty/timeout-only outcomes.
        """
        family = self._heavy_family(tool_name, args)
        if not family:
            return None
        cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(result))
        ignored = (
            "qemu-system-",
            "terminating on signal",
            "command timed out",
            "(no output)",
            "[exit 124]",
            "[exit 137]",
        )
        meaningful = [
            line.strip()
            for line in cleaned.splitlines()
            if line.strip() and not any(marker in line.casefold() for marker in ignored)
        ]
        if meaningful:
            self._heavy_failures[family] = 0
            self._blocked_heavy.discard(family)
            return None
        failures = self._heavy_failures.get(family, 0) + 1
        self._heavy_failures[family] = failures
        if failures >= 2:
            self._blocked_heavy.add(family)
            return "warn"
        return None

    @property
    def last_sig(self) -> str:
        return self._recent[-1] if self._recent else ""

    def reset(self) -> None:
        self._recent.clear()
        self._heavy_failures.clear()
        self._blocked_heavy.clear()


LOOP_WARNING_MESSAGE = (
    "⚠️ **You are stuck in a loop** — you have repeated the same command or expensive "
    "environment boot family. STOP repeating it. Step back, reconsider your approach, "
    "and try a **completely different** technique or tool. "
    "If you were grepping/searching, try a Python script instead. "
    "If you were analyzing one aspect of the file, switch to another. "
    "What other angles haven't you explored?"
)
