"""OpenAI-compatible adapter (`/v1/chat/completions`).

Auth: `Authorization: Bearer <api_key>`.
Endpoints used: /v1/chat/completions, /v1/models, /v1/embeddings,
/v1/responses/input_tokens.
"""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from src.config import LLMConfig
from src.llm.base import LLMCapabilities
from src.llm.errors import LLMResponseError
from src.llm.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingResult,
    FinishReason,
    LLMStandard,
    Message,
    ModelInfo,
    Role,
    StreamChunk,
    ToolCall,
    Usage,
)
from src.llm.providers.common import HttpLLMClient

logger = logging.getLogger(__name__)

CHAT_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
EMBEDDINGS_PATH = "/v1/embeddings"
INPUT_TOKENS_PATH = "/v1/responses/input_tokens"

FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


class OpenAIClient(HttpLLMClient):
    standard = LLMStandard.OPENAI
    capabilities = LLMCapabilities(
        streaming=True,
        tools=True,
        json_mode=True,
        model_listing=True,
        embeddings=True,
        token_counting=True,
    )

    def _auth_headers(self, config: LLMConfig) -> Dict[str, str]:
        return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}

    # --- generation -------------------------------------------------------

    async def chat(self, request: ChatRequest) -> ChatResponse:
        body = self._build_body(request, stream=False)
        data = await self._transport.request_json(
            "POST", CHAT_PATH, json_body=body, timeout_sec=request.timeout_sec
        )
        return self._parse_response(data)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        body = self._build_body(request, stream=True)
        # Tool-call fragments arrive split across chunks, keyed by index.
        pending: Dict[int, Dict[str, str]] = {}
        model = ""

        async for event in self._transport.stream_sse(
            "POST", CHAT_PATH, json_body=body, timeout_sec=request.timeout_sec
        ):
            data = event.data
            model = data.get("model", model)
            usage = _parse_usage(data.get("usage"))
            choices = data.get("choices") or []
            if not choices:
                if usage:
                    yield StreamChunk(usage=usage, model=model, raw=data)
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            for fragment in delta.get("tool_calls") or []:
                _accumulate_tool_call(pending, fragment)

            raw_finish = choice.get("finish_reason")
            finish_reason = FINISH_REASONS.get(raw_finish, FinishReason.UNKNOWN) if raw_finish else None
            tool_calls = _build_tool_calls(pending) if finish_reason else []
            if tool_calls and finish_reason == FinishReason.UNKNOWN:
                finish_reason = FinishReason.TOOL_CALLS

            yield StreamChunk(
                delta_text=delta.get("content") or "",
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                model=model,
                raw=data,
            )

    # --- optional capabilities -------------------------------------------

    async def list_models(self) -> List[ModelInfo]:
        data = await self._transport.request_json("GET", MODELS_PATH)
        return [
            ModelInfo(
                id=str(item.get("id", "")),
                display_name=str(item.get("id", "")),
                owned_by=str(item.get("owned_by", "")),
                raw=item,
            )
            for item in data.get("data") or []
        ]

    async def count_tokens(self, request: ChatRequest) -> int:
        system, messages = self.split_system(request)
        payload: Dict[str, Any] = {
            "model": self.resolve_model(request),
            "input": [_to_wire_message(m) for m in messages],
        }
        if system:
            payload["instructions"] = system
        data = await self._transport.request_json("POST", INPUT_TOKENS_PATH, json_body=payload)
        for key in ("input_tokens", "total_tokens", "count", "tokens"):
            if isinstance(data.get(key), int):
                return data[key]
        raise LLMResponseError("no token count in response", provider=self.standard.value, raw=data)

    async def embed(self, texts: Sequence[str], model: Optional[str] = None) -> EmbeddingResult:
        payload = {"model": self.resolve_embedding_model(model), "input": list(texts)}
        data = await self._transport.request_json("POST", EMBEDDINGS_PATH, json_body=payload)
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        return EmbeddingResult(
            vectors=[list(item.get("embedding") or []) for item in items],
            model=str(data.get("model", payload["model"])),
            usage=_parse_usage(data.get("usage")) or Usage(),
            raw=data,
        )

    # --- mapping ----------------------------------------------------------

    def _build_body(self, request: ChatRequest, *, stream: bool) -> Dict[str, Any]:
        system, messages = self.split_system(request)
        wire_messages: List[Dict[str, Any]] = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(_to_wire_message(m) for m in messages)

        body: Dict[str, Any] = {
            "model": self.resolve_model(request),
            "messages": wire_messages,
            "temperature": self.resolve_temperature(request),
            "max_tokens": self.resolve_max_tokens(request),
            "stream": stream,
        }
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop"] = request.stop
        if stream:
            body["stream_options"] = {"include_usage": True}

        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
            mode = self.tool_choice_mode(request.tool_choice)
            if mode == "named":
                body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": self.named_tool_choice(request.tool_choice)},
                }
            else:
                body["tool_choice"] = mode

        fmt = request.response_format
        if fmt.type == "json":
            body["response_format"] = {"type": "json_object"}
        elif fmt.type == "json_schema" and fmt.schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": fmt.schema, "strict": True},
            }

        return self.merge_extra(body, request)

    def _parse_response(self, data: Dict[str, Any]) -> ChatResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMResponseError("response contains no choices", provider=self.standard.value, raw=data)

        choice = choices[0]
        message = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=str(call.get("id", "")),
                name=str((call.get("function") or {}).get("name", "")),
                arguments=_parse_arguments((call.get("function") or {}).get("arguments")),
            )
            for call in message.get("tool_calls") or []
        ]
        finish_reason = FINISH_REASONS.get(choice.get("finish_reason"), FinishReason.UNKNOWN)
        if tool_calls and finish_reason == FinishReason.UNKNOWN:
            finish_reason = FinishReason.TOOL_CALLS

        return ChatResponse(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=str(data.get("model", "")),
            usage=_parse_usage(data.get("usage")) or Usage(),
            request_id=str(data.get("id", "")),
            raw=data,
        )


def _to_wire_message(message: Message) -> Dict[str, Any]:
    if message.role == Role.TOOL:
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }

    wire: Dict[str, Any] = {"role": message.role.value, "content": message.content or None}
    if message.name:
        wire["name"] = message.name
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in message.tool_calls
        ]
    return wire


def _accumulate_tool_call(pending: Dict[int, Dict[str, str]], fragment: Dict[str, Any]) -> None:
    index = fragment.get("index", 0)
    slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if fragment.get("id"):
        slot["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name"):
        slot["name"] = function["name"]
    if function.get("arguments"):
        slot["arguments"] += function["arguments"]


def _build_tool_calls(pending: Dict[int, Dict[str, str]]) -> List[ToolCall]:
    return [
        ToolCall(id=slot["id"], name=slot["name"], arguments=_parse_arguments(slot["arguments"]))
        for _, slot in sorted(pending.items())
    ]


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tool call arguments are not valid JSON: %.200s", raw)
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _parse_usage(raw: Any) -> Optional[Usage]:
    if not isinstance(raw, dict):
        return None
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
        completion_tokens=int(raw.get("completion_tokens", 0) or 0),
        total_tokens=int(raw.get("total_tokens", 0) or 0),
    )
