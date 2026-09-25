"""Уведомления об остатке LTE ДонБатона: «почти закончился» и «закончился».

Решение и отметку принимает цикл учёта (``lte_quota_service.decide_lte_notice``)
под блокировкой подписки, в одной транзакции со списанием; сюда кандидаты
приходят уже после коммита. Модуль только доставляет: читает подписку, собирает
текст и кнопки, шлёт через ``notification_delivery_service`` и никогда не
бросает — сбой доставки не должен трогать учёт. Отметка стоит до отправки,
поэтому неудача теряет сообщение этого периода, но не повторяет его.

Предохранитель: больше ``DONBATON_LTE_NOTICE_MAX_PER_CYCLE`` кандидатов за один
проход (первый запуск, возврат телеметрии после долгого сбоя) — клиентам не
пишем никому, в админ-чат сообщаем не чаще раза в час.

Текст берётся из ``DONBATON_LTE_LOW_NOTICE_TEXT`` / ``DONBATON_LTE_EXHAUSTED_NOTICE_TEXT``,
если владелец задал его в админке, иначе из локали (``LTE_QUOTA_*``). Строку
про докупку и кнопку «Докупить LTE» добавляем, только если докупка этой
подписке действительно доступна — те же условия, что у самой покупки.
"""

from __future__ import annotations

import html
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.services.notification_types import NotificationType


logger = structlog.get_logger(__name__)

_GIB = 1024**3
#: Дата — как на экране LTE (``handlers/subscription/lte.py``): местный пояс и его имя.
_DATE_FORMAT = '%d.%m.%Y %H:%M %Z'
_OVERFLOW_ALERT_INTERVAL_SECONDS = 3600

_NOTIFICATION_TYPES = {
    'low': NotificationType.LTE_QUOTA_LOW,
    'exhausted': NotificationType.LTE_QUOTA_EXHAUSTED,
}
_TEXT_KEYS = {'low': 'LTE_QUOTA_LOW_NOTICE', 'exhausted': 'LTE_QUOTA_EXHAUSTED_NOTICE'}
_OVERRIDE_SETTINGS = {'low': 'DONBATON_LTE_LOW_NOTICE_TEXT', 'exhausted': 'DONBATON_LTE_EXHAUSTED_NOTICE_TEXT'}
#: Последний рубеж, если локаль переопределена с ошибкой: сообщение уйдёт хотя бы коротким.
_FALLBACK_TEXTS = {
    'low': '📶 <b>LTE почти закончился</b>\n\nОсталось {remaining} ГиБ LTE. Wi-Fi работает до {end_date}.',
    'exhausted': '📵 <b>LTE закончился</b>\n\nWi-Fi работает как обычно до {end_date}.',
}

#: Время (``time.monotonic``) последнего сообщения о сработавшем предохранителе.
_overflow_alert: dict[str, float | None] = {'sent_at': None}


async def dispatch(bot: Any, candidates) -> dict[str, int]:
    """Доставить кандидатов одного прохода учёта. Никогда не бросает."""
    try:
        return await _dispatch(bot, list(candidates or ()))
    except Exception:
        logger.exception('LTE notice dispatch failed')
        return {}


async def _dispatch(bot: Any, candidates: list) -> dict[str, int]:
    outcome: Counter[str] = Counter()
    if not candidates:
        return {}
    if bot is None:
        logger.warning('LTE notices not sent: bot is unavailable', candidates=len(candidates))
        return {'skipped': len(candidates)}
    limit = max(1, int(settings.DONBATON_LTE_NOTICE_MAX_PER_CYCLE))
    if len(candidates) > limit:
        logger.warning('LTE notices skipped: too many candidates in one pass', candidates=len(candidates), limit=limit)
        await _alert_overflow(bot, len(candidates), limit)
        return {'skipped': len(candidates)}
    for candidate in candidates:
        try:
            if candidate.subscription_id is not None:
                result = await _notify_customer(bot, candidate)
            else:
                result = await _notify_admins_about_orphan(bot, candidate)
        except Exception:
            logger.exception(
                'LTE notice delivery failed',
                subscription_id=candidate.subscription_id,
                panel_id=candidate.panel_id,
                lte_event=candidate.event,
            )
            result = 'failed'
        outcome[result] += 1
    logger.info('LTE notices dispatched', candidates=len(candidates), **outcome)
    return dict(outcome)


