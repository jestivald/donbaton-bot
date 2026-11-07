"""Контракт ИИ-помощника поддержки ДонБатона (DeepSeek / OpenAI-совместимый провайдер).

Интеграции (бот, тикеты, админка) импортируют ТОЛЬКО этот модуль. Реализация живёт в соседних
модулях пакета и подставляется сюда; сигнатуры ниже — договор между исполнителями, менять их
можно только согласованно.

Общие правила реализации:
- функции не бросают исключений наружу (кроме явно оговорённых): сбой провайдера, БД или Redis
  превращается в ``AiAnswer(outcome=AiOutcome.ERROR, ...)`` / ``False`` / пустой результат и пишется в лог;
- каждая функция сама открывает короткие сессии БД (``AsyncSessionLocal``); HTTP-запрос к провайдеру
  выполняется вне транзакции;
- тексты клиента перед отправкой провайдеру обезличиваются, ответ модели проходит выходной фильтр.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal


# Префикс, с которым ответ ИИ публикуется в тикете (виден клиенту в кабинете и в Telegram).
AI_REPLY_PREFIX = '🤖 ИИ-помощник'

TicketMode = Literal['off', 'draft', 'auto']


class AiChannel(StrEnum):
    BOT_CHAT = 'bot_chat'  # свободный текст в личке бота
    TICKET = 'ticket'  # автоответ или черновик в тикете
    ADMIN_TEST = 'admin_test'  # «проверить ответ» в админке


class AiOutcome(StrEnum):
    ANSWERED = 'answered'  # есть ответ клиенту
    HANDOFF = 'handoff'  # модель (или детектор) решила передать человеку; reply_text — что сказать клиенту
    DISABLED = 'disabled'  # выключено настройками / нет ключа
    RATE_LIMITED = 'rate_limited'  # лимит клиента
    BUDGET_EXCEEDED = 'budget_exceeded'  # дневной бюджет исчерпан
    FILTERED = 'filtered'  # ответ не прошёл выходной фильтр — клиенту не отправлять
    DUPLICATE = 'duplicate'  # этот trigger_message_id уже обрабатывался
    ERROR = 'error'  # сбой провайдера/БД; error_code — машинный код


class HandoffReason(StrEnum):
    CLIENT_REQUEST = 'client_request'  # клиент просит человека
    MONEY = 'money'  # оплата/списание/баланс — спор или «оплатил, а не работает»
    REFUND = 'refund'  # возврат, компенсация
    ACCOUNT = 'account'  # привязка/слияние аккаунтов, перенос подписки, не тот тариф/лимит
    REFERRAL = 'referral'  # реферальные начисления
    NOT_RESOLVED = 'not_resolved'  # шаги не помогли
    NO_KNOWLEDGE = 'no_knowledge'  # вне базы знаний / непроверенный пункт
    ANGRY = 'angry'  # раздражение, брань
    LEGAL = 'legal'  # претензии, персональные данные
    OUTAGE = 'outage'  # похоже на массовый сбой
    OTHER = 'other'


@dataclass(frozen=True)
class ChatTurn:
    role: Literal['user', 'assistant']
    text: str  # исходный текст; обезличивание делает реализация


@dataclass(frozen=True)
class AiRequestContext:
    channel: AiChannel
    user_id: int | None  # users.id (НЕ telegram_id); None — гость/админский тест
    language: str = 'ru'
    ticket_id: int | None = None
    # ticket_messages.id сообщения клиента, на которое отвечаем; для TICKET — ключ идемпотентности
    trigger_message_id: int | None = None
    ticket_title: str | None = None
    # False — не давать модели инструменты с данными аккаунта (итог ещё зависит от настройки)
    allow_account_tools: bool = True


@dataclass
class AiAnswer:
    outcome: AiOutcome
    reply_text: str | None = None  # обычный текст для клиента (без HTML/Markdown), уже прошёл фильтр
    handoff_reason: HandoffReason | None = None
    operator_summary: str | None = None  # кратко для оператора (обезличено)
    request_id: int | None = None  # ai_support_requests.id
    error_code: str | None = None


@dataclass
class AiRequestRecord:
    id: int
    channel: AiChannel
    outcome: str  # значение AiOutcome или 'pending'
    user_id: int | None
    ticket_id: int | None
    trigger_message_id: int | None
    reply_message_id: int | None
    answer_text: str | None  # ответ модели (для черновика)
    handoff_reason: str | None
    operator_summary: str | None
    feedback: str | None  # 'helpful' | 'not_helpful' | None
    created_at: datetime


@dataclass
class AiStats:
    days: int
    requests: int = 0
    answered: int = 0
    handoffs: int = 0
    errors: int = 0
    blocked: int = 0  # rate_limited + budget_exceeded + disabled + filtered
    cost_usd: float = 0.0
    avg_latency_ms: int = 0
    helpful: int = 0
    not_helpful: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)
    handoff_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class KbStatus:
    pages: int  # сколько инфо-страниц вошло в базу
    items: int  # сколько вопросов/статей
    chars: int
    approx_tokens: int
    version: str  # короткий хэш содержимого
    loaded_at: datetime | None
    slugs: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- настройки (снимок на момент вызова)


def is_enabled() -> bool:
    """Общий выключатель И задан ключ API."""
    raise NotImplementedError


def bot_chat_enabled() -> bool:
    """Отвечать ли на свободный текст в личке бота (учитывает is_enabled())."""
    raise NotImplementedError


def ticket_mode() -> TicketMode:
    """Режим в тикетах: off / draft / auto ('off', если is_enabled() ложно)."""
    raise NotImplementedError


def max_ai_replies_per_ticket() -> int:
    raise NotImplementedError


def notify_admins_enabled() -> bool:
    """Слать ли админам в топик тикетов копии ответов ИИ и передачи человеку."""
    raise NotImplementedError


def history_turns() -> int:
    """Сколько последних реплик диалога передавать модели."""
    raise NotImplementedError


def chat_memory_ttl_seconds() -> int:
    """Сколько хранить память диалога свободного текста в боте."""
    raise NotImplementedError


def reply_footer_text() -> str:
    """Подпись под ответом ИИ в тикете (как позвать человека)."""
    raise NotImplementedError


# ---------------------------------------------------------------- основное


async def answer(question: str, history: list[ChatTurn], ctx: AiRequestContext) -> AiAnswer:
    """Сгенерировать ответ клиенту.

    Внутри: проверка включения, лимитов клиента и дневного бюджета; для TICKET — атомарная заявка
    по ``ctx.trigger_message_id`` (повтор → DUPLICATE); обезличивание; база знаний; инструменты
    (только чтение, только текущий клиент); вызов провайдера; выходной фильтр; запись в журнал.
    Решение «передать человеку» возвращается как HANDOFF (reply_text — что сказать клиенту),
    саму передачу (тикет, уведомление админам) выполняет вызывающий код.
    """
    raise NotImplementedError


def is_human_request(text: str) -> bool:
    """Быстрый детектор «позовите оператора/человека» без вызова модели."""
    raise NotImplementedError


async def get_request(request_id: int) -> AiRequestRecord | None:
    raise NotImplementedError


async def record_feedback(request_id: int, user_id: int, helpful: bool) -> bool:
    """Отзыв клиента; True — записано (только владелец запроса, повтор перезаписывает)."""
    raise NotImplementedError


async def attach_reply_message(request_id: int, reply_message_id: int) -> None:
    """Связать запрос с опубликованным сообщением тикета (ответ ИИ, его отправка из черновика или передача)."""
    raise NotImplementedError


async def mark_handoff_done(request_id: int, ticket_id: int | None) -> None:
    """Отметить, что передача человеку выполнена (тикет создан/админы уведомлены)."""
    raise NotImplementedError


async def ai_reply_message_ids(ticket_id: int) -> set[int]:
    """ID сообщений тикета, опубликованных ИИ-помощником (чтобы отличать их от ответов людей)."""
    raise NotImplementedError


async def count_ai_replies_in_ticket(ticket_id: int) -> int:
    raise NotImplementedError


async def ticket_has_handoff(ticket_id: int) -> bool:
    """Была ли в тикете передача человеку (после неё ИИ в этом тикете молчит)."""
    raise NotImplementedError


async def get_stats(days: int) -> AiStats:
    raise NotImplementedError


async def kb_status() -> KbStatus:
    raise NotImplementedError


def invalidate_kb_cache() -> None:
    raise NotImplementedError


async def startup() -> None:
    """Запуск при старте бота (после загрузки настроек): HTTP-клиент, фоновая чистка журнала."""
    raise NotImplementedError


async def shutdown() -> None:
    """Остановка: закрыть HTTP-клиент, отменить фоновые задачи."""
    raise NotImplementedError


def describe_settings() -> dict[str, Any]:
    """Сводка настроек для экрана админки (без значения ключа: только «задан/не задан»)."""
    raise NotImplementedError


# kind: 'auth' (401/403 — неверный ключ), 'balance' (402 — кончился баланс), 'budget' (дневной бюджет),
# 'provider_down' (серия 5xx/таймаутов); text — готовое русское сообщение для админов, без секретов.
AdminAlertHandler = Callable[[str, str], Awaitable[None]]


def set_admin_alert_handler(handler: AdminAlertHandler | None) -> None:
    """Интеграция регистрирует отправку тревог админам; ядро зовёт её не чаще раза в час на каждый kind."""
    raise NotImplementedError
