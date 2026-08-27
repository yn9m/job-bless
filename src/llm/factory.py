"""Selects the adapter for the standard configured in `llm.standard`."""

import logging
from typing import Callable, Dict

from src.config import Config, LLMConfig
from src.llm.base import LLMClient
from src.llm.errors import LLMConfigError
from src.llm.models import LLMStandard

logger = logging.getLogger(__name__)


def _openai(config: LLMConfig) -> LLMClient:
    from src.llm.providers.openai import OpenAIClient
    return OpenAIClient(config)


def _gemini(config: LLMConfig) -> LLMClient:
    from src.llm.providers.gemini import GeminiClient
    return GeminiClient(config)


def _anthropic(config: LLMConfig) -> LLMClient:
    from src.llm.providers.anthropic import AnthropicClient
    return AnthropicClient(config)


# Adapters are imported lazily so an unused standard never has to be importable.
_REGISTRY: Dict[LLMStandard, Callable[[LLMConfig], LLMClient]] = {
    LLMStandard.OPENAI: _openai,
    LLMStandard.GEMINI: _gemini,
    LLMStandard.ANTHROPIC: _anthropic,
}


def create_llm_client(config: LLMConfig) -> LLMClient:
    """Build the client for `config.standard`.

    Raises `LLMConfigError` if the standard is unknown or the LLM layer is
    disabled — callers should check `config.llm.enabled` first.
    """
    if not config.enabled:
        raise LLMConfigError("llm layer is disabled (set llm.enabled: true in the config)")

    try:
        standard = LLMStandard(config.standard.strip().lower())
    except ValueError as e:
        supported = ", ".join(s.value for s in LLMStandard)
        raise LLMConfigError(f"unknown llm.standard {config.standard!r}, expected one of: {supported}") from e

    client = _REGISTRY[standard](config)
    logger.info(
        "llm client created: standard=%s base_url=%s model=%s",
        standard.value, config.base_url, config.model,
    )
    return client


def create_llm_client_from(config: Config) -> LLMClient:
    """Convenience wrapper for the application-wide `Config`."""
    return create_llm_client(config.llm)
