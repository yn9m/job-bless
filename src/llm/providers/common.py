"""Shared plumbing for HTTP-based adapters (config defaults + transport)."""

import logging
from typing import Any, Dict, List, Optional

from src.config import LLMConfig
from src.llm.base import LLMClient
from src.llm.errors import LLMConfigError
from src.llm.http import HttpTransport
from src.llm.models import ChatRequest, Message, Role, ToolChoice, build_model_name

logger = logging.getLogger(__name__)


class HttpLLMClient(LLMClient):
    """Base for the OpenAI / Gemini / Anthropic adapters.

    Subclasses provide auth headers and the request/response mapping; defaults,
    model-name resolution and connection lifecycle are handled here.
    """

    def __init__(self, config: LLMConfig):
        if not config.base_url:
            raise LLMConfigError("llm.base_url is empty", provider=self.standard.value)
        if not config.model:
            raise LLMConfigError("llm.model is empty", provider=self.standard.value)

        self.config = config
        headers = {"Content-Type": "application/json", **self._auth_headers(config), **config.extra_headers}
        self._transport = HttpTransport(
            provider=self.standard.value,
            base_url=config.base_url,
            headers=headers,
            timeout_sec=config.timeout_sec,
            max_retries=config.max_retries,
            retry_backoff_sec=config.retry_backoff_sec,
        )

    def _auth_headers(self, config: LLMConfig) -> Dict[str, str]:
        raise NotImplementedError

    async def aclose(self) -> None:
        await self._transport.aclose()

    # --- request defaults -------------------------------------------------

    def resolve_model(self, request: ChatRequest) -> str:
        """Model for this request, with the configured bridge modifiers applied."""
        mods = self.config.modifiers
        return build_model_name(
            request.model or self.config.model,
            thinking=mods.thinking,
            stream_mode=mods.stream_mode,
            search=mods.search,
            code=mods.code,
        )

    def resolve_temperature(self, request: ChatRequest) -> float:
        return self.config.temperature if request.temperature is None else request.temperature

    def resolve_max_tokens(self, request: ChatRequest) -> int:
        return self.config.max_tokens if request.max_tokens is None else request.max_tokens

    def resolve_embedding_model(self, model: Optional[str]) -> str:
        return model or self.config.embedding_model

    # --- message helpers --------------------------------------------------

    @staticmethod
    def split_system(request: ChatRequest) -> tuple[str, List[Message]]:
        """Extract system instructions for standards that keep them separate."""
        blocks: List[str] = []
        if request.system:
            blocks.append(request.system)
        rest: List[Message] = []
        for message in request.messages:
            if message.role == Role.SYSTEM:
                blocks.append(message.content)
            else:
                rest.append(message)
        return "\n\n".join(b for b in blocks if b), rest

    @staticmethod
    def named_tool_choice(tool_choice: Any) -> str:
        """Return the tool name if `tool_choice` names a specific tool."""
        if isinstance(tool_choice, ToolChoice):
            return ""
        if isinstance(tool_choice, str) and tool_choice not in {c.value for c in ToolChoice}:
            return tool_choice
        return ""

    @staticmethod
    def tool_choice_mode(tool_choice: Any) -> str:
        """Normalize `tool_choice` to 'auto' | 'none' | 'required' | 'named'."""
        if isinstance(tool_choice, ToolChoice):
            return tool_choice.value
        if isinstance(tool_choice, str):
            if tool_choice in {c.value for c in ToolChoice}:
                return tool_choice
            return "named"
        return ToolChoice.AUTO.value

    @staticmethod
    def merge_extra(body: Dict[str, Any], request: ChatRequest) -> Dict[str, Any]:
        if request.extra_body:
            body.update(request.extra_body)
        return body
