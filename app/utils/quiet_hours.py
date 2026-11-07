"""Тихие часы поддержки (SUPPORT_QUIET_HOURS, «ЧЧ:ММ-ЧЧ:ММ» по TIMEZONE): ночью нет сводок админам и автоматических сообщений
клиентам; ночной автоответ клиенту на его сообщение — исключение. Общий помощник для SLA-сводки, утренней сводки и автоматизации тикетов."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(getattr(settings, 'TIMEZONE', None) or 'Europe/Moscow')
    except Exception:
        return ZoneInfo('Europe/Moscow')


def parse_quiet_hours(value: str | None) -> tuple[time, time] | None:
    """«23:00-09:00» → (23:00, 09:00); пусто или ошибка формата → None (тихих часов нет)."""
    if not value or '-' not in value:
        return None
    try:
        start_s, end_s = (part.strip() for part in value.split('-', 1))
        start = time.fromisoformat(start_s)
        end = time.fromisoformat(end_s)
    except ValueError:
        return None
    return None if start == end else (start, end)


def _local(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(_tz())


def is_quiet_now(now: datetime | None = None) -> bool:
    window = parse_quiet_hours(getattr(settings, 'SUPPORT_QUIET_HOURS', ''))
    if not window:
        return False
    start, end = window
    t = _local(now).time()
    return (start <= t < end) if start < end else (t >= start or t < end)


def quiet_end_label() -> str:
    """Время конца тихих часов для текста клиенту («09:00»); без тихих часов — пусто."""
    window = parse_quiet_hours(getattr(settings, 'SUPPORT_QUIET_HOURS', ''))
    return window[1].strftime('%H:%M') if window else ''


def last_quiet_end(now: datetime | None = None) -> datetime | None:
    """Последний момент окончания тихих часов ≤ now (UTC). Для «утренней сводки один раз после ночи»."""
    window = parse_quiet_hours(getattr(settings, 'SUPPORT_QUIET_HOURS', ''))
    if not window:
        return None
    local = _local(now)
    candidate = local.replace(hour=window[1].hour, minute=window[1].minute, second=0, microsecond=0)
    if candidate > local:
        candidate -= timedelta(days=1)
    return candidate.astimezone(UTC)
