"""Validated, non-secret runtime settings managed by the local dashboard."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.model_specs import effort_from_spec, provider_from_spec
from backend.models import DEFAULT_MODELS

logger = logging.getLogger(__name__)

RUNTIME_SETTINGS_FILENAME = ".ctf-agent-settings.json"
SUPPORTED_PROVIDERS = {
    "azure",
    "bedrock",
    "claude-sdk",
    "codex",
    "google",
    "openai",
    "zen",
}
_MEMORY_LIMIT_RE = re.compile(r"^[1-9]\d*(?:\.\d+)?[kmgt]?(?:b)?$", re.IGNORECASE)


class RuntimeSettings(BaseModel):
    """Editable coordinator policy. Credentials and filesystem roots are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True)

    models: list[str] = Field(default_factory=lambda: list(DEFAULT_MODELS), min_length=1, max_length=4)
    writeup_model_spec: str = "codex/gpt-5.6-terra/medium"
    writeup_review_model_spec: str = "codex/gpt-5.6-luna/medium"
    max_concurrent_challenges: int = Field(default=1, ge=1, le=32)
    container_memory_limit: str = "4g"
    container_cpu_limit: float = Field(default=2.0, ge=0.1, le=64.0)

    max_attempts_per_challenge: int = Field(default=8, ge=1, le=100)
    solver_turn_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    solver_turn_idle_timeout_seconds: int = Field(default=300, ge=0, le=86_400)
    solver_max_runtime_seconds: int = Field(default=10_800, ge=60, le=604_800)
    solver_max_steps: int = Field(default=300, ge=1, le=10_000)
    solver_max_tokens: int = Field(default=1_500_000, ge=0, le=100_000_000)
    solver_max_raw_tokens: int = Field(default=12_000_000, ge=0, le=500_000_000)
    solver_cached_token_weight: float = Field(default=0.10, ge=0.0, le=1.0)
    solver_turn_slice_tokens: int = Field(default=1_500_000, ge=0, le=100_000_000)
    solver_compaction_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    solver_compaction_max_waits: int = Field(default=2, ge=1, le=10)
    solver_max_estimated_cost_usd: float = Field(default=0.0, ge=0.0, le=10_000.0)
    max_flag_submissions_per_challenge: int = Field(default=8, ge=1, le=100)
    max_command_timeout_seconds: int = Field(default=600, ge=1, le=3600)
    max_interactive_sessions: int = Field(default=4, ge=1, le=16)
    interactive_session_ttl_seconds: int = Field(default=900, ge=30, le=86_400)

    dynamic_delegation_enabled: bool = True
    delegate_model_spec: str = "codex/gpt-5.6-luna/low"
    delegate_hard_model_spec: str = "codex/gpt-5.6-sol/high"
    delegate_verifier_model_spec: str = "codex/gpt-5.6-terra/medium"
    delegate_max_agents: int = Field(default=4, ge=0, le=32)
    delegate_max_concurrent: int = Field(default=2, ge=0, le=16)
    delegate_max_attempts: int = Field(default=4, ge=1, le=100)
    delegate_max_runtime_seconds: int = Field(default=1800, ge=60, le=86_400)
    delegate_turn_timeout_seconds: int = Field(default=600, ge=30, le=86_400)
    delegate_turn_idle_timeout_seconds: int = Field(default=180, ge=0, le=86_400)
    delegate_max_steps: int = Field(default=96, ge=1, le=10_000)
    delegate_max_tokens: int = Field(default=250_000, ge=0, le=100_000_000)
    delegate_max_raw_tokens: int = Field(default=1_200_000, ge=0, le=500_000_000)
    delegate_turn_slice_tokens: int = Field(default=300_000, ge=0, le=100_000_000)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, values: list[str]) -> list[str]:
        normalized = [_validate_model_spec(value, delegate=False) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("models cannot contain duplicates")
        return normalized

    @field_validator(
        "delegate_model_spec", "delegate_hard_model_spec", "delegate_verifier_model_spec"
    )
    @classmethod
    def _validate_delegate_model(cls, value: str) -> str:
        return _validate_model_spec(value, delegate=True)

    @field_validator("writeup_model_spec")
    @classmethod
    def _validate_writeup_model(cls, value: str) -> str:
        return _validate_model_spec(value, delegate=True)

    @field_validator("writeup_review_model_spec")
    @classmethod
    def _validate_writeup_review_model(cls, value: str) -> str:
        return _validate_model_spec(value, delegate=True)

    @field_validator("container_memory_limit")
    @classmethod
    def _validate_memory_limit(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _MEMORY_LIMIT_RE.fullmatch(normalized):
            raise ValueError("container_memory_limit must look like '4g' or '4096m'")
        return normalized

    @model_validator(mode="after")
    def _validate_relationships(self) -> RuntimeSettings:
        if self.solver_turn_idle_timeout_seconds > self.solver_turn_timeout_seconds:
            raise ValueError("solver idle timeout cannot exceed the turn timeout")
        if self.solver_turn_timeout_seconds > self.solver_max_runtime_seconds:
            raise ValueError("solver turn timeout cannot exceed max runtime")
        if (
            self.solver_max_raw_tokens
            and self.solver_turn_slice_tokens > self.solver_max_raw_tokens
        ):
            raise ValueError("solver turn slice cannot exceed the raw token limit")
        if self.delegate_turn_idle_timeout_seconds > self.delegate_turn_timeout_seconds:
            raise ValueError("delegate idle timeout cannot exceed the turn timeout")
        if self.delegate_turn_timeout_seconds > self.delegate_max_runtime_seconds:
            raise ValueError("delegate turn timeout cannot exceed max runtime")
        if (
            self.delegate_max_raw_tokens
            and self.delegate_turn_slice_tokens > self.delegate_max_raw_tokens
        ):
            raise ValueError("delegate turn slice cannot exceed the raw token limit")
        if self.delegate_max_concurrent > self.delegate_max_agents:
            raise ValueError("delegate concurrency cannot exceed the total delegate limit")
        if self.dynamic_delegation_enabled and (
            self.delegate_max_agents < 1 or self.delegate_max_concurrent < 1
        ):
            raise ValueError("enabled delegation requires at least one delegate slot")
        return self


def _validate_model_spec(value: str, *, delegate: bool) -> str:
    spec = str(value).strip()
    parts = spec.split("/")
    if len(parts) not in {2, 3} or not all(parts):
        raise ValueError("model specs must use provider/model[/effort]")
    if provider_from_spec(spec) not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported model provider: {provider_from_spec(spec)}")
    if len(parts) == 3 and effort_from_spec(spec) is None:
        raise ValueError(f"unsupported reasoning effort: {parts[2]}")
    if delegate and provider_from_spec(spec) != "codex":
        raise ValueError("delegate_model_spec must use the codex provider")
    return spec


def runtime_settings_path(challenges_root: str | Path) -> Path:
    """Keep operator preferences beside, but outside, disposable challenge data."""
    return Path(challenges_root).expanduser().resolve().parent / RUNTIME_SETTINGS_FILENAME


def runtime_settings_from(
    settings: object,
    model_specs: list[str],
    max_concurrent_challenges: int | None = None,
) -> RuntimeSettings:
    values = RuntimeSettings().model_dump()
    values["models"] = list(model_specs)
    for field_name in RuntimeSettings.model_fields:
        if field_name == "models":
            continue
        if field_name == "max_concurrent_challenges" and max_concurrent_challenges is not None:
            values[field_name] = max_concurrent_challenges
        elif hasattr(settings, field_name):
            values[field_name] = getattr(settings, field_name)
    return RuntimeSettings.model_validate(values)


def apply_runtime_settings(settings: object, runtime: RuntimeSettings) -> None:
    for field_name, value in runtime.model_dump(exclude={"models"}).items():
        setattr(settings, field_name, value)


def save_runtime_settings(runtime: RuntimeSettings, challenges_root: str | Path) -> Path:
    path = runtime_settings_path(challenges_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(runtime.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def load_runtime_settings(
    settings: object,
    model_specs: list[str],
    challenges_root: str | Path,
) -> RuntimeSettings:
    """Load saved preferences, falling back safely when the file is absent or invalid."""
    current = runtime_settings_from(settings, model_specs)
    path = runtime_settings_path(challenges_root)
    if not path.is_file():
        return current
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("settings file must contain a JSON object")
        merged = current.model_dump()
        merged.update(raw)
        loaded = RuntimeSettings.model_validate(merged)
    except Exception as exc:
        logger.warning("Ignoring invalid dashboard runtime settings at %s: %s", path, exc)
        return current
    apply_runtime_settings(settings, loaded)
    return loaded


def reset_runtime_settings(challenges_root: str | Path) -> RuntimeSettings:
    runtime = RuntimeSettings()
    save_runtime_settings(runtime, challenges_root)
    return runtime
