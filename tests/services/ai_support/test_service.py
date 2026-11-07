"""Цикл инструментов, изоляция клиента, идемпотентность, журнал, лимиты и сбои."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.database.models import AiSupportRequest
from app.services.ai_support import api, knowledge, limits, repository, retention, service, tools
from app.services.ai_support.settings_view import snapshot


@pytest.fixture
def kb(monkeypatch):
    value = knowledge.Knowledge('Помощь по настройке.', api.KbStatus(1, 1, 20, 5, 'version', datetime.now(UTC)))
    monkeypatch.setattr(knowledge, 'load', AsyncMock(return_value=value))
    return value


def response(content='Откройте приложение.', *, calls=None, reasoning=None):
    return httpx.Response(
        200,
        json={
            'choices': [
                {
                    'message': {
                        'role': 'assistant',
                        'content': content,
                        'tool_calls': calls,
                        'reasoning_content': reasoning,
                    },
                    'finish_reason': 'tool_calls' if calls else 'stop',
                }
            ],
            'usage': {
                'prompt_tokens': 100,
                'prompt_cache_hit_tokens': 20,
                'prompt_cache_miss_tokens': 80,
                'completion_tokens': 30,
                'completion_tokens_details': {'reasoning_tokens': 10},
            },
        },
    )


def call(name='handoff_to_human', args=None, id='call1'):
    return {'id': id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args or {})}}


def ctx(*, channel=api.AiChannel.BOT_CHAT, user_id=1, trigger=None, ticket_id=None):
    return api.AiRequestContext(channel, user_id, trigger_message_id=trigger, ticket_id=ticket_id)


async def test_answer_journal_feedback_stats_and_retention(journal, transport, kb, monkeypatch):
    bodies = []
    transport(lambda request: bodies.append(json.loads(request.content)) or response())
    result = await api.answer('Почта test@example.com', [api.ChatTurn('user', '@privateuser')], ctx())
    assert result.outcome == api.AiOutcome.ANSWERED
    assert 'test@example.com' not in json.dumps(bodies) and '@privateuser' not in json.dumps(bodies)
    record = await api.get_request(result.request_id)
    assert record.answer_text == result.reply_text and record.channel == api.AiChannel.BOT_CHAT
    assert not await api.record_feedback(record.id, 2, True)
    assert await api.record_feedback(record.id, 1, True)
    assert await api.record_feedback(record.id, 1, False)
    stats = await api.get_stats(7)
    assert stats.requests == stats.answered == stats.not_helpful == 1
    assert stats.cost_usd > 0 and stats.by_channel == {'bot_chat': 1}
    await api.attach_reply_message(record.id, 555)
    await repository.update_request(record.id, ticket_id=10)
    assert await api.ai_reply_message_ids(10) == {555}
    assert await api.count_ai_replies_in_ticket(10) == 1
    assert not await api.ticket_has_handoff(10)
    await api.mark_handoff_done(record.id, 10)
    assert await api.ticket_has_handoff(10)
    assert await api.count_ai_replies_in_ticket(10) == 0
    with Session(journal.engine) as db:
        row = db.get(AiSupportRequest, record.id)
        assert row.question_text == 'Почта [почта]' and row.prompt_tokens == 100 and row.reasoning_tokens == 10
        row.created_at = datetime.now(UTC) - timedelta(days=31)
        db.commit()
    await retention.cleanup()
    assert (await api.get_request(record.id)).answer_text is None
    with Session(journal.engine) as db:
        assert db.get(AiSupportRequest, record.id).question_text is None


async def test_duplicate_and_human_request(journal, transport, kb):
    calls = []
    transport(lambda request: calls.append(request) or response())
    context = ctx(channel=api.AiChannel.TICKET, trigger=42, ticket_id=10)
    first = await api.answer('Как подключиться?', [], context)
    second = await api.answer('Как подключиться?', [], context)
    assert first.outcome == api.AiOutcome.ANSWERED and second.outcome == api.AiOutcome.DUPLICATE
    assert len(calls) == 1
    human = await api.answer('Позовите оператора', [], ctx(user_id=2))
    assert human.outcome == api.AiOutcome.HANDOFF and human.handoff_reason == api.HandoffReason.CLIENT_REQUEST
    assert len(calls) == 1 and human.request_id
    assert await limits.user_requests_today(2) == 0


@pytest.mark.parametrize(
    'text',
    [
        'оператор',
        'Оператор!',
        'оператора пожалуйста',
        'Человек',
        'Позови человека',
        'Позовите оператора',
        'Хочу живого человека',
        'нужен живой оператор',
        'Свяжите меня с сотрудником',
        'Можно оператора?',
        'Не хочу говорить с ботом, позовите человека',
        'connect me to a human',
        'I want to talk to a person',
    ],
)
def test_human_detector(text):
    assert api.is_human_request(text)


@pytest.mark.parametrize(
    'text',
    [
        'оператор связи',
        'Мне не нужен оператор',
        'Как подключиться?',
        'Ошибка в приложении',
        'У меня оператор МТС, не работает впн',
        'На мобильном операторе Билайн не грузится',
        'оператор блокирует VPN?',
        'Сменил оператора и всё перестало работать',
        'без оператора разберусь',
        'Человек, который мне посоветовал ваш VPN, сказал что есть пробный период',
    ],
)
def test_human_false_positives(text):
    assert not api.is_human_request(text)


async def test_handoff_model_and_thinking_roundtrip(journal, transport, kb, monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_THINKING_ENABLED', True)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return response(
                None,
                calls=[call(args={'reason': 'refund', 'summary': 'Возврат для user@example.org'})],
                reasoning='Скрытое рассуждение',
            )
        assert body['messages'][-2]['reasoning_content'] == 'Скрытое рассуждение'
        assert body['messages'][-1]['role'] == 'tool'
        return response('Вопрос передан оператору, ответ придёт сюда.')

    transport(handler)
    result = await api.answer('Верните оплату', [], ctx())
    assert result.outcome == api.AiOutcome.HANDOFF and result.handoff_reason == api.HandoffReason.REFUND
    assert result.operator_summary == 'Возврат для [почта]'
    with Session(journal.engine) as db:
        row = db.get(AiSupportRequest, result.request_id)
        assert row.prompt_tokens == 200 and row.tool_calls == 1 and row.cost_usd > 0


async def test_tool_round_limit(journal, transport, kb):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) <= 3:
            return response(None, calls=[call('unknown')])
        assert body['tool_choice'] == 'none'
        return response()

    transport(handler)
    result = await api.answer('Подскажите', [], ctx())
    assert result.outcome == api.AiOutcome.ANSWERED and len(bodies) == 4
    with Session(journal.engine) as db:
        assert db.get(AiSupportRequest, result.request_id).tool_calls == 3


async def test_account_tools_only_context_user(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_SHARE_ACCOUNT_DATA', True)
    overview = AsyncMock(return_value={'balance_rub': 12})
    monkeypatch.setattr(tools, 'account_overview', overview)
    state = tools.ToolState()
    assert (
        await tools.execute(call('get_account_overview', {'user_id': 55}), ctx(), snapshot(), state)
        == tools.UNAVAILABLE
    )
    overview.assert_not_awaited()
    assert await tools.execute(call('get_account_overview'), ctx(user_id=7), snapshot(), state) == {'balance_rub': 12}
    overview.assert_awaited_once_with(7)
    allowed = tools.definitions(ctx(), snapshot())
    assert len(allowed) == 3
    assert all(d['function']['parameters']['additionalProperties'] is False for d in allowed)
    monkeypatch.setattr(settings, 'AI_SUPPORT_SHARE_ACCOUNT_DATA', False)
    assert len(tools.definitions(ctx(), snapshot())) == 1
    assert await tools.execute(call('get_account_overview'), ctx(), snapshot(), state) == tools.UNAVAILABLE


async def test_limits_budget_and_concurrency(journal, transport, kb, monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_USER_DAILY_LIMIT', 1)
    transport(lambda request: response())
    assert (await api.answer('Вопрос', [], ctx())).outcome == api.AiOutcome.ANSWERED
    assert (await api.answer('Вопрос', [], ctx())).outcome == api.AiOutcome.RATE_LIMITED
    monkeypatch.setattr(settings, 'AI_SUPPORT_DAILY_BUDGET_USD', 0.000001)
    assert (await api.answer('Вопрос', [], ctx(user_id=2))).outcome == api.AiOutcome.BUDGET_EXCEEDED
    monkeypatch.setattr(settings, 'AI_SUPPORT_DAILY_BUDGET_USD', 0)
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def slow(request):
        waiting.set()
        await release.wait()
        return response()

    transport(slow)
    first = asyncio.create_task(api.answer('Вопрос', [], ctx(user_id=3)))
    await waiting.wait()
    assert (await api.answer('Другой', [], ctx(user_id=3))).outcome == api.AiOutcome.RATE_LIMITED
    release.set()
    assert (await first).outcome == api.AiOutcome.ANSWERED
    assert 3 not in limits._users


async def test_error_alerts_and_guard(journal, transport, kb, monkeypatch):
    handler = AsyncMock()
    api.set_admin_alert_handler(handler)
    transport(lambda request: httpx.Response(401))
    for user_id in (1, 2):
        result = await api.answer('Вопрос', [], ctx(user_id=user_id))
        assert result.outcome == api.AiOutcome.ERROR and result.error_code == 'auth'
    assert handler.await_count == 1
    transport(lambda request: response('Подключитесь к msk01'))
    assert (await api.answer('Вопрос', [], ctx())).outcome == api.AiOutcome.FILTERED
    transport(lambda request: httpx.Response(503, headers={'Retry-After': '0'}))
    for _ in range(3):
        await api.answer('Вопрос', [], ctx())
    assert handler.await_count == 2 and handler.call_args.args[0] == 'provider_down'
    monkeypatch.setattr(repository, 'claim', AsyncMock(side_effect=RuntimeError('db')))
    assert (await api.answer('Вопрос', [], ctx())).outcome == api.AiOutcome.ERROR


async def test_disabled_and_db_errors(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_ENABLED', False)
    assert (await api.answer('Вопрос', [], ctx())).outcome == api.AiOutcome.DISABLED
    monkeypatch.setattr(repository, 'AsyncSessionLocal', lambda: (_ for _ in ()).throw(RuntimeError('db')))
    assert await api.get_request(1) is None
    assert not await api.record_feedback(1, 1, True)
    assert await api.ai_reply_message_ids(1) == set()
    assert await api.count_ai_replies_in_ticket(1) == 0
    assert not await api.ticket_has_handoff(1)
    assert (await api.get_stats(7)).requests == 0


async def test_log_texts_off(journal, transport, kb, monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_LOG_TEXTS', False)
    transport(lambda request: response())
    result = await api.answer('Приватный вопрос', [], ctx())
    with Session(journal.engine) as db:
        row = db.get(AiSupportRequest, result.request_id)
        assert row.question_text is None and row.answer_text == result.reply_text


async def test_handoff_empty_final_uses_template(journal, transport, kb):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return response(None, calls=[call(args={'reason': 'no_knowledge', 'summary': 'Вне базы'})])
        return response('')

    transport(handler)
    result = await api.answer('Вопрос вне базы', [], ctx())
    assert result.outcome == api.AiOutcome.HANDOFF
    assert result.reply_text == service.HANDOFF_TEXT


async def test_total_deadline_including_tools(journal, transport, kb, monkeypatch):
    # Тип настройки int, но снимок на время теста разрешает короткий дедлайн без ожидания 45 с.
    from dataclasses import replace

    config = replace(snapshot(), timeout=0.03)
    monkeypatch.setattr(service.settings_view, 'snapshot', lambda: config)
    transport(lambda request: response(None, calls=[call('get_devices')]))

    async def slow(*args):
        await asyncio.sleep(1)
        return {}

    monkeypatch.setattr(tools, 'execute', slow)
    result = await api.answer('Вопрос', [], ctx())
    assert result.outcome == api.AiOutcome.ERROR and result.error_code == 'timeout'
    assert not limits._users
    assert (await api.get_request(result.request_id)).outcome == 'error'


async def test_global_concurrency_four(journal, transport, kb):
    active, maximum = 0, 0
    four_entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            four_entered.set()
        await release.wait()
        active -= 1
        return response()

    transport(handler)
    tasks = [asyncio.create_task(api.answer('Вопрос', [], ctx(user_id=i))) for i in range(1, 7)]
    await asyncio.wait_for(four_entered.wait(), 1)
    assert maximum == 4
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(result.outcome == api.AiOutcome.ANSWERED for result in results)
    assert maximum == 4


async def test_budget_cache_invalidates_after_write(journal, monkeypatch):
    assert await limits.spent_today() == 0
    with Session(journal.engine) as db:
        db.add(AiSupportRequest(channel='admin_test', model='model', provider='deepseek', cost_usd=Decimal('0.5')))
        db.commit()
    assert await limits.spent_today() == 0
    limits.invalidate_budget()
    assert await limits.spent_today() == Decimal('0.5')
    assert limits.day_start().hour == 21
    assert limits.day_start().tzinfo is UTC


async def test_journal_failure_blocks_publish(journal, transport, kb, monkeypatch):
    transport(lambda request: response())
    monkeypatch.setattr(repository, 'finish', AsyncMock(side_effect=RuntimeError('storage')))
    result = await api.answer('Вопрос', [], ctx())
    assert result.outcome == api.AiOutcome.ERROR and result.error_code == 'journal'
    assert not limits._users


async def test_total_deadline_bounds_alert_and_finalization(journal, transport, kb, monkeypatch):
    import time
    from dataclasses import replace

    config = replace(snapshot(), timeout=0.05)
    monkeypatch.setattr(service.settings_view, 'snapshot', lambda: config)

    async def slow_alert(*args):
        await asyncio.sleep(1)

    api.set_admin_alert_handler(slow_alert)
    transport(lambda request: httpx.Response(401))
    started = time.monotonic()
    result = await api.answer('Вопрос', [], ctx())
    assert result.error_code == 'auth'
    assert time.monotonic() - started < 0.15

    transport(lambda request: response())

    async def slow_finish(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(repository, 'finish', slow_finish)
    started = time.monotonic()
    result = await api.answer('Вопрос', [], ctx())
    assert result.error_code == 'journal'
    assert time.monotonic() - started < 0.15


@pytest.mark.parametrize(
    ('text', 'handoff'),
    [
        ('Передаю вопрос оператору: ответ придёт сюда.', True),
        ('Подписка активна. Передал ваш вопрос оператору, ответ придёт сюда.', True),
        ('Вопрос передан сотруднику поддержки.', True),
        ('Если ошибка останется — передам вопрос оператору.', False),
        ('Обновите подписку в приложении и выберите другую вкладку.', False),
    ],
)
async def test_handoff_claim_in_text_becomes_handoff(journal, transport, kb, text, handoff):
    transport(lambda request: response(text))
    result = await api.answer('Не работает YouTube', [], ctx())
    if handoff:
        assert result.outcome == api.AiOutcome.HANDOFF and result.handoff_reason == api.HandoffReason.OTHER
        assert result.reply_text == text
    else:
        assert result.outcome == api.AiOutcome.ANSWERED and result.handoff_reason is None


async def test_model_reply_prefix_is_stripped(journal, transport, kb):
    transport(lambda request: response('🤖 ИИ-помощник\n\n🤖 ИИ-помощник\n\nОбновите подписку.'))
    result = await api.answer('Не работает', [], ctx())
    assert result.outcome == api.AiOutcome.ANSWERED and result.reply_text == 'Обновите подписку.'
