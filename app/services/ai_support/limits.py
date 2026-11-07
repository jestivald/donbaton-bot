"""Суточные лимиты по журналу, локальная блокировка клиента и общий семафор."""

import asyncio
import time
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest
from app.utils.timezone import local_day_start


semaphore = asyncio.Semaphore(4)
_users: set[int] = set()
_budget_cache: tuple[datetime, float, Decimal] | None = None
_budget_lock = asyncio.Lock()


def day_start() -> datetime:
    return local_day_start(tz=ZoneInfo('Europe/Moscow'))


def acquire_user(user_id: int | None) -> bool:
    if user_id is None:
        return True
    if user_id in _users:
        return False
    _users.add(user_id)
    return True


def release_user(user_id: int | None) -> None:
    if user_id is not None:
        _users.discard(user_id)


def invalidate_budget() -> None:
    global _budget_cache
    _budget_cache = None


async def spent_today() -> Decimal:
    global _budget_cache
    async with _budget_lock:
        start = day_start()
        if _budget_cache and _budget_cache[0] == start and time.monotonic() - _budget_cache[1] < 30:
            return _budget_cache[2]
        async with AsyncSessionLocal() as db:
            value = (
                await db.execute(
                    select(func.coalesce(func.sum(AiSupportRequest.cost_usd), 0)).where(
                        AiSupportRequest.created_at >= start,
                    )
                )
            ).scalar_one()
        total = Decimal(str(value))
        _budget_cache = (start, time.monotonic(), total)
        return total


async def user_requests_today(user_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(AiSupportRequest)
                .where(
                    AiSupportRequest.user_id == user_id,
                    AiSupportRequest.created_at >= day_start(),
                    # Версия фиксируется перед HTTP: ошибки провайдера тоже расходуют лимит,
                    # детектор оператора и отказы лимитера — нет. Pending учитывает другой процесс.
                    AiSupportRequest.prompt_version != '',
                )
            )
        ).scalar_one()
