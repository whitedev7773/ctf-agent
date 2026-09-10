from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.codex_solver import REASONING_TOOLS, CodexSolver
from backend.agents.coordinator_loop import _rank_unsolved
from backend.agents.swarm import ChallengeSwarm
from backend.message_bus import ChallengeMessageBus
from backend.prompts import ChallengeMeta
from backend.reasoning_state import ReasoningStateStore, observation_from_result


@pytest.mark.asyncio
async def test_evidence_receipt_and_hypothesis_persist(tmp_path: Path) -> None:
    store = ReasoningStateStore(tmp_path)
    initial_signature = store.semantic_signature()
    observation = observation_from_result(
        "OBS-1",
        "bash",
        {"command": "objdump -d chall"},
        "4012fa: call memcmp",
    )

    evidence = await store.record_evidence(
        kind="static",
        claim="The comparison call is at 0x4012fa.",
        confidence=0.9,
        observation=observation,
        source_agent="lead",
    )
    hypothesis = await store.upsert_hypothesis(
        hypothesis_id=None,
        statement="Recovering the comparison buffer yields the accepted input.",
        status="supported",
        evidence_for=[evidence.id],
        expected_signal="RSI points to the expected bytes before memcmp.",
        next_experiment="Break at 0x4012fa and inspect RSI.",
    )

    restored = ReasoningStateStore(tmp_path).load()
    assert restored.evidence[0].output_hash == observation.output_hash
    assert restored.hypotheses[0].id == hypothesis.id
    assert restored.hypotheses[0].status == "supported"
    assert store.semantic_signature() != initial_signature


@pytest.mark.asyncio
async def test_supported_hypothesis_requires_known_evidence(tmp_path: Path) -> None:
    store = ReasoningStateStore(tmp_path)
    with pytest.raises(ValueError, match="unknown evidence"):
        await store.upsert_hypothesis(
            hypothesis_id="H-1",
            statement="An unsupported conclusion.",
            status="supported",
            evidence_for=["EV-missing"],
        )


@pytest.mark.asyncio
async def test_refutation_records_failure_pivot_and_cannot_be_reactivated(
    tmp_path: Path,
) -> None:
    store = ReasoningStateStore(tmp_path)
    observation = observation_from_result(
        "OBS-mismatch",
        "bash",
        {"command": "python3 differential.py"},
        "native=9f model=b4 mismatch",
    )
    evidence = await store.record_evidence(
        kind="negative",
        claim="The extracted round model disagrees with native execution.",
        confidence=0.99,
        observation=observation,
    )
    await store.upsert_hypothesis(
        hypothesis_id="H-round",
        statement="The nearby tables fully encode the round function.",
        status="refuted",
        evidence_against=[evidence.id],
        expected_signal="Model and native checkpoints match.",
        next_experiment="Try another table permutation.",
        pivot_if_absent="Lift one native transition end-to-end.",
    )

    state = store.load()
    assert state.next_experiment == "Lift one native transition end-to-end."
    assert state.failed_experiments
    assert state.attempted_routes
    with pytest.raises(ValueError, match="cannot be reactivated"):
        await store.upsert_hypothesis(
            hypothesis_id="H-round",
            statement="The nearby tables fully encode the round function.",
            status="active",
            expected_signal="A different constant makes it match.",
            next_experiment="Tune constants.",
        )


@pytest.mark.asyncio
async def test_only_one_hypothesis_is_active_and_context_updates_preserve_fields(
    tmp_path: Path,
) -> None:
    store = ReasoningStateStore(tmp_path)
    await store.upsert_hypothesis(
        hypothesis_id="H-one",
        statement="First route.",
        status="active",
        expected_signal="one",
        next_experiment="test one",
    )
    await store.update_context(blocker="Need native checkpoint", next_experiment="trace once")
    await store.update_context(failed_experiment="debugger timed out")
    await store.upsert_hypothesis(
        hypothesis_id="H-two",
        statement="Second route.",
        status="active",
        expected_signal="two",
        next_experiment="test two",
    )

    state = store.load()
    statuses = {item.id: item.status for item in state.hypotheses}
    assert statuses == {"H-one": "candidate", "H-two": "active"}
    assert state.current_blocker == "Need native checkpoint"


@pytest.mark.asyncio
async def test_resolved_hypothesis_does_not_consume_unresolved_limit(tmp_path: Path) -> None:
    store = ReasoningStateStore(tmp_path)
    for index in range(4):
        await store.upsert_hypothesis(
            hypothesis_id=f"H-{index}",
            statement=f"Unresolved route {index}.",
            status="candidate",
        )
    observation = observation_from_result(
        "OBS-negative", "bash", {"command": "./check"}, "mismatch"
    )
    evidence = await store.record_evidence(
        kind="negative",
        claim="A separate route was disproved.",
        confidence=0.9,
        observation=observation,
    )

    await store.upsert_hypothesis(
        hypothesis_id="H-resolved",
        statement="A separate disproved route.",
        status="refuted",
        evidence_against=[evidence.id],
    )
    assert store.load().hypotheses[-1].status == "refuted"


