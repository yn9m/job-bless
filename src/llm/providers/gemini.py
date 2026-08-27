"""Google Gemini native adapter (`/v1beta/models/{model}:generateContent`).

Auth: `Authorization: Bearer <api_key>` (as accepted by the AIStudioToAPI bridge).
Endpoints used: :generateContent, :streamGenerateContent?alt=sse,
:embedContent, :batchEmbedContents, GET /v1beta/models.
"""

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

FINISH_REASONS = {
    "STOP": FinishReason.STOP,
    "MAX_TOKENS": FinishReason.LENGTH,
    "SAFETY": FinishReason.CONTENT_FILTER,
    "RECITATION": FinishReason.CONTENT_FILTER,
    "PROHIBITED_CONTENT": FinishReason.CONTENT_FILTER,
    "BLOCKLIST": FinishReason.CONTENT_FILTER,
}

TOOL_MODES = {"auto": "AUTO", "none": "NONE", "required": "ANY", "named": "ANY"}


class GeminiClient(HttpLLMClient):
    standard = LLMStandard.GEMINI
    capabilities = LLMCapabilities(
        streaming=True,
        tools=True,
        json_mode=True,
        model_listing=True,
        embeddings=True,
        token_counting=False,  # not exposed by the bridge
    )

    def _auth_headers(self, config: LLMConfig) -> Dict[str, str]:
        if not config.api_key:
            return {}
        # The bridge accepts Bearer; x-goog-api-key keeps stock Google SDKs happy.
        return {"Authorization": f"Bearer {config.api_key}", "x-goog-api-key": config.api_key}

    def _model_path(self, model: str, action: str) -> str:
        return f"/{self.config.gemini_api_version.strip('/')}/models/{model}:{action}"

    # --- generation -------------------------------------------------------

    async def chat(self, request: ChatRequest) -> ChatResponse:
        model = self.resolve_model(request)
        body = self._build_body(request)
        data = await self._transport.request_json(
            "POST",
            self._model_path(model, "generateContent"),
            json_body=body,
            timeout_sec=request.timeout_sec,
        )
        return self._parse_response(data, model)

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[StreamChunk]:
        model = self.resolve_model(request)
        body = self._build_body(request)
        tool_calls: List[ToolCall] = []

        async for event in self._transport.stream_sse(
            "POST",
            self._model_path(model, "streamGenerateContent"),
            json_body=body,
            params={"alt": "sse"},
            timeout_sec=request.timeout_sec,
        ):
            data = event.data
            candidates = data.get("candidates") or []
            usage = _parse_usage(data.get("usageMetadata"))
            if not candidates:
                if usage:
                    yield StreamChunk(usage=usage, model=model, raw=data)
                continue

            candidate = candidates[0]
            text, calls = _split_parts((candidate.get("content") or {}).get("parts") or [], len(tool_calls))
            tool_calls.extend(calls)

            raw_finish = candidate.get("finishReason")
            finish_reason = FINISH_REASONS.get(raw_finish, FinishReason.UNKNOWN) if raw_finish else None
            if finish_reason and tool_calls:
                finish_reason = FinishReason.TOOL_CALLS

            yield StreamChunk(
                delta_text=text,
                tool_calls=list(tool_calls) if finish_reason else [],
                finish_reason=finish_reason,
                usage=usage,
                model=model,
                raw=data,
            )

    # --- optional capabilities -------------------------------------------

    async def list_models(self) -> List[ModelInfo]:
        path = f"/{self.config.gemini_api_version.strip('/')}/models"
        data = await self._transport.request_json("GET", path)
        models = []
        for item in data.get("models") or []:
            # Gemini returns fully-qualified names like "models/gemini-2.5-flash".
            name = str(item.get("name", ""))
            models.append(
                ModelInfo(
                    id=name.split("/")[-1] or name,
                    display_name=str(item.get("displayName", "")),
                    owned_by="google",
                    raw=item,
                )
            )
        return models

    async def embed(self, texts: Sequence[str], model: Optional[str] = None) -> EmbeddingResult:
        embed_model = self.resolve_embedding_model(model)
        items = list(texts)

        if len(items) == 1:
            payload = {"model": f"models/{embed_model}", "content": {"parts": [{"text": items[0]}]}}
            data = await self._transport.request_json(
                "POST", self._model_path(embed_model, "embedContent"), json_body=payload
            )
            vector = ((data.get("embedding") or {}).get("values")) or []
            return EmbeddingResult(vectors=[list(vector)], model=embed_model, raw=data)

        payload = {
            "requests": [
                {"model": f"models/{embed_model}", "content": {"parts": [{"text": text}]}}
                for text in items
            ]
        }
        data = await self._transport.request_json(
            "POST", self._model_path(embed_model, "batchEmbedContents"), json_body=payload
        )
        return EmbeddingResult(
            vectors=[list(item.get("values") or []) for item in data.get("embeddings") or []],
            model=embed_model,
            raw=data,
        )

    # --- mapping ----------------------------------------------------------

    def _build_body(self, request: ChatRequest) -> Dict[str, Any]:
        system, messages = self.split_system(request)

        generation_config: Dict[str, Any] = {
            "temperature": self.resolve_temperature(request),
            "maxOutputTokens": self.resolve_max_tokens(request),
        }
        if request.top_p is not None:
            generation_config["topP"] = request.top_p
        if request.stop:
            generation_config["stopSequences"] = request.stop

        fmt = request.response_format
        if fmt.type in ("json", "json_schema"):
            generation_config["responseMimeType"] = "application/json"
            if fmt.type == "json_schema" and fmt.schema:
                generation_config["responseSchema"] = fmt.schema

        body: Dict[str, Any] = {
            "contents": _to_contents(messages),
            "generationConfig": generation_config,
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            mode = self.tool_choice_mode(request.tool_choice)
            function_config: Dict[str, Any] = {"mode": TOOL_MODES.get(mode, "AUTO")}
            if mode == "named":
                function_config["allowedFunctionNames"] = [self.named_tool_choice(request.tool_choice)]
            body["toolConfig"] = {"functionCallingConfig": function_config}

        return self.merge_extra(body, request)

    def _parse_response(self, data: Dict[str, Any], model: str) -> ChatResponse:
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                return ChatResponse(
                    finish_reason=FinishReason.CONTENT_FILTER,
                    model=model,
                    usage=_parse_usage(data.get("usageMetadata")) or Usage(),
                    raw=data,
                )
            raise LLMResponseError("response contains no candidates", provider=self.standard.value, raw=data)

        candidate = candidates[0]
        text, tool_calls = _split_parts((candidate.get("content") or {}).get("parts") or [], 0)
        finish_reason = FINISH_REASONS.get(candidate.get("finishReason"), FinishReason.UNKNOWN)
        if tool_calls:
            finish_reason = FinishReason.TOOL_CALLS

        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            model=str(data.get("modelVersion", model)),
            usage=_parse_usage(data.get("usageMetadata")) or Usage(),
            request_id=str(data.get("responseId", "")),
            raw=data,
        )


