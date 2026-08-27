"""Contract tests for the LLM layer: config wiring, mapping, error handling.

No network access: the HTTP transport of each adapter is replaced by a fake
that records the outgoing payload and returns a canned provider response.
"""

import json
import pytest

from src.config import Config, LLMConfig, LLMModifiersConfig
from src.llm import (
    ChatRequest,
    FinishReason,
    LLMConfigError,
    LLMStandard,
    LLMUnsupportedError,
    Message,
    ResponseFormat,
    ToolCall,
    ToolChoice,
    ToolSpec,
    create_llm_client,
    build_model_name,
)
from src.llm.http import SSEEvent
from src.llm.providers.anthropic import AnthropicClient
from src.llm.providers.gemini import GeminiClient
from src.llm.providers.openai import OpenAIClient


class FakeTransport:
    """Stands in for HttpTransport: records requests, replays canned answers."""

    def __init__(self, json_response=None, sse_events=None):
        self.json_response = json_response or {}
        self.sse_events = sse_events or []
        self.calls = []

    async def request_json(self, method, path, *, json_body=None, params=None, timeout_sec=None):
        self.calls.append({"method": method, "path": path, "body": json_body, "params": params})
        return self.json_response

    async def stream_sse(self, method, path, *, json_body=None, params=None, timeout_sec=None):
        self.calls.append({"method": method, "path": path, "body": json_body, "params": params})
        for event in self.sse_events:
            yield event

    async def aclose(self):
        pass


def make_client(cls, transport, **overrides):
    config = LLMConfig(enabled=True, api_key="test-key", model="test-model", **overrides)
    client = cls(config)
    client._transport = transport
    return client


def last_body(transport):
    return transport.calls[-1]["body"]


# --- config & factory ----------------------------------------------------

def test_config_loads_llm_section(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n"
        "  enabled: true\n"
        "  standard: \"anthropic\"\n"
        "  base_url: \"http://localhost:7860\"\n"
        "  model: \"gemini-2.5-flash-lite\"\n"
        "  max_tokens: 512\n"
        "  modifiers:\n"
        "    thinking: \"high\"\n"
        "    search: true\n",
        encoding="utf-8",
    )
    config = Config.load(str(config_file))

    assert config.llm.enabled is True
    assert config.llm.standard == "anthropic"
    assert config.llm.max_tokens == 512
    assert config.llm.modifiers.thinking == "high"
    assert config.llm.modifiers.search is True


def test_config_defaults_when_section_missing(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("app:\n  isProduction: false\n", encoding="utf-8")
    config = Config.load(str(config_file))

    assert config.llm.enabled is False
    assert config.llm.standard == "openai"


@pytest.mark.parametrize(
    "standard,expected",
    [("openai", LLMStandard.OPENAI), ("gemini", LLMStandard.GEMINI), ("anthropic", LLMStandard.ANTHROPIC)],
)
def test_factory_selects_standard_from_config(standard, expected):
    client = create_llm_client(LLMConfig(enabled=True, standard=standard, api_key="k"))
    assert client.standard is expected


def test_factory_rejects_unknown_standard():
    with pytest.raises(LLMConfigError):
        create_llm_client(LLMConfig(enabled=True, standard="llama"))


def test_factory_rejects_disabled_layer():
    with pytest.raises(LLMConfigError):
        create_llm_client(LLMConfig(enabled=False))


def test_model_modifiers_order():
    name = build_model_name("gemini-3-flash-preview", thinking="minimal", stream_mode="fake", search=True, code=True)
    assert name == "gemini-3-flash-preview-minimal-fake-search-code"


def test_client_applies_configured_modifiers():
    client = make_client(OpenAIClient, FakeTransport(), modifiers=LLMModifiersConfig(thinking="high", search=True))
    assert client.resolve_model(ChatRequest.of("hi")) == "test-model-high-search"


# --- OpenAI standard -----------------------------------------------------

async def test_openai_chat_maps_request_and_response():
    transport = FakeTransport(
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{"message": {"content": "Привет"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
    )
    client = make_client(OpenAIClient, transport)

    response = await client.chat(
        ChatRequest(messages=[Message.user("Привет")], system="Ты ассистент", max_tokens=100)
    )

    body = last_body(transport)
    assert transport.calls[-1]["path"] == "/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "Ты ассистент"}
    assert body["messages"][1]["role"] == "user"
    assert body["max_tokens"] == 100
    assert response.text == "Привет"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.total_tokens == 13


async def test_openai_maps_tools_and_tool_calls():
    transport = FakeTransport(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "rate_vacancy", "arguments": '{"score": 8}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    client = make_client(OpenAIClient, transport)

    request = ChatRequest(
        messages=[Message.user("оцени")],
        tools=[ToolSpec(name="rate_vacancy", parameters={"type": "object", "properties": {"score": {"type": "integer"}}})],
        tool_choice="rate_vacancy",
        response_format=ResponseFormat(type="json"),
    )
    response = await client.chat(request)

    body = last_body(transport)
    assert body["tools"][0]["function"]["name"] == "rate_vacancy"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "rate_vacancy"}}
    assert body["response_format"] == {"type": "json_object"}
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"score": 8}


async def test_openai_stream_accumulates_tool_call_fragments():
    events = [
        SSEEvent("", {"model": "test-model", "choices": [{"delta": {"content": "При"}}]}),
        SSEEvent("", {"choices": [{"delta": {"content": "вет"}}]}),
        SSEEvent(
            "",
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": '{"a":'}}]}}
                ]
            },
        ),
        SSEEvent("", {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}}]}),
        SSEEvent("", {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 7}}),
    ]
    client = make_client(OpenAIClient, FakeTransport(sse_events=events))

    response = await client.collect_stream(ChatRequest.of("привет"))

    assert response.text == "Привет"
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls == [ToolCall(id="c1", name="f", arguments={"a": 1})]
    assert response.usage.total_tokens == 7


