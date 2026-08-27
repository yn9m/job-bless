# LLM-слой: контракт общения приложения с нейронкой

Приложение работает **только** с портом `LLMClient` и провайдер-независимыми DTO.
Конкретный стандарт API (OpenAI / Gemini / Anthropic) выбирается в конфиге и
подменяется без единой правки в бизнес-логике.

```text
[ ApplicationService / Orchestrator / будущий CoverLetterService ]
                     ↓ зависит только от порта
[ LLMClient (src/llm/base.py) + DTO (src/llm/models.py) ]
                     ↑ реализуют
[ OpenAIClient | GeminiClient | AnthropicClient ]  ← выбор в llm.standard
                     ↓
[ HttpTransport: ретраи, таймауты, SSE, маппинг ошибок ]
                     ↓
[ AIStudioToAPI (http://localhost:7860) ]
```

## Структура

| Файл | Назначение |
| --- | --- |
| `models.py` | DTO: `Message`, `ChatRequest`, `ChatResponse`, `StreamChunk`, `ToolSpec`, `ToolCall`, `Usage`, `LLMStandard` |
| `base.py` | Порт `LLMClient` + `LLMCapabilities` (что умеет конкретный стандарт) |
| `errors.py` | Единая иерархия ошибок (`LLMAuthError`, `LLMRateLimitError`, …) |
| `http.py` | HTTP-транспорт: ретраи с backoff, SSE-парсер, маппинг HTTP-статусов в ошибки |
| `factory.py` | `create_llm_client(config.llm)` — выбор адаптера по `llm.standard` |
| `providers/openai.py` | Стандарт OpenAI: `/v1/chat/completions`, `/v1/models`, `/v1/embeddings`, `/v1/responses/input_tokens` |
| `providers/gemini.py` | Стандарт Google: `/v1beta/models/{model}:generateContent`, `:streamGenerateContent?alt=sse`, `:embedContent`, `:batchEmbedContents` |
| `providers/anthropic.py` | Стандарт Anthropic: `/v1/messages`, `/v1/messages/count_tokens`, `/v1/models` |

## Конфиг

```yaml
llm:
  enabled: true
  standard: "openai"      # openai | gemini | anthropic
  base_url: "http://localhost:7860"
  api_key: ""             # или LLM_API_KEY
  model: "gemini-2.5-flash-lite"
  embedding_model: "gemini-embedding-001"
  temperature: 0.7
  max_tokens: 2048
  timeout_sec: 120
  max_retries: 3
  retry_backoff_sec: 1.0
  gemini_api_version: "v1beta"     # только для standard: gemini
  anthropic_version: "2023-06-01"  # только для standard: anthropic
  modifiers:                       # суффиксы имени модели у моста AI Studio
    thinking: ""                   # minimal | low | medium | high
    stream_mode: ""                # real | fake
    search: false
    code: false
```

Переопределение через окружение: `LLM_ENABLED`, `LLM_STANDARD`, `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`, `LLM_EMBEDDING_MODEL`.

Аутентификация подставляется адаптером сама: `Authorization: Bearer` для
OpenAI/Gemini, `x-api-key` + `anthropic-version` для Anthropic.

## Использование

```python
from src.config import Config
from src.llm import ChatRequest, Message, create_llm_client_from

config = Config.load("configs/config.local.yaml")

async with create_llm_client_from(config) as llm:
    # 1. Самый частый кейс — одна строка на вход, одна на выход
    text = await llm.complete(
        "Оцени соответствие вакансии моему резюме от 1 до 10.",
        system="Ты помощник по поиску работы.",
    )

    # 2. Полный диалог
    response = await llm.chat(ChatRequest(
        messages=[Message.user("Составь сопроводительное письмо")],
        system="Пиши кратко и по делу.",
        max_tokens=512,
    ))
    print(response.text, response.usage.total_tokens, response.finish_reason)

    # 3. Стриминг
    async for chunk in llm.stream_chat(ChatRequest.of("Опиши вакансию")):
        print(chunk.delta_text, end="")
```

Структурированный ответ (то, что нужно для скоринга вакансий и ответов на
вопросы работодателя):

```python
from src.llm import ChatRequest, Message, ResponseFormat

schema = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}

response = await llm.chat(ChatRequest(
    messages=[Message.user(vacancy_text)],
    system="Верни только JSON.",
    response_format=ResponseFormat(type="json_schema", schema=schema),
))
```

Tool calling единообразен для всех трёх стандартов:

```python
from src.llm import ChatRequest, Message, ToolSpec

request = ChatRequest(
    messages=[Message.user("Оцени вакансию")],
    tools=[ToolSpec(name="rate_vacancy", description="Поставить оценку", parameters=schema)],
    tool_choice="rate_vacancy",   # или ToolChoice.AUTO / NONE / REQUIRED
)
response = await llm.chat(request)
for call in response.tool_calls:
    result = run_tool(call.name, call.arguments)
    request.messages += [response.as_message(), Message.tool_result(call.id, result)]
```

## Матрица возможностей

| Возможность | OpenAI | Gemini | Anthropic |
| --- | :---: | :---: | :---: |
| `chat` / `stream_chat` | ✅ | ✅ | ✅ |
| tool calling | ✅ | ✅ | ✅ |
| JSON / JSON Schema режим | ✅ | ✅ | ❌ (только через промпт или tool) |
| `list_models` | ✅ | ✅ | ✅ |
| `embed` | ✅ | ✅ | ❌ |
| `count_tokens` | ✅ | ❌ | ✅ |

Неподдерживаемый вызов бросает `LLMUnsupportedError`; перед вызовом можно
проверить флаг: `if llm.capabilities.embeddings: ...`.

## Ошибки и ретраи

Все сбои приходят как наследники `LLMError`: `LLMAuthError` (401/403),
`LLMBadRequestError` (4xx), `LLMRateLimitError` (429, с `retry_after_sec`),
`LLMServerError` (5xx), `LLMTimeoutError`, `LLMConnectionError`,
`LLMResponseError` (не удалось разобрать ответ), `LLMConfigError`,
`LLMUnsupportedError`.

Транспорт сам повторяет запрос при 429/5xx/таймауте/обрыве соединения
(`max_retries` попыток, экспоненциальный backoff с джиттером, `Retry-After`
учитывается). Стриминг ретраится только до первого выданного чанка.

## Добавление нового стандарта

1. Создать `providers/<standard>.py` с классом-наследником `HttpLLMClient`:
   задать `standard`, `capabilities`, `_auth_headers`, `chat`, `stream_chat`.
2. Добавить значение в `LLMStandard` и запись в `_REGISTRY` в `factory.py`.
3. Дописать кейсы в `tests/test_llm_contract.py` (сеть не нужна — там подменяется
   транспорт).
