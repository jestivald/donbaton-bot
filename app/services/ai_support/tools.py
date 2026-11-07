"""Инструменты поддержки: субъект всегда берётся из контекста запроса."""

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, Transaction, User
from app.services.ai_support.api import AiChannel, HandoffReason
from app.services.ai_support.output_guard import operator_summary
from app.services.ai_support.redaction import redact


MSK = ZoneInfo('Europe/Moscow')
UNAVAILABLE = {'error': 'недоступно'}


def date_text(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(MSK).strftime('%d.%m.%Y %H:%M МСК')


def schema(name, description, properties=None, required=None):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties or {},
                'required': required or [],
                'additionalProperties': False,
            },
        },
    }


def definitions(ctx, config):
    tools = [
        schema(
            'handoff_to_human',
            'Передать вопрос оператору, ничего не менять в аккаунте.',
            {
                'reason': {'type': 'string', 'enum': [reason.value for reason in HandoffReason]},
                'summary': {'type': 'string', 'maxLength': 500},
            },
            ['reason', 'summary'],
        )
    ]
    if settings.AI_SUPPORT_CAN_CLOSE_TICKETS and ctx.channel == AiChannel.TICKET and ctx.ticket_id and ctx.user_id:
        from app.services.ai_support import api

        if api.ticket_mode() == 'auto':
            tools.append(schema('close_ticket', 'Запросить закрытие текущего обращения по явной просьбе клиента.'))
    if config.share_account and ctx.allow_account_tools and ctx.user_id:
        tools.extend(
            [
                schema('get_account_overview', 'Прочитать подписки, баланс и последние операции текущего клиента.'),
                schema('get_devices', 'Прочитать устройства текущего клиента без идентификаторов.'),
            ]
        )
    return tools


def category(subscription):
    tariff = subscription.tariff
    value = subscription.network_category or (tariff.network_category if tariff else None)
    if value not in ('wifi', 'wifi_lte'):
        squads = set(subscription.connected_squads or (tariff.allowed_squads if tariff else []) or [])
        from app.config import settings

        wifi = set((settings.DONBATON_WIFI_SQUAD_UUIDS or '').split(',')) - {''}
        lte = set((settings.DONBATON_LTE_SQUAD_UUIDS or '').split(',')) - {''}
        if squads & lte:
            value = 'wifi_lte'
        elif squads & wifi:
            value = 'wifi'
    return {'wifi': 'Wi-Fi', 'wifi_lte': 'Wi-Fi + LTE'}.get(value, 'не определено')


async def subscriptions_for(db, user_id):
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .options(selectinload(Subscription.tariff))
        .order_by(Subscription.created_at.desc())
    )
    return result.scalars().all()


