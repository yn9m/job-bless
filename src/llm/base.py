"""The port: what the application is allowed to ask any LLM provider for.

`LLMClient` is the single contract the rest of `job-bless` depends on.
Adapters live in `src/llm/providers` and are chosen by `llm.standard` in config.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, ClassVar, List, Optional, Sequence

from src.llm.errors import LLMUnsupportedError
from src.llm.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingResult,
    FinishReason,
    ModelInfo,
    StreamChunk,
    ToolCall,
    Usage,
    LLMStandard,
)


@dataclass(frozen=True)
class LLMCapabilities:
    """What a concrete standard can actually do.

    Callers should check the flag instead of catching `LLMUnsupportedError`
    when the feature is optional for them.
    """

    streaming: bool = True
    tools: bool = True
    json_mode: bool = True
    model_listing: bool = True
    embeddings: bool = False
    token_counting: bool = False


class LLMClient(ABC):
    """Provider-agnostic LLM port.

    Implementations must be safe to reuse across many calls and must be closed
    via `aclose()` (or used as an async context manager).
    """

    standard: ClassVar[LLMStandard]
    capabilities: ClassVar[LLMCapabilities] = LLMCapabilities()

    # --- generation ------------------------------------------------------

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """Run a single generation and return the complete answer."""

    @abstractmethod
    def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        """Run a generation and yield increments as they arrive.

        Implemented as an async generator, so it is *not* awaited:
            async for chunk in client.stream_chat(req): ...
        """

    # --- optional capabilities ------------------------------------------

    async def list_models(self) -> List[ModelInfo]:
        raise LLMUnsupportedError("model listing is not supported", provider=self.standard.value)

    async def count_tokens(self, request: ChatRequest) -> int:
        raise LLMUnsupportedError("token counting is not supported", provider=self.standard.value)

    async def embed(self, texts: Sequence[str], model: Optional[str] = None) -> EmbeddingResult:
        raise LLMUnsupportedError("embeddings are not supported", provider=self.standard.value)

    # --- convenience -----------------------------------------------------

    async def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        """One-shot text generation — the call the application uses most."""
        response = await self.chat(ChatRequest.of(prompt, system=system, **kwargs))
        return response.text

    async def collect_stream(self, request: ChatRequest) -> ChatResponse:
        """Consume `stream_chat` and fold it into a regular `ChatResponse`."""
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        finish_reason = FinishReason.UNKNOWN
        usage = Usage()
        model = request.model or ""
        last_raw = {}

        async for chunk in self.stream_chat(request):
            if chunk.delta_text:
                text_parts.append(chunk.delta_text)
            if chunk.tool_calls:
                tool_calls = chunk.tool_calls
            if chunk.usage:
                usage = chunk.usage
            if chunk.model:
                model = chunk.model
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            last_raw = chunk.raw or last_raw

        return ChatResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=model,
            usage=usage,
            raw=last_raw,
        )

    # --- lifecycle -------------------------------------------------------

    @abstractmethod
    async def aclose(self) -> None:
        """Release the underlying connection pool."""

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
