"""Anthropic-compatible adapter (`/v1/messages`).

Auth: `x-api-key: <api_key>` plus the required `anthropic-version` header.
Endpoints used: /v1/messages, /v1/messages/count_tokens, GET /v1/models.
"""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from src.config import LLMConfig
from src.llm.base import LLMCapabilities
from src.llm.errors import LLMResponseError
from src.llm.models import (
    ChatRequest,
    ChatResponse,
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

MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
MODELS_PATH = "/v1/models"

STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "refusal": FinishReason.CONTENT_FILTER,
}

TOOL_MODES = {"auto": "auto", "none": "none", "required": "any"}


class AnthropicClient(HttpLLMClient):
    standard = LLMStandard.ANTHROPIC
    capabilities = LLMCapabilities(
        streaming=True,
        tools=True,
        json_mode=False,  # no native JSON mode; use a tool or prompt instructions
        model_listing=True,
        embeddings=False,
        token_counting=True,
    )

    def _auth_headers(self, config: LLMConfig) -> Dict[str, str]:
        headers = {"anthropic-version": config.anthropic_version}
        if config.api_key:
            headers["x-api-key"] = config.api_key
        return headers

    # --- generation -------------------------------------------------------

    async def chat(self, request: ChatRequest) -> ChatResponse:
        body = self._build_body(request, stream=False)
        data = await self._transport.request_json(
            "POST", MESSAGES_PATH, json_body=body, timeout_sec=request.timeout_sec
        )
        return self._parse_response(data)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        body = self._build_body(request, stream=True)
        # content_block_start announces a block; deltas fill it in by index.
        blocks: Dict[int, Dict[str, Any]] = {}
        model = ""
        usage = Usage()

        async for sse in self._transport.stream_sse(
            "POST", MESSAGES_PATH, json_body=body, timeout_sec=request.timeout_sec
        ):
            data = sse.data
            event_type = str(data.get("type") or sse.event)

            if event_type == "message_start":
                message = data.get("message") or {}
                model = str(message.get("model", model))
                usage = _parse_usage(message.get("usage")) or usage
                continue

            if event_type == "content_block_start":
                block = data.get("content_block") or {}
                blocks[data.get("index", 0)] = {
                    "type": block.get("type"),
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "json": "",
                }
                continue

            if event_type == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield StreamChunk(delta_text=delta.get("text") or "", model=model, raw=data)
                elif delta.get("type") == "input_json_delta":
                    slot = blocks.setdefault(data.get("index", 0), {"type": "tool_use", "id": "", "name": "", "json": ""})
                    slot["json"] += delta.get("partial_json") or ""
                continue

            if event_type == "message_delta":
                delta = data.get("delta") or {}
                stop_reason = delta.get("stop_reason")
                chunk_usage = _parse_usage(data.get("usage"))
                if chunk_usage:
                    usage = Usage(
                        prompt_tokens=usage.prompt_tokens or chunk_usage.prompt_tokens,
                        completion_tokens=chunk_usage.completion_tokens or usage.completion_tokens,
                        total_tokens=(usage.prompt_tokens or chunk_usage.prompt_tokens) + chunk_usage.completion_tokens,
                    )
                tool_calls = _tool_calls_from_blocks(blocks)
                finish_reason = STOP_REASONS.get(stop_reason, FinishReason.UNKNOWN)
                if tool_calls:
                    finish_reason = FinishReason.TOOL_CALLS
                yield StreamChunk(
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    usage=usage,
                    model=model,
                    raw=data,
                )
                continue

            if event_type == "error":
                error = data.get("error") or {}
                raise LLMResponseError(
                    str(error.get("message", "stream error")),
                    provider=self.standard.value,
                    raw=data,
                )

    # --- optional capabilities -------------------------------------------

    async def list_models(self) -> List[ModelInfo]:
        data = await self._transport.request_json("GET", MODELS_PATH)
        items = data.get("data") or data.get("models") or []
        return [
            ModelInfo(
                id=str(item.get("id", "")),
                display_name=str(item.get("display_name", item.get("id", ""))),
                owned_by="anthropic",
                raw=item,
            )
            for item in items
        ]

    async def count_tokens(self, request: ChatRequest) -> int:
        body = self._build_body(request, stream=False)
        body.pop("stream", None)
        body.pop("max_tokens", None)
        body.pop("temperature", None)
        data = await self._transport.request_json("POST", COUNT_TOKENS_PATH, json_body=body)
        if isinstance(data.get("input_tokens"), int):
            return data["input_tokens"]
        raise LLMResponseError("no token count in response", provider=self.standard.value, raw=data)

    # --- mapping ----------------------------------------------------------

    def _build_body(self, request: ChatRequest, *, stream: bool) -> Dict[str, Any]:
        system, messages = self.split_system(request)

        body: Dict[str, Any] = {
            "model": self.resolve_model(request),
            "messages": _to_wire_messages(messages),
            # max_tokens is mandatory in the Anthropic API.
            "max_tokens": self.resolve_max_tokens(request),
            "temperature": self.resolve_temperature(request),
            "stream": stream,
        }
        if system:
            body["system"] = system
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.stop:
            body["stop_sequences"] = request.stop

        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            mode = self.tool_choice_mode(request.tool_choice)
            if mode == "named":
                body["tool_choice"] = {"type": "tool", "name": self.named_tool_choice(request.tool_choice)}
            else:
                body["tool_choice"] = {"type": TOOL_MODES.get(mode, "auto")}

        if request.response_format.type in ("json", "json_schema"):
            logger.debug("anthropic standard has no native JSON mode; relying on prompt instructions")

        return self.merge_extra(body, request)

    def _parse_response(self, data: Dict[str, Any]) -> ChatResponse:
        content = data.get("content")
        if content is None:
            raise LLMResponseError("response contains no content", provider=self.standard.value, raw=data)

        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in content:
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=dict(block.get("input") or {}),
                    )
                )

        finish_reason = STOP_REASONS.get(data.get("stop_reason"), FinishReason.UNKNOWN)
        if tool_calls:
            finish_reason = FinishReason.TOOL_CALLS

        return ChatResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=str(data.get("model", "")),
            usage=_parse_usage(data.get("usage")) or Usage(),
            request_id=str(data.get("id", "")),
            raw=data,
        )


