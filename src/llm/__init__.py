"""LLM integration layer.

The application depends only on `LLMClient` and the DTOs re-exported here; the
concrete API standard (OpenAI / Gemini / Anthropic) is chosen in the config:

    from src.config import Config
    from src.llm import create_llm_client_from, ChatRequest

    config = Config.load()
    async with create_llm_client_from(config) as llm:
        text = await llm.complete("Оцени вакансию", system="Ты HR-аналитик")
"""

from src.llm.base import LLMCapabilities, LLMClient
from src.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnsupportedError,
)
from src.llm.factory import create_llm_client, create_llm_client_from
from src.llm.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingResult,
    FinishReason,
    LLMStandard,
    Message,
    ModelInfo,
    ResponseFormat,
    Role,
    StreamChunk,
    ToolCall,
    ToolChoice,
    ToolSpec,
    Usage,
    build_model_name,
)

__all__ = [
    "LLMClient",
    "LLMCapabilities",
    "LLMStandard",
    "create_llm_client",
    "create_llm_client_from",
    "ChatRequest",
    "ChatResponse",
    "EmbeddingResult",
    "FinishReason",
    "Message",
    "ModelInfo",
    "ResponseFormat",
    "Role",
    "StreamChunk",
    "ToolCall",
    "ToolChoice",
    "ToolSpec",
    "Usage",
    "build_model_name",
    "LLMError",
    "LLMConfigError",
    "LLMUnsupportedError",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMConnectionError",
    "LLMResponseError",
]
