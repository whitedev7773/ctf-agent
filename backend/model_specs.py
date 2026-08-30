"""Pure helpers for parsing solver model specifications."""

from __future__ import annotations


# Subscription-backed providers can continue through these API-backed models.
QUOTA_FALLBACKS: dict[str, str] = {
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.6-sol": "openai/gpt-5.6-sol",
    "codex/gpt-5.6-terra": "openai/gpt-5.6-terra",
    "codex/gpt-5.6-luna": "openai/gpt-5.6-luna",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def provider_from_spec(spec: str) -> str:
    """Extract the provider from ``provider/model[/effort]``."""
    return spec.split("/", 1)[0]


def model_id_from_spec(spec: str) -> str:
    """Extract the model ID from ``provider/model[/effort]``."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def effort_from_spec(spec: str) -> str | None:
    """Extract a supported effort suffix from a model specification."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ):
        return parts[2]
    return None


def base_model_spec(spec: str) -> str:
    """Return ``provider/model`` with any effort suffix removed."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 else spec


def quota_fallback_spec(spec: str) -> str | None:
    """Resolve a quota fallback while ignoring an effort suffix."""
    return QUOTA_FALLBACKS.get(base_model_spec(spec))
