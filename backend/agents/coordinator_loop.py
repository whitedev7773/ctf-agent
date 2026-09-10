"""Shared coordinator event loop — used by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.deps import CoordinatorDeps
from backend.models import DEFAULT_MODELS
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta
from backend.runtime_state import load_dismissed_challenges, load_runtime_state

logger = logging.getLogger(__name__)

# Callable type for a coordinator turn: (message) -> None
TurnFn = Callable[[str], Coroutine[Any, Any, None]]


def _unsolved_names(deps: CoordinatorDeps, poller: CTFdPoller) -> set[str]:
    """Treat locally persisted confirmations as solved across restarts."""
    known = poller.known_challenges | set(deps.challenge_metas)
    solved = poller.known_solved | set(deps.results)
    return known - solved - getattr(deps, "dismissed_challenges", set())


def _challenge_priority(deps: CoordinatorDeps, challenge_name: str) -> float:
    """Estimate expected points per minute from locally available metadata."""
    meta = getattr(deps, "challenge_metas", {}).get(challenge_name)
    if meta is None:
        return 0.0
    category = str(getattr(meta, "category", "") or "misc").casefold()
    category_key = next(
        (
            key
            for key in ("web", "misc", "forensics", "crypto", "reversing", "pwn")
            if key in category
        ),
        "misc",
    )
    base_probability = {
        "web": 0.58,
        "misc": 0.52,
        "forensics": 0.50,
        "crypto": 0.43,
        "reversing": 0.40,
        "pwn": 0.36,
    }[category_key]
    expected_minutes = {
        "web": 22.0,
        "misc": 18.0,
        "forensics": 28.0,
        "crypto": 34.0,
        "reversing": 38.0,
        "pwn": 42.0,
    }[category_key]
    solves = max(0, int(getattr(meta, "solves", 0) or 0))
    solve_probability = min(0.95, base_probability + math.log1p(solves) / 14.0)
    points = max(1, int(getattr(meta, "value", 0) or 0))

    challenge_dir = getattr(deps, "challenge_dirs", {}).get(challenge_name, "")
    distfiles = Path(challenge_dir) / "distfiles" if challenge_dir else None
    try:
        file_count = sum(1 for item in distfiles.iterdir() if item.is_file()) if distfiles else 0
    except OSError:
        file_count = 0
    connection_info = str(getattr(meta, "connection_info", "") or "")
    if file_count:
        expected_minutes *= min(1.25, 0.90 + 0.04 * file_count)
    elif connection_info:
        expected_minutes *= 1.15
    return points * solve_probability / expected_minutes


def _rank_unsolved(deps: CoordinatorDeps, poller: CTFdPoller) -> list[str]:
    """Return a deterministic value/cost ordering for auto-spawn."""
    return sorted(
        _unsolved_names(deps, poller),
        key=lambda name: (-_challenge_priority(deps, name), name.casefold()),
    )


def build_deps(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    challenge_dirs: dict[str, str] | None = None,
    challenge_metas: dict[str, ChallengeMeta] | None = None,
) -> tuple[CTFdClient, CostTracker, CoordinatorDeps]:
    """Create CTFd client, cost tracker, and coordinator deps."""
    ctfd = CTFdClient(
        base_url=settings.ctfd_url,
        token=settings.ctfd_token,
        username=settings.ctfd_user,
        password=settings.ctfd_pass,
    )
    cost_tracker = CostTracker()
    specs = model_specs or list(DEFAULT_MODELS)
    Path(challenges_root).mkdir(parents=True, exist_ok=True)

    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=specs,
        challenges_root=challenges_root,
        no_submit=no_submit or not ctfd.is_configured,
        force_no_submit=no_submit,
        max_concurrent_challenges=getattr(settings, "max_concurrent_challenges", 10),
        challenge_dirs=challenge_dirs or {},
        challenge_metas=challenge_metas or {},
    )

    # Standalone confirmations and pending review candidates survive a normal
    # coordinator restart. A dashboard runtime reset removes this file together
    # with the workspace tree.
    persisted_results, persisted_candidates = load_runtime_state(settings)
    deps.results.update(persisted_results)
    deps.candidates.update(persisted_candidates)
    deps.dismissed_challenges.update(load_dismissed_challenges(settings))

    # Pre-load already-pulled challenges
    for d in Path(challenges_root).iterdir():
        meta_path = d / "metadata.yml"
        if meta_path.exists():
            meta = ChallengeMeta.from_yaml(meta_path)
            if meta.name not in deps.challenge_dirs:
                deps.challenge_dirs[meta.name] = str(d)
                deps.challenge_metas[meta.name] = meta

    return ctfd, cost_tracker, deps


async def run_event_loop(
    deps: CoordinatorDeps,
    ctfd: CTFdClient,
    cost_tracker: CostTracker,
    turn_fn: TurnFn,
    status_interval: int = 60,
) -> dict[str, Any]:
    """Run the shared coordinator event loop.

    Args:
        deps: Coordinator dependencies (shared state).
        ctfd: CTFd client (for poller).
        cost_tracker: Cost tracker.
        turn_fn: Async function that sends a message to the coordinator LLM.
        status_interval: Seconds between status updates.
    """
    poller = CTFdPoller(ctfd=ctfd, interval_s=5.0)
    await poller.start()

    # Dashboard runs in this process so it can inspect and control live swarms.
    dashboard_server = None
    try:
        from backend.dashboard import DashboardServer

        dashboard_server = DashboardServer(
            deps,
            poller,
            cost_tracker,
            deps.msg_port,
            deps.msg_host,
        )
        await dashboard_server.start()
        logger.info(
            "Dashboard listening on %s:%d",
            deps.msg_host,
            dashboard_server.actual_port,
        )
    except Exception as e:
        logger.warning("Could not start dashboard: %s", e, exc_info=True)

    logger.info(
        "Coordinator starting: %d models, %d challenges, %d solved",
        len(deps.model_specs),
        len(poller.known_challenges),
        len(poller.known_solved),
    )

    dismissed = getattr(deps, "dismissed_challenges", set())
    known = (poller.known_challenges | set(deps.challenge_metas)) - dismissed
    solved = poller.known_solved | set(deps.results)
    unsolved = known - solved
    mode = "CTFd connected" if ctfd.is_configured else "standalone local mode"
    initial_msg = (
        f"CTF Agent started in {mode}. {len(known)} challenges, "
        f"{len(solved)} solved.\n"
        f"Unsolved: {sorted(unsolved) if unsolved else 'NONE'}\n"
        "Spawn swarms for available unsolved challenges."
    )

    try:
        await turn_fn(initial_msg)

        # Auto-spawn swarms for unsolved challenges if coordinator LLM didn't
        await _auto_spawn_unsolved(deps, poller)

        last_status = asyncio.get_event_loop().time()

        while True:
            events = []
            evt = await poller.get_event(timeout=5.0)
            if evt:
                events.append(evt)
            events.extend(poller.drain_events())

            # Auto-kill swarms for solved challenges
            for evt in events:
                if evt.kind == "challenge_solved" and evt.challenge_name in deps.swarms:
                    swarm = deps.swarms[evt.challenge_name]
                    if not swarm.cancel_event.is_set():
                        swarm.kill()
                        logger.info("Auto-killed swarm for: %s", evt.challenge_name)

            parts: list[str] = []
            for evt in events:
                if evt.kind == "new_challenge":
                    if evt.challenge_name in dismissed:
                        continue
                    parts.append(f"NEW CHALLENGE: '{evt.challenge_name}' appeared. Spawn a swarm.")
                    # Auto-spawn for new challenges
                    await _auto_spawn_one(deps, evt.challenge_name)
                elif evt.kind == "challenge_solved":
                    parts.append(f"SOLVED: '{evt.challenge_name}' — swarm auto-killed.")

            # Detect finished swarms
            for name, task in list(deps.swarm_tasks.items()):
                if task.done():
                    parts.append(
                        f"SOLVER FINISHED: Swarm for '{name}' completed. Check results or retry."
                    )
                    deps.swarm_tasks.pop(name, None)

            # Drain solver-to-coordinator messages
            while True:
                try:
                    solver_msg = deps.coordinator_inbox.get_nowait()
                    parts.append(f"SOLVER MESSAGE: {solver_msg}")
                except asyncio.QueueEmpty:
                    break

            # Drain operator messages
            while True:
                try:
                    op_msg = deps.operator_inbox.get_nowait()
                    parts.append(f"OPERATOR MESSAGE: {op_msg}")
                    logger.info("Operator message: %s", op_msg[:200])
                except asyncio.QueueEmpty:
                    break

            # Periodic status update — only when there are active swarms or other events
            now = asyncio.get_event_loop().time()
            if now - last_status >= status_interval:
                last_status = now
                active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
                solved_set = poller.known_solved | set(deps.results)
                unsolved_set = (
                    (poller.known_challenges | set(deps.challenge_metas)) - solved_set - dismissed
                )
                status_line = (
                    f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
                    f"{len(active)} active swarms. Cost: ${cost_tracker.total_cost_usd:.2f}"
                )
                # Only send to coordinator if there's something happening
                if active or parts:
                    parts.append(status_line)
                else:
                    logger.info(f"Event -> coordinator: {status_line}")

            if parts:
                msg = "\n\n".join(parts)
                logger.info("Event -> coordinator: %s", msg[:200])
                await turn_fn(msg)

    except KeyboardInterrupt, asyncio.CancelledError:
        logger.info("Coordinator shutting down...")
    except Exception as e:
        logger.error("Coordinator fatal: %s", e, exc_info=True)
    finally:
        if dashboard_server:
            await dashboard_server.stop()
        await poller.stop()
        for swarm in deps.swarms.values():
            swarm.kill()
        for task in deps.swarm_tasks.values():
            task.cancel()
        if deps.swarm_tasks:
            await asyncio.gather(*deps.swarm_tasks.values(), return_exceptions=True)
        cost_tracker.log_summary()
        try:
            await ctfd.close()
        except Exception:
            pass

    return {
        "results": deps.results,
        "candidates": deps.candidates,
        "total_cost_usd": cost_tracker.total_cost_usd,
        "total_tokens": cost_tracker.total_tokens,
    }


async def _auto_spawn_one(deps: CoordinatorDeps, challenge_name: str) -> None:
    """Auto-spawn a swarm for a single challenge if not already running."""
    if challenge_name in getattr(deps, "dismissed_challenges", set()):
        return
    if challenge_name in deps.swarms:
        return
    active = sum(1 for t in deps.swarm_tasks.values() if not t.done())
    if active >= deps.max_concurrent_challenges:
        return
    try:
        from backend.agents.coordinator_core import do_spawn_swarm

        result = await do_spawn_swarm(deps, challenge_name)
        logger.info(f"Auto-spawn {challenge_name}: {result[:100]}")
    except Exception as e:
        logger.warning(f"Auto-spawn failed for {challenge_name}: {e}")


async def _auto_spawn_unsolved(deps: CoordinatorDeps, poller) -> None:
    """Auto-spawn swarms for all unsolved challenges that don't have active swarms."""
    for name in _rank_unsolved(deps, poller):
        await _auto_spawn_one(deps, name)
    return
