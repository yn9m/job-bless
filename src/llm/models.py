"""Unified data contract between the application and any LLM provider.

These DTOs are the only vocabulary the application layer is allowed to use.
Adapters in `src/llm/providers` translate them to/from the wire format of a
concrete API standard (OpenAI / Gemini / Anthropic).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LLMStandard(str, Enum):
    """API standard the client speaks. Selected via `llm.standard` in config."""

    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(str, Enum):
    STOP = "stop"                      # model finished naturally
    LENGTH = "length"                  # hit max_tokens
    TOOL_CALLS = "tool_calls"          # model wants a tool to be executed
    CONTENT_FILTER = "content_filter"  # blocked by safety filters
    UNKNOWN = "unknown"


class ToolChoice(str, Enum):
    AUTO = "auto"        # model decides
    NONE = "none"        # tools disabled for this call
    REQUIRED = "required"  # model must call some tool


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """Declaration of a tool the model is allowed to call.

    `parameters` is a plain JSON Schema object, identical across all standards.
    """

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class Message:
    """One turn of the conversation."""

    role: Role
    content: str = ""
    # Only for role=ASSISTANT: tool calls the model produced.
    tool_calls: List[ToolCall] = field(default_factory=list)
    # Only for role=TOOL: id of the ToolCall this message answers.
    tool_call_id: str = ""
    # Optional participant name (supported by OpenAI, ignored elsewhere).
    name: str = ""

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Optional[List[ToolCall]] = None) -> "Message":
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str, name: str = "") -> "Message":
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id, name=name)


@dataclass
class ResponseFormat:
    """Structured output request.

    type="text"        — free-form text (default)
    type="json"        — model must answer with a JSON object
    type="json_schema" — model must answer with JSON matching `schema`
    """

    type: str = "text"
    schema: Optional[Dict[str, Any]] = None


@dataclass
class ChatRequest:
    """A single generation request in provider-neutral form.

    Fields left as `None` fall back to the values from `LLMConfig`.
    """

    messages: List[Message] = field(default_factory=list)
    # Convenience: prepended as a system instruction if set.
    system: str = ""
    model: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: List[str] = field(default_factory=list)
    tools: List[ToolSpec] = field(default_factory=list)
    tool_choice: Any = ToolChoice.AUTO  # ToolChoice | str (exact tool name)
    response_format: ResponseFormat = field(default_factory=ResponseFormat)
    timeout_sec: Optional[float] = None
    # Raw provider-specific payload keys, merged into the request body as-is.
    # Escape hatch — using it couples the caller to one standard.
    extra_body: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, prompt: str, *, system: str = "", **kwargs) -> "ChatRequest":
        """Shortcut for the common single-prompt case."""
        return cls(messages=[Message.user(prompt)], system=system, **kwargs)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResponse:
    """Complete model answer."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: FinishReason = FinishReason.UNKNOWN
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    request_id: str = ""
    # Untouched provider payload, for debugging and provider-specific extras.
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> Message:
        """Turn the answer into an assistant message for the next turn."""
        return Message.assistant(content=self.text, tool_calls=list(self.tool_calls))


@dataclass
class StreamChunk:
    """One increment of a streamed answer.

    Adapters accumulate tool-call fragments internally and emit complete
    `tool_calls` only on the final chunk (the one carrying `finish_reason`).
    """

    delta_text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[FinishReason] = None
    usage: Optional[Usage] = None
    model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        return self.finish_reason is not None


@dataclass
class ModelInfo:
    id: str
    display_name: str = ""
    owned_by: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    vectors: List[List[float]] = field(default_factory=list)
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    raw: Dict[str, Any] = field(default_factory=dict)


# --- AI Studio bridge model-name modifiers -------------------------------
# The bridge encodes non-standard Gemini options as suffixes of the model name,
# which is the only way to pass them through stock OpenAI/Anthropic clients.
# Required order: thinking -> stream -> tools.

THINKING_LEVELS = ("minimal", "low", "medium", "high")
STREAM_MODES = ("real", "fake")


def build_model_name(
    model: str,
    *,
    thinking: str = "",
    stream_mode: str = "",
    search: bool = False,
    code: bool = False,
) -> str:
    """Append AI Studio bridge modifiers to a model name.

    >>> build_model_name("gemini-3-flash-preview", thinking="minimal", stream_mode="fake", search=True)
    'gemini-3-flash-preview-minimal-fake-search'
    """
    name = model
    if thinking:
        if thinking not in THINKING_LEVELS:
            raise ValueError(f"unknown thinking level {thinking!r}, expected one of {THINKING_LEVELS}")
        name += f"-{thinking}"
    if stream_mode:
        if stream_mode not in STREAM_MODES:
            raise ValueError(f"unknown stream mode {stream_mode!r}, expected one of {STREAM_MODES}")
        name += f"-{stream_mode}"
    if search:
        name += "-search"
    if code:
        name += "-code"
    return name
