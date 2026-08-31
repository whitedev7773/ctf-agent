"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTFd
    # Empty means standalone mode. A CTFd instance can be connected later from
    # the local dashboard or supplied through CTFD_URL/--ctfd-url.
    ctfd_url: str = ""
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""
    codex_cli_path: str = ""
    enable_api_fallback: bool = False

    # Infra
    sandbox_image: str = "ctf-sandbox"
    # Desktop-safe default: one three-agent swarm at a time. Override explicitly
    # on a larger contest workstation.
    max_concurrent_challenges: int = 1
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "4g"
    container_cpu_limit: float = 2.0
    workspace_root: str = "workspace"

    # Per-solver hard budgets. Zero disables token/cost limits only; time, step,
    # and attempt limits stay mandatory so a wedged model cannot run forever.
    solver_turn_timeout_seconds: int = 1800
    solver_max_runtime_seconds: int = 7200
    solver_max_steps: int = 240
    solver_max_tokens: int = 1_000_000
    solver_max_estimated_cost_usd: float = 0.0
    max_flag_submissions_per_challenge: int = 8
    max_command_timeout_seconds: int = 600

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
