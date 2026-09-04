"""Structured output contract and evidence-aware result classification."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from backend.flag_format import flag_matches_format
from backend.solver_base import CANDIDATE_FOUND, FLAG_FOUND, GAVE_UP


class SolverTurnOutput(BaseModel):
    type: Literal["flag_found", "incomplete"]
    flag: str = ""
    method: str


@dataclass(frozen=True)
class OutputAssessment:
    status: str
    flag: str | None
    findings: str


def assess_solver_output(
    *,
    output_type: str,
    flag: str | None,
    method: str | None,
    confirmed_flag: str | None,
    flag_format: str = "",
) -> OutputAssessment:
    """Keep CTFd confirmation authoritative; classify all other flags as candidates."""
    method_text = (method or "unspecified method").strip()

    if confirmed_flag:
        verified = confirmed_flag.strip()
        return OutputAssessment(
            FLAG_FOUND,
            verified,
            f"Flag confirmed by submission: {verified}",
        )

    if output_type == "flag_found":
        candidate = (flag or "").strip()
        if not candidate:
            return OutputAssessment(GAVE_UP, None, "Rejected empty flag candidate.")
        if not flag_matches_format(candidate, flag_format):
            return OutputAssessment(
                GAVE_UP,
                None,
                f'Rejected candidate that does not match flag format "{flag_format}".',
            )
        return OutputAssessment(
            CANDIDATE_FOUND,
            candidate,
            f"Unverified candidate via {method_text}: {candidate}",
        )

    return OutputAssessment(GAVE_UP, None, f"Progress: {method_text}")


def solver_output_json_schema() -> dict:
    """Return the shared Claude/Codex output schema.

    ``incomplete`` lets a turn end without forcing a fabricated flag. The swarm
    remains responsible for retrying until a budget or verified flag stops it.
    """
    return {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["flag_found", "incomplete"],
                "description": "Use flag_found only for a candidate backed by direct solve output.",
            },
            "flag": {
                "type": "string",
                "description": "Candidate flag, or an empty string when type is incomplete.",
            },
            "method": {
                "type": "string",
                "description": "Reproducible verification method or concise progress for the next turn.",
            },
        },
        "required": ["type", "flag", "method"],
        "additionalProperties": False,
    }
