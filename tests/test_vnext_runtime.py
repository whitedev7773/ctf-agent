from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.swarm import ChallengeSwarm
from backend.artifacts import handoff_reproducer, verify_handoff_reproducer
from backend.benchmark import BenchmarkTrial, summarize_trials
from backend.experience import retrieve_experience
from backend.loop_detect import LoopDetector
from backend.prompts import ChallengeMeta
from backend.sandbox import DockerSandbox


class _FakeStream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.writes: list[bytes] = []
        self.closed = False

    async def read_out(self):
        return await self.queue.get()

    async def write_in(self, data: bytes) -> None:
        self.writes.append(data)

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)


class _FakeExec:
    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream

    def start(self, detach: bool = False):
        assert not detach
        return self.stream


class _FakeContainer:
    id = "container-id"

    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream

    async def exec(self, **_kwargs):
        return _FakeExec(self.stream)


@pytest.mark.asyncio
async def test_persistent_session_open_send_read_interrupt_close(tmp_path: Path) -> None:
    stream = _FakeStream()
    sandbox = DockerSandbox(image="test", challenge_dir=str(tmp_path))
    sandbox._container = _FakeContainer(stream)

    session_id = await sandbox.session_open("python3 -i")
    await stream.queue.put(SimpleNamespace(data=b">>> ready\n"))
    output = await sandbox.session_read(session_id, wait_seconds=1)
    assert ">>> ready" in output

    await sandbox.session_send(session_id, "print(7)\n")
    await sandbox.session_interrupt(session_id)
    assert stream.writes == [b"print(7)\n", b"\x03"]

    await sandbox.session_close(session_id)
    assert stream.closed
    assert session_id not in sandbox._sessions


def test_semantic_loop_groups_cosmetic_search_variants() -> None:
    detector = LoopDetector(warn_threshold=3, break_threshold=5)
    outputs = []
    for term in ("pass", "key", "secret", "flag"):
        args = {"command": f"strings chall | grep {term}"}
        outputs.append(detector.check("bash", args, "H-strings"))
        detector.record_result("bash", args, "(no output)", "H-strings")

    blocked = detector.check(
        "bash", {"command": "strings chall | grep token"}, "H-strings"
    )
    assert "warn" in outputs
    assert blocked == "break"

    detector.reset_transient()
    assert (
        detector.check("bash", {"command": "strings chall | grep code"}, "H-strings")
        == "break"
    )


@pytest.mark.asyncio
async def test_machine_readable_reproducer_is_executed(tmp_path: Path) -> None:
    handoff = tmp_path / "handoff.md"
    handoff.write_text(
        """## Reproduction
```yaml
reproducer:
  command: python3 /challenge/shared/delegates/01/repro.py
  expect:
    exit_code: 0
    stdout_contains: LEAK_CONFIRMED
```
""",
        encoding="utf-8",
    )
    sandbox = SimpleNamespace(
        exec=lambda *_args, **_kwargs: None,
    )

    async def execute(command: str, timeout_s: int):
        assert "repro.py" in command
        assert timeout_s == 120
        return SimpleNamespace(exit_code=0, stdout="LEAK_CONFIRMED", stderr="")

    sandbox.exec = execute
    spec = handoff_reproducer(handoff)
    assert spec and spec.stdout_contains == "LEAK_CONFIRMED"
    assert await verify_handoff_reproducer(sandbox, handoff) == (
        True,
        "exit=0; marker=LEAK_CONFIRMED",
    )


def test_adaptive_delegate_model_selection(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        delegate_model_spec="codex/gpt-5.6-luna/low",
        delegate_hard_model_spec="codex/gpt-5.6-sol/high",
        delegate_verifier_model_spec="codex/gpt-5.6-terra/medium",
    )
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="routing"),
        ctfd=SimpleNamespace(),
        cost_tracker=SimpleNamespace(),
        settings=settings,
    )
    assert swarm._delegate_model_for("extraction", "easy").endswith("luna/low")
    assert swarm._delegate_model_for("vm_analysis", "hard").endswith("sol/high")
    assert swarm._delegate_model_for("verification", "medium").endswith("terra/medium")


def test_experience_retrieval_uses_symptoms_not_only_category(tmp_path: Path) -> None:
    root = tmp_path / "experience"
    (root / "reversing-a").mkdir(parents=True)
    (root / "crypto-b").mkdir(parents=True)
    (root / "reversing-a" / "vm.md").write_text(
        "custom VM stateful rounds transcript-derived state capture decrypted round blob",
        encoding="utf-8",
    )
    (root / "crypto-b" / "rsa.md").write_text(
        "RSA shared prime modulus gcd factorization",
        encoding="utf-8",
    )
    settings = SimpleNamespace(experience_root=str(root))

    results = retrieve_experience(
        settings,
        "stateful protocol direct round request fails; recover transcript state",
        category="reversing",
    )
    assert results
    assert results[0]["path"].endswith("vm.md")


def test_benchmark_summary_reports_solve_at_1_and_3() -> None:
    def trial(challenge: str, repetition: int, solved: bool) -> BenchmarkTrial:
        return BenchmarkTrial(
            challenge=challenge,
            variant="structured",
            repetition=repetition,
            solved=solved,
            status="flag_found" if solved else "gave_up",
            elapsed_seconds=10 * repetition,
            fresh_tokens=100,
            tool_calls=10,
            candidate_count=1,
            false_candidate_count=0 if solved else 1,
            clean_reproduction=solved,
            delegate_count=1,
            useful_delegate_count=1 if solved else 0,
            peer_context_tokens=5,
            duplicate_experiment_count=1,
            hypothesis_refutation_count=2,
            time_to_first_primitive_seconds=4.0,
            hypothesis_refutation_latency_seconds=3.0,
            recorded_at=1.0,
        )

    summary = summarize_trials(
        [
            trial("a", 1, True),
            trial("b", 1, False),
            trial("b", 2, True),
        ]
    )["structured"]
    assert summary["solve_at_1"] == 0.5
    assert summary["solve_at_3"] == 1.0
    assert summary["duplicate_experiment_rate"] == 0.1