def _to_contents(messages: List[Message]) -> List[Dict[str, Any]]:
    """Map unified messages to Gemini `contents`.

    Gemini has no tool role: results go back as a user turn with functionResponse.
    """
    contents: List[Dict[str, Any]] = []
    for message in messages:
        if message.role == Role.TOOL:
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": message.name or message.tool_call_id,
                                "response": {"result": message.content},
                            }
                        }
                    ],
                }
            )
            continue

        parts: List[Dict[str, Any]] = []
        if message.content:
            parts.append({"text": message.content})
        for call in message.tool_calls:
            parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
        if not parts:
            continue
        contents.append({"role": "model" if message.role == Role.ASSISTANT else "user", "parts": parts})
    return contents


def _split_parts(parts: List[Dict[str, Any]], call_offset: int) -> tuple[str, List[ToolCall]]:
    """Split Gemini parts into plain text and function calls.

    Gemini does not assign ids to function calls, so we synthesize stable ones.
    """
    text_parts: List[str] = []
    tool_calls: List[ToolCall] = []
    for part in parts:
        if "text" in part and part["text"]:
            text_parts.append(str(part["text"]))
        function_call = part.get("functionCall")
        if function_call:
            index = call_offset + len(tool_calls)
            name = str(function_call.get("name", ""))
            tool_calls.append(
                ToolCall(
                    id=f"call_{index}_{name}",
                    name=name,
                    arguments=dict(function_call.get("args") or {}),
                )
            )
    return "".join(text_parts), tool_calls


def _parse_usage(raw: Any) -> Optional[Usage]:
    if not isinstance(raw, dict):
        return None
    return Usage(
        prompt_tokens=int(raw.get("promptTokenCount", 0) or 0),
        completion_tokens=int(raw.get("candidatesTokenCount", 0) or 0),
        total_tokens=int(raw.get("totalTokenCount", 0) or 0),
    )
