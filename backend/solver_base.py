"""Solver result type, status constants, and solver protocol — shared across all backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# Status constants
FLAG_FOUND = "flag_found"
CANDIDATE_FOUND = "candidate_found"
GAVE_UP = "gave_up"
HANDOFF_COMPLETE = "handoff_complete"
CANCELLED = "cancelled"
ERROR = "error"
QUOTA_ERROR = "quota_error"
BUDGET_EXHAUSTED = "budget_exhausted"
PROGRESS_CHECKPOINT = "progress_checkpoint"

# Only an explicit verifier acceptance proves the submitted value. An
# ``already_solved`` response describes challenge state, not this candidate.
CORRECT_MARKERS = ("CORRECT",)


def solver_agent_name(challenge_name: str, model_spec: str) -> str:
    """Build a unique accounting/logging key for a solver configuration."""
    return f"{challenge_name}/{model_spec}"


@dataclass
class SolverResult:
    flag: str | None
    status: str
    findings_summary: str
    step_count: int
    cost_usd: float
    log_path: str
    stop_reason: str = ""
    workspace_path: str = ""
    attempt: int = 0


class SolverProtocol(Protocol):
    """Common interface for all solver backends (Pydantic AI, Claude SDK, Codex)."""

    model_spec: str
    agent_name: str
    sandbox: Any

    async def start(self) -> None: ...
    async def run_until_done_or_gave_up(self) -> SolverResult: ...
    def bump(self, insights: str) -> None: ...
    def set_cancel_reason(self, reason: str) -> None: ...
    async def stop(self) -> None: ...
