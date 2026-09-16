"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from backend.artifacts import challenge_shared_path
from backend.deps import CoordinatorDeps
from backend.experience import promote_challenge_experience
from backend.notifications import notify_discord, spoiler
from backend.prompts import ChallengeMeta
from backend.runtime_state import persist_deps_state
from backend.solver_base import FLAG_FOUND
from backend.writeups import finalize_writeup

logger = logging.getLogger(__name__)

_EVIDENCE_FLAG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]{0,63}\{[^{}\r\n]{1,500}\})"
)

_SOLUTION_REVIEW_CONTAINER_PATH = (
    "/challenge/shared/review/CURRENT_SOLUTION_REVIEW.md"
)


def _accepted_candidate_flags(deps: CoordinatorDeps, challenge_name: str) -> set[str]:
    """Return every operator- or verifier-confirmed flag that must not re-enter review."""
    result = getattr(deps, "results", {}).get(challenge_name, {})
    if not isinstance(result, dict):
        return set()
    return {
        item.strip()
        for item in [result.get("flag"), *result.get("accepted_flags", [])]
        if isinstance(item, str) and item.strip()
    }


def _merge_candidate_record(
    deps: CoordinatorDeps,
    challenge_name: str,
    flags: list[str],
    *,
    source: str,
) -> dict:
    """Merge candidates without losing alternate wrappers or prior review history."""
    from backend.flag_format import flag_matches_format

    record = dict(deps.candidates.get(challenge_name, {}))
    rejected = {
        item
        for item in record.get("rejected_flags", [])
        if isinstance(item, str) and item.strip()
    }
    accepted = _accepted_candidate_flags(deps, challenge_name)
    merged_flags = [
        item
        for item in [record.get("flag"), *record.get("flags", []), *flags]
        if isinstance(item, str) and item.strip()
    ]
    merged_flags = [
        item
        for item in dict.fromkeys(item.strip() for item in merged_flags)
        if item not in rejected and item not in accepted
    ][:20]
    meta = deps.challenge_metas.get(challenge_name)
    format_hint = str(getattr(meta, "flag_format", "") or "") if meta else ""
    mismatches = [
        item
        for item in [*record.get("format_mismatches", []), *merged_flags]
        if isinstance(item, str)
        and item in merged_flags
        and not flag_matches_format(item, format_hint)
    ]
    sources = [
        item
        for item in [*record.get("sources", []), record.get("source"), source]
        if isinstance(item, str) and item.strip()
    ]
    record.update(
        {
            "flag": merged_flags[0] if merged_flags else "",
            "flags": merged_flags,
            "sources": list(dict.fromkeys(sources))[:20],
            "source": source,
            "status": "unverified" if merged_flags else record.get("status", "unverified"),
            "review_required": bool(merged_flags),
            "format_hint": format_hint,
            "format_mismatches": list(dict.fromkeys(mismatches))[:20],
        }
    )
    if merged_flags:
        deps.candidates[challenge_name] = record
    else:
        deps.candidates.pop(challenge_name, None)
    return record


def _candidate_flags_from_evidence(
    deps: CoordinatorDeps,
    challenge_name: str,
) -> tuple[list[str], list[str]]:
    """Recover output-backed candidates that were recorded before structured submission.

    A solver can be cancelled after ``record_evidence(kind="candidate")`` but before
    it calls ``submit_flag`` or returns ``CANDIDATE_FOUND``.  Only candidate evidence
    with a concrete observed tool excerpt is eligible here; claims and ordinary notes
    are deliberately ignored so this recovery path cannot promote an unsupported guess.
    """
    settings = getattr(deps, "settings", None)
    if settings is None:
        return [], []
    state_path = Path(challenge_shared_path(settings, challenge_name)) / "reasoning" / "state.json"
    try:
        if state_path.stat().st_size > 10_000_000:
            return [], []
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(payload, dict):
        return [], []

    flags: list[str] = []
    evidence_ids: list[str] = []
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list):
        return [], []
    for item in evidence[-200:]:
        if not isinstance(item, dict) or item.get("kind") != "candidate":
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        excerpt = item.get("observed_excerpt", "")
        if confidence < 0.8 or not isinstance(excerpt, str) or not excerpt.strip():
            continue
        recovered = [match.group(1).strip() for match in _EVIDENCE_FLAG_PATTERN.finditer(excerpt)]
        if not recovered:
            continue
        flags.extend(recovered)
        evidence_id = str(item.get("id", "")).strip()
        if evidence_id:
            evidence_ids.append(evidence_id[:100])
    return list(dict.fromkeys(flags))[:20], list(dict.fromkeys(evidence_ids))[:20]


