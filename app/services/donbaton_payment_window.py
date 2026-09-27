"""Окно оплаты ДонБатона: после окончания срока клиенту остаётся один сервер.

Клиент в РФ без VPN не откроет ни Telegram (бота), ни кабинет — вход туда тоже
через Telegram, — и заплатить ему нечем. Поэтому ещё ``DONBATON_PAYMENT_WINDOW_HOURS``
часов после ``end_date`` подписка Wi-Fi/LTE держит в панели ровно один внутренний
сквад — ``DONBATON_PAYMENT_WINDOW_SQUAD_UUID``: отдельный inbound, маршрутизация
которого пропускает только Telegram, наши домены и оплату. Что именно доступно,
решает сетевая настройка сквада и нод, а не бот.

Здесь только решение «есть ли окно и до какого момента». Пишет его в панель
единственный владелец доступа управляемых подписок — ``apply_panel_policy``
(и через него цикл LTE, и ``panel_sync.push_subscription``). Апстримовый
grace-доступ для этих подписок выключен и второго владельца сквадов не
появляется: конец окна снимает тот же цикл LTE, оплата в окне даёт обычные
сквады той же политикой.

Окно не продлевает подписку в базе и не меняет её статус: в боте она истекла,
продлить её можно как обычно. В панели на время окна аккаунт ``ACTIVE`` с датой
``end_date + окно`` — это состояние проекции, а не продление: импорт из панели
для управляемых подписок не берёт ни дату, ни ACTIVE (``panel_sync.projection``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from app.config import normalize_squad_uuid, settings


logger = structlog.get_logger(__name__)

#: Статусы подписки, у которых после окончания срока открывается окно. ``disabled``
#: (обнулена админом) и ``pending`` (не начатый пробный) окна не получают;
#: ``limited`` у подписок Wi-Fi/LTE не бывает — исчерпание LTE снимает только LTE.
_WINDOW_STATUSES = frozenset({'active', 'trial', 'expired'})

#: Панель хранит миллисекунды; секунды хватает, чтобы узнать свою дату.
DATE_TOLERANCE = timedelta(seconds=2)

_REPORTED = set()


@dataclass(frozen=True, slots=True)
class PaymentWindow:
    """Открытое окно: какой сквад оставить и когда оно закрывается."""

    squad_uuid: str
    until: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _warn_once(event: str, **fields) -> None:
    key = (event, tuple(sorted(fields.items())))
    if key not in _REPORTED:
        _REPORTED.add(key)
        logger.warning(event, **fields)


def _configured_squads(raw: str) -> set[str]:
    squads = set()
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        try:
            squads.add(str(UUID(item)))
        except ValueError:
            continue  # неверный список Wi-Fi/LTE закрывает доступ целиком (_squad_sets)
    return squads


def window_hours() -> int:
    """Длина окна в часах; 0 — окна нет (выключено или нет сквада)."""
    return settings.DONBATON_PAYMENT_WINDOW_HOURS if window_squad() else 0


def window_squad() -> str | None:
    """UUID сквада окна или ``None``, если окно выключено или настроено неверно.

    Неверная настройка закрывает окно, а не доступ: подписка ведёт себя как без
    окна (доступ снимается сразу). Сквад окна не может совпадать со сквадом
    Wi-Fi или LTE — тогда истёкшая подписка сохранила бы обычный доступ.
    """
    try:
        hours = int(settings.DONBATON_PAYMENT_WINDOW_HOURS or 0)
    except (TypeError, ValueError):
        _warn_once('Окно оплаты выключено: неверная длина', value=str(settings.DONBATON_PAYMENT_WINDOW_HOURS))
        return None
    raw = settings.DONBATON_PAYMENT_WINDOW_SQUAD_UUID
    if hours <= 0 or not (raw or '').strip():
        return None
    try:
        squad = normalize_squad_uuid(raw)
    except ValueError:
        _warn_once('Окно оплаты выключено: сквад окна — не UUID', value=str(raw)[:64])
        return None
    managed = _configured_squads(settings.DONBATON_WIFI_SQUAD_UUIDS) | _configured_squads(
        settings.DONBATON_LTE_SQUAD_UUIDS
    )
    if squad in managed:
        _warn_once('Окно оплаты выключено: сквад окна совпадает со сквадом Wi-Fi/LTE', squad=squad)
        return None
    return squad


def payment_window(subscription, *, now: datetime | None = None, user=None) -> PaymentWindow | None:
    """Окно оплаты подписки в момент ``now`` или ``None``.

    Окно есть у подписки Wi-Fi/LTE, у которой срок кончился не раньше, чем
    ``окно`` часов назад: ``end_date <= now < end_date + окно``. Считается от
    ``end_date``, поэтому подписка, истёкшая до включения окна или до выкладки,
    получает его на остаток этих часов, а не на полные 48.

    Пробные — только при ``DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED``. Пользователь,
    если передан, должен быть активным: заблокированному окна нет. Если не
    передан — это обязан проверить вызывающий (у ``apply_panel_policy`` блокировку
    приносит статус DISABLED в наборе полей, у цикла LTE — сам пользователь).
    """
    from app.services.lte_quota_service import is_managed

    if not is_managed(subscription):
        return None
    squad = window_squad()
    if squad is None:
        return None
    if getattr(subscription, 'status', None) not in _WINDOW_STATUSES:
        return None
    if getattr(subscription, 'is_trial', False) and not settings.DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED:
        return None
    if user is not None and getattr(user, 'status', None) != 'active':
        return None
    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return None
    end_date = _utc(end_date)
    moment = _utc(now or datetime.now(UTC))
    until = end_date + timedelta(hours=settings.DONBATON_PAYMENT_WINDOW_HOURS)
    if end_date <= moment < until:
        return PaymentWindow(squad_uuid=squad, until=until)
    return None


def cycle_candidates(now: datetime):
    """Условие выборки цикла LTE для подписок, у которых сейчас может быть окно.

    Цикл берёт подписку, пока ей не сняли доступ (``panel_access_cleared_at IS
    NULL``) или пока она жива. Подписка, которой доступ сняли ещё до включения
    окна (или до выкладки), больше не выбиралась бы — и окна не получила бы. Это
    условие возвращает её на время окна; сам ли момент попадает в окно, решает
    ``payment_window``. ``None`` — окно выключено, выборка прежняя.
    """
    hours = window_hours()
    if not hours:
        return None
    from sqlalchemy import select

    from app.database.models import Subscription, User

    clause = (
        (Subscription.end_date > now - timedelta(hours=hours))
        & Subscription.status.in_(sorted(_WINDOW_STATUSES))
        & Subscription.user_id.in_(select(User.id).where(User.status == 'active'))
    )
    if not settings.DONBATON_PAYMENT_WINDOW_TRIAL_ENABLED:
        clause = clause & Subscription.is_trial.isnot(True)
    return clause


def panel_date_is_window(subscription, panel_expire_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Дата аккаунта в панели — не срок подписки, а конец окна оплаты.

    Так узнаются события панели об «истечении» управляемой подписки, которые
    относятся к окну: срок подписки в боте уже прошёл, а панель считает от
    ``end_date + окно``. Напоминания «истекает через 24 ч» и «истекла» по такой
    дате клиенту врут — подписка закончилась раньше, об этом уже сообщил бот.
    Сравнение не требует включённого окна: окно могли выключить, пока аккаунт
    ещё держит его дату.
    """
    from app.services.lte_quota_service import is_managed

    if panel_expire_at is None or not is_managed(subscription):
        return False
    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    end_date = _utc(end_date)
    if end_date > _utc(now or datetime.now(UTC)):
        return False  # срок ещё идёт: напоминания панели — про него
    return _utc(panel_expire_at) - end_date > DATE_TOLERANCE


