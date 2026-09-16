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
from backend.notifications import DiscordWebhookNotifier, notify_discord
from backend.poller import CTFdPoller
from backend.prompts import ChallengeMeta
from backend.runtime_state import load_dismissed_challenges, load_runtime_state

logger = logging.getLogger(__name__)

# Callable type for a coordinator turn: (message) -> None
TurnFn = Callable[[str], Coroutine[Any, Any, None]]


def _unsolved_names(deps: CoordinatorDeps, poller: CTFdPoller) -> set[str]:
    """Exclude solved and operator-review candidates from automatic execution."""
    known = poller.known_challenges | set(deps.challenge_metas)
    terminal = (
        poller.known_solved
        | set(deps.results)
        | set(getattr(deps, "candidates", {}))
    )
    return known - terminal - getattr(deps, "dismissed_challenges", set())


def _challenge_priority(deps: CoordinatorDeps, challenge_name: str) -> float:
    """Estimate expected points per minute from locally available metadata."""
    meta = getattr(deps, "challenge_metas", {}).get(challenge_name)
    if meta is None:
        return 0.0
    category = str(getattr(meta, "category", "") or "misc").casefold()
    category_key = next(
        (
            key
            for key in (
                "web",
                "ai",
                "malware",
                "misc",
                "forensics",
                "crypto",
                "reversing",
                "pwn",
            )
            if key in category
        ),
        "misc",
    )
    base_probability = {
        "web": 0.58,
        "ai": 0.46,
        "malware": 0.38,
        "misc": 0.52,
        "forensics": 0.50,
        "crypto": 0.43,
        "reversing": 0.40,
        "pwn": 0.36,
    }[category_key]
    expected_minutes = {
        "web": 22.0,
        "ai": 32.0,
        "malware": 40.0,
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
        notifier=DiscordWebhookNotifier(getattr(settings, "discord_webhook_url", "")),
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

    # A prior process may have been stopped after recording candidate evidence
    # but before returning CANDIDATE_FOUND. Backfill those candidates at startup
    # so the dashboard can review them without spending another solver run.
    from backend.agents.coordinator_core import preserve_evidence_candidates

    for challenge_name in deps.challenge_metas:
        if challenge_name not in deps.results:
            preserve_evidence_candidates(deps, challenge_name)

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

    async def safe_turn(message: str) -> None:
        """Keep coordinator control-plane failures from terminating active solvers."""
        try:
            await turn_fn(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Coordinator turn failed; solver swarms continue: %s", exc, exc_info=True)

    try:
        # Solver work must not wait for a slow or unavailable coordinator model.
        await _auto_spawn_unsolved(deps, poller)
        await safe_turn(initial_msg)

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
                    await notify_discord(
                        deps,
                        "challenge_added",
                        evt.challenge_name,
                        description=f"**{evt.challenge_name}** 문제가 CTFd에 추가되었습니다.",
                        fields=[("출처", "CTFd")],
                    )
                    parts.append(f"NEW CHALLENGE: '{evt.challenge_name}' appeared. Spawn a swarm.")
                    # Auto-spawn for new challenges
                    await _auto_spawn_one(deps, evt.challenge_name)
                elif evt.kind == "challenge_solved":
                    await notify_discord(
                        deps,
                        "solve_completed",
                        evt.challenge_name,
                        description=f"**{evt.challenge_name}** 문제가 해결되었습니다.",
                    )
                    parts.append(f"SOLVED: '{evt.challenge_name}' — swarm auto-killed.")
                    getattr(deps, "swarm_retry_after", {}).pop(evt.challenge_name, None)

            # Detect finished swarms
            retired_any = False
            for name, task in list(deps.swarm_tasks.items()):
                if task.done():
                    retired_any = True
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        parts.append(f"SOLVER ERROR: Swarm for '{name}' failed: {exc}")
                    has_terminal_state = name in deps.results or name in deps.candidates
                    if has_terminal_state:
                        parts.append(f"SOLVER FINISHED: Swarm for '{name}' completed.")
                    else:
                        runs = getattr(deps, "swarm_run_counts", {}).get(name, 1)
                        max_runs = max(
                            1,
                            int(
                                getattr(
                                    deps.settings,
                                    "coordinator_max_swarm_runs_per_challenge",
                                    2,
                                )
                            ),
                        )
                        if runs < max_runs:
                            base_delay = max(
                                0.0,
                                float(
                                    getattr(
                                        deps.settings,
                                        "coordinator_swarm_retry_delay_seconds",
                                        30,
                                    )
                                ),
                            )
                            delay = base_delay * (2 ** max(0, runs - 1))
                            retry_after = getattr(deps, "swarm_retry_after", None)
                            if retry_after is None:
                                retry_after = {}
                                deps.swarm_retry_after = retry_after
                            retry_after[name] = asyncio.get_running_loop().time() + delay
                            parts.append(
                                f"SOLVER FINISHED: Swarm for '{name}' produced no terminal result; "
                                f"automatic retry {runs + 1}/{max_runs} is queued after {int(delay)}s."
                            )
                        else:
                            parts.append(
                                f"SOLVER EXHAUSTED: Swarm for '{name}' used {runs}/{max_runs} "
                                "bounded runs; operator review is required."
                            )
                    deps.swarm_tasks.pop(name, None)
                    deps.swarms.pop(name, None)

            # Fill every free slot deterministically. This also activates retries
            # once their backoff expires, without depending on an LLM tool call.
            if retired_any or len(deps.swarm_tasks) < deps.max_concurrent_challenges:
                await _auto_spawn_unsolved(deps, poller)

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
                await safe_turn(msg)

    except (KeyboardInterrupt, asyncio.CancelledError):
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
    run_counts = getattr(deps, "swarm_run_counts", {})
    max_runs = max(
        1,
        int(getattr(deps.settings, "coordinator_max_swarm_runs_per_challenge", 2)),
    )
    if run_counts.get(challenge_name, 0) >= max_runs:
        return
    retry_after = getattr(deps, "swarm_retry_after", {}).get(challenge_name, 0.0)
    if retry_after > asyncio.get_running_loop().time():
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
