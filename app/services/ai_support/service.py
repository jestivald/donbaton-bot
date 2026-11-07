"""Оркестрация ответа ИИ: любой сбой возвращается вызывающей интеграции как ERROR."""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from app.services.ai_support import knowledge, limits, pricing, prompts, provider, repository, settings_view, tools
from app.services.ai_support.api import AdminAlertHandler, AiAnswer, AiOutcome, HandoffReason
from app.services.ai_support.output_guard import guard
from app.services.ai_support.redaction import redact


logger = structlog.get_logger(__name__)
HANDOFF_TEXT = 'Вопрос передан оператору. Ответ придёт сюда.'
_alert_handler: AdminAlertHandler | None = None
_alert_times: dict[str, float] = {}
_provider_failures = 0
ALERT_TEXT = {
    'auth': 'ИИ-помощник: провайдер отклонил ключ API. Проверьте настройки доступа.',
    'balance': 'ИИ-помощник: закончился баланс провайдера. Пополните его для продолжения работы.',
    'budget': 'ИИ-помощник: достигнут дневной бюджет. Ответы приостановлены до следующих суток МСК.',
    'provider_down': 'ИИ-помощник: провайдер недоступен несколько запросов подряд. Используется обычная поддержка.',
}


def set_admin_alert_handler(handler: AdminAlertHandler | None) -> None:
    global _alert_handler
    _alert_handler = handler


async def alert(kind: str, *, deadline: float | None = None) -> None:
    if _alert_handler is None or time.monotonic() - _alert_times.get(kind, float('-inf')) < 3600:
        return
    timeout = 5 if deadline is None else min(5, max(0, (deadline - time.monotonic()) / 2))
    if timeout <= 0:
        return
    _alert_times[kind] = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            await _alert_handler(kind, ALERT_TEXT[kind])
    except Exception:
        logger.warning('Не удалось отправить тревогу ИИ', kind=kind)


def is_human_request(text: str) -> bool:
    value = (text or '').lower().strip()
    if re.search(r'\b(?:не\s+(?:нужен|надо|зови|нужна)|без)\s+(?:оператор|человек|сотрудник)', value):
        return False
    if re.search(r'\bоператор\s+(?:связи|мобильн|интернет)', value):
        return False
    return bool(
        re.search(
            r'\b(?:оператор(?:а|у|ом)?|жив(?:ой|ого|ому)\s+(?:человек\w*|сотрудник\w*)|'
            r'(?:позови(?:те)?|дайте|хочу|нужен|соедините|переведите|свяжите)\s+(?:меня\s+)?(?:с\s+)?(?:человек\w*|сотрудник\w*)|'
            r'human|live\s+agent|speak\s+to\s+(?:a\s+)?(?:person|agent))\b',
            value,
            re.IGNORECASE,
        )
    )


@dataclass
class Run:
    usage: provider.Usage = field(default_factory=provider.Usage)
    cost: Decimal = Decimal(0)
    tool_state: tools.ToolState = field(default_factory=tools.ToolState)
    kb_version: str = ''
    prompt_version: str = ''


async def generate(question, history, ctx, config, request_id, run: Run, deadline):
    kb = await knowledge.load(ctx.language)
    run.kb_version = kb.status.version
    messages = prompts.messages(question, history, ctx, kb, config)
    definitions = tools.definitions(ctx, config)
    for round_number in range(4):
        if not run.prompt_version:
            # Метка вызова модели нужна лимитеру, даже если провайдер вернул ошибку.
            await repository.update_request(request_id, prompt_version=prompts.PROMPT_VERSION)
            run.prompt_version = prompts.PROMPT_VERSION
        moment = datetime.now(UTC)
        try:
            completion = await provider.complete(
                messages,
                definitions,
                config,
                deadline=deadline,
                tool_choice='none' if round_number == 3 else 'auto',
            )
        except provider.ProviderError as exc:
            run.usage.add(exc.usage)
            run.cost += pricing.cost(exc.usage, config, moment)
            if exc.code == 'empty' and run.tool_state.reason:
                return AiAnswer(
                    AiOutcome.HANDOFF,
                    reply_text=HANDOFF_TEXT,
                    handoff_reason=run.tool_state.reason,
                    operator_summary=run.tool_state.summary,
                )
            raise
        run.usage.add(completion.usage)
        run.cost += pricing.cost(completion.usage, config, moment)
        message = completion.message
        calls = message.get('tool_calls') or []
        if not calls:
            text = guard(message.get('content') or (HANDOFF_TEXT if run.tool_state.reason else ''))
            if text is None:
                return AiAnswer(AiOutcome.FILTERED, error_code='output_filtered')
            return AiAnswer(
                AiOutcome.HANDOFF if run.tool_state.reason else AiOutcome.ANSWERED,
                reply_text=text,
                handoff_reason=run.tool_state.reason,
                operator_summary=run.tool_state.summary,
            )
        if round_number == 3 or not isinstance(calls, list) or len(calls) > 8:
            return AiAnswer(AiOutcome.ERROR, error_code='tool_limit')
        # Возвращаем reasoning_content ВСЕХ прошлых ассистентских ходов вместе с tools.
        assistant = {'role': 'assistant', 'content': message.get('content'), 'tool_calls': calls}
        if config.thinking:
            assistant['reasoning_content'] = message.get('reasoning_content') or ''
        messages.append(assistant)
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get('id'), str):
                return AiAnswer(AiOutcome.ERROR, error_code='invalid_tool_call')
            output = await tools.execute(call, ctx, config, run.tool_state)
            messages.append(
                {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(output, ensure_ascii=False)}
            )
    return AiAnswer(AiOutcome.ERROR, error_code='tool_limit')