async def _notify_customer(bot: Any, candidate) -> str:
    from app.localization.texts import get_texts
    from app.services.notification_delivery_service import notification_delivery_service
    from app.utils.timezone import format_local_datetime

    async with AsyncSessionLocal() as db:
        subscription = (
            await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
                .where(Subscription.id == candidate.subscription_id)
            )
        ).scalar_one_or_none()
    if subscription is None or subscription.user is None:
        logger.warning('LTE notice has no subscription to describe', subscription_id=candidate.subscription_id)
        return 'missing'

    user = subscription.user
    tariff = subscription.tariff
    texts = get_texts(user.language)
    topup = topup_available(subscription, tariff, user, topup_bytes=candidate.topup_bytes)
    tariff_name = str(getattr(tariff, 'name', '') or '').strip() or f'#{subscription.id}'
    end_date = format_local_datetime(subscription.end_date, _DATE_FORMAT, na_placeholder='—')
    remaining = format_remaining(candidate.remaining_bytes)
    message = render_notice_text(
        candidate.event,
        texts,
        tariff=html.escape(tariff_name, quote=False),
        remaining=remaining,
        end_date=end_date,
        topup=topup,
    )
    sent = await notification_delivery_service.send_notification(
        user=user,
        notification_type=_NOTIFICATION_TYPES[candidate.event],
        # Письму и событию кабинета — сырые значения: их шаблоны экранируют сами.
        context={
            'lte_event': candidate.event,
            'tariff_name': tariff_name,
            'remaining_gib': remaining,
            'end_date': end_date,
            'subscription_id': subscription.id,
            'topup_available': topup,
        },
        bot=bot,
        telegram_message=message,
        telegram_markup=build_notice_keyboard(texts, subscription.id, topup=topup),
    )
    return 'sent' if sent else 'not_delivered'


def format_remaining(remaining_bytes: int) -> str:
    """Остаток в ГиБ с двумя знаками — так же, как его пишет экран LTE."""
    return f'{max(0, remaining_bytes) / _GIB:.2f}'


def render_notice_text(event: str, texts: Any, *, tariff: str, remaining: str, end_date: str, topup: bool) -> str:
    """Текст владельца из админки, иначе локаль; строка о докупке — только если она доступна."""
    values = {'tariff': tariff, 'remaining': remaining, 'end_date': end_date}
    body = _format_override(event, values)
    if body is None:
        try:
            body = texts.t(_TEXT_KEYS[event], _FALLBACK_TEXTS[event]).format(**values)
        except (AttributeError, IndexError, KeyError, ValueError):
            logger.warning('LTE notice locale text is broken, using the built-in one', text_key=_TEXT_KEYS[event])
            body = _FALLBACK_TEXTS[event].format(**values)
    if topup:
        hint = texts.t('LTE_QUOTA_NOTICE_TOPUP_HINT', 'Докупить LTE можно кнопкой ниже.')
        body = f'{body}\n\n{hint}'
    return body


def _format_override(event: str, values: dict[str, str]) -> str | None:
    key = _OVERRIDE_SETTINGS[event]
    template = str(getattr(settings, key, '') or '').strip()
    if not template:
        return None
    try:
        return template.format(**values)
    except (AttributeError, IndexError, KeyError, ValueError):
        # Опечатка в фигурных скобках не должна стоить клиенту сообщения.
        logger.warning('LTE notice text from settings has a broken placeholder, using the locale text', setting=key)
        return None


