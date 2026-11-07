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
from app.services.ai_support.output_guard import PROMISE_RE, guard, has_control_leak, normalize, operator_summary
from app.services.ai_support.redaction import redact


logger = structlog.get_logger(__name__)
HANDOFF_TEXT = 'Вопрос передан оператору. Ответ придёт сюда.'
# Модель иногда пишет «передаю вопрос оператору», не вызвав handoff_to_human (07.11: тикеты #19, #22 ушли в answered
# мимо SLA). Настоящее/прошедшее время («передаю», «передал», «передан») — это передача; условное «передам» — нет.
HANDOFF_CLAIM_RE = re.compile(
    r'(?<![\w-])(?:переда(?:ю|л[аи]?|н[аоы]?|[её]м)|перевожу|перев[её]л[аи]?)(?![\w-])[^.!?\n]{0,60}?'
    r'(?<![\w-])(?:оператор\w*|сотрудник\w*|специалист\w*|человек\w*)|'
    r'\b(?:forwarded|passed|transferred|escalated)\b[^.!?\n]{0,60}?\bto\s+'
    r'(?:(?:a|an|our)\s+)?(?:human|operator|agent|support|specialist)\b',
    re.IGNORECASE,
)


def has_handoff_claim(text: str) -> bool:
    for match in HANDOFF_CLAIM_RE.finditer(text):
        prefix = text[max(0, match.start() - 40) : match.start()]
        if not re.search(
            r'\b(?:не|not|never|haven[’\']t|hasn[’\']t|wasn[’\']t)\s*'
            r'(?:(?:yet|been|being|был|была|было|были|будет|будут)\s*)?$',
            prefix,
            re.IGNORECASE,
        ):
            return True
    return False


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


# «Оператор» в вопросах о VPN чаще всего — мобильный оператор (МТС, Билайн…), поэтому слово само по себе
# не считается просьбой о человеке: нужна либо реплика из одного обращения («оператор», «человека пожалуйста»),
# либо глагол просьбы рядом («позовите оператора», «нужен живой человек», «свяжите с сотрудником»).
_HUMAN_NOUN = r'(?:оператор\w*|человек\w*|сотрудник\w*|специалист\w*|менеджер\w*|поддержк\w*|админ\w*)'
_HUMAN_ONLY_RE = re.compile(
    r'^(?:(?:мне|нам)\s+)?(?:(?:жив\w*|реальн\w*|настоящ\w*)\s+)?' + _HUMAN_NOUN + r'(?:\s+(?:пожалуйста|плиз|pls))?$'
)
_HUMAN_ASK_RE = re.compile(
    r'(?:позови\w*|позвать|зови\w*|дайте|дай|хочу|нужен|нужна|нужно|соедини\w*|переключи\w*|переведи\w*|'
    r'свяжи\w*|связаться|подключи\w*|пригласи\w*|верните|вызови\w*|вызвать|можно)\s+'
    r'(?:мне\s+|меня\s+|нас\s+)?(?:с\s+|на\s+|к\s+)?(?:(?:жив\w*|реальн\w*|настоящ\w*)\s+)?' + _HUMAN_NOUN
)
_HUMAN_NEGATION_RE = re.compile(
    r'(?:не\s+(?:нужен|нужна|нужно|надо|зови\w*|хочу)|без)\s+(?:(?:жив\w*)\s+)?' + _HUMAN_NOUN
)
_HUMAN_EN_RE = re.compile(
    r'\b(?:(?:talk|speak|chat|connect\s+me)\s+(?:to|with)\s+(?:a\s+)?(?:human|person|agent|operator|someone)|'
    r'(?:real|live)\s+(?:human|person|agent|operator)|^(?:human|operator|agent)(?:\s+please)?$)\b'
)


def is_human_request(text: str) -> bool:
    value = re.sub(r'[!?.,…:;)(«»"\']+', ' ', (text or '').lower()).strip()
    value = re.sub(r'\s+', ' ', value)
    if not value or _HUMAN_NEGATION_RE.search(value):
        return False
    return bool(_HUMAN_ONLY_RE.match(value) or _HUMAN_ASK_RE.search(value) or _HUMAN_EN_RE.search(value))


@dataclass
class Run:
    usage: provider.Usage = field(default_factory=provider.Usage)
    cost: Decimal = Decimal(0)
    tool_state: tools.ToolState = field(default_factory=tools.ToolState)
    kb_version: str = ''
    prompt_version: str = ''


async def generate(question, history, ctx, config, request_id, run: Run, deadline, images=None):
    kb = await knowledge.load(ctx.language)
    run.kb_version = kb.status.version
    messages = prompts.messages(question, history, ctx, kb, config, images=images)
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
            if has_control_leak(message.get('content') or ''):
                return AiAnswer(AiOutcome.FILTERED, error_code='output_filtered')
            text = guard(
                prompts.clean_assistant_text(message.get('content') or '')
                or (HANDOFF_TEXT if run.tool_state.reason else '')
            )
            if text is None and run.tool_state.reason:
                # Передача человеку важнее текста: ответ модели не прошёл фильтр — клиенту говорим шаблон.
                return AiAnswer(
                    AiOutcome.HANDOFF,
                    reply_text=HANDOFF_TEXT,
                    handoff_reason=run.tool_state.reason,
                    operator_summary=run.tool_state.summary,
                )
            if text is None:
                return AiAnswer(AiOutcome.FILTERED, error_code='output_filtered')
            if PROMISE_RE.search(text) or PROMISE_RE.search(normalize(message.get('content') or '')):
                return AiAnswer(
                    AiOutcome.HANDOFF,
                    reply_text=HANDOFF_TEXT,
                    handoff_reason=HandoffReason.MONEY,
                    operator_summary=operator_summary(None, HandoffReason.MONEY),
                )
            if not run.tool_state.reason and has_handoff_claim(text):
                # Клиенту уже обещана передача — оформляем её, иначе тикет уйдёт в answered мимо очереди и SLA.
                run.tool_state.reason = HandoffReason.OTHER
                run.tool_state.summary = 'ИИ сообщил клиенту о передаче оператору без вызова инструмента'
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


async def answer(question, history, ctx, images=None) -> AiAnswer:
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
        logger.debug('Запрос ИИ-поддержки', image_count=len(images or []))
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
                            result = await generate(
                                question, history, ctx, config, request_id, run, work_deadline, images=images
                            )
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
            if result.outcome == AiOutcome.HANDOFF:
                result.operator_summary = operator_summary(result.operator_summary, result.handoff_reason)
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
