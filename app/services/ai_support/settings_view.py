"""Нормализованный снимок редактируемых настроек ИИ."""

import math
from dataclasses import dataclass
from typing import Any

from app.config import Settings, settings


@dataclass(frozen=True)
class Config:
    """Параметры одного запроса; секрет не попадает в repr."""

    provider: str
    base_url: str
    model: str
    thinking: bool
    temperature: float
    max_tokens: int
    timeout: float
    budget: float
    user_limit: int
    share_account: bool
    log_texts: bool
    retention_days: int
    proxy: str
    input_hit: float
    input_miss: float
    output_price: float
    owner_instructions: str
    status_note: str
    vision: bool = False
    max_images: int = 3
    max_image_bytes: int = 5 * 1024 * 1024


def number(suffix: str) -> float:
    key = 'AI_SUPPORT_' + suffix
    default = Settings.model_fields[key].default
    try:
        value = float(getattr(settings, key))
        return value if math.isfinite(value) and value >= 0 else float(default)
    except (TypeError, ValueError):
        return float(default)


def is_enabled() -> bool:
    return bool(settings.AI_SUPPORT_ENABLED and (settings.AI_SUPPORT_API_KEY or '').strip())


def bot_chat_enabled() -> bool:
    return is_enabled() and bool(settings.AI_SUPPORT_BOT_CHAT_ENABLED)


def vision_enabled() -> bool:
    return bool(settings.AI_SUPPORT_VISION_ENABLED) and (
        settings.AI_SUPPORT_PROVIDER == 'openai_compatible'
        or (settings.AI_SUPPORT_PROVIDER == 'deepseek' and 'flash' in (settings.AI_SUPPORT_MODEL or '').lower())
    )


def ticket_mode():
    value = (settings.AI_SUPPORT_TICKET_MODE or '').strip().lower()
    return value if is_enabled() and value in ('off', 'draft', 'auto') else 'off'


def max_ai_replies_per_ticket() -> int:
    return int(number('TICKET_MAX_AI_REPLIES'))


def notify_admins_enabled() -> bool:
    return bool(settings.AI_SUPPORT_NOTIFY_ADMINS)


def history_turns() -> int:
    return int(number('HISTORY_TURNS'))


def chat_memory_ttl_seconds() -> int:
    return int(number('CHAT_MEMORY_MINUTES') * 60)


def reply_footer_text() -> str:
    return settings.AI_SUPPORT_REPLY_FOOTER_TEMPLATE or ''


def snapshot() -> Config:
    return Config(
        provider='deepseek' if settings.AI_SUPPORT_PROVIDER == 'deepseek' else 'openai_compatible',
        base_url=(settings.AI_SUPPORT_BASE_URL or 'https://api.deepseek.com').rstrip('/'),
        model=(settings.AI_SUPPORT_MODEL or 'deepseek-flash').strip(),
        thinking=settings.AI_SUPPORT_PROVIDER == 'deepseek' and bool(settings.AI_SUPPORT_THINKING_ENABLED),
        temperature=min(2.0, number('TEMPERATURE')),
        max_tokens=max(1, int(number('MAX_OUTPUT_TOKENS'))),
        timeout=max(0.001, number('TIMEOUT_SECONDS')),
        budget=number('DAILY_BUDGET_USD'),
        user_limit=int(number('USER_DAILY_LIMIT')),
        share_account=bool(settings.AI_SUPPORT_SHARE_ACCOUNT_DATA),
        log_texts=bool(settings.AI_SUPPORT_LOG_TEXTS),
        retention_days=int(number('LOG_RETENTION_DAYS')),
        proxy=settings.AI_SUPPORT_PROXY_URL or '',
        input_hit=number('PRICE_INPUT_HIT_PER_1M'),
        input_miss=number('PRICE_INPUT_MISS_PER_1M'),
        output_price=number('PRICE_OUTPUT_PER_1M'),
        owner_instructions=settings.AI_SUPPORT_OWNER_INSTRUCTIONS_TEMPLATE or '',
        status_note=settings.AI_SUPPORT_STATUS_NOTE_TEMPLATE or '',
        vision=vision_enabled(),
        max_images=int(number('VISION_MAX_IMAGES')),
        max_image_bytes=min(32, int(number('VISION_MAX_MB'))) * 1024 * 1024,
    )


def describe_settings() -> dict[str, Any]:
    # Адрес API и прокси могут содержать пароль/токен; в сводку их не включаем.
    return {
        'enabled': is_enabled(),
        'bot_chat_enabled': bot_chat_enabled(),
        'ticket_mode': ticket_mode(),
        'provider': settings.AI_SUPPORT_PROVIDER,
        'model': settings.AI_SUPPORT_MODEL,
        'api_key_configured': bool((settings.AI_SUPPORT_API_KEY or '').strip()),
        'thinking_enabled': snapshot().thinking,
        'vision_enabled': vision_enabled(),
        'vision_max_images': snapshot().max_images,
        'vision_max_mb': snapshot().max_image_bytes // (1024 * 1024),
        'daily_budget_usd': number('DAILY_BUDGET_USD'),
        'user_daily_limit': int(number('USER_DAILY_LIMIT')),
        'share_account_data': bool(settings.AI_SUPPORT_SHARE_ACCOUNT_DATA),
        'max_ai_replies_per_ticket': max_ai_replies_per_ticket(),
        'notify_admins': notify_admins_enabled(),
    }
