"""HTTP-клиент провайдера с единым дедлайном и одним ограниченным повтором."""

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import settings
from app.services.ai_support.settings_view import Config, snapshot


@dataclass
class Usage:
    prompt_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: 'Usage') -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass
class Completion:
    message: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


class ProviderError(Exception):
    """Безопасная ошибка без тела HTTP-ответа и заголовков запроса."""

    def __init__(self, code: str, *, transient: bool = False, usage: Usage | None = None):
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.usage = usage or Usage()


_client: httpx.AsyncClient | None = None
_proxy: str | None = None
_client_lock = asyncio.Lock()


async def startup() -> None:
    await get_client(snapshot())


async def get_client(config: Config) -> httpx.AsyncClient:
    global _client, _proxy
    async with _client_lock:
        if _client is None or _client.is_closed or _proxy != config.proxy:
            previous = _client
            _client = httpx.AsyncClient(proxy=config.proxy or None, trust_env=False, follow_redirects=False)
            _proxy = config.proxy
            if previous is not None:
                await previous.aclose()
        return _client


async def shutdown() -> None:
    global _client
    async with _client_lock:
        if _client is not None:
            await _client.aclose()
            _client = None


def parse_usage(raw: dict) -> Usage:
    def count(value):
        try:
            return max(0, int(value or 0))
        except (ValueError, TypeError):
            return 0

    return Usage(
        prompt_tokens=count(raw.get('prompt_tokens')),
        cache_hit_tokens=count(
            raw.get('prompt_cache_hit_tokens', (raw.get('prompt_tokens_details') or {}).get('cached_tokens'))
        ),
        cache_miss_tokens=count(raw.get('prompt_cache_miss_tokens')),
        completion_tokens=count(raw.get('completion_tokens')),
        reasoning_tokens=count((raw.get('completion_tokens_details') or {}).get('reasoning_tokens')),
    )


def retry_delay(value: str | None) -> float:
    try:
        return max(0.0, float(value)) if value else 0.25 + random.uniform(0, 0.15)
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.3


def parse_response(data) -> Completion:
    try:
        usage = parse_usage(data.get('usage') or {})
        choice = data['choices'][0]
        message = choice['message']
        if choice.get('finish_reason') == 'length':
            raise ProviderError('truncated', usage=usage)
        if not isinstance(message, dict):
            raise ProviderError('invalid_response', usage=usage)
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            raise ProviderError('invalid_response', usage=usage)
        if not (content or '').strip() and not message.get('tool_calls'):
            raise ProviderError('empty', usage=usage)
        return Completion(message, usage)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError('invalid_response') from None


async def complete(
    messages: list[dict], tools: list[dict], config: Config, *, deadline: float, tool_choice='auto'
) -> Completion:
    body = {
        'model': config.model,
        'messages': messages,
        'temperature': config.temperature,
        'max_tokens': config.max_tokens,
        'stream': False,
    }
    if tools:
        body.update(tools=tools, tool_choice=tool_choice)
    if config.provider == 'deepseek':
        body['thinking'] = {'type': 'enabled' if config.thinking else 'disabled'}
        if config.thinking:
            body['reasoning_effort'] = 'low'
    client = await get_client(config)
    codes = {400: 'bad_request', 401: 'auth', 403: 'auth', 402: 'balance', 422: 'invalid', 429: 'rate_limit'}
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError('timeout', transient=True)
        delay = 0.3
        try:
            async with asyncio.timeout(remaining):
                response = await client.post(
                    config.base_url + '/chat/completions',
                    json=body,
                    headers={'Authorization': 'Bearer ' + (settings.AI_SUPPORT_API_KEY or '')},
                    timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
                )
            if response.is_success:
                try:
                    data = response.json()
                except ValueError:
                    raise ProviderError('invalid_response') from None
                return parse_response(data)
            error = ProviderError(
                codes.get(response.status_code, 'provider_error'), transient=response.status_code >= 500
            )
            retryable = response.status_code in (429, 500, 503)
            delay = retry_delay(response.headers.get('Retry-After'))
        except (httpx.TransportError, TimeoutError) as exc:
            error = ProviderError(
                'timeout' if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else 'network', transient=True
            )
            retryable = True
        if attempt or not retryable or delay + 0.05 >= deadline - time.monotonic():
            raise error
        await asyncio.sleep(delay)
    raise ProviderError('provider_error')