@pytest.mark.asyncio
async def test_hypotheses_are_ranked_by_value_over_cost(tmp_path: Path) -> None:
    store = ReasoningStateStore(tmp_path)
    slow = await store.upsert_hypothesis(
        hypothesis_id="H-slow",
        statement="Run full symbolic exploration.",
        confidence=0.8,
        information_gain=0.5,
        expected_seconds=600,
        expected_tokens=200_000,
    )
    fast = await store.upsert_hypothesis(
        hypothesis_id="H-fast",
        statement="Inspect one register at the comparison call.",
        confidence=0.5,
        information_gain=0.8,
        expected_seconds=15,
        expected_tokens=500,
    )
    payload = json.loads(store.format_state())
    assert payload["ranked_hypotheses"][0]["id"] == fast.id
    assert store.hypothesis_priority(fast) > store.hypothesis_priority(slow)


@pytest.mark.asyncio
async def test_codex_state_tools_use_runtime_observation_receipt(tmp_path: Path) -> None:
    solver = CodexSolver.__new__(CodexSolver)
    solver.reasoning_state_store = ReasoningStateStore(tmp_path)
    solver.model_spec = "codex/gpt-5.6-sol/high"
    solver.tracer = SimpleNamespace(event=lambda *_args, **_kwargs: None)
    observation = observation_from_result(
        "OBS-runtime", "bash", {"command": "printf signal"}, "signal"
    )
    solver._observations = {observation.id: observation}

    result = await solver._exec_tool(
        "record_evidence",
        {
            "observation_id": observation.id,
            "kind": "dynamic",
            "claim": "The runtime emitted the expected signal.",
            "confidence": 0.95,
        },
    )

    assert str(result).startswith("EVIDENCE RECORDED: EV-")
    assert solver.reasoning_state_store.load().evidence[0].source_command == "printf signal"
    assert {tool["name"] for tool in REASONING_TOOLS} == {
        "record_evidence",
        "update_hypothesis",
        "get_solve_state",
        "update_solve_context",
        "sync_findings",
        "search_experience",
    }


@pytest.mark.asyncio
async def test_progress_combines_semantic_state_and_workspace_artifacts(tmp_path: Path) -> None:
    store = ReasoningStateStore(tmp_path)

    class Solver:
        reasoning_state_store = store
        sandbox = SimpleNamespace(workspace_dir=str(tmp_path), shared_workspace_dir="")

    before = ChallengeSwarm._progress_signature(Solver())
    await store.update_context(blocker="Need a leak", next_experiment="Inspect GOT")
    assert ChallengeSwarm._progress_signature(Solver()) == before

    artifact = tmp_path / "STATE.md"
    artifact.write_text("still investigating", encoding="utf-8")
    assert ChallengeSwarm._progress_signature(Solver()) != before


@pytest.mark.asyncio
async def test_relevance_sync_does_not_consume_unrelated_findings() -> None:
    bus = ChallengeMessageBus()
    await bus.post(
        "delegate-a",
        "VM opcode mapping",
        kind="hypothesis",
        hypothesis_id="H-vm",
        tags=["vm"],
    )
    await bus.post(
        "delegate-b",
        "AES IV is reused",
        kind="evidence",
        hypothesis_id="H-aes",
        tags=["aes"],
        evidence_ids=["EV-aes"],
        confidence=0.9,
    )

    aes = await bus.sync("lead", hypothesis_id="H-aes")
    assert [finding.hypothesis_id for finding in aes] == ["H-aes"]

    vm = await bus.sync("lead", hypothesis_id="H-vm")
    assert [finding.hypothesis_id for finding in vm] == ["H-vm"]


@pytest.mark.asyncio
async def test_only_evidence_backed_urgent_findings_bypass_relevance() -> None:
    bus = ChallengeMessageBus()
    await bus.post(
        "delegate",
        "urgent guess",
        kind="hypothesis",
        urgency="urgent",
        tags=["other"],
    )
    await bus.post(
        "delegate",
        "verified contradiction",
        kind="conflict",
        urgency="urgent",
        evidence_ids=["EV-7"],
        tags=["other"],
    )

    findings = await bus.sync("lead", hypothesis_id="H-current", tags=["current"])
    assert [finding.claim for finding in findings] == ["verified contradiction"]


def test_scheduler_ranks_expected_points_per_minute(tmp_path: Path) -> None:
    easy_dir = tmp_path / "easy"
    hard_dir = tmp_path / "hard"
    (easy_dir / "distfiles").mkdir(parents=True)
    (hard_dir / "distfiles").mkdir(parents=True)
    (easy_dir / "distfiles" / "task.py").write_text("pass", encoding="utf-8")
    (hard_dir / "distfiles" / "chall").write_bytes(b"binary")
    deps = SimpleNamespace(
        challenge_metas={
            "easy-web": ChallengeMeta(name="easy-web", category="web", value=100, solves=80),
            "hard-pwn": ChallengeMeta(name="hard-pwn", category="pwn", value=100, solves=0),
        },
        challenge_dirs={"easy-web": str(easy_dir), "hard-pwn": str(hard_dir)},
        results={},
        dismissed_challenges=set(),
    )
    poller = SimpleNamespace(
        known_challenges={"hard-pwn", "easy-web"},
        known_solved=set(),
    )

    assert _rank_unsolved(deps, poller) == ["easy-web", "hard-pwn"]