def build_notice_keyboard(texts: Any, subscription_id: int, *, topup: bool) -> InlineKeyboardMarkup:
    """«📶 Докупить LTE» (если доступна) и «📱 Моя подписка»; в кабинете — его страницы.

    Как у ``build_subscription_extend_button``: в мультитарифе кнопки ведут
    к этой подписке (``st:{id}`` / ``sm:{id}``, в кабинете ``/subscriptions/{id}``),
    иначе — на общие ``buy_traffic`` / ``menu_subscription``.
    """
    from app.utils.miniapp_buttons import build_miniapp_or_callback_button

    multi = settings.is_multi_tariff_enabled()
    rows = []
    if topup:
        text = texts.t('LTE_QUOTA_NOTICE_TOPUP_BUTTON', '📶 Докупить LTE')
        if multi:
            button = build_miniapp_or_callback_button(
                text,
                callback_data=f'st:{subscription_id}',
                cabinet_path=f'/subscriptions/{subscription_id}',
                style_key='buy_traffic',
            )
        else:
            button = build_miniapp_or_callback_button(text, callback_data='buy_traffic')
        rows.append([button])
    text = texts.t('MY_SUBSCRIPTION_BUTTON', '📱 Моя подписка')
    if multi:
        button = build_miniapp_or_callback_button(
            text,
            callback_data=f'sm:{subscription_id}',
            cabinet_path=f'/subscriptions/{subscription_id}',
            style_key='menu_subscription',
        )
    else:
        button = build_miniapp_or_callback_button(text, callback_data='menu_subscription')
    rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topup_available(subscription: Any, tariff: Any, user: Any, *, topup_bytes: int = 0, now=None) -> bool:
    """Докупка LTE доступна этой подписке — условия ``purchase_lte_topup``, без денег.

    Пробной подписке и тарифу «Только Wi-Fi» докупки нет; выключенная докупка
    тарифа, пустой список пакетов и упор в ``max_topup_traffic_gb`` тоже её
    закрывают. Кнопка, ведущая в отказ, хуже её отсутствия.
    """
    from app.services.lte_quota_service import LtePolicyError, validate_tariff_for_sale

    now = now or datetime.now(UTC)
    end_date = getattr(subscription, 'end_date', None)
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    if (
        getattr(subscription, 'network_category', None) != 'wifi_lte'
        or getattr(subscription, 'is_trial', False)
        or getattr(subscription, 'status', None) != 'active'
        or end_date is None
        or end_date <= now
        or getattr(user, 'restriction_subscription', False)
    ):
        return False
    if (
        tariff is None
        or getattr(tariff, 'network_category', None) != 'wifi_lte'
        or not getattr(tariff, 'traffic_topup_enabled', False)
    ):
        return False
    try:
        validate_tariff_for_sale(tariff)
    except (LtePolicyError, ValueError):
        return False
    packages = [gb for gb, price in tariff.get_traffic_topup_packages().items() if gb > 0 and price > 0]
    if not packages:
        return False
    maximum = getattr(tariff, 'max_topup_traffic_gb', 0) or 0
    return not (maximum > 0 and topup_bytes + min(packages) * _GIB > maximum * _GIB)


async def _notify_admins_about_orphan(bot: Any, candidate) -> str:
    """Права только в панели: клиенту написать некуда, сообщаем админам."""
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    title = 'LTE закончился' if candidate.event == 'exhausted' else 'LTE почти закончился'
    username = f'<code>{html.escape(candidate.panel_username)}</code>' if candidate.panel_username else '—'
    text = (
        f'📶 <b>{title}: пользователь только в панели</b>\n\n'
        f'🆔 <b>ID в панели:</b> <code>{candidate.panel_id}</code>\n'
        f'👤 <b>Username в панели:</b> {username}\n'
        f'📱 <b>Осталось LTE:</b> {format_remaining(candidate.remaining_bytes)} ГиБ\n\n'
        '<i>В боте этого клиента нет — ему ничего не отправлено.</i>'
    )
    sent = await AdminNotificationService(bot).send_admin_notification(
        text, category=NotificationCategory.INFRASTRUCTURE
    )
    return 'sent' if sent else 'not_delivered'


async def _alert_overflow(bot: Any, count: int, limit: int) -> None:
    """Предохранитель сработал: сказать админам, но не чаще раза в час."""
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    now = time.monotonic()
    last = _overflow_alert['sent_at']
    if last is not None and now - last < _OVERFLOW_ALERT_INTERVAL_SECONDS:
        return
    _overflow_alert['sent_at'] = now
    text = (
        '📶 <b>LTE-уведомления: отправка пропущена</b>\n\n'
        f'За один проход учёта набралось {count} кандидатов — больше лимита {limit} '
        '(<code>DONBATON_LTE_NOTICE_MAX_PER_CYCLE</code>). Клиентам ничего не отправлено. '
        'Отметки поставлены: повторно этим клиентам напишем только после того, как LTE у них пополнится.\n\n'
        '<i>Следующее такое сообщение — не раньше чем через час.</i>'
    )
    try:
        await AdminNotificationService(bot).send_admin_notification(text, category=NotificationCategory.INFRASTRUCTURE)
    except Exception:
        logger.warning('LTE notice overflow alert could not be sent', exc_info=True)
