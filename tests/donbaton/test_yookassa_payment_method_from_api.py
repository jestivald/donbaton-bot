"""Привязка для автоплатежа ЮKassa берётся из подтверждения API, не из тела вебхука
(аудит безопасности 01.10.2026, §3.1).

Статус, сумму и metadata вебхук уже брал из API, а ``payment_method`` (id, saved,
card) — из неподписанного тела: подменённый id сохранил бы клиенту чужую привязку.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.payment.yookassa import YooKassaPaymentMixin
from app.services.payment_service import PaymentService


FORGED_BODY_METHOD = {
    'type': 'bank_card',
    'id': 'pm_FORGED',
    'saved': True,
    'card': {'first6': '411111', 'last4': '0000', 'card_type': 'Visa', 'expiry_month': '01', 'expiry_year': '2030'},
    'account_number': '410019999999999',
}
API_CARD = {'first6': '555555', 'last4': '4444', 'card_type': 'MasterCard', 'expiry_month': '12', 'expiry_year': '2031'}


def api_answer(**method) -> dict:
    answer = {'id': 'yk_1', 'status': 'succeeded', 'paid': True, 'amount_value': 299.0, 'amount_currency': 'RUB'}
    answer.update(method)
    return answer


def test_merge_takes_payment_method_only_from_api():
    merged = YooKassaPaymentMixin._merge_remote_yookassa_payload(
        {'id': 'yk_1', 'status': 'succeeded', 'payment_method': FORGED_BODY_METHOD},
        api_answer(
            payment_method_type='bank_card',
            payment_method_id='pm_API',
            payment_method_saved=True,
            payment_method_card=API_CARD,
            payment_method_title='Bank card *4444',
        ),
    )
    assert merged['payment_method'] == {
        'type': 'bank_card',
        'id': 'pm_API',
        'saved': True,
        'card': API_CARD,
        'title': 'Bank card *4444',
    }


def test_merge_without_api_method_keeps_no_binding():
    merged = YooKassaPaymentMixin._merge_remote_yookassa_payload(
        {'id': 'yk_1', 'payment_method': FORGED_BODY_METHOD},
        api_answer(payment_method_type=None, payment_method_id=None, payment_method_saved=False),
    )
    assert merged['payment_method'] == {'type': 'bank_card'}  # только справочный тип, привязки нет


@pytest.fixture
def saved_methods(monkeypatch):
    created = AsyncMock(return_value=SimpleNamespace(id=5))
    monkeypatch.setattr('app.database.crud.saved_payment_method.create_saved_payment_method', created)
    monkeypatch.setattr(
        'app.database.crud.saved_payment_method.get_payment_method_by_yookassa_id', AsyncMock(return_value=None)
    )
    return created


async def run_webhook(monkeypatch, remote: dict) -> dict:
    """Настоящий process_yookassa_webhook до передачи объекта в обработку успешного платежа."""
    service = PaymentService.__new__(PaymentService)
    service.bot = None
    service.yookassa_service = SimpleNamespace(get_payment_info=AsyncMock(return_value=remote))
    payment = SimpleNamespace(
        id=1, yookassa_payment_id='yk_1', user_id=21, status='pending', is_paid=False, payment_method_type=None
    )
    monkeypatch.setattr('app.services.payment_service.get_yookassa_payment_by_id', AsyncMock(return_value=payment))
    handed_over: dict = {}

    async def success(db, paid_payment, *, event_object=None):
        handed_over.update(event_object or {})
        handed_over['_payment'] = paid_payment
        return True

    monkeypatch.setattr(service, '_process_successful_yookassa_payment', success)
    body = {'object': {'id': 'yk_1', 'status': 'succeeded', 'paid': True, 'payment_method': FORGED_BODY_METHOD}}
    assert await service.process_yookassa_webhook(AsyncMock(), body) is True
    return handed_over


async def test_webhook_with_forged_method_saves_the_api_binding(monkeypatch, saved_methods):
    monkeypatch.setattr(settings, 'YOOKASSA_RECURRENT_ENABLED', True)
    event_object = await run_webhook(
        monkeypatch,
        api_answer(
            payment_method_type='bank_card',
            payment_method_id='pm_API',
            payment_method_saved=True,
            payment_method_card=API_CARD,
        ),
    )
    payment = event_object.pop('_payment')
    await PaymentService._save_payment_method_if_available(SimpleNamespace(), AsyncMock(), payment, event_object)
    saved_methods.assert_awaited_once()
    kwargs = saved_methods.await_args.kwargs
    assert kwargs['yookassa_payment_method_id'] == 'pm_API'
    assert (kwargs['card_first6'], kwargs['card_last4'], kwargs['card_type']) == ('555555', '4444', 'MasterCard')
    assert kwargs['user_id'] == 21


async def test_webhook_without_api_method_saves_nothing(monkeypatch, saved_methods):
    event_object = await run_webhook(monkeypatch, api_answer(payment_method_id=None, payment_method_saved=False))
    payment = event_object.pop('_payment')
    await PaymentService._save_payment_method_if_available(SimpleNamespace(), AsyncMock(), payment, event_object)
    saved_methods.assert_not_awaited()


async def test_body_saved_flag_cannot_force_saving(monkeypatch, saved_methods):
    # Клиент не согласился сохранить карту (API: saved=False) — тело вебхука с saved=True не поможет.
    event_object = await run_webhook(
        monkeypatch,
        api_answer(
            payment_method_type='bank_card',
            payment_method_id='pm_API',
            payment_method_saved=False,
            payment_method_card=API_CARD,
        ),
    )
    payment = event_object.pop('_payment')
    await PaymentService._save_payment_method_if_available(SimpleNamespace(), AsyncMock(), payment, event_object)
    saved_methods.assert_not_awaited()


async def test_wallet_title_comes_from_api_account(monkeypatch, saved_methods):
    event_object = await run_webhook(
        monkeypatch,
        api_answer(
            payment_method_type='yoo_money',
            payment_method_id='pm_WALLET',
            payment_method_saved=True,
            payment_method_account_number='410011234567890',
        ),
    )
    payment = event_object.pop('_payment')
    await PaymentService._save_payment_method_if_available(SimpleNamespace(), AsyncMock(), payment, event_object)
    kwargs = saved_methods.await_args.kwargs
    assert kwargs['yookassa_payment_method_id'] == 'pm_WALLET'
    assert kwargs['title'] == '*7890'  # не '*9999' из подделанного тела


async def test_get_payment_info_returns_wallet_requisites(monkeypatch):
    from app.services import yookassa_service as module

    method = SimpleNamespace(
        type='yoo_money',
        id='pm_WALLET',
        saved=True,
        title='YooMoney wallet 410011234567890',
        account_number='410011234567890',
    )
    sdk_payment = SimpleNamespace(
        id='yk_1',
        status='succeeded',
        paid=True,
        amount=SimpleNamespace(value='299.00', currency='RUB'),
        metadata={},
        description='',
        refundable=True,
        created_at='2026-10-01T10:00:00Z',
        captured_at=None,
        payment_method=method,
        test=False,
    )
    # conftest подменяет SDK болванкой без find_one — добавляем свой.
    monkeypatch.setattr(module.YooKassaPayment, 'find_one', lambda _id: sdk_payment, raising=False)
    service = module.YooKassaService.__new__(module.YooKassaService)
    service.configured = True
    info = await service.get_payment_info('yk_1')
    assert info['payment_method_id'] == 'pm_WALLET' and info['payment_method_saved'] is True
    assert info['payment_method_account_number'] == '410011234567890'
    assert info['payment_method_title'] == 'YooMoney wallet 410011234567890'
    assert info['payment_method_phone'] is None