def preserve_evidence_candidates(deps: CoordinatorDeps, challenge_name: str) -> list[str]:
    """Persist any output-backed candidate evidence for dashboard review."""
    flags, evidence_ids = _candidate_flags_from_evidence(deps, challenge_name)
    if not flags:
        return []
    source = "candidate evidence"
    if evidence_ids:
        source += ": " + ", ".join(evidence_ids)
    _merge_candidate_record(deps, challenge_name, flags, source=source)
    persist_deps_state(deps)
    return flags


def solution_review_guidance(settings: object, challenge_name: str) -> str:
    """Load the latest successful review as bounded solver guidance.

    The report itself is the restart-safe queue: it lives in the challenge's
    shared workspace, so a new coordinator process can still attach it to the
    next swarm without adding another runtime-state format.
    """
    report_path = (
        Path(challenge_shared_path(settings, challenge_name))
        / "review"
        / "CURRENT_SOLUTION_REVIEW.md"
    )
    try:
        report = report_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return ""
    if not report:
        return ""
    prefix = (
        "An independent review of the current solution has completed. Read the full "
        f"report at `{_SOLUTION_REVIEW_CONTAINER_PATH}` and incorporate its verified "
        "risks and next recommendations before choosing the next experiment. Treat it "
        "as review guidance and independently verify decisive claims.\n\n"
        "## Independent solution review\n\n"
    )
    return prefix + report[: max(0, 6000 - len(prefix))]


async def deliver_solution_review(deps: CoordinatorDeps, challenge_name: str) -> bool:
    """Push a completed review into a live swarm, if one is still solving."""
    guidance = solution_review_guidance(deps.settings, challenge_name)
    if not guidance:
        return False
    swarm = getattr(deps, "swarms", {}).get(challenge_name)
    task = getattr(deps, "swarm_tasks", {}).get(challenge_name)
    if (
        swarm is None
        or task is None
        or task.done()
        or (
            getattr(swarm, "cancel_event", None) is not None
            and swarm.cancel_event.is_set()
        )
    ):
        return False

    # Persist it on the live swarm for delayed and dynamically-created workers.
    # bump() reaches already-instantiated workers; Codex queues every update and
    # debounces repeated active-turn interrupts.
    swarm.solution_review_directive = guidance
    current_solvers = dict(getattr(swarm, "solvers", {}))
    for solver in tuple(current_solvers.values()):
        bump = getattr(solver, "bump", None)
        if callable(bump):
            bump(guidance)
    return True


def _trace_timestamp(value: object) -> str:
    """Format an epoch trace timestamp in the server's local timezone."""
    try:
        timestamp = datetime.fromtimestamp(float(str(value))).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return "[time unknown]"
    return f"[{timestamp.isoformat(sep=' ', timespec='milliseconds')}]"


