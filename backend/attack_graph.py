"""Persistent attack-topology state for one CTF challenge.

The attack graph complements :mod:`backend.reasoning_state`: the reasoning
store records what is known, while this module records what the solve still
requires.  All mutations are runtime validated and atomically persisted in the
challenge shared workspace.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Literal

from backend.reasoning_state import ReasoningStateStore

NodeKind = Literal[
    "goal",
    "primitive",
    "precondition",
    "hypothesis",
    "experiment",
    "artifact",
]
NodeStatus = Literal["unknown", "ready", "active", "satisfied", "refuted", "blocked"]
EdgeKind = Literal["requires", "produces", "tests", "supports", "refutes", "conflicts"]

GRAPH_VERSION = 1
NODE_KINDS = {"goal", "primitive", "precondition", "hypothesis", "experiment", "artifact"}
NODE_STATUSES = {"unknown", "ready", "active", "satisfied", "refuted", "blocked"}
EDGE_KINDS = {"requires", "produces", "tests", "supports", "refutes", "conflicts"}
_PREFIXES = {
    "goal": "G",
    "primitive": "P",
    "precondition": "C",
    "hypothesis": "H",
    "experiment": "E",
    "artifact": "A",
}
_STORE_LOCKS: dict[str, asyncio.Lock] = {}


def _shared_lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve()).casefold()
    lock = _STORE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _STORE_LOCKS[key] = lock
    return lock


@dataclass
class AttackNode:
    id: str
    kind: NodeKind
    title: str
    description: str = ""
    status: NodeStatus = "unknown"
    confidence: float = 0.0
    information_gain: float = 0.0
    expected_seconds: float = 0.0
    expected_tokens: int = 0
    execution_risk: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    owner_agent: str = ""
    delegateable: bool = False
    independent: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class AttackEdge:
    source: str
    target: str
    kind: EdgeKind


@dataclass
class AttackGraphState:
    version: int = GRAPH_VERSION
    revision: int = 0
    root_goal: str | None = None
    nodes: list[AttackNode] = field(default_factory=list)
    edges: list[AttackEdge] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


class AttackGraphStore:
    """Atomic JSON graph store with evidence and dependency invariants."""

    def __init__(
        self,
        shared_root: str | Path,
        reasoning_store: ReasoningStateStore | None = None,
    ) -> None:
        self.shared_root = Path(shared_root)
        self.path = self.shared_root / "graph" / "attack-graph.json"
        self.lease_path = self.shared_root / "graph" / "task-leases.json"
        self.reasoning_store = reasoning_store or ReasoningStateStore(self.shared_root)
        self._lock = _shared_lock(self.path)

    def load(self) -> AttackGraphState:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return AttackGraphState()
        if not isinstance(payload, dict) or payload.get("version") != GRAPH_VERSION:
            return AttackGraphState()
        try:
            nodes = [AttackNode(**item) for item in payload.get("nodes", [])]
            edges = [AttackEdge(**item) for item in payload.get("edges", [])]
        except (TypeError, ValueError):
            return AttackGraphState()
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"nodes", "edges"} and key in AttackGraphState.__dataclass_fields__
        }
        return AttackGraphState(**fields, nodes=nodes, edges=edges)

    def semantic_signature(self) -> str:
        state = self.load()
        return f"attack-graph-v{state.version}:r{state.revision}"

    def _save(self, state: AttackGraphState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _touch(state: AttackGraphState) -> None:
        state.revision += 1
        state.updated_at = time.time()

    @staticmethod
    def _node(state: AttackGraphState, node_id: str) -> AttackNode:
        node = next((item for item in state.nodes if item.id == node_id), None)
        if node is None:
            raise ValueError(f"unknown attack graph node: {node_id}")
        return node

    @staticmethod
    def _next_id(state: AttackGraphState, kind: NodeKind) -> str:
        prefix = _PREFIXES[kind]
        used = {
            int(item.id.removeprefix(f"{prefix}-"))
            for item in state.nodes
            if item.id.startswith(f"{prefix}-")
            and item.id.removeprefix(f"{prefix}-").isdigit()
        }
        candidate = 1
        while candidate in used:
            candidate += 1
        return f"{prefix}-{candidate}"

    async def add_node(
        self,
        *,
        kind: NodeKind,
        title: str,
        description: str = "",
        confidence: float = 0.0,
        information_gain: float = 0.0,
        expected_seconds: float = 0.0,
        expected_tokens: int = 0,
        execution_risk: float = 0.0,
        tags: list[str] | None = None,
        delegateable: bool = False,
        independent: bool = False,
    ) -> AttackNode:
        if kind not in NODE_KINDS:
            raise ValueError(f"unsupported node kind: {kind}")
        clean_title = " ".join(title.split())[:500]
        if not clean_title:
            raise ValueError("node title is required")
        async with self._lock:
            state = self.load()
            now = time.time()
            node = AttackNode(
                id=self._next_id(state, kind),
                kind=kind,
                title=clean_title,
                description=" ".join(description.split())[:4000],
                confidence=max(0.0, min(1.0, float(confidence))),
                information_gain=max(0.0, min(1.0, float(information_gain))),
                expected_seconds=max(0.0, float(expected_seconds)),
                expected_tokens=max(0, int(expected_tokens)),
                execution_risk=max(0.0, min(1.0, float(execution_risk))),
                tags=list(dict.fromkeys(str(tag).casefold()[:100] for tag in (tags or []) if tag))[
                    :30
                ],
                delegateable=bool(delegateable),
                independent=bool(independent),
                created_at=now,
                updated_at=now,
            )
            state.nodes.append(node)
            if kind == "goal" and state.root_goal is None:
                state.root_goal = node.id
            self._refresh_ready(state)
            self._touch(state)
            self._save(state)
            return replace(node)

    async def add_edge(self, source: str, target: str, kind: EdgeKind) -> AttackEdge:
        if kind not in EDGE_KINDS:
            raise ValueError(f"unsupported edge kind: {kind}")
        if source == target:
            raise ValueError("self edges are not allowed")
        async with self._lock:
            state = self.load()
            self._node(state, source)
            self._node(state, target)
            edge = AttackEdge(source=source, target=target, kind=kind)
            if edge in state.edges:
                return edge
            if kind == "requires" and self._reachable(state, target, source):
                raise ValueError("requires edge would create a dependency cycle")
            state.edges.append(edge)
            self._refresh_ready(state)
            self._touch(state)
            self._save(state)
            return edge

    async def transition(
        self,
        node_id: str,
        status: NodeStatus,
        *,
        evidence_ids: list[str] | None = None,
        owner_agent: str = "",
    ) -> AttackNode:
        if status not in NODE_STATUSES:
            raise ValueError(f"unsupported node status: {status}")
        async with self._lock:
            state = self.load()
            node = self._node(state, node_id)
            merged_evidence = list(dict.fromkeys([*node.evidence_ids, *(evidence_ids or [])]))[:100]
            evidence = {item.id: item for item in self.reasoning_store.load().evidence}
            unknown = [item for item in merged_evidence if item not in evidence]
            if unknown:
                raise ValueError(f"unknown evidence id: {unknown[0]}")
            if (
                status == "satisfied"
                and node.kind in {"primitive", "experiment"}
                and not merged_evidence
            ):
                raise ValueError(f"satisfied {node.kind} requires evidence")
            if (
                status == "refuted"
                and node.kind == "hypothesis"
                and not any(evidence[item].kind == "negative" for item in merged_evidence)
            ):
                raise ValueError("refuted hypothesis requires negative evidence")
            clean_owner = owner_agent.strip()[:300]
            if status == "active":
                if not clean_owner or not self._has_lease(node_id, clean_owner):
                    raise ValueError("active node requires a live task lease for its owner")
                node.owner_agent = clean_owner
            elif status != "active":
                node.owner_agent = ""
            node.status = status
            node.evidence_ids = merged_evidence
            node.updated_at = time.time()
            self._refresh_ready(state)
            self._touch(state)
            self._save(state)
            return replace(node)

    def ready_nodes(self, state: AttackGraphState | None = None) -> list[AttackNode]:
        graph = state or self.load()
        statuses = {item.id: item.status for item in graph.nodes}
        ready = []
        for node in graph.nodes:
            if node.status != "ready":
                continue
            dependencies = [
                edge.target
                for edge in graph.edges
                if edge.kind == "requires" and edge.source == node.id
            ]
            if all(statuses.get(item) == "satisfied" for item in dependencies):
                ready.append(replace(node))
        return ready

    def blocked_nodes(self, state: AttackGraphState | None = None) -> list[AttackNode]:
        graph = state or self.load()
        return [replace(item) for item in graph.nodes if item.status == "blocked"]

    def descendants(self, node_id: str, state: AttackGraphState | None = None) -> set[str]:
        graph = state or self.load()
        self._node(graph, node_id)
        found: set[str] = set()
        pending = [node_id]
        while pending:
            current = pending.pop()
            for edge in graph.edges:
                if edge.source == current and edge.target not in found:
                    found.add(edge.target)
                    pending.append(edge.target)
        found.discard(node_id)
        return found

    def critical_path(self, state: AttackGraphState | None = None) -> list[AttackNode]:
        graph = state or self.load()
        if not graph.root_goal:
            return []
        by_id = {item.id: item for item in graph.nodes}

        def visit(node_id: str, seen: set[str]) -> list[str]:
            if node_id in seen or node_id not in by_id:
                return []
            dependencies = [
                edge.target
                for edge in graph.edges
                if edge.kind == "requires"
                and edge.source == node_id
                and edge.target in by_id
                and by_id[edge.target].status != "satisfied"
            ]
            if not dependencies:
                return [node_id]
            candidates = [visit(item, seen | {node_id}) for item in dependencies]
            candidates = [item for item in candidates if item]
            return [node_id, *max(candidates, key=len)] if candidates else [node_id]

        return [replace(by_id[item]) for item in visit(graph.root_goal, set())]

    def summary(self) -> dict[str, object]:
        state = self.load()
        path = self.critical_path(state)
        ready = self.ready_nodes(state)
        blocked = self.blocked_nodes(state)
        unresolved_path = [item for item in path if item.status != "satisfied"]
        return {
            "revision": state.revision,
            "root_goal": state.root_goal,
            "critical_path": [item.id for item in path],
            "critical_blocker": unresolved_path[-1].id if unresolved_path else None,
            "ready": [item.id for item in ready],
            "blocked": [item.id for item in blocked],
            "satisfied": sum(item.status == "satisfied" for item in state.nodes),
        }

    def format_graph(self) -> str:
        state = self.load()
        payload = asdict(state)
        payload["summary"] = self.summary()
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _refresh_ready(self, state: AttackGraphState) -> None:
        statuses = {item.id: item.status for item in state.nodes}
        for node in state.nodes:
            if node.status not in {"unknown", "ready"}:
                continue
            dependencies = [
                edge.target
                for edge in state.edges
                if edge.kind == "requires" and edge.source == node.id
            ]
            node.status = (
                "ready"
                if all(statuses.get(item) == "satisfied" for item in dependencies)
                else "unknown"
            )

    @staticmethod
    def _reachable(state: AttackGraphState, start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(
                edge.target
                for edge in state.edges
                if edge.kind == "requires" and edge.source == current
            )
        return False

    def _has_lease(self, node_id: str, owner: str) -> bool:
        try:
            payload = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        now = time.time()
        return any(
            isinstance(item, dict)
            and item.get("node_id") == node_id
            and item.get("owner") == owner
            and float(item.get("expires_at", 0.0)) > now
            for item in payload.get("leases", [])
        )