async def account_overview(user_id):
    from app.services.donbaton_payment_window import payment_window
    from app.services.lte_quota_service import get_quota_status

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            return UNAVAILABLE
        subscriptions = await subscriptions_for(db, user_id)
        entries = []
        now = datetime.now(UTC)
        for sub in subscriptions:
            end = sub.end_date.replace(tzinfo=UTC) if sub.end_date.tzinfo is None else sub.end_date
            quota = await get_quota_status(db, sub)
            status = {
                'active': 'активна',
                'expired': 'истекла',
                'trial': 'пробная',
                'disabled': 'отключена',
                'limited': 'ограничена',
                'pending': 'ожидает оплаты',
            }.get(sub.status, 'не определено')
            if sub.status in ('active', 'trial') and end <= now:
                status = 'истекла'
            elif sub.is_trial and sub.status in ('active', 'trial'):
                status = 'пробная'
            entries.append(
                {
                    'status': status,
                    'tariff': redact(sub.tariff.name) if sub.tariff else 'не определено',
                    'category': category(sub),
                    'ends_at': date_text(end),
                    'days_remaining': max(0, math.ceil((end - now).total_seconds() / 86400)),
                    'device_limit': sub.device_limit,
                    'autopay': bool(sub.autopay_enabled),
                    'payment_window_active': payment_window(sub, now=now, user=user) is not None,
                    'lte': None
                    if quota is None
                    else {
                        'remaining_gb': round(quota['remaining_bytes'] / 1024**3, 2),
                        'package_gb': round((quota['limit_bytes'] + quota.get('topup_bytes', 0)) / 1024**3, 2),
                        'unlimited': quota['is_unlimited'],
                        'renews_at': date_text(quota['period_ends_at']),
                        'accounting_state': quota['accounting_state'],
                    },
                }
            )
        transactions = (
            (
                await db.execute(
                    select(Transaction)
                    .where(Transaction.user_id == user_id)
                    .order_by(Transaction.created_at.desc(), Transaction.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        types = {
            'deposit': 'пополнение',
            'withdrawal': 'списание',
            'subscription_payment': 'покупка подписки',
            'refund': 'возврат',
            'failed_refund': 'неудачный возврат',
            'referral_reward': 'реферальное начисление',
            'poll_reward': 'награда за опрос',
            'gift_payment': 'покупка подарка',
        }
        return {
            'subscriptions': entries,
            'balance_rub': user.balance_kopeks / 100,
            'telegram_login': bool(user.telegram_id),
            'email_login': bool(user.email and user.email_verified),
            'transactions': [
                {
                    'date': date_text(tx.created_at),
                    'type': types.get(tx.type, 'операция'),
                    'amount_rub': tx.amount_kopeks / 100,
                    'method': redact(tx.payment_method or 'не указан'),
                    'completed': bool(tx.is_completed),
                }
                for tx in transactions
            ],
        }


async def devices(user_id):
    from app.services.remnawave_service import RemnaWaveService

    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None:
            return UNAVAILABLE
        subscriptions = await subscriptions_for(db, user_id)
        panel_ids = {sub.remnawave_id for sub in subscriptions if sub.remnawave_id}
        from app.config import settings

        if not panel_ids and not settings.is_multi_tariff_enabled() and user.remnawave_id:
            panel_ids.add(user.remnawave_id)
    result = []
    async with asyncio.timeout(5):
        async with RemnaWaveService().get_api_client() as api:
            for panel_id in sorted(panel_ids):
                response = await api.get_user_devices_all(panel_id)
                for device in response.get('devices', []):
                    ua = device.get('userAgent') or device.get('user_agent') or ''
                    app = re.search(
                        r'\b(Happ|Hiddify|v2rayNG|v2rayTun|Streisand|sing-box|Clash|Shadowrocket)[/\s]([\d.]+)',
                        ua,
                        re.IGNORECASE,
                    )
                    result.append(
                        {
                            'platform': redact(device.get('platform') or device.get('platformType') or 'неизвестно'),
                            'model': redact(device.get('deviceModel') or device.get('model') or 'неизвестно'),
                            'application': app[1] if app else None,
                            'version': app[2] if app else None,
                            'added_at': date_text(device.get('createdAt')),
                            'updated_at': date_text(device.get('updatedAt') or device.get('lastSeen')),
                        }
                    )
    return {'devices': result}


@dataclass
class ToolState:
    reason: HandoffReason | None = None
    summary: str | None = None
    calls: int = 0


async def execute(call, ctx, config, state: ToolState):
    state.calls += 1
    try:
        function = call['function']
        name = function['name']
        args = json.loads(function.get('arguments') or '{}')
        if not isinstance(args, dict):
            return UNAVAILABLE
        if name == 'handoff_to_human':
            if set(args) != {'reason', 'summary'} or not isinstance(args['summary'], str):
                return UNAVAILABLE
            state.reason = HandoffReason(args['reason'])
            state.summary = operator_summary(args['summary'], state.reason)
            return {'accepted': True, 'note': 'Сообщи клиенту, что вопрос передан оператору; ответ придёт сюда'}
        if name == 'close_ticket':
            from app.services.ticket_automation_service import request_ai_close

            if args or state.reason is not None:
                return UNAVAILABLE
            if await request_ai_close(ctx):
                return {'accepted': True, 'note': 'Закрытие запрошено; приложение подтвердит его клиенту само.'}
            return UNAVAILABLE
        if not (config.share_account and ctx.allow_account_tools and ctx.user_id) or args:
            return UNAVAILABLE
        if name == 'get_account_overview':
            return await account_overview(ctx.user_id)
        if name == 'get_devices':
            return await devices(ctx.user_id)
    except Exception:
        return UNAVAILABLE
    return UNAVAILABLE