async def _generate_or_finalize_writeup(
    deps: CoordinatorDeps,
    challenge_name: str,
    category: str,
    flag: str,
) -> dict:
    """Use the dashboard's AI generator when available, with a CLI fallback."""
    generator = getattr(deps, "request_writeup_generation", None)
    if callable(generator):
        return await generator(challenge_name)
    return finalize_writeup(deps.settings, challenge_name, category, flag)


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    if deps.ctfd.is_configured:
        challenges = await deps.ctfd.fetch_all_challenges()
        solved = await deps.ctfd.fetch_solved_names()
    else:
        challenges = [
            {
                "name": meta.name,
                "category": meta.category,
                "value": meta.value,
                "solves": meta.solves,
                "description": meta.description,
            }
            for meta in deps.challenge_metas.values()
        ]
        solved = set(deps.results)
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
        }
        for ch in challenges
        if ch.get("name") not in getattr(deps, "dismissed_challenges", set())
    ]
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.ctfd.fetch_solved_names() if deps.ctfd.is_configured else set(deps.results)
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps(
        {
            "solved": sorted(solved),
            "candidates": deps.candidates,
            "active_swarms": swarm_status,
        },
        indent=2,
    )


async def do_spawn_swarm(
    deps: CoordinatorDeps,
    challenge_name: str,
    feedback: str = "",
) -> str:
    # Retire ALL finished swarms before checking capacity
    finished = [
        name for name, swarm in deps.swarms.items()
        if swarm.cancel_event.is_set()
        or (name in deps.swarm_tasks and deps.swarm_tasks[name].done())
    ]
    for name in finished:
        del deps.swarms[name]
        deps.swarm_tasks.pop(name, None)

    if challenge_name in getattr(deps, "dismissed_challenges", set()):
        return f"Challenge '{challenge_name}' was deleted by the operator"

    if challenge_name in deps.results:
        return f"Already solved: {challenge_name}. Refusing to start another solver swarm."

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return f"At capacity ({active_count}/{deps.max_concurrent_challenges} challenges running). Wait for one to finish."

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        if not deps.ctfd.is_configured:
            return f"Challenge '{challenge_name}' not found locally; add it from the dashboard first"
        challenges = await deps.ctfd.fetch_all_challenges()
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on CTFd"
        output_dir = str(Path(deps.challenges_root))
        ch_dir = await deps.ctfd.pull_challenge(ch_data, output_dir)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")

    # A completed review may have arrived after the previous swarm stopped.
    # Reattach it to every subsequent solve so it is not stranded in the UI.
    review_guidance = solution_review_guidance(deps.settings, challenge_name)

    from backend.agents.swarm import ChallengeSwarm

    # A dashboard policy update applies to future work only. Solvers repeatedly
    # consult their settings while running, so give each swarm an isolated copy.
    swarm_settings = (
        deps.settings.model_copy(deep=True)
        if hasattr(deps.settings, "model_copy")
        else copy.deepcopy(deps.settings)
    )
    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=swarm_settings,
        model_specs=list(deps.model_specs),
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
        feedback_directive=feedback,
        solution_review_directive=review_guidance,
    )
    deps.swarms[challenge_name] = swarm
    run_counts = getattr(deps, "swarm_run_counts", None)
    if run_counts is None:
        run_counts = {}
        deps.swarm_run_counts = run_counts
    retry_after = getattr(deps, "swarm_retry_after", None)
    if retry_after is None:
        retry_after = {}
        deps.swarm_retry_after = retry_after
    run_counts[challenge_name] = run_counts.get(challenge_name, 0) + 1
    retry_after.pop(challenge_name, None)

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        evidence_flags, evidence_ids = _candidate_flags_from_evidence(deps, challenge_name)
        flags = list(dict.fromkeys([*swarm.all_candidate_flags(), *evidence_flags]))[:20]
        if flags:
            source_items = [*sorted(swarm.candidate_flags or swarm.candidates)]
            if evidence_ids:
                source_items.append("candidate evidence: " + ", ".join(evidence_ids))
            sources = ", ".join(source_items)
            candidate_record = _merge_candidate_record(
                deps,
                challenge_name,
                flags,
                source=sources or "solver",
            )
            pending_flags = list(candidate_record.get("flags", []))
            if pending_flags and (not result or result.status != FLAG_FOUND):
                await notify_discord(
                    deps,
                    "candidate_review",
                    challenge_name,
                    description=(
                        f"**{challenge_name}** 문제의 확정 Flag와 다른 후보를 확인해 주세요."
                        if challenge_name in deps.results
                        else f"**{challenge_name}** 문제의 Flag 후보를 확인해 주세요."
                    ),
                    fields=[("후보", spoiler(pending_flags[0])), ("출처", sources or "solver")],
                    dedupe_key=f"candidate:{challenge_name}:{pending_flags[0]}",
                )
        # A solved result must have been confirmed by the live submission path.
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "accepted_flags": [result.flag] if result.flag else [],
                "submit": "confirmed by solver",
            }
            deps.candidates.pop(challenge_name, None)
            await notify_discord(
                deps,
                "solve_completed",
                challenge_name,
                description=f"**{challenge_name}** 문제 풀이가 완료되었습니다.",
                fields=[("Flag", spoiler(result.flag or ""))],
            )
            meta = deps.challenge_metas[challenge_name]
            try:
                deps.results[challenge_name]["writeup"] = await _generate_or_finalize_writeup(
                    deps,
                    challenge_name,
                    meta.category,
                    result.flag or "",
                )
                deps.results[challenge_name]["experience"] = promote_challenge_experience(
                    deps.settings,
                    challenge_name,
                    meta.category,
                    result.flag or "",
                )
            except Exception as exc:
                logger.warning("Could not finalize documentation for %s: %s", challenge_name, exc)
        persist_deps_state(deps)

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return (
        f"SOL-led swarm spawned for {challenge_name} with {len(deps.model_specs)} primary model(s); "
        "bounded delegates are created on demand"
    )


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    from backend.flag_format import flag_matches_format
    from backend.tools.core import do_submit_flag_detailed

    candidate = flag.strip()
    meta = deps.challenge_metas.get(challenge_name)
    format_hint = getattr(meta, "flag_format", "") if meta else ""
    if not candidate:
        return "REJECTED — empty flag candidate."
    format_mismatch = not flag_matches_format(candidate, format_hint)
    format_warning = ""
    if format_mismatch:
        _merge_candidate_record(
            deps,
            challenge_name,
            [candidate],
            source="operator/format-mismatch",
        )
        swarm = getattr(deps, "swarms", {}).get(challenge_name)
        if swarm is not None:
            swarm.record_candidate("operator/coordinator", candidate)
        persist_deps_state(deps)
        format_warning = (
            f'FORMAT-HINT MISMATCH - preserved exact value "{candidate}" even though it does '
            f'not match "{format_hint}"; the configured verifier remains authoritative.\n'
        )

    if not deps.ctfd.is_configured:
        _merge_candidate_record(deps, challenge_name, [candidate], source="operator")
        persist_deps_state(deps)
        await notify_discord(
            deps,
            "candidate_review",
            challenge_name,
            description=f"**{challenge_name}** 문제의 Flag 후보를 확인해 주세요.",
            fields=[("후보", spoiler(candidate)), ("출처", "operator")],
            dedupe_key=f"candidate:{challenge_name}:{candidate}",
        )
        return format_warning + f'LOCAL CANDIDATE — recorded "{candidate}" for {challenge_name}'
    if deps.no_submit:
        _merge_candidate_record(deps, challenge_name, [candidate], source="operator/dry-run")
        persist_deps_state(deps)
        await notify_discord(
            deps,
            "candidate_review",
            challenge_name,
            description=f"**{challenge_name}** 문제의 Flag 후보를 확인해 주세요.",
            fields=[("후보", spoiler(candidate)), ("출처", "operator/dry-run")],
            dedupe_key=f"candidate:{challenge_name}:{candidate}",
        )
        return format_warning + f'DRY RUN — recorded unverified candidate "{candidate}" for {challenge_name}'
    swarm = getattr(deps, "swarms", {}).get(challenge_name)
    if swarm:
        display, _ = await swarm.try_submit_flag(flag, "operator/coordinator")
        return display
    outcome = await do_submit_flag_detailed(deps.ctfd, challenge_name, flag)
    if outcome.status in {"already_solved", "retryable"}:
        _merge_candidate_record(
            deps,
            challenge_name,
            [candidate],
            source=f"operator/{outcome.status}",
        )
        persist_deps_state(deps)
    return format_warning + outcome.display


