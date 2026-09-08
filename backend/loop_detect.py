"""Tool signature tracking for loop detection."""

from __future__ import annotations

import hashlib
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
    _semantic_recent: deque[str] = field(init=False)
    _pending_semantic: str = field(init=False, default="")
    duplicate_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._recent = deque(maxlen=self.window)
        self._heavy_failures = {}
        self._blocked_heavy = set()
        self._semantic_recent = deque(maxlen=max(self.window * 2, 24))

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

    @staticmethod
    def _action_family(tool_name: str, args: dict | str | None) -> str:
        name = tool_name.casefold()
        raw = str(args.get("command", "")) if isinstance(args, dict) else str(args or "")
        command = raw.casefold()
        if name in {"session_open", "session_send", "session_read", "session_interrupt"}:
            return "interactive-session"
        if name not in {"bash", "shell"}:
            return name
        if re.search(r"\b(?:rg|grep|strings)\b", command):
            return "static-string-search"
        if re.search(r"\b(?:objdump|readelf|nm|file|checksec)\b", command):
            return "static-binary-inspection"
        if re.search(r"\b(?:gdb|lldb|rr)\b", command):
            return "debugger"
        if re.search(r"\b(?:nc|ncat|netcat|curl|wget|httpx?)\b", command):
            return "network-probe"
        if re.search(r"\b(?:python|python3|sage|ruby|perl)\b", command):
            return "script-execution"
        heavy = LoopDetector._heavy_family(tool_name, args)
        return heavy or "shell-command"

    @staticmethod
    def _target(args: dict | str | None) -> str:
        if not isinstance(args, dict):
            return ""
        for key in ("target", "path", "filename", "url", "session_id"):
            value = str(args.get(key, "")).strip()
            if value:
                return value[:200].casefold()
        command = str(args.get("command", ""))
        paths = re.findall(r"(?:https?://\S+|/[\w./-]+|[\w.-]+\.(?:bin|elf|py|so|txt))", command)
        return paths[-1][:200].casefold() if paths else ""

    def check(
        self,
        tool_name: str,
        args: dict | str | None = None,
        hypothesis_id: str | None = None,
    ) -> str | None:
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

        family = self._action_family(tool_name, args)
        target = self._target(args)
        self._pending_semantic = f"{hypothesis_id or '-'}|{family}|{target}"
        repeated_semantic = [
            item for item in self._semantic_recent if item.startswith(self._pending_semantic + "|")
        ]
        if len(repeated_semantic) >= self.break_threshold - 1:
            hashes = {item.rsplit("|", 1)[-1] for item in repeated_semantic[-self.break_threshold :]}
            if len(hashes) == 1:
                self.duplicate_count += 1
                return "break"
        if len(repeated_semantic) >= self.warn_threshold - 1:
            hashes = {item.rsplit("|", 1)[-1] for item in repeated_semantic[-self.warn_threshold :]}
            if len(hashes) == 1:
                self.duplicate_count += 1
                return "warn"

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
        hypothesis_id: str | None = None,
    ) -> str | None:
        """Learn whether a costly execution family produced any usable signal.

        Distinct emulator boots are legitimate during exploit development. They
        are blocked only after two consecutive empty/timeout-only outcomes.
        """
        output_hash = hashlib.sha256(
            str(result).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        semantic_key = (
            f"{hypothesis_id or '-'}|{self._action_family(tool_name, args)}|"
            f"{self._target(args)}|{output_hash}"
        )
        self._semantic_recent.append(semantic_key)
        semantic_count = sum(1 for item in self._semantic_recent if item == semantic_key)

        family = self._heavy_family(tool_name, args)
        if not family:
            return "warn" if semantic_count >= self.warn_threshold else None
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
            return "warn" if semantic_count >= self.warn_threshold else None
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
        self._semantic_recent.clear()
        self._pending_semantic = ""
        self.duplicate_count = 0

    def reset_transient(self) -> None:
        """Clear exact-call noise while preserving failed semantic experiments."""
        self._recent.clear()
        self._pending_semantic = ""


LOOP_WARNING_MESSAGE = (
    "⚠️ **You are stuck in a loop** — you have repeated the same command or expensive "
    "environment boot family. STOP repeating it. Step back, reconsider your approach, "
    "and try a **completely different** technique or tool. "
    "If you were grepping/searching, try a Python script instead. "
    "If you were analyzing one aspect of the file, switch to another. "
    "What other angles haven't you explored?"
)
