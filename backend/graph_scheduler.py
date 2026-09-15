"""Cost-aware READY-node ranking and persistent task leases."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.attack_graph import AttackGraphState, AttackGraphStore, AttackNode

_LEASE_LOCKS: dict[str, asyncio.Lock] = {}


def _shared_lock(path: Path) -> asyncio.Lock:
    key = str(path.resolve()).casefold()
    lock = _LEASE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LEASE_LOCKS[key] = lock
    return lock


@dataclass(frozen=True)
class TaskLease:
    node_id: str
    owner: str
    leased_at: float
    expires_at: float


class GraphScheduler:
    def __init__(self, graph_store: AttackGraphStore) -> None:
        self.graph_store = graph_store
        self.path = graph_store.lease_path
        self._lock = _shared_lock(self.path)

    def leases(self) -> list[TaskLease]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        now = time.time()
        leases: list[TaskLease] = []
        for item in payload.get("leases", []):
            try:
                lease = TaskLease(**item)
            except (TypeError, ValueError):
                continue
            if lease.expires_at > now:
                leases.append(lease)
        return leases

    def lease_owner(self, node_id: str) -> str | None:
        lease = next((item for item in self.leases() if item.node_id == node_id), None)
        return lease.owner if lease else None

    def _save(self, leases: list[TaskLease]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "leases": [asdict(item) for item in leases]}, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    async def acquire_lease(
        self,
        node_id: str,
        owner: str,
        *,
        ttl_seconds: float = 900.0,
    ) -> TaskLease | None:
        clean_owner = owner.strip()[:300]
        if not clean_owner:
            raise ValueError("lease owner is required")
        async with self._lock:
            graph = self.graph_store.load()
            node = next((item for item in graph.nodes if item.id == node_id), None)
            if node is None:
                raise ValueError(f"unknown attack graph node: {node_id}")
            if node.id not in {item.id for item in self.graph_store.ready_nodes(graph)}:
                return None
            leases = self.leases()
            if any(item.node_id == node_id for item in leases):
                return None
            now = time.time()
            lease = TaskLease(
                node_id=node_id,
                owner=clean_owner,
                leased_at=now,
                expires_at=now + max(1.0, float(ttl_seconds)),
            )
            leases.append(lease)
            self._save(leases)
            return lease

    async def release_lease(self, node_id: str, *, owner: str = "") -> bool:
        async with self._lock:
            leases = self.leases()
            retained = [
                item
                for item in leases
                if not (item.node_id == node_id and (not owner or item.owner == owner))
            ]
            if len(retained) == len(leases):
                return False
            self._save(retained)
        state = self.graph_store.load()
        node = next((item for item in state.nodes if item.id == node_id), None)
        if node is not None and node.status == "active":
            await self.graph_store.transition(node_id, "ready")
        return True

    def ready_tasks(self, graph: AttackGraphState | None = None) -> list[AttackNode]:
        state = graph or self.graph_store.load()
        leased = {item.node_id for item in self.leases()}
        nodes = [item for item in self.graph_store.ready_nodes(state) if item.id not in leased]
        return sorted(nodes, key=lambda item: self.score(item, state), reverse=True)

    def score(self, node: AttackNode, graph: AttackGraphState | None = None) -> float:
        state = graph or self.graph_store.load()
        critical_ids = {item.id for item in self.graph_store.critical_path(state)}
        goal_impact = 1.0 if node.id in critical_ids else 0.5
        dependents = {
            edge.source
            for edge in state.edges
            if edge.kind == "requires" and edge.target == node.id
        }
        unblock_gain = min(1.0, len(dependents) / 3.0)
        parallelism_bonus = 0.25 if node.independent else 0.0
        value = (
            2.0 * goal_impact
            + 1.5 * unblock_gain
            + 1.2 * node.information_gain
            + node.confidence
            + parallelism_bonus
        )
        cost = (
            max(node.expected_seconds, 5.0) / 60.0
            + node.expected_tokens / 50_000.0
            + node.execution_risk * 2.0
        )
        return value / max(cost, 0.1)

    def select(
        self,
        graph: AttackGraphState | None = None,
        *,
        available_delegate_slots: int,
    ) -> list[AttackNode]:
        slots = max(0, int(available_delegate_slots))
        candidates = [item for item in self.ready_tasks(graph) if item.delegateable]
        if slots <= 1 or not candidates:
            return candidates[:slots]
        first = candidates[0]
        if not first.independent:
            return [first]
        return [item for item in candidates if item.independent][:slots]