async def do_review_candidate(
    deps: CoordinatorDeps,
    challenge_name: str,
    flag: str,
    accepted: bool,
) -> str:
    """Resolve a human review for a standalone/dry-run candidate."""
    record = deps.candidates.get(challenge_name)
    candidate = flag.strip()
    known_flags = set(record.get("flags", [])) if record else set()
    if record and record.get("flag"):
        known_flags.add(record["flag"])
    if not record or candidate not in known_flags:
        return f'NO PENDING CANDIDATE — "{candidate}" is not awaiting review.'

    if accepted:
        prior_result = dict(deps.results.get(challenge_name, {}))
        accepted_flags = list(
            dict.fromkeys(
                item.strip()
                for item in [
                    prior_result.get("flag"),
                    *prior_result.get("accepted_flags", []),
                    candidate,
                ]
                if isinstance(item, str) and item.strip()
            )
        )
        deps.results[challenge_name] = {
            **prior_result,
            "flag": str(prior_result.get("flag", "") or candidate),
            "accepted_flags": accepted_flags,
            "submit": "operator confirmed local candidate",
        }
        remaining_flags = [
            item
            for item in record.get("flags", [])
            if isinstance(item, str)
            and item.strip()
            and item.strip() not in accepted_flags
        ]
        if remaining_flags:
            record = dict(record)
            record.update(
                {
                    "flag": remaining_flags[0],
                    "flags": remaining_flags,
                    "format_mismatches": [
                        item for item in record.get("format_mismatches", [])
                        if item in remaining_flags
                    ],
                    "status": "conflicts_with_solved",
                    "review_required": True,
                }
            )
            deps.candidates[challenge_name] = record
        else:
            deps.candidates.pop(challenge_name, None)
        swarm = getattr(deps, "swarms", {}).get(challenge_name)
        discard = getattr(swarm, "discard_candidate", None)
        if callable(discard):
            discard(candidate)
        meta = deps.challenge_metas.get(challenge_name)
        settings = getattr(deps, "settings", None)
        if meta and settings:
            try:
                deps.results[challenge_name]["writeup"] = await _generate_or_finalize_writeup(
                    deps,
                    challenge_name,
                    getattr(meta, "category", ""),
                    candidate,
                )
                deps.results[challenge_name]["experience"] = promote_challenge_experience(
                    settings,
                    challenge_name,
                    getattr(meta, "category", ""),
                    candidate,
                )
            except Exception as exc:
                logger.warning("Could not finalize documentation for %s: %s", challenge_name, exc)
        persist_deps_state(deps)
        await notify_discord(
            deps,
            "solve_completed",
            challenge_name,
            description=f"**{challenge_name}** 문제를 운영자가 해결로 확정했습니다.",
            fields=[("Flag", spoiler(candidate))],
        )
        return f'LOCAL CONFIRMED — recorded "{candidate}" as solved for {challenge_name}'

    record = dict(record)
    rejected_flags = list(record.get("rejected_flags", []))
    if candidate not in rejected_flags:
        rejected_flags.append(candidate)
    rejected_flags = rejected_flags[-50:]
    remaining_flags = [
        item for item in record.get("flags", [])
        if isinstance(item, str) and item != candidate
    ]
    remaining_mismatches = [
        item for item in record.get("format_mismatches", [])
        if isinstance(item, str) and item != candidate and item in remaining_flags
    ]
    feedback = (
        "CANDIDATE REJECTED BY OPERATOR. Treat this as hard negative evidence: "
        f"the candidate `{candidate}` is incorrect. Do not reuse it or merely "
        "reformat it. Re-check the verifier and pivot from the assumptions that "
        "produced this candidate. Reproduce the next candidate before reporting it."
    )
    record.update(
        {
            "flag": remaining_flags[0] if remaining_flags else "",
            "flags": remaining_flags,
            "rejected_flags": rejected_flags,
            "format_mismatches": remaining_mismatches,
            "feedback": feedback,
            "source": "operator review",
            "status": "unverified" if remaining_flags else "rejected",
            "review_required": bool(remaining_flags),
        }
    )
    deps.candidates[challenge_name] = record

    settings = getattr(deps, "settings", None)
    if settings is not None:
        shared_dir = Path(challenge_shared_path(settings, challenge_name))
        feedback_path = shared_dir / "REJECTED_CANDIDATES.md"
        prior = feedback_path.read_text(encoding="utf-8") if feedback_path.exists() else ""
        entry = (
            f"\n\n## Conflicts\n\n- The operator rejected candidate `{candidate}` as incorrect.\n"
            f"\n## Next experiment\n\n- {feedback}\n"
        )
        feedback_path.write_text((prior + entry)[-30_000:], encoding="utf-8")

    swarm = getattr(deps, "swarms", {}).get(challenge_name)
    task = getattr(deps, "swarm_tasks", {}).get(challenge_name)
    if swarm is not None:
        discard = getattr(swarm, "discard_candidate", None)
        if callable(discard):
            discard(candidate)
    if swarm and task and not task.done():
        await swarm.message_bus.broadcast(feedback, source="operator-review")
        for solver in swarm.solvers.values():
            bump = getattr(solver, "bump", None)
            if callable(bump):
                bump(feedback)
        persist_deps_state(deps)
        return (
            f'LOCAL REJECTED — removed "{candidate}" for {challenge_name}. '
            "The rejection was sent to the active solver as hard negative evidence."
        )

    restart_message = ""
    if settings is not None and challenge_name in getattr(deps, "challenge_dirs", {}):
        restart_message = await do_spawn_swarm(deps, challenge_name, feedback=feedback)
    persist_deps_state(deps)
    return (
        f'LOCAL REJECTED — removed "{candidate}" for {challenge_name}. '
        + ("A new solver swarm was started with the rejection as feedback. " + restart_message
           if restart_message
           else "The rejection was saved; start a new swarm to continue solving.")
    )


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    preserved = preserve_evidence_candidates(deps, challenge_name)
    swarm.kill()
    if preserved:
        return (
            f"Swarm for {challenge_name} cancelled; preserved "
            f"{len(preserved)} unverified candidate(s) from evidence"
        )
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    try:
        solver.bump(insights, urgent=True)
    except TypeError:
        solver.bump(insights)
    await swarm.message_bus.post("coordinator", insights, target=model_spec)
    return (
        f"Bumped {model_spec} on {challenge_name}; guidance queued for both "
        "the active turn and the next turn"
    )


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                timestamp = _trace_timestamp(d.get("ts"))
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(
                        f"{timestamp} step {d.get('step','?')} "
                        f"CALL {d.get('tool','?')}: {args_str}"
                    )
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(
                        f"{timestamp} step {d.get('step','?')} "
                        f"RESULT {d.get('tool','?')}: {result_str}"
                    )
                elif t in ("finish", "error", "bump", "turn_failed"):
                    details = json.dumps({key: value for key, value in d.items() if key != "ts"})
                    summary.append(f"{timestamp} ** {t}: {details}")
                elif t == "usage":
                    summary.append(
                        f"{timestamp} usage: in={d.get('input_tokens',0)} "
                        f"out={d.get('output_tokens',0)} "
                        f"cost=${d.get('cost_usd',0):.4f}"
                    )
                else:
                    summary.append(f"{timestamp} {t}: {str(d)[:80]}")
            except Exception:
                summary.append(f"[time unknown] {line[:100]}")
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
