"""Per-challenge message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Finding:
    model: str
    content: str
    target: str | None = None
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: f"F-{uuid.uuid4().hex[:12]}")
    kind: Literal["note", "hypothesis", "evidence", "conflict", "reproduction"] = "note"
    claim: str = ""
    confidence: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    hypothesis_id: str | None = None
    status: str = "active"
    supersedes: str | None = None
    tags: list[str] = field(default_factory=list)
    urgency: Literal["normal", "urgent"] = "normal"

    def __post_init__(self) -> None:
        self.content = self.content[:6000]
        self.claim = (self.claim or self.content)[:4000]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.evidence_ids = list(dict.fromkeys(self.evidence_ids))[:50]
        self.tags = list(dict.fromkeys(tag.casefold() for tag in self.tags if tag))[:30]


MAX_FINDINGS = 200


@dataclass
class ChallengeMessageBus:
    """Append-only shared findings list with per-model cursors."""

    findings: list[Finding] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    delivered: dict[str, set[str]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def post(
        self,
        model: str,
        content: str,
        target: str | None = None,
        **metadata: Any,
    ) -> None:
        """Post a finding, optionally visible to only one solver model spec."""
        async with self._lock:
            self.findings.append(Finding(model=model, content=content, target=target, **metadata))
            if len(self.findings) > MAX_FINDINGS:
                trim = len(self.findings) - MAX_FINDINGS
                self.findings = self.findings[trim:]
                self.cursors = {k: max(0, v - trim) for k, v in self.cursors.items()}
                retained = {item.id for item in self.findings}
                self.delivered = {key: ids & retained for key, ids in self.delivered.items()}

    async def check(self, model: str) -> list[Finding]:
        """Get unread findings from other models. Advances the cursor."""
        async with self._lock:
            cursor = self.cursors.get(model, 0)
            unread = [
                finding
                for finding in self.findings[cursor:]
                if finding.model != model and (finding.target is None or finding.target == model)
            ]
            self.cursors[model] = len(self.findings)
            self.delivered.setdefault(model, set()).update(item.id for item in unread)
            return unread

    async def sync(
        self,
        model: str,
        *,
        hypothesis_id: str | None = None,
        blocker: str = "",
        tags: list[str] | None = None,
        limit: int = 6,
    ) -> list[Finding]:
        """Return only unread findings relevant to the current decision state."""
        query_tags = {tag.casefold() for tag in (tags or []) if tag}
        query_tags.update(token.casefold() for token in blocker.split() if len(token) >= 4)
        async with self._lock:
            already_delivered = self.delivered.setdefault(model, set())
            candidates = [
                finding
                for finding in self.findings
                if finding.id not in already_delivered
                if finding.model != model and (finding.target is None or finding.target == model)
            ]
        relevant: list[Finding] = []
        for finding in candidates:
            same_hypothesis = bool(hypothesis_id and finding.hypothesis_id == hypothesis_id)
            finding_terms = set(finding.tags)
            finding_terms.update(
                token.casefold() for token in finding.claim.split() if len(token) >= 4
            )
            tag_overlap = bool(query_tags & finding_terms)
            verified_urgent = (
                finding.urgency == "urgent"
                and finding.kind in {"evidence", "conflict", "reproduction"}
                and bool(finding.evidence_ids)
            )
            targeted = finding.target == model
            if same_hypothesis or tag_overlap or verified_urgent or targeted:
                relevant.append(finding)
        relevant.sort(
            key=lambda item: (
                item.urgency == "urgent",
                bool(item.evidence_ids),
                item.confidence,
                item.timestamp,
            ),
            reverse=True,
        )
        selected = relevant[: max(1, min(limit, 20))]
        async with self._lock:
            self.delivered.setdefault(model, set()).update(item.id for item in selected)
        return selected

    async def broadcast(self, content: str, source: str = "coordinator") -> None:
        """Coordinator broadcasts a message to all solvers."""
        await self.post(source, content)

    def format_unread(self, findings: list[Finding]) -> str:
        """Format findings for injection into a solver prompt."""
        if not findings:
            return ""
        parts = [
            f"[{f.model}] {f.kind.upper()} {f.claim}"
            + (f" (evidence: {', '.join(f.evidence_ids)})" if f.evidence_ids else "")
            for f in findings
        ]
        return "**Findings from other agents:**\n\n" + "\n\n".join(parts)