async def test_openai_tool_result_message_shape():
    transport = FakeTransport({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    client = make_client(OpenAIClient, transport)

    await client.chat(
        ChatRequest(
            messages=[
                Message.user("оцени"),
                Message.assistant(tool_calls=[ToolCall(id="c1", name="f", arguments={"a": 1})]),
                Message.tool_result("c1", "8"),
            ]
        )
    )

    messages = last_body(transport)["messages"]
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == json.dumps({"a": 1}, ensure_ascii=False)
    assert messages[2] == {"role": "tool", "tool_call_id": "c1", "content": "8"}


# --- Gemini standard -----------------------------------------------------

async def test_gemini_chat_maps_request_and_response():
    transport = FakeTransport(
        {
            "candidates": [{"content": {"parts": [{"text": "Привет"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
        }
    )
    client = make_client(GeminiClient, transport)

    response = await client.chat(
        ChatRequest(messages=[Message.user("Привет")], system="Ты ассистент", max_tokens=64)
    )

    body = last_body(transport)
    assert transport.calls[-1]["path"] == "/v1beta/models/test-model:generateContent"
    assert body["systemInstruction"] == {"parts": [{"text": "Ты ассистент"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Привет"}]}]
    assert body["generationConfig"]["maxOutputTokens"] == 64
    assert response.text == "Привет"
    assert response.usage.total_tokens == 7


async def test_gemini_maps_function_calls_and_json_schema():
    transport = FakeTransport(
        {
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": {"name": "rate_vacancy", "args": {"score": 8}}}]},
                    "finishReason": "STOP",
                }
            ]
        }
    )
    client = make_client(GeminiClient, transport)

    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    response = await client.chat(
        ChatRequest(
            messages=[Message.user("оцени")],
            tools=[ToolSpec(name="rate_vacancy", parameters=schema)],
            tool_choice=ToolChoice.REQUIRED,
            response_format=ResponseFormat(type="json_schema", schema=schema),
        )
    )

    body = last_body(transport)
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "rate_vacancy"
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseSchema"] == schema
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].name == "rate_vacancy"


async def test_gemini_tool_result_becomes_function_response():
    transport = FakeTransport({"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})
    client = make_client(GeminiClient, transport)

    await client.chat(ChatRequest(messages=[Message.user("q"), Message.tool_result("c1", "8", name="rate_vacancy")]))

    contents = last_body(transport)["contents"]
    assert contents[1]["parts"][0]["functionResponse"]["name"] == "rate_vacancy"


async def test_gemini_stream_uses_sse_param():
    events = [
        SSEEvent("", {"candidates": [{"content": {"parts": [{"text": "При"}]}}]}),
        SSEEvent("", {"candidates": [{"content": {"parts": [{"text": "вет"}]}, "finishReason": "STOP"}]}),
    ]
    transport = FakeTransport(sse_events=events)
    client = make_client(GeminiClient, transport)

    response = await client.collect_stream(ChatRequest.of("привет"))

    assert transport.calls[-1]["path"] == "/v1beta/models/test-model:streamGenerateContent"
    assert transport.calls[-1]["params"] == {"alt": "sse"}
    assert response.text == "Привет"
    assert response.finish_reason is FinishReason.STOP


async def test_gemini_does_not_support_token_counting():
    client = make_client(GeminiClient, FakeTransport())
    assert client.capabilities.token_counting is False
    with pytest.raises(LLMUnsupportedError):
        await client.count_tokens(ChatRequest.of("hi"))


# --- Anthropic standard --------------------------------------------------

async def test_anthropic_chat_maps_request_and_response():
    transport = FakeTransport(
        {
            "id": "msg_1",
            "model": "test-model",
            "content": [{"type": "text", "text": "Привет"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 9, "output_tokens": 2},
        }
    )
    client = make_client(AnthropicClient, transport)

    response = await client.chat(
        ChatRequest(messages=[Message.user("Привет")], system="Ты ассистент", max_tokens=256)
    )

    body = last_body(transport)
    assert transport.calls[-1]["path"] == "/v1/messages"
    assert body["system"] == "Ты ассистент"
    assert body["max_tokens"] == 256  # mandatory for this standard
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Привет"}]}]
    assert response.text == "Привет"
    assert response.usage.total_tokens == 11


async def test_anthropic_max_tokens_falls_back_to_config():
    transport = FakeTransport({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})
    client = make_client(AnthropicClient, transport, max_tokens=1234)

    await client.chat(ChatRequest.of("hi"))

    assert last_body(transport)["max_tokens"] == 1234


async def test_anthropic_maps_tool_use_blocks():
    transport = FakeTransport(
        {
            "content": [{"type": "tool_use", "id": "tu_1", "name": "rate_vacancy", "input": {"score": 8}}],
            "stop_reason": "tool_use",
        }
    )
    client = make_client(AnthropicClient, transport)

    response = await client.chat(
        ChatRequest(
            messages=[Message.user("оцени"), Message.tool_result("tu_0", "прошлый результат")],
            tools=[ToolSpec(name="rate_vacancy", parameters={"type": "object"})],
            tool_choice=ToolChoice.REQUIRED,
        )
    )

    body = last_body(transport)
    assert body["tools"][0]["input_schema"] == {"type": "object"}
    assert body["tool_choice"] == {"type": "any"}
    assert body["messages"][1]["content"][0]["type"] == "tool_result"
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].arguments == {"score": 8}


async def test_anthropic_stream_events():
    events = [
        SSEEvent("message_start", {"type": "message_start", "message": {"model": "test-model", "usage": {"input_tokens": 4}}}),
        SSEEvent("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "При"}}),
        SSEEvent("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "вет"}}),
        SSEEvent("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
    ]
    client = make_client(AnthropicClient, FakeTransport(sse_events=events))

    response = await client.collect_stream(ChatRequest.of("привет"))

    assert response.text == "Привет"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.completion_tokens == 2


async def test_anthropic_does_not_support_embeddings():
    client = make_client(AnthropicClient, FakeTransport())
    assert client.capabilities.embeddings is False
    with pytest.raises(LLMUnsupportedError):
        await client.embed(["текст"])


# --- cross-standard contract ---------------------------------------------

@pytest.mark.parametrize(
    "cls,response",
    [
        (OpenAIClient, {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}]}),
        (GeminiClient, {"candidates": [{"content": {"parts": [{"text": "ответ"}]}, "finishReason": "STOP"}]}),
        (AnthropicClient, {"content": [{"type": "text", "text": "ответ"}], "stop_reason": "end_turn"}),
    ],
)
async def test_complete_returns_plain_text_for_every_standard(cls, response):
    client = make_client(cls, FakeTransport(response))
    assert await client.complete("вопрос", system="система") == "ответ"
