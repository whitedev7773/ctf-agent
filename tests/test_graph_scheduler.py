from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.attack_graph import AttackGraphStore
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.graph_scheduler import GraphScheduler
from backend.prompts import ChallengeMeta


@pytest.mark.asyncio
async def test_scheduler_ranks_value_against_cost_and_excludes_leases(tmp_path: Path) -> None:
    graph = AttackGraphStore(tmp_path)
    scheduler = GraphScheduler(graph)
    fast = await graph.add_node(
        kind="experiment",
        title="Cheap discriminator",
        confidence=0.8,
        information_gain=0.9,
        expected_seconds=10,
        delegateable=True,
        independent=True,
    )
    slow = await graph.add_node(
        kind="experiment",
        title="Heavy symbolic solve",
        confidence=0.8,
        information_gain=0.9,
        expected_seconds=600,
        expected_tokens=50_000,
        execution_risk=0.5,
        delegateable=True,
        independent=True,
    )

    assert [item.id for item in scheduler.ready_tasks()] == [fast.id, slow.id]
    assert [item.id for item in scheduler.select(available_delegate_slots=2)] == [fast.id, slow.id]

    assert await scheduler.acquire_lease(fast.id, "delegate-01") is not None
    assert await scheduler.acquire_lease(fast.id, "delegate-02") is None
    assert [item.id for item in scheduler.ready_tasks()] == [slow.id]


@pytest.mark.asyncio
async def test_multi_slot_selection_only_parallelizes_independent_work(tmp_path: Path) -> None:
    graph = AttackGraphStore(tmp_path)
    scheduler = GraphScheduler(graph)
    serial = await graph.add_node(
        kind="experiment",
        title="Serial critical task",
        expected_seconds=5,
        delegateable=True,
        independent=False,
    )
    parallel = await graph.add_node(
        kind="experiment",
        title="Independent task",
        expected_seconds=10,
        delegateable=True,
        independent=True,
    )

    selected = scheduler.select(available_delegate_slots=2)
    assert selected[0].id == serial.id
    assert [item.id for item in selected] == [serial.id]

    await graph.transition(serial.id, "blocked")
    assert [item.id for item in scheduler.select(available_delegate_slots=2)] == [parallel.id]


@pytest.mark.asyncio
async def test_swarm_delegate_uses_graph_node_lease_and_releases_it(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, workspace_root=str(tmp_path))
    swarm = ChallengeSwarm(
        challenge_dir=".",
        meta=ChallengeMeta(name="graph-delegate", category="pwn"),
        ctfd=SimpleNamespace(is_configured=False),
        cost_tracker=CostTracker(),
        settings=settings,
        model_specs=["codex/gpt-5.6-sol/high"],
        no_submit=True,
    )
    node = await swarm.attack_graph_store.add_node(
        kind="experiment",
        title="Fingerprint libc",
        delegateable=True,
        independent=True,
    )
    worker_gate = asyncio.Event()

    async def fake_run(_model_spec: str, task_directive: str = "") -> None:
        assert f"Assigned graph node: {node.id}" in task_directive
        await worker_gate.wait()

    swarm._run_solver = fake_run  # type: ignore[method-assign]
    started = await swarm._spawn_delegate(
        "Fingerprint the supplied libc",
        "Build ID and matching version",
        graph_node_id=node.id,
    )
    duplicate = await swarm._spawn_delegate(
        "Identify the runtime libc",
        "Exact build ID",
        graph_node_id=node.id,
    )

    assert "delegate-01" in started
    assert f"{node.id} is already leased" in duplicate
    assert swarm.attack_graph_store.load().nodes[0].status == "active"

    worker_gate.set()
    await asyncio.gather(*swarm.delegate_tasks.values())
    assert swarm.graph_scheduler.lease_owner(node.id) is None
    assert swarm.attack_graph_store.load().nodes[0].status == "ready"
