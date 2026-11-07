"""Фоновое удаление текстов, включая неопубликованные черновики."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import update

from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest
from app.services.ai_support import provider
from app.services.ai_support.settings_view import snapshot


logger = structlog.get_logger(__name__)
_task: asyncio.Task | None = None


async def cleanup() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(AiSupportRequest)
            .where(AiSupportRequest.created_at < datetime.now(UTC) - timedelta(days=snapshot().retention_days))
            .values(question_text=None, answer_text=None, operator_summary=None)
        )
        await db.commit()


async def run() -> None:
    while True:
        try:
            await cleanup()
        except Exception:
            logger.warning('Не удалось очистить тексты журнала ИИ')
        await asyncio.sleep(6 * 60 * 60)


async def startup() -> None:
    global _task
    try:
        await provider.startup()
        if _task is None or _task.done():
            _task = asyncio.create_task(run(), name='ai-support-retention')
    except Exception:
        logger.warning('Не удалось запустить ядро ИИ')


async def shutdown() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
        _task = None
    try:
        await provider.shutdown()
    except Exception:
        logger.warning('Не удалось закрыть HTTP-клиент ИИ')
