"""Persistent, evidence-backed decision state for one CTF challenge.

The store deliberately captures verifiable decisions, not model chain-of-thought.
Only semantic events advance ``semantic_revision`` so runtime progress gating is
independent from incidental workspace file changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

EvidenceKind = Literal[
    "static",
    "dynamic",
    "network",
    "negative",
    "candidate",
    "reproduction",
]
HypothesisStatus = Literal["candidate", "active", "supported", "refuted", "blocked"]
SolvePhase = Literal["TRIAGE", "HYPOTHESIS_TEST", "EXPLOIT_BUILD", "VERIFY", "RECOVERY"]

STATE_VERSION = 1
SEMANTIC_EVENTS = {
    "NEW_EVIDENCE",
    "HYPOTHESIS_SUPPORTED",
    "HYPOTHESIS_REFUTED",
    "CONFLICT_RESOLVED",
    "REPRODUCER_PASSED",
    "NEW_PRIMITIVE",
    "CANDIDATE_GENERATED",
}
_STORE_LOCKS: dict[str, asyncio.Lock] = {}


def _shared_lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve()).casefold()
    lock = _STORE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _STORE_LOCKS[key] = lock
    return lock


@dataclass(frozen=True)
class Observation:
    id: str
    source_tool: str
    source_command: str
    output_hash: str
    observed_excerpt: str
    artifact_path: str | None = None


@dataclass
class Evidence:
    id: str
    kind: EvidenceKind
    claim: str
    source_tool: str
    source_command: str
    artifact_path: str | None
    output_hash: str
    observed_excerpt: str
    confidence: float
    source_agent: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class Hypothesis:
    id: str
    statement: str
    status: HypothesisStatus = "candidate"
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    expected_signal: str = ""
    next_experiment: str = ""
    pivot_if_absent: str = ""
    expected_seconds: float = 0.0
    expected_tokens: int = 0
    execution_risk: float = 0.0
    confidence: float = 0.0
    information_gain: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SolveState:
    version: int = STATE_VERSION
    phase: SolvePhase = "TRIAGE"
    confirmed_facts: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    active_hypothesis: str | None = None
    current_blocker: str = ""
    contradictions: list[str] = field(default_factory=list)
    attempted_routes: list[str] = field(default_factory=list)
    failed_experiments: list[str] = field(default_factory=list)
    next_experiment: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    semantic_revision: int = 0
    last_event: str = ""
    updated_at: float = field(default_factory=time.time)


def observation_from_result(
    observation_id: str,
    tool_name: str,
    args: dict[str, Any],
    result: object,
) -> Observation:
    """Create a bounded runtime receipt for an actual tool result."""
    raw = str(result)
    command = str(args.get("command", ""))[:4000]
    if not command:
        command = json.dumps(args, ensure_ascii=False, sort_keys=True)[:4000]
    artifact = args.get("path") or args.get("filename")
    return Observation(
        id=observation_id,
        source_tool=tool_name[:120],
        source_command=command,
        output_hash=hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        observed_excerpt=" ".join(raw.split())[:2000],
        artifact_path=str(artifact)[:2000] if artifact else None,
    )


class ReasoningStateStore:
    """Atomic JSON store shared by the lead and its delegates."""

    def __init__(self, shared_root: str | Path) -> None:
        self.path = Path(shared_root) / "reasoning" / "state.json"
        self._lock = _shared_lock(self.path)

    def load(self) -> SolveState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError, OSError, json.JSONDecodeError:
            return SolveState()
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            return SolveState()
        hypotheses = [
            Hypothesis(**item) for item in payload.get("hypotheses", []) if isinstance(item, dict)
        ]
        evidence = [
            Evidence(**item) for item in payload.get("evidence", []) if isinstance(item, dict)
        ]
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"hypotheses", "evidence"} and key in SolveState.__dataclass_fields__
        }
        return SolveState(**fields, hypotheses=hypotheses, evidence=evidence)

    def semantic_signature(self) -> str:
        state = self.load()
        return f"reasoning-v{state.version}:r{state.semantic_revision}"

    def _save(self, state: SolveState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _event(state: SolveState, event: str) -> None:
        if event in SEMANTIC_EVENTS:
            state.semantic_revision += 1
        state.last_event = event
        state.updated_at = time.time()

    async def record_evidence(
        self,
        *,
        kind: EvidenceKind,
        claim: str,
        confidence: float,
        observation: Observation,
        source_agent: str = "",
    ) -> Evidence:
        if kind not in {"static", "dynamic", "network", "negative", "candidate", "reproduction"}:
            raise ValueError(f"unsupported evidence kind: {kind}")
        claim = " ".join(claim.split())[:4000]
        if not claim:
            raise ValueError("evidence claim is required")
        evidence = Evidence(
            id=f"EV-{uuid.uuid4().hex[:12]}",
            kind=kind,
            claim=claim,
            source_tool=observation.source_tool,
            source_command=observation.source_command,
            artifact_path=observation.artifact_path,
            output_hash=observation.output_hash,
            observed_excerpt=observation.observed_excerpt,
            confidence=max(0.0, min(1.0, float(confidence))),
            source_agent=source_agent[:300],
        )
        async with self._lock:
            state = self.load()
            state.evidence.append(evidence)
            if kind == "reproduction":
                event = "REPRODUCER_PASSED"
                state.phase = "VERIFY"
            elif kind == "candidate":
                event = "CANDIDATE_GENERATED"
                state.phase = "VERIFY"
            else:
                event = "NEW_EVIDENCE"
            self._event(state, event)
            self._save(state)
        return evidence

    async def upsert_hypothesis(
        self,
        *,
        hypothesis_id: str | None,
        statement: str,
        status: HypothesisStatus = "candidate",
        evidence_for: list[str] | None = None,
        evidence_against: list[str] | None = None,
        expected_signal: str = "",
        next_experiment: str = "",
        pivot_if_absent: str = "",
        expected_seconds: float = 0.0,
        expected_tokens: int = 0,
        execution_risk: float = 0.0,
        confidence: float = 0.0,
        information_gain: float = 0.0,
    ) -> Hypothesis:
        if status not in {"candidate", "active", "supported", "refuted", "blocked"}:
            raise ValueError(f"unsupported hypothesis status: {status}")
        statement = " ".join(statement.split())[:4000]
        if not statement:
            raise ValueError("hypothesis statement is required")
        async with self._lock:
            state = self.load()
            known_evidence = {item.id for item in state.evidence}
            for evidence_id in [*(evidence_for or []), *(evidence_against or [])]:
                if evidence_id not in known_evidence:
                    raise ValueError(f"unknown evidence id: {evidence_id}")
            existing = next(
                (item for item in state.hypotheses if item.id == hypothesis_id),
                None,
            )
            merged_for = list(dict.fromkeys(evidence_for or (existing.evidence_for if existing else [])))
            merged_against = list(
                dict.fromkeys(evidence_against or (existing.evidence_against if existing else []))
            )
            if status == "supported" and not merged_for:
                raise ValueError("supported hypothesis requires supporting evidence")
            if status == "refuted" and not merged_against:
                raise ValueError("refuted hypothesis requires negative evidence")
            if existing is not None and existing.status == "refuted" and status in {
                "candidate",
                "active",
                "supported",
            }:
                raise ValueError(
                    "a refuted hypothesis cannot be reactivated; create a new hypothesis "
                    "with a materially revised statement"
                )
            unresolved = [
                item
                for item in state.hypotheses
                if item.status in {"candidate", "active", "blocked"}
            ]
            if (
                existing is None
                and status in {"candidate", "active", "blocked"}
                and len(unresolved) >= 4
            ):
                raise ValueError("solve state is limited to four unresolved hypotheses")
            previous_status = existing.status if existing else ""
            item = existing or Hypothesis(
                id=hypothesis_id or f"H-{uuid.uuid4().hex[:10]}", statement=statement
            )
            item.statement = statement
            item.status = status
            item.evidence_for = merged_for[:100]
            item.evidence_against = merged_against[:100]
            item.expected_signal = expected_signal[:2000]
            item.next_experiment = next_experiment[:2000]
            item.pivot_if_absent = pivot_if_absent[:2000]
            item.expected_seconds = max(0.0, float(expected_seconds))
            item.expected_tokens = max(0, int(expected_tokens))
            item.execution_risk = max(0.0, min(1.0, float(execution_risk)))
            item.confidence = max(0.0, min(1.0, float(confidence)))
            item.information_gain = max(0.0, min(1.0, float(information_gain)))
            item.updated_at = time.time()
            if existing is None:
                state.hypotheses.append(item)
            if status == "active":
                for other in state.hypotheses:
                    if other.id != item.id and other.status == "active":
                        other.status = "candidate"
                        other.updated_at = time.time()
                state.active_hypothesis = item.id
                state.phase = "HYPOTHESIS_TEST"
            elif state.active_hypothesis == item.id and status != "active":
                state.active_hypothesis = None
            if status == "supported":
                state.phase = "EXPLOIT_BUILD"
            elif status == "refuted":
                if not state.active_hypothesis:
                    state.phase = "HYPOTHESIS_TEST"
                route = f"[{item.id}] {item.statement}"[:3000]
                failure = (
                    f"[{item.id}] expected signal absent or contradicted; "
                    f"pivot: {item.pivot_if_absent or 'choose a materially different hypothesis'}"
                )[:3000]
                if route not in state.attempted_routes:
                    state.attempted_routes.append(route)
                if failure not in state.failed_experiments:
                    state.failed_experiments.append(failure)
                state.attempted_routes = state.attempted_routes[-100:]
                state.failed_experiments = state.failed_experiments[-100:]
            state.next_experiment = (
                item.pivot_if_absent if status == "refuted" and item.pivot_if_absent
                else item.next_experiment
            )
            event = (
                "HYPOTHESIS_SUPPORTED"
                if status == "supported" and previous_status != status
                else "HYPOTHESIS_REFUTED"
                if status == "refuted" and previous_status != status
                else "HYPOTHESIS_UPDATED"
            )
            self._event(state, event)
            self._save(state)
            return item

    async def update_context(
        self,
        *,
        blocker: str | None = None,
        next_experiment: str | None = None,
        failed_experiment: str = "",
        attempted_route: str = "",
        confirmed_fact: str = "",
        contradiction: str = "",
        resolved_contradiction: str = "",
        evidence_id: str = "",
    ) -> SolveState:
        async with self._lock:
            state = self.load()
            known_evidence = {item.id for item in state.evidence}
            if (confirmed_fact or resolved_contradiction) and evidence_id not in known_evidence:
                raise ValueError("confirmed facts and conflict resolution require known evidence")
            if blocker is not None:
                state.current_blocker = blocker[:3000]
            if next_experiment is not None:
                state.next_experiment = next_experiment[:3000]
            if failed_experiment:
                state.failed_experiments.append(failed_experiment[:3000])
                state.failed_experiments = state.failed_experiments[-100:]
            if attempted_route:
                state.attempted_routes.append(attempted_route[:3000])
                state.attempted_routes = state.attempted_routes[-100:]
            if confirmed_fact:
                fact = f"[{evidence_id}] {confirmed_fact[:3000]}"
                if fact not in state.confirmed_facts:
                    state.confirmed_facts.append(fact)
            if contradiction and contradiction not in state.contradictions:
                state.contradictions.append(contradiction[:3000])
            event = "CONTEXT_UPDATED"
            if resolved_contradiction:
                state.contradictions = [
                    item for item in state.contradictions if item != resolved_contradiction
                ]
                event = "CONFLICT_RESOLVED"
            self._event(state, event)
            self._save(state)
            return state

    def format_state(self) -> str:
        state = self.load()
        payload = asdict(state)
        payload["ranked_hypotheses"] = [
            {
                "id": item.id,
                "priority": round(self.hypothesis_priority(item), 8),
                "status": item.status,
                "next_experiment": item.next_experiment,
            }
            for item in sorted(
                state.hypotheses,
                key=self.hypothesis_priority,
                reverse=True,
            )
            if item.status not in {"refuted", "supported"}
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def hypothesis_priority(hypothesis: Hypothesis) -> float:
        """Rank expected solve/information value against time, tokens, and risk."""
        value = hypothesis.confidence + 0.5 * hypothesis.information_gain
        cost = (
            max(1.0, hypothesis.expected_seconds)
            + hypothesis.expected_tokens / 1000.0
            + 60.0 * hypothesis.execution_risk
        )
        return value / cost
