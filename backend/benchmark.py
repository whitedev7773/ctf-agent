"""Reproducible benchmark runner and metric aggregation for CTF Agent."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from backend.solver_base import CANDIDATE_FOUND, FLAG_FOUND, HANDOFF_COMPLETE


@dataclass
class BenchmarkTrial:
    challenge: str
    variant: str
    repetition: int
    solved: bool
    status: str
    elapsed_seconds: float
    fresh_tokens: int
    tool_calls: int
    candidate_count: int
    false_candidate_count: int
    clean_reproduction: bool
    delegate_count: int
    useful_delegate_count: int
    peer_context_tokens: int
    duplicate_experiment_count: int
    hypothesis_refutation_count: int
    time_to_first_primitive_seconds: float
    hypothesis_refutation_latency_seconds: float
    recorded_at: float


def trial_from_swarm(
    swarm, result, elapsed_seconds: float, started_wall: float | None = None
) -> BenchmarkTrial:
    usages = list(getattr(swarm.cost_tracker, "by_agent", {}).values())
    fresh_tokens = sum(
        max(0, item.usage.input_tokens - item.usage.cache_read_tokens)
        + item.usage.output_tokens
        for item in usages
    )
    outcomes = list(getattr(swarm, "outcomes", {}).values())
    candidates = [item for item in outcomes if item.status == CANDIDATE_FOUND]
    delegates = [
        item
        for spec, item in getattr(swarm, "outcomes", {}).items()
        if "delegate-" in spec
    ]
    useful_delegates = [item for item in delegates if item.status == HANDOFF_COMPLETE]
    findings = getattr(getattr(swarm, "message_bus", None), "findings", [])
    delivered_ids = set().union(
        *getattr(getattr(swarm, "message_bus", None), "delivered", {}).values()
    ) if getattr(getattr(swarm, "message_bus", None), "delivered", {}) else set()
    peer_tokens = sum(
        max(1, len(item.claim.split())) for item in findings if item.id in delivered_ids
    )
    duplicate_count = sum(
        int(getattr(getattr(solver, "loop_detector", None), "duplicate_count", 0))
        for solver in getattr(swarm, "solvers", {}).values()
    )
    state = None
    for solver in getattr(swarm, "solvers", {}).values():
        store = getattr(solver, "reasoning_state_store", None)
        if store is not None:
            state = store.load()
            break
    reproduction = bool(
        state and any(item.kind == "reproduction" for item in state.evidence)
    ) or bool(useful_delegates)
    refutations = sum(
        1 for item in (state.hypotheses if state else []) if item.status == "refuted"
    )
    evidence_times = [item.timestamp for item in (state.evidence if state else [])]
    time_to_primitive = (
        max(0.0, min(evidence_times) - started_wall)
        if evidence_times and started_wall is not None
        else 0.0
    )
    refutation_latencies = [
        max(0.0, item.updated_at - item.created_at)
        for item in (state.hypotheses if state else [])
        if item.status == "refuted"
    ]
    expected_hash = str(
        getattr(swarm.settings, "benchmark_expected_flag_sha256", "") or ""
    ).casefold()
    candidate_matches = bool(
        result
        and result.flag
        and expected_hash
        and hashlib.sha256(result.flag.encode()).hexdigest().casefold() == expected_hash
    )
    solved = bool(result and result.status == FLAG_FOUND) or candidate_matches
    return BenchmarkTrial(
        challenge=swarm.meta.name,
        variant=str(getattr(swarm.settings, "benchmark_variant", "default")),
        repetition=max(1, int(getattr(swarm.settings, "benchmark_repetition", 1))),
        solved=solved,
        status="oracle_verified" if candidate_matches else result.status if result else "no_result",
        elapsed_seconds=round(max(0.0, elapsed_seconds), 3),
        fresh_tokens=fresh_tokens,
        tool_calls=sum(item.step_count for item in outcomes),
        candidate_count=len(candidates),
        false_candidate_count=len(candidates) if not solved else max(0, len(candidates) - 1),
        clean_reproduction=reproduction,
        delegate_count=len(delegates),
        useful_delegate_count=len(useful_delegates),
        peer_context_tokens=peer_tokens,
        duplicate_experiment_count=duplicate_count,
        hypothesis_refutation_count=refutations,
        time_to_first_primitive_seconds=round(time_to_primitive, 3),
        hypothesis_refutation_latency_seconds=round(
            statistics.median(refutation_latencies), 3
        ) if refutation_latencies else 0.0,
        recorded_at=time.time(),
    )


def append_trial(path: str | Path, trial: BenchmarkTrial) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(trial), ensure_ascii=False) + "\n")


def load_trials(path: str | Path) -> list[BenchmarkTrial]:
    trials: list[BenchmarkTrial] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload.setdefault("time_to_first_primitive_seconds", 0.0)
        payload.setdefault("hypothesis_refutation_latency_seconds", 0.0)
        trials.append(BenchmarkTrial(**payload))
    return trials


def summarize_trials(trials: list[BenchmarkTrial]) -> dict[str, dict[str, float | int]]:
    variants = sorted({trial.variant for trial in trials})
    summary: dict[str, dict[str, float | int]] = {}
    for variant in variants:
        subset = [trial for trial in trials if trial.variant == variant]
        by_challenge: dict[str, list[BenchmarkTrial]] = {}
        for trial in subset:
            by_challenge.setdefault(trial.challenge, []).append(trial)
        solve_at_1 = []
        solve_at_3 = []
        for challenge_trials in by_challenge.values():
            ordered = sorted(challenge_trials, key=lambda item: item.repetition)
            solve_at_1.append(bool(ordered and ordered[0].solved))
            solve_at_3.append(any(item.solved for item in ordered[:3]))
        solved = [trial for trial in subset if trial.solved]
        total_candidates = sum(item.candidate_count for item in subset)
        total_delegates = sum(item.delegate_count for item in subset)
        total_tools = sum(item.tool_calls for item in subset)
        summary[variant] = {
            "trials": len(subset),
            "challenges": len(by_challenge),
            "solve_at_1": round(sum(solve_at_1) / max(1, len(solve_at_1)), 4),
            "solve_at_3": round(sum(solve_at_3) / max(1, len(solve_at_3)), 4),
            "median_time_to_flag": round(
                statistics.median(item.elapsed_seconds for item in solved), 3
            ) if solved else 0.0,
            "fresh_tokens_per_solve": round(
                sum(item.fresh_tokens for item in subset) / max(1, len(solved)), 2
            ),
            "tool_calls_per_solve": round(total_tools / max(1, len(solved)), 2),
            "duplicate_experiment_rate": round(
                sum(item.duplicate_experiment_count for item in subset) / max(1, total_tools),
                4,
            ),
            "false_candidate_rate": round(
                sum(item.false_candidate_count for item in subset) / max(1, total_candidates),
                4,
            ),
            "clean_reproduction_rate": round(
                sum(item.clean_reproduction for item in solved) / max(1, len(solved)), 4
            ),
            "delegate_utility": round(
                sum(item.useful_delegate_count for item in subset) / max(1, total_delegates),
                4,
            ),
            "peer_context_tokens": sum(item.peer_context_tokens for item in subset),
            "hypothesis_refutations": sum(
                item.hypothesis_refutation_count for item in subset
            ),
            "median_time_to_first_primitive": round(
                statistics.median(
                    item.time_to_first_primitive_seconds
                    for item in subset
                    if item.time_to_first_primitive_seconds > 0
                ),
                3,
            ) if any(item.time_to_first_primitive_seconds > 0 for item in subset) else 0.0,
            "median_hypothesis_refutation_latency": round(
                statistics.median(
                    item.hypothesis_refutation_latency_seconds
                    for item in subset
                    if item.hypothesis_refutation_latency_seconds > 0
                ),
                3,
            ) if any(item.hypothesis_refutation_latency_seconds > 0 for item in subset) else 0.0,
        }
    return summary


async def run_manifest(path: str | Path) -> Path:
    manifest_path = Path(path).resolve()
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases", [])
    variants = payload.get("variants", {})
    if not isinstance(cases, list) or not cases:
        raise ValueError("benchmark manifest requires at least one case")
    if not isinstance(variants, dict) or not variants:
        raise ValueError("benchmark manifest requires at least one variant")
    repetitions = max(1, int(payload.get("repetitions", 1)))
    timeout = max(60, int(payload.get("timeout_seconds", 14_400)))
    output = (manifest_path.parent / payload.get("output", "benchmark-results.jsonl")).resolve()
    if output.exists():
        if not bool(payload.get("overwrite", False)):
            raise FileExistsError(
                f"benchmark output already exists: {output}; set overwrite: true explicitly"
            )
        output.unlink()
    for variant, config in variants.items():
        models = list((config or {}).get("models", []))
        no_submit = bool((config or {}).get("no_submit", True))
        variant_env = {str(k): str(v) for k, v in (config or {}).get("env", {}).items()}
        for case in cases:
            challenge = (manifest_path.parent / str(case["path"])).resolve()
            for repetition in range(1, repetitions + 1):
                command = [
                    sys.executable,
                    "-c",
                    "from backend.cli import main; main()",
                    "--challenge",
                    str(challenge),
                ]
                if no_submit:
                    command.append("--no-submit")
                for model in models:
                    command.extend(["--models", str(model)])
                environment = os.environ.copy()
                environment.update(variant_env)
                environment.update(
                    {
                        "BENCHMARK_RESULTS_PATH": str(output),
                        "BENCHMARK_VARIANT": str(variant),
                        "BENCHMARK_REPETITION": str(repetition),
                    }
                )
                expected_flag = str(case.get("expected_flag", ""))
                if expected_flag:
                    environment["BENCHMARK_EXPECTED_FLAG_SHA256"] = hashlib.sha256(
                        expected_flag.encode()
                    ).hexdigest()
                process = await asyncio.create_subprocess_exec(
                    *command,
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise RuntimeError(
                        f"benchmark trial timed out: {variant}/{challenge.name}/{repetition}"
                    ) from None
                if process.returncode:
                    tail = stdout.decode("utf-8", errors="replace")[-4000:]
                    raise RuntimeError(
                        f"benchmark trial failed: {variant}/{challenge.name}/{repetition}\n{tail}"
                    )
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summarize_trials(load_trials(output)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", help="YAML benchmark manifest to execute")
    parser.add_argument("--summarize", help="Existing JSONL results to summarize")
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize_trials(load_trials(args.summarize)), indent=2))
        return
    if not args.manifest:
        parser.error("manifest is required unless --summarize is used")
    print(asyncio.run(run_manifest(args.manifest)))


if __name__ == "__main__":
    main()
