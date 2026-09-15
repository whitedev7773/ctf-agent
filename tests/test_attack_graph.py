from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.codex_solver import GRAPH_TOOLS, CodexSolver
from backend.attack_graph import AttackGraphStore
from backend.graph_scheduler import GraphScheduler
from backend.message_bus import ChallengeMessageBus
from backend.reasoning_state import ReasoningStateStore, observation_from_result


async def _evidence(
    store: ReasoningStateStore,
    *,
    kind: str = "dynamic",
    marker: str = "signal",
) -> str:
    observation = observation_from_result(
        f"OBS-{marker}",
        "bash",
        {"command": f"printf {marker}"},
        marker,
    )
    item = await store.record_evidence(
        kind=kind,  # type: ignore[arg-type]
        claim=f"Observed {marker}",
        confidence=0.95,
        observation=observation,
        source_agent="test",
    )
    return item.id


@pytest.mark.asyncio
async def test_requires_dag_drives_ready_nodes_and_critical_path(tmp_path: Path) -> None:
    reasoning = ReasoningStateStore(tmp_path)
    graph = AttackGraphStore(tmp_path, reasoning)
    goal = await graph.add_node(kind="goal", title="Get flag")
    primitive = await graph.add_node(kind="primitive", title="Arbitrary write")
    experiment = await graph.add_node(kind="experiment", title="Test tcache poisoning")

    await graph.add_edge(goal.id, primitive.id, "requires")
    await graph.add_edge(primitive.id, experiment.id, "requires")

    assert [item.id for item in graph.ready_nodes()] == [experiment.id]
    assert [item.id for item in graph.critical_path()] == [goal.id, primitive.id, experiment.id]

    evidence_id = await _evidence(reasoning)
    await graph.transition(experiment.id, "satisfied", evidence_ids=[evidence_id])
    assert [item.id for item in graph.ready_nodes()] == [primitive.id]

    await graph.transition(primitive.id, "satisfied", evidence_ids=[evidence_id])
    assert [item.id for item in graph.ready_nodes()] == [goal.id]
    assert AttackGraphStore(tmp_path).load().revision == graph.load().revision


@pytest.mark.asyncio
async def test_requires_edges_reject_cycles(tmp_path: Path) -> None:
    graph = AttackGraphStore(tmp_path)
    first = await graph.add_node(kind="goal", title="First")
    second = await graph.add_node(kind="precondition", title="Second")
    await graph.add_edge(first.id, second.id, "requires")

    with pytest.raises(ValueError, match="cycle"):
        await graph.add_edge(second.id, first.id, "requires")


@pytest.mark.asyncio
async def test_terminal_transitions_are_evidence_gated(tmp_path: Path) -> None:
    reasoning = ReasoningStateStore(tmp_path)
    graph = AttackGraphStore(tmp_path, reasoning)
    primitive = await graph.add_node(kind="primitive", title="Libc base")
    experiment = await graph.add_node(kind="experiment", title="Leak pointer")
    hypothesis = await graph.add_node(kind="hypothesis", title="Unsorted-bin disclosure")

    for node in (primitive, experiment):
        with pytest.raises(ValueError, match="requires evidence"):
            await graph.transition(node.id, "satisfied")

    positive = await _evidence(reasoning, marker="pointer")
    with pytest.raises(ValueError, match="negative evidence"):
        await graph.transition(hypothesis.id, "refuted", evidence_ids=[positive])

    negative = await _evidence(reasoning, kind="negative", marker="no-leak")
    refuted = await graph.transition(hypothesis.id, "refuted", evidence_ids=[negative])
    assert refuted.status == "refuted"


@pytest.mark.asyncio
async def test_active_transition_requires_live_lease(tmp_path: Path) -> None:
    graph = AttackGraphStore(tmp_path)
    scheduler = GraphScheduler(graph)
    experiment = await graph.add_node(kind="experiment", title="Inspect handlers")

    with pytest.raises(ValueError, match="live task lease"):
        await graph.transition(experiment.id, "active", owner_agent="delegate-01")

    lease = await scheduler.acquire_lease(experiment.id, "delegate-01")
    assert lease is not None
    active = await graph.transition(experiment.id, "active", owner_agent="delegate-01")
    assert active.owner_agent == "delegate-01"
    assert await scheduler.release_lease(experiment.id, owner="delegate-01")
    assert graph.load().nodes[0].status == "ready"


@pytest.mark.asyncio
async def test_codex_graph_tools_share_evidence_and_solve_summary(tmp_path: Path) -> None:
    solver = CodexSolver.__new__(CodexSolver)
    solver.model_spec = "codex/gpt-5.6-sol/high"
    solver.settings = SimpleNamespace(solver_runtime_limit_seconds=900)
    solver.reasoning_state_store = ReasoningStateStore(tmp_path)
    solver.attack_graph_store = AttackGraphStore(tmp_path, solver.reasoning_state_store)
    solver.graph_scheduler = GraphScheduler(solver.attack_graph_store)
    solver.tracer = SimpleNamespace(event=lambda *_args, **_kwargs: None)

    registered = await solver._exec_tool(
        "register_attack_node",
        {"kind": "experiment", "title": "Probe oracle", "information_gain": 0.9},
    )
    assert str(registered).startswith("GRAPH NODE REGISTERED: E-1")

    ready = json.loads(str(await solver._exec_tool("get_ready_tasks", {})))
    assert ready["mode"] == "passive-shadow"
    assert ready["ready_tasks"][0]["id"] == "E-1"

    solve_state = json.loads(str(await solver._exec_tool("get_solve_state", {})))
    assert solve_state["attack_graph_summary"]["ready"] == ["E-1"]
    assert {tool["name"] for tool in GRAPH_TOOLS} == {
        "get_attack_graph",
        "register_attack_node",
        "link_attack_nodes",
        "update_attack_node",
        "get_ready_tasks",
    }


@pytest.mark.asyncio
async def test_graph_node_findings_have_highest_relevance() -> None:
    bus = ChallengeMessageBus()
    await bus.post(
        "delegate-graph",
        "Verified libc leak",
        kind="evidence",
        graph_node_ids=["P-17"],
        evidence_ids=["EV-libc"],
    )
    await bus.post(
        "delegate-text",
        "A lexical libc note",
        kind="note",
        tags=["libc"],
    )

    findings = await bus.sync("lead", graph_node_ids=["P-17"], blocker="libc")
    assert [item.model for item in findings] == ["delegate-graph", "delegate-text"]
