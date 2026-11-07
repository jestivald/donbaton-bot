"""Короткие транзакции журнала, не удерживаемые во время HTTP-запроса."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from functools import wraps

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.database.database import AsyncSessionLocal
from app.database.models import AiSupportRequest
from app.services.ai_support.api import AiChannel, AiRequestRecord, AiStats


logger = structlog.get_logger(__name__)


def fallback(factory):
    """Контракт чтения/обратной связи безопасен при недоступной БД."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except Exception:
                logger.warning('Не удалось выполнить операцию журнала ИИ', operation=function.__name__)
                return factory()

        return wrapped

    return decorate


async def claim(ctx, config) -> int | None:
    async with AsyncSessionLocal() as db:
        insert = sqlite_insert if db.get_bind().dialect.name == 'sqlite' else pg_insert
        statement = (
            insert(AiSupportRequest)
            .values(
                channel=ctx.channel.value,
                user_id=ctx.user_id,
                ticket_id=ctx.ticket_id,
                trigger_message_id=ctx.trigger_message_id if ctx.channel == AiChannel.TICKET else None,
                model=config.model[:200],
                provider=config.provider,
                thinking=config.thinking,
            )
            .on_conflict_do_nothing(index_elements=['trigger_message_id'])
            .returning(AiSupportRequest.id)
        )
        request_id = (await db.execute(statement)).scalar_one_or_none()
        await db.commit()
        return request_id


async def update_request(request_id, **values):
    async with AsyncSessionLocal() as db:
        await db.execute(update(AiSupportRequest).where(AiSupportRequest.id == request_id).values(**values))
        await db.commit()


async def finish(request_id, result, *, usage, cost, latency_ms, tool_calls, question, kb_version, prompt_version):
    await update_request(
        request_id,
        outcome=result.outcome.value,
        answer_text=result.reply_text,
        handoff_reason=result.handoff_reason.value if result.handoff_reason else None,
        operator_summary=result.operator_summary,
        error_code=result.error_code,
        prompt_tokens=usage.prompt_tokens,
        cache_hit_tokens=usage.cache_hit_tokens,
        cache_miss_tokens=usage.cache_miss_tokens,
        completion_tokens=usage.completion_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=Decimal(cost).quantize(Decimal('.000001'), rounding=ROUND_UP),
        latency_ms=latency_ms,
        tool_calls=tool_calls,
        question_text=question,
        kb_version=kb_version,
        prompt_version=prompt_version,
    )


@fallback(lambda: None)
async def get_request(request_id: int) -> AiRequestRecord | None:
    async with AsyncSessionLocal() as db:
        row = await db.get(AiSupportRequest, request_id)
        if row is None:
            return None
        return AiRequestRecord(
            **{
                name: AiChannel(row.channel) if name == 'channel' else getattr(row, name)
                for name in AiRequestRecord.__dataclass_fields__
            }
        )


@fallback(lambda: False)
async def record_feedback(request_id: int, user_id: int, helpful: bool) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            update(AiSupportRequest)
            .where(
                AiSupportRequest.id == request_id,
                AiSupportRequest.user_id == user_id,
            )
            .values(feedback='helpful' if helpful else 'not_helpful', feedback_at=datetime.now(UTC))
        )
        await db.commit()
        return result.rowcount == 1


@fallback(lambda: None)
async def attach_reply_message(request_id: int, reply_message_id: int) -> None:
    await update_request(request_id, reply_message_id=reply_message_id)


@fallback(lambda: None)
async def mark_handoff_done(request_id: int, ticket_id: int | None) -> None:
    values = {'outcome': 'handoff'}
    if ticket_id is not None:
        values['ticket_id'] = ticket_id
    await update_request(request_id, **values)


@fallback(set)
async def ai_reply_message_ids(ticket_id: int) -> set[int]:
    async with AsyncSessionLocal() as db:
        return set(
            (
                await db.execute(
                    select(AiSupportRequest.reply_message_id).where(
                        AiSupportRequest.ticket_id == ticket_id,
                        AiSupportRequest.reply_message_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )


@fallback(lambda: 0)
async def count_ai_replies_in_ticket(ticket_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(AiSupportRequest)
                .where(
                    AiSupportRequest.ticket_id == ticket_id,
                    AiSupportRequest.outcome == 'answered',
                    AiSupportRequest.reply_message_id.is_not(None),
                )
            )
        ).scalar_one()


@fallback(lambda: False)
async def ticket_has_handoff(ticket_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(AiSupportRequest.id)
                .where(
                    AiSupportRequest.ticket_id == ticket_id,
                    AiSupportRequest.outcome == 'handoff',
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None


async def get_stats(days: int) -> AiStats:
    days = max(1, min(int(days), 365))
    stats = AiStats(days=days)
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(
                        AiSupportRequest.outcome,
                        AiSupportRequest.channel,
                        AiSupportRequest.cost_usd,
                        AiSupportRequest.latency_ms,
                        AiSupportRequest.feedback,
                        AiSupportRequest.handoff_reason,
                    ).where(AiSupportRequest.created_at >= datetime.now(UTC) - timedelta(days=days))
                )
            ).all()
        outcomes = Counter(row.outcome for row in rows)
        stats.requests = len(rows)
        stats.answered = outcomes['answered']
        stats.handoffs = outcomes['handoff']
        stats.errors = outcomes['error']
        stats.blocked = sum(outcomes[key] for key in ('rate_limited', 'budget_exceeded', 'disabled', 'filtered'))
        stats.cost_usd = float(sum((row.cost_usd for row in rows), Decimal(0)))
        stats.avg_latency_ms = int(sum(row.latency_ms for row in rows) / len(rows)) if rows else 0
        stats.helpful = sum(row.feedback == 'helpful' for row in rows)
        stats.not_helpful = sum(row.feedback == 'not_helpful' for row in rows)
        stats.by_channel = dict(Counter(row.channel for row in rows))
        stats.handoff_reasons = dict(Counter(row.handoff_reason for row in rows if row.handoff_reason))
    except Exception:
        logger.warning('Не удалось прочитать статистику ИИ')
    return stats
