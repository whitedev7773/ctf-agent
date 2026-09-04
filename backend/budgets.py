"""Role-aware token accounting and adaptive solver limits."""

from __future__ import annotations

from dataclasses import dataclass

from backend.challenge_profiles import solver_role


@dataclass(frozen=True)
class TokenMetrics:
    raw_tokens: int
    effective_tokens: int
    fresh_input_tokens: int
    cached_input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class SolverTokenLimits:
    effective_tokens: int
    raw_tokens: int
    turn_slice_raw_tokens: int
    attempts: int


_ROLE_FACTORS: dict[str, tuple[float, float, float, float]] = {
    # effective, raw, per-turn slice, attempts
    "scout": (0.30, 0.15, 0.40, 0.40),
    "analyst": (0.85, 0.85, 1.00, 0.85),
    "verifier": (1.00, 1.00, 1.00, 1.00),
    "lead": (1.00, 1.00, 1.00, 1.00),
    "delegate": (0.15, 0.075, 0.17, 0.34),
    "specialist": (0.65, 0.65, 0.80, 0.70),
}


def token_metrics(
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    cached_weight: float,
) -> TokenMetrics:
    """Convert raw provider usage into a cache-weighted work budget.

    Raw tokens remain visible and hard-capped. Effective tokens weight cached
    context reads lower so a productive long-running solve is not stopped merely
    because the same context prefix was served from cache many times.
    """
    input_value = max(0, int(input_tokens or 0))
    output_value = max(0, int(output_tokens or 0))
    cached_value = min(input_value, max(0, int(cached_input_tokens or 0)))
    fresh_value = input_value - cached_value
    weight = min(1.0, max(0.0, float(cached_weight)))
    effective = fresh_value + output_value + round(cached_value * weight)
    return TokenMetrics(
        raw_tokens=input_value + output_value,
        effective_tokens=effective,
        fresh_input_tokens=fresh_value,
        cached_input_tokens=cached_value,
        output_tokens=output_value,
    )


def solver_token_limits(settings: object, model_spec: str) -> SolverTokenLimits:
    role_key = solver_role(model_spec).key
    if role_key == "delegate":
        if "postprocess" in model_spec.casefold():
            return SolverTokenLimits(
                effective_tokens=max(
                    0,
                    int(getattr(settings, "delegate_postprocess_max_tokens", 80_000)),
                ),
                raw_tokens=max(
                    0,
                    int(getattr(settings, "delegate_postprocess_max_raw_tokens", 400_000)),
                ),
                turn_slice_raw_tokens=max(
                    0,
                    int(getattr(settings, "delegate_postprocess_turn_slice_tokens", 150_000)),
                ),
                attempts=max(
                    1,
                    int(getattr(settings, "delegate_postprocess_max_attempts", 1)),
                ),
            )
        return SolverTokenLimits(
            effective_tokens=max(0, int(getattr(settings, "delegate_max_tokens", 250_000))),
            raw_tokens=max(0, int(getattr(settings, "delegate_max_raw_tokens", 1_200_000))),
            turn_slice_raw_tokens=max(
                0,
                int(getattr(settings, "delegate_turn_slice_tokens", 300_000)),
            ),
            attempts=max(1, int(getattr(settings, "delegate_max_attempts", 4))),
        )
    effective_factor, raw_factor, slice_factor, attempt_factor = _ROLE_FACTORS.get(
        role_key,
        _ROLE_FACTORS["specialist"],
    )
    base_effective = max(0, int(getattr(settings, "solver_max_tokens", 1_500_000)))
    base_raw = max(0, int(getattr(settings, "solver_max_raw_tokens", 12_000_000)))
    base_slice = max(0, int(getattr(settings, "solver_turn_slice_tokens", 1_500_000)))
    base_attempts = max(1, int(getattr(settings, "max_attempts_per_challenge", 8)))

    def scaled(value: int, factor: float) -> int:
        return max(1, round(value * factor)) if value else 0

    return SolverTokenLimits(
        effective_tokens=scaled(base_effective, effective_factor),
        raw_tokens=scaled(base_raw, raw_factor),
        turn_slice_raw_tokens=scaled(base_slice, slice_factor),
        attempts=max(1, round(base_attempts * attempt_factor)),
    )


def solver_step_limit(settings: object, model_spec: str) -> int:
    """Return a mandatory per-agent tool-step ceiling."""
    if solver_role(model_spec).key == "delegate":
        if "postprocess" in model_spec.casefold():
            return max(1, int(getattr(settings, "delegate_postprocess_max_steps", 40)))
        return max(1, int(getattr(settings, "delegate_max_steps", 96)))
    return max(1, int(getattr(settings, "solver_max_steps", 300)))


def solver_runtime_limit(settings: object, model_spec: str) -> int:
    """Return a mandatory wall-clock ceiling for one agent."""
    if solver_role(model_spec).key == "delegate":
        if "postprocess" in model_spec.casefold():
            return max(
                1,
                int(getattr(settings, "delegate_postprocess_max_runtime_seconds", 600)),
            )
        return max(1, int(getattr(settings, "delegate_max_runtime_seconds", 1800)))
    return max(1, int(getattr(settings, "solver_max_runtime_seconds", 10800)))


def solver_turn_timeout_limit(settings: object, model_spec: str) -> int:
    """Return a mandatory ceiling for one model turn."""
    if solver_role(model_spec).key == "delegate":
        if "postprocess" in model_spec.casefold():
            return max(
                30,
                int(getattr(settings, "delegate_postprocess_turn_timeout_seconds", 300)),
            )
        return max(30, int(getattr(settings, "delegate_turn_timeout_seconds", 600)))
    return max(30, int(getattr(settings, "solver_turn_timeout_seconds", 1800)))
