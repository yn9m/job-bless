"""Connectivity check for the configured LLM endpoint.

Used by the web UI to show a live "есть связь / нет связи" lamp. The check is
deliberately cheap: model listing where the standard supports it, otherwise a
one-token generation.
"""

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import List, Optional

from src.config import LLMConfig
from src.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from src.llm.factory import create_llm_client
from src.llm.models import ChatRequest

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SEC = 8.0


@dataclass
class LLMHealth:
    """Result of a connectivity probe, ready to render."""

    ok: bool = False
    state: str = "unknown"  # ok | error | disabled
    message: str = "не проверено"
    detail: str = ""
    standard: str = ""
    model: str = ""
    base_url: str = ""
    latency_ms: int = 0
    models_available: int = 0
    # Model ids reported by the endpoint — feeds the model dropdown in settings.
    models: List[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


async def check_llm(config: LLMConfig, timeout_sec: float = CHECK_TIMEOUT_SEC) -> LLMHealth:
    """Probe the endpoint and describe the outcome in plain Russian."""
    health = LLMHealth(standard=config.standard, model=config.model, base_url=config.base_url)

    if not config.enabled:
        health.state = "disabled"
        health.message = "нейросеть выключена"
        health.detail = "включите «Использовать нейросеть» в настройках"
        return health

    # A probe must fail fast instead of retrying for a minute.
    probe_config = replace(config, timeout_sec=timeout_sec, max_retries=0)
    started = time.monotonic()

    try:
        async with create_llm_client(probe_config) as client:
            if client.capabilities.model_listing:
                models = await client.list_models()
                health.models = sorted({m.id for m in models if m.id})
                health.models_available = len(health.models)
            else:
                await client.chat(ChatRequest.of("ping", max_tokens=1))
    except LLMAuthError as e:
        return _fail(health, started, "ключ отклонён", str(e))
    except LLMTimeoutError as e:
        return _fail(health, started, f"таймаут ({timeout_sec:.0f} с)", str(e))
    except LLMConnectionError as e:
        return _fail(health, started, "сервер недоступен", str(e))
    except LLMRateLimitError as e:
        return _fail(health, started, "лимит запросов исчерпан", str(e))
    except LLMServerError as e:
        return _fail(health, started, "ошибка на стороне сервера", str(e))
    except LLMBadRequestError as e:
        return _fail(health, started, "запрос отклонён", str(e))
    except LLMError as e:
        return _fail(health, started, "нет связи", str(e))
    except Exception as e:  # noqa: BLE001 - the lamp must never crash a page
        logger.exception("llm health check failed unexpectedly")
        return _fail(health, started, "нет связи", str(e))

    health.ok = True
    health.state = "ok"
    health.latency_ms = int((time.monotonic() - started) * 1000)
    health.message = "есть подключение"
    health.detail = (
        f"доступно моделей: {health.models_available}" if health.models_available else "ответ получен"
    )
    return health


def _fail(health: LLMHealth, started: float, message: str, detail: str) -> LLMHealth:
    health.ok = False
    health.state = "error"
    health.latency_ms = int((time.monotonic() - started) * 1000)
    health.message = f"нет подключения: {message}"
    health.detail = detail[:300]
    logger.warning("llm health: %s (%s)", health.message, health.detail)
    return health


class LLMHealthMonitor:
    """Caches the probe so page loads and polling do not hammer the endpoint."""

    def __init__(self, settings, ttl_seconds: float = 30.0):
        self.settings = settings
        self.ttl_seconds = ttl_seconds
        self._health: Optional[LLMHealth] = None
        self._checked_monotonic: float = 0.0

    @property
    def cached(self) -> Optional[LLMHealth]:
        return self._health

    @property
    def models(self) -> List[str]:
        return list(self._health.models) if self._health else []

    async def get(self, force: bool = False) -> LLMHealth:
        fresh = self._health is not None and (time.monotonic() - self._checked_monotonic) < self.ttl_seconds
        if fresh and not force:
            return self._health

        self._health = await check_llm(self.settings.llm_config())
        self._checked_monotonic = time.monotonic()
        return self._health

    def invalidate(self) -> None:
        """Called after the settings change — the old verdict is meaningless."""
        self._health = None
        self._checked_monotonic = 0.0