async def check_limits(ctx, config):
    if ctx.user_id is not None and await limits.user_requests_today(ctx.user_id) >= config.user_limit:
        return AiAnswer(AiOutcome.RATE_LIMITED)
    if config.budget and await limits.spent_today() >= Decimal(str(config.budget)):
        await alert('budget')
        return AiAnswer(AiOutcome.BUDGET_EXCEEDED)
    return None


async def answer(question, history, ctx) -> AiAnswer:
    global _provider_failures
    if not settings_view.is_enabled():
        return AiAnswer(AiOutcome.DISABLED)
    started = time.monotonic()
    deadline = started + 45
    request_id = None
    acquired = False
    run = Run()
    result = AiAnswer(AiOutcome.ERROR, error_code='internal')
    try:
        config = settings_view.snapshot()
        deadline = started + config.timeout
        # Оставляем часть общего дедлайна для короткой финализации журнала.
        work_deadline = deadline - min(1.0, config.timeout / 10)
        async with asyncio.timeout(max(0, work_deadline - time.monotonic())):
            human_requested = is_human_request(question)
            request_id = await repository.claim(ctx, config)
            if request_id is None:
                return AiAnswer(AiOutcome.DUPLICATE)
            if human_requested:
                result = AiAnswer(
                    AiOutcome.HANDOFF, reply_text=HANDOFF_TEXT, handoff_reason=HandoffReason.CLIENT_REQUEST
                )
            else:
                acquired = limits.acquire_user(ctx.user_id)
                if not acquired:
                    result = AiAnswer(AiOutcome.RATE_LIMITED)
                else:
                    async with limits.semaphore:
                        result = await check_limits(ctx, config)
                        if result is None:
                            result = await generate(question, history, ctx, config, request_id, run, work_deadline)
                            _provider_failures = 0
    except provider.ProviderError as exc:
        result = AiAnswer(AiOutcome.ERROR, error_code=exc.code)
        if exc.code in ('auth', 'balance'):
            _provider_failures = 0
            await alert(exc.code, deadline=deadline)
        elif exc.transient:
            _provider_failures += 1
            if _provider_failures >= 3:
                await alert('provider_down', deadline=deadline)
        else:
            _provider_failures = 0
    except TimeoutError:
        result = AiAnswer(AiOutcome.ERROR, error_code='timeout')
        _provider_failures += 1
        if _provider_failures >= 3:
            await alert('provider_down', deadline=deadline)
    except Exception:
        logger.warning('Сбой ядра ИИ; основной поток поддержки продолжается')
        result = AiAnswer(AiOutcome.ERROR, error_code='internal')
    finally:
        # Блокировка живёт до записи журнала: следующий запрос увидит стоимость и лимит.
        try:
            if request_id is not None:
                async with asyncio.timeout(max(0, min(5, deadline - time.monotonic()))):
                    await repository.finish(
                        request_id,
                        result,
                        usage=run.usage,
                        cost=run.cost,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        tool_calls=run.tool_state.calls,
                        question=redact(question)[:2000] if settings_view.snapshot().log_texts else None,
                        kb_version=run.kb_version,
                        prompt_version=run.prompt_version,
                    )
                limits.invalidate_budget()
        except Exception:
            logger.warning('Не удалось завершить запись журнала ИИ')
            if result.outcome != AiOutcome.ERROR:
                result = AiAnswer(AiOutcome.ERROR, error_code='journal')
        finally:
            if acquired:
                limits.release_user(ctx.user_id)
    result.request_id = request_id
    return result