def _to_wire_messages(messages: List[Message]) -> List[Dict[str, Any]]:
    """Map unified messages to Anthropic blocks.

    Tool results are user messages carrying `tool_result` blocks; consecutive
    results are merged into one message, as the API expects.
    """
    wire: List[Dict[str, Any]] = []
    for message in messages:
        if message.role == Role.TOOL:
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id,
                "content": message.content,
            }
            if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list) \
                    and wire[-1]["content"] and wire[-1]["content"][0].get("type") == "tool_result":
                wire[-1]["content"].append(block)
            else:
                wire.append({"role": "user", "content": [block]})
            continue

        blocks: List[Dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
        if not blocks:
            continue
        wire.append({"role": "assistant" if message.role == Role.ASSISTANT else "user", "content": blocks})
    return wire


def _tool_calls_from_blocks(blocks: Dict[int, Dict[str, Any]]) -> List[ToolCall]:
    calls: List[ToolCall] = []
    for _, slot in sorted(blocks.items()):
        if slot.get("type") != "tool_use":
            continue
        raw = slot.get("json") or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("tool_use input is not valid JSON: %.200s", raw)
            arguments = {"_raw": raw}
        calls.append(
            ToolCall(
                id=str(slot.get("id", "")),
                name=str(slot.get("name", "")),
                arguments=arguments if isinstance(arguments, dict) else {"_raw": arguments},
            )
        )
    return calls


def _parse_usage(raw: Any) -> Optional[Usage]:
    if not isinstance(raw, dict):
        return None
    prompt = int(raw.get("input_tokens", 0) or 0)
    completion = int(raw.get("output_tokens", 0) or 0)
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion)
