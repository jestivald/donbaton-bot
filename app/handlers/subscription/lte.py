"""LTE package selection and confirmation using the shared atomic purchase."""

from uuid import uuid4

from aiogram import F, types

from app.database.crud.subscription import get_subscription_by_id_for_user
from app.database.crud.tariff import get_tariff_by_id
from app.services.lte_quota_service import LtePolicyError, get_quota_status
from app.services.lte_topup_purchase_service import LtePurchaseError, purchase_lte_topup, quote_lte_package
from app.utils.formatting import format_price_kopeks


def format_lte_status(category, quota):
    if category == 'wifi' and not (quota and quota.get('has_carried_lte')):
        return '📶 Wi-Fi: безлимит'
    if category not in ('wifi', 'wifi_lte'):
        return ''
    if quota is None:
        return '📶 Wi-Fi: безлимит\n📱 LTE: данные пакета обновляются'
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.config import settings

    def date_text(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo(settings.TIMEZONE)).strftime('%d.%m.%Y %H:%M %Z')

    if quota.get('is_unlimited'):
        result = '📶 Wi-Fi: безлимит\n📱 LTE: безлимит'
    else:
        remaining = quota['remaining_bytes'] / 1024**3
        result = f'📶 Wi-Fi: безлимит\n📱 LTE: осталось {remaining:.2f} ГиБ'
        if quota.get('period_ends_at') and quota.get('period_mode') not in ('legacy_no_reset', 'trial'):
            result += f'\nОбновление пакета: {date_text(quota["period_ends_at"])}'
    if quota.get('legacy_compatibility_until'):
        result += f'\nПрежние оплаченные условия действуют до {date_text(quota["legacy_compatibility_until"])}'
    if quota.get('renewal_requires_expiry'):
        result += '\nНовый тариф можно выбрать после окончания подписки.'
    if quota.get('exhausted'):
        result += '\nПакет LTE закончился. Wi-Fi продолжает работать до окончания подписки.'
    elif quota.get('accounting_state') != 'ready':
        result += '\nДоступ к LTE появится после проверки счётчика.'
    return result


async def show_lte_packages(callback, db_user, db, subscription):
    tariff = await get_tariff_by_id(db, subscription.tariff_id)
    try:
        if tariff is None or subscription.actual_status != 'active' or subscription.is_trial:
            raise LtePurchaseError('Докупка доступна для действующей платной подписки')
        packages = [quote_lte_package(db_user, tariff, gb) for gb in sorted(tariff.get_traffic_topup_packages())]
    except (LtePurchaseError, LtePolicyError) as error:
        await callback.answer(str(error), show_alert=True)
        return
    token = uuid4().hex[:16]
    buttons = [
        [
            types.InlineKeyboardButton(
                text=f'{package["gb"]} ГиБ — {format_price_kopeks(package["price_kopeks"])}',
                callback_data=f'lte_preview:{subscription.id}:{package["gb"]}:{token}',
            )
        ]
        for package in packages
    ]
    buttons.append([types.InlineKeyboardButton(text='Назад', callback_data='menu_subscription')])
    text = format_lte_status(subscription.network_category, await get_quota_status(db, subscription))
    text += '\n\n<b>Докупить LTE</b>\nПакет действует 30 дней с покупки. Срок подписки не продлевается.'
    if not packages:
        text += '\nДоступных пакетов пока нет.'
    await callback.message.edit_text(
        text, parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


async def preview_lte_purchase(callback, db_user, db):
    try:
        _, sub_id, size, token = callback.data.split(':')
        subscription = await get_subscription_by_id_for_user(db, int(sub_id), db_user.id)
        if subscription is None or subscription.network_category != 'wifi_lte':
            raise LtePurchaseError('Подписка не найдена')
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        quote = quote_lte_package(db_user, tariff, int(size))
        text = (
            f'<b>LTE: {quote["gb"]} ГиБ</b>\n'
            f'К оплате: {format_price_kopeks(quote["price_kopeks"])}\n'
            'Срок пакета — 30 дней с покупки. Wi-Fi остаётся безлимитным.\n'
            'Пакет расходуется на обходных серверах, независимо от сети телефона.'
        )
        buttons = [
            [
                types.InlineKeyboardButton(
                    text='Оплатить с баланса',
                    callback_data=f'lte_buy:{sub_id}:{size}:{quote["price_kopeks"]}:{token}',
                )
            ],
            [types.InlineKeyboardButton(text='Назад', callback_data=f'st:{sub_id}')],
        ]
        await callback.message.edit_text(
            text, parse_mode='HTML', reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()
    except (ValueError, LtePolicyError, LtePurchaseError) as error:
        await callback.answer(str(error), show_alert=True)


async def confirm_lte_purchase(callback, db_user, db):
    try:
        _, sub_id, size, price, token = callback.data.split(':')
        subscription = await get_subscription_by_id_for_user(db, int(sub_id), db_user.id)
        if subscription is None:
            raise LtePurchaseError('Подписка не найдена')
        result = await purchase_lte_topup(
            db,
            db_user,
            subscription,
            gb=int(size),
            request_id=token,
            expected_price_kopeks=int(price),
        )
        text = f'✅ LTE-пакет {int(size)} ГиБ добавлен.\n\n' + format_lte_status('wifi_lte', result['lte_quota'])
        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(text='Моя подписка', callback_data='menu_subscription'),
                    ]
                ]
            ),
        )
        await callback.answer()
    except (ValueError, LtePolicyError, LtePurchaseError) as error:
        await callback.answer(str(error), show_alert=True)


def register_lte_handlers(dp):
    dp.callback_query.register(preview_lte_purchase, F.data.startswith('lte_preview:'))
    dp.callback_query.register(confirm_lte_purchase, F.data.startswith('lte_buy:'))