_DEFAULT_SERVER_NAME = '💳 Оплата | Telegram'
_EXPIRED_HINT_DEFAULT = (
    '⏳ {period} (до {until}) в приложении работает сервер «{server}» — выберите его, '
    'чтобы открыть бот и оплатить; остальные серверы не работают до оплаты.'
)


def _hours_phrase(hours: int, language: str | None) -> str:
    code = (language or 'ru').split('-')[0].lower()
    if code in ('ru', 'ua', 'uk'):
        forms = ('час', 'часа', 'часов') if code == 'ru' else ('година', 'години', 'годин')
        if hours % 10 == 1 and hours % 100 != 11:
            return f'{hours} {forms[0]}'
        if hours % 10 in (2, 3, 4) and hours % 100 not in (12, 13, 14):
            return f'{hours} {forms[1]}'
        return f'{hours} {forms[2]}'
    if code == 'en':
        return f'{hours} hour' + ('' if hours == 1 else 's')
    if code == 'fa':
        return f'{hours} ساعت'
    if code == 'zh':
        return f'{hours} 小时'
    return f'{hours} h'


def expired_notice_hint(subscription, user, *, now: datetime | None = None) -> str | None:
    """Строка для «⛔ Подписка истекла»: какой сервер открыть, чтобы заплатить.

    ``None`` — окна у подписки нет (выключено, пробная без флага, заблокирован,
    окно уже прошло): тогда уведомление остаётся прежним. HTML Telegram: имя
    сервера — настройка, поэтому экранируется.
    """
    import html

    from app.localization.texts import get_texts
    from app.utils.timezone import format_local_datetime

    window = payment_window(subscription, now=now, user=user)
    if window is None:
        return None
    language = getattr(user, 'language', None) or settings.DEFAULT_LANGUAGE
    template = get_texts(language).t('DONBATON_PAYMENT_WINDOW_EXPIRED_HINT', _EXPIRED_HINT_DEFAULT)
    server = (settings.DONBATON_PAYMENT_WINDOW_SERVER_NAME or '').strip() or _DEFAULT_SERVER_NAME
    return template.format(
        period=_hours_phrase(settings.DONBATON_PAYMENT_WINDOW_HOURS, language),
        until=format_local_datetime(window.until, '%d.%m.%Y %H:%M'),
        server=html.escape(server),
    )


def same_panel_date(left: datetime | None, right: datetime | None) -> bool:
    """Совпадают ли две даты панели с точностью до её округления."""
    if left is None or right is None:
        return left is right
    return abs(_utc(left) - _utc(right)) <= DATE_TOLERANCE
