"""Per-challenge message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Finding:
    model: str
    content: str
    target: str | None = None
    timestamp: float = field(default_factory=time.time)


MAX_FINDINGS = 200


@dataclass
class ChallengeMessageBus:
    """Append-only shared findings list with per-model cursors."""

    findings: list[Finding] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def post(self, model: str, content: str, target: str | None = None) -> None:
        """Post a finding, optionally visible to only one solver model spec."""
        async with self._lock:
            self.findings.append(Finding(model=model, content=content, target=target))
            if len(self.findings) > MAX_FINDINGS:
                trim = len(self.findings) - MAX_FINDINGS
                self.findings = self.findings[trim:]
                self.cursors = {k: max(0, v - trim) for k, v in self.cursors.items()}

    async def check(self, model: str) -> list[Finding]:
        """Get unread findings from other models. Advances the cursor."""
        async with self._lock:
            cursor = self.cursors.get(model, 0)
            unread = [
                finding
                for finding in self.findings[cursor:]
                if finding.model != model
                and (finding.target is None or finding.target == model)
            ]
            self.cursors[model] = len(self.findings)
            return unread

    async def broadcast(self, content: str, source: str = "coordinator") -> None:
        """Coordinator broadcasts a message to all solvers."""
        await self.post(source, content)

    def format_unread(self, findings: list[Finding]) -> str:
        """Format findings for injection into a solver prompt."""
        if not findings:
            return ""
        parts = [f"[{f.model}] {f.content}" for f in findings]
        return "**Findings from other agents:**\n\n" + "\n\n".join(parts)
