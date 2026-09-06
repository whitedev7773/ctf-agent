"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

from backend.artifacts import challenge_shared_path
from backend.deps import CoordinatorDeps
from backend.experience import promote_challenge_experience
from backend.prompts import ChallengeMeta
from backend.runtime_state import persist_deps_state
from backend.solver_base import FLAG_FOUND
from backend.writeups import finalize_writeup

logger = logging.getLogger(__name__)


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
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        result = await swarm.run()
        if swarm.candidates:
            flags = sorted(
                {candidate.flag for candidate in swarm.candidates.values() if candidate.flag}
            )
            if flags:
                deps.candidates[challenge_name] = {
                    "flag": flags[0],
                    "flags": flags,
                    "sources": sorted(swarm.candidates),
                    "status": "unverified",
                    "review_required": True,
                }
        # A solved result must have been confirmed by the live submission path.
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "confirmed by solver",
            }
            deps.candidates.pop(challenge_name, None)
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

    candidate = flag.strip()
    meta = deps.challenge_metas.get(challenge_name)
    format_hint = getattr(meta, "flag_format", "") if meta else ""
    if not flag_matches_format(candidate, format_hint):
        return f'REJECTED — candidate does not match flag format "{format_hint}".'

    if not deps.ctfd.is_configured:
        deps.candidates[challenge_name] = {
            "flag": candidate,
            "flags": [candidate],
            "source": "operator",
            "status": "unverified",
            "review_required": True,
        }
        persist_deps_state(deps)
        return f'LOCAL CANDIDATE — recorded "{candidate}" for {challenge_name}'
    if deps.no_submit:
        deps.candidates[challenge_name] = {
            "flag": candidate,
            "flags": [candidate],
            "source": "operator/dry-run",
            "status": "unverified",
            "review_required": True,
        }
        persist_deps_state(deps)
        return f'DRY RUN — recorded unverified candidate "{candidate}" for {challenge_name}'
    swarm = getattr(deps, "swarms", {}).get(challenge_name)
    if swarm:
        display, _ = await swarm.try_submit_flag(flag, "operator/coordinator")
        return display
    try:
        result = await deps.ctfd.submit_flag(challenge_name, flag)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


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
        deps.results[challenge_name] = {
            "flag": candidate,
            "submit": "operator confirmed local candidate",
        }
        deps.candidates.pop(challenge_name, None)
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
    feedback = (
        "CANDIDATE REJECTED BY OPERATOR. Treat this as hard negative evidence: "
        f"the candidate `{candidate}` is incorrect. Do not reuse it or merely "
        "reformat it. Re-check the verifier and pivot from the assumptions that "
        "produced this candidate. Reproduce the next candidate before reporting it."
    )
    record.update(
        {
            "flag": "",
            "flags": remaining_flags,
            "rejected_flags": rejected_flags,
            "feedback": feedback,
            "source": "operator review",
            "status": "rejected",
            "review_required": False,
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
    swarm.kill()
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
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
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
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
