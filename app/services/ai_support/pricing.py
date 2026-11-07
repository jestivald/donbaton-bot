"""Стоимость Chat Completions, без повторного учёта reasoning-токенов."""

from datetime import UTC, datetime
from decimal import Decimal

from app.services.ai_support.settings_view import Config


PRICES = {
    'deepseek-flash': ((0.003, 0.15, 0.60), (0.006, 0.30, 1.20)),
    'deepseek-v4-pro': ((0.022, 0.66, 1.98), (0.044, 1.32, 3.96)),
}


def is_peak(moment: datetime) -> bool:
    moment = moment.astimezone(UTC)
    return moment.weekday() < 5 and (1 <= moment.hour < 4 or 6 <= moment.hour < 10)


def cost(usage, config: Config, moment: datetime | None = None) -> Decimal:
    if config.provider == 'deepseek' and config.model in PRICES:
        prices = PRICES[config.model][int(is_peak(moment or datetime.now(UTC)))]
    else:
        fallback = PRICES['deepseek-flash'][1]
        prices = tuple(
            value or fallback[i] for i, value in enumerate((config.input_hit, config.input_miss, config.output_price))
        )
    # Совместимый провайдер может не отдавать разбивку кэша: остаток считаем miss.
    miss = max(usage.cache_miss_tokens, usage.prompt_tokens - usage.cache_hit_tokens)
    counts = (usage.cache_hit_tokens, miss, usage.completion_tokens)
    return (
        sum((Decimal(str(price)) * count for price, count in zip(prices, counts, strict=True)), Decimal(0)) / 1_000_000
    )
