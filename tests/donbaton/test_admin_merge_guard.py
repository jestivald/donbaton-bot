"""Аккаунт администратора не поглощается слиянием (аудит безопасности 01.10.2026, §2.2-B).

Слияние переносит к инициатору Telegram ID, почту и способы входа поглощаемого
аккаунта. Утёкшая initData админа (или его виджет/почта) + любой свой аккаунт
кабинета давали бы через /link/telegram → /merge Telegram ID из ADMIN_IDS — то есть
админку. Поэтому токен слияния не выдаётся ни на одном пути (соцсеть, Telegram,
почта), а execute_merge перепроверяет — на случай токена, выданного до запрета.
Обратное — админ поглощает обычный аккаунт — не ограничено.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes.account_linking import (
    LinkTelegramRequest,
    MergeRequest,
    _exchange_and_link_oauth,
    execute_merge_endpoint,
    link_telegram,
)
from app.cabinet.routes.auth import register_email, verify_email_merge
from app.cabinet.schemas.auth import EmailMergeVerifyRequest, EmailRegisterRequest
from app.config import settings
from app.database.models import AdminRole, User, UserRole, UserStatus
from app.services import donbaton_admin_guards
from app.services.account_merge_service import MergeForbiddenError, execute_merge
from app.services.donbaton_admin_guards import ADMIN_MERGE_REFUSAL, admin_merge_refusal, has_cabinet_role


ADMIN_TG = 777000
ADMIN_MAIL = 'boss@example.com'


@pytest.fixture(autouse=True)
def admins(monkeypatch):
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(ADMIN_TG))
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', ADMIN_MAIL)
    # По умолчанию ролей нет; тесты RBAC подменяют ответ сами (или идут в настоящий PostgreSQL).
    monkeypatch.setattr(donbaton_admin_guards, 'has_cabinet_role', AsyncMock(return_value=False))


def person(user_id: int, *, telegram_id=None, email=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        email=email,
        email_verified=bool(email),
        status=UserStatus.ACTIVE.value,
        google_id=None,
        language='en',
        first_name='X',
    )


# --------------------------------------------------------------------------- правило


@pytest.mark.parametrize(
    'secondary',
    [person(2, telegram_id=ADMIN_TG), person(2, email=ADMIN_MAIL), person(2, email=ADMIN_MAIL.upper())],
    ids=['admin_ids', 'admin_emails', 'admin_emails_case'],
)
async def test_env_admin_is_refused(secondary):
    assert await admin_merge_refusal(None, secondary) == ADMIN_MERGE_REFUSAL


async def test_rbac_role_holder_is_refused(monkeypatch):
    monkeypatch.setattr(donbaton_admin_guards, 'has_cabinet_role', AsyncMock(return_value=True))
    assert await admin_merge_refusal(None, person(2, telegram_id=5)) == ADMIN_MERGE_REFUSAL


async def test_ordinary_account_is_not_refused():
    assert await admin_merge_refusal(None, person(2, telegram_id=5, email='client@example.com')) is None


async def test_has_cabinet_role_counts_any_active_assignment():
    # has_cabinet_role импортирована до подмены атрибута модуля — это настоящая функция.
    roles = AsyncMock(return_value=[SimpleNamespace(role=SimpleNamespace(level=30, name='Marketer'))])
    with patch('app.database.crud.rbac.UserRoleCRUD.get_user_roles', roles):
        assert await has_cabinet_role(None, 2) is True
    with patch('app.database.crud.rbac.UserRoleCRUD.get_user_roles', AsyncMock(return_value=[])):
        assert await has_cabinet_role(None, 2) is False


@pytest.mark.postgres
async def test_rbac_role_on_real_postgres(postgres_database, monkeypatch):
    from tests.fixtures.postgres_db import postgres_session

    monkeypatch.setattr(donbaton_admin_guards, 'has_cabinet_role', has_cabinet_role)
    tables = [UserRole.__table__, AdminRole.__table__, User.__table__]
    async with postgres_session(postgres_database, tables) as db:
        db.add_all([User(id=1, telegram_id=11, first_name='A'), User(id=2, telegram_id=22, first_name='B')])
        await db.flush()
        role = AdminRole(name='designer-test', level=30, permissions=['broadcasts:*'])
        db.add(role)
        await db.flush()
        db.add(UserRole(user_id=2, role_id=role.id, is_active=True))
        await db.commit()
        holder = await db.get(User, 2)
        client = await db.get(User, 1)
        assert await admin_merge_refusal(db, holder) == ADMIN_MERGE_REFUSAL
        assert await admin_merge_refusal(db, client) is None
        await db.execute(UserRole.__table__.update().values(is_active=False))
        await db.commit()
        assert await admin_merge_refusal(db, holder) is None  # отозванная роль — не админ


# --------------------------------------------------------------------------- соцсеть (OAuth)


async def oauth_link(user, existing_owner):
    prov = MagicMock()
    prov.exchange_code = AsyncMock(return_value={'access_token': 'x'})
    prov.get_user_info = AsyncMock(return_value=SimpleNamespace(provider_id='G2', email=None, email_verified=False))
    mint = AsyncMock(return_value='m' * 40)
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.account_linking.get_provider', MagicMock(return_value=prov)))
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_user_by_oauth_provider',
                AsyncMock(return_value=existing_owner),
            )
        )
        s.enter_context(patch('app.cabinet.routes.account_linking.create_merge_token', mint))
        call = _exchange_and_link_oauth(
            db=AsyncMock(),
            user=user,
            provider='google',
            code='code',
            state='state',
            state_data={},
            device_id=None,
            log_context='test',
        )
        try:
            return await call, mint
        except HTTPException as error:
            return error, mint


async def test_oauth_conflict_with_admin_gets_403_and_no_token():
    error, mint = await oauth_link(person(1), person(2, telegram_id=ADMIN_TG))
    assert isinstance(error, HTTPException) and error.status_code == status.HTTP_403_FORBIDDEN
    assert error.detail == ADMIN_MERGE_REFUSAL
    mint.assert_not_awaited()


async def test_oauth_conflict_with_role_holder_gets_403(monkeypatch):
    monkeypatch.setattr(donbaton_admin_guards, 'has_cabinet_role', AsyncMock(return_value=True))
    error, mint = await oauth_link(person(1), person(2, telegram_id=5))
    assert isinstance(error, HTTPException) and error.status_code == status.HTTP_403_FORBIDDEN
    mint.assert_not_awaited()


async def test_admin_absorbing_ordinary_account_still_gets_token():
    result, mint = await oauth_link(person(1, telegram_id=ADMIN_TG), person(2, telegram_id=5))
    assert result.merge_required is True and result.merge_token == 'm' * 40
    mint.assert_awaited_once()


# --------------------------------------------------------------------------- Telegram


async def test_telegram_link_to_admin_gets_403_and_no_token():
    mint = AsyncMock(return_value='t' * 40)
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.account_linking.get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.RateLimitCache.is_ip_rate_limited',
                AsyncMock(return_value=False),
            )
        )
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.validate_telegram_init_data',
                MagicMock(return_value={'id': ADMIN_TG, 'username': 'boss'}),
            )
        )
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.get_user_by_telegram_id',
                AsyncMock(return_value=person(2, telegram_id=ADMIN_TG)),
            )
        )
        s.enter_context(patch('app.cabinet.routes.account_linking.create_merge_token', mint))
        with pytest.raises(HTTPException) as failure:
            await link_telegram(
                request=LinkTelegramRequest(init_data='query_id=1&user=%7B%7D&hash=x'),
                raw_request=MagicMock(),
                user=person(1),
                db=AsyncMock(),
            )
    assert failure.value.status_code == status.HTTP_403_FORBIDDEN
    mint.assert_not_awaited()


# --------------------------------------------------------------------------- почта


def auth_patch(name, value):
    return patch(f'app.cabinet.routes.auth.{name}', value)


@pytest.fixture
def email_auth_enabled(monkeypatch):
    from app.cabinet.auth import email_auth_gate

    monkeypatch.setattr(email_auth_gate, 'get_setting_value', AsyncMock(return_value=None))
    monkeypatch.setattr(email_auth_gate.settings, 'CABINET_EMAIL_AUTH_ENABLED', True)


async def test_email_conflict_with_admin_sends_no_code(email_auth_enabled):
    store = AsyncMock()
    send = MagicMock()
    result = MagicMock()
    # Почту из ADMIN_EMAILS апстрим отсекает раньше своим 403; здесь админ — по Telegram ID,
    # а его почта обычная: именно этот случай закрывает сторож слияния.
    result.scalar_one_or_none.return_value = person(2, telegram_id=ADMIN_TG, email='ops@example.com')
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    with ExitStack() as s:
        s.enter_context(auth_patch('get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(auth_patch('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        s.enter_context(auth_patch('disposable_email_service.is_disposable', MagicMock(return_value=False)))
        s.enter_context(auth_patch('email_service.is_configured', MagicMock(return_value=True)))
        s.enter_context(auth_patch('email_service.send_email_change_code', send))
        s.enter_context(auth_patch('get_rendered_override', AsyncMock(return_value=None)))
        s.enter_context(auth_patch('store_email_merge_otp', store))
        with pytest.raises(HTTPException) as failure:
            await register_email(
                request=EmailRegisterRequest(email='ops@example.com', password='whatever-pw'),
                raw_request=MagicMock(),
                user=person(1),
                db=db,
            )
    assert failure.value.status_code == status.HTTP_403_FORBIDDEN
    assert failure.value.detail == ADMIN_MERGE_REFUSAL
    store.assert_not_awaited()
    send.assert_not_called()


async def test_email_code_issued_before_the_rule_mints_no_token(email_auth_enabled):
    mint = AsyncMock(return_value='tok')
    pending = {'secondary_user_id': 2, 'email': ADMIN_MAIL, 'code': '654321'}
    with ExitStack() as s:
        s.enter_context(auth_patch('get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(auth_patch('RateLimitCache.is_ip_rate_limited', AsyncMock(return_value=False)))
        s.enter_context(auth_patch('get_email_merge_otp', AsyncMock(return_value=pending)))
        s.enter_context(auth_patch('clear_email_merge_otp', AsyncMock()))
        s.enter_context(auth_patch('get_user_by_id', AsyncMock(return_value=person(2, email=ADMIN_MAIL))))
        s.enter_context(auth_patch('create_merge_token', mint))
        with pytest.raises(HTTPException) as failure:
            await verify_email_merge(
                request=EmailMergeVerifyRequest(code='654321'),
                raw_request=MagicMock(),
                user=person(1),
                db=AsyncMock(),
            )
    assert failure.value.status_code == status.HTTP_403_FORBIDDEN
    mint.assert_not_awaited()


# --------------------------------------------------------------------------- execute_merge


async def test_execute_merge_refuses_admin_before_touching_anything(monkeypatch):
    users = {1: person(1), 2: person(2, telegram_id=ADMIN_TG)}
    monkeypatch.setattr(
        'app.services.account_merge_service.get_user_by_id', AsyncMock(side_effect=lambda _db, uid: users[uid])
    )
    grace = AsyncMock()
    monkeypatch.setattr('app.services.grace_access_runtime.ensure_no_open_grace_for_users', grace)
    db = AsyncMock()
    with pytest.raises(MergeForbiddenError):
        await execute_merge(db, primary_user_id=1, secondary_user_id=2, provider='telegram')
    grace.assert_not_awaited()
    db.flush.assert_not_awaited()
    assert users[2].telegram_id == ADMIN_TG and users[1].telegram_id is None


async def test_old_token_on_admin_is_refused_and_not_restored():
    consumed = {'primary_user_id': 1, 'secondary_user_id': 2, 'provider': 'telegram', 'provider_id': str(ADMIN_TG)}
    restore = AsyncMock()
    db = AsyncMock()
    with ExitStack() as s:
        s.enter_context(patch('app.cabinet.routes.account_linking.get_client_ip', MagicMock(return_value='1.2.3.4')))
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.RateLimitCache.is_ip_rate_limited',
                AsyncMock(return_value=False),
            )
        )
        s.enter_context(
            patch('app.cabinet.routes.account_linking.consume_merge_token', AsyncMock(return_value=consumed))
        )
        s.enter_context(patch('app.cabinet.routes.account_linking.restore_merge_token', restore))
        s.enter_context(
            patch(
                'app.cabinet.routes.account_linking.execute_merge',
                AsyncMock(side_effect=MergeForbiddenError(ADMIN_MERGE_REFUSAL)),
            )
        )
        with pytest.raises(HTTPException) as failure:
            await execute_merge_endpoint(
                request=MergeRequest(keep_subscription_from=1),
                raw_request=MagicMock(),
                merge_token='x' * 40,
                user=SimpleNamespace(id=1),
                db=db,
            )
    assert failure.value.status_code == status.HTTP_403_FORBIDDEN
    assert failure.value.detail == ADMIN_MERGE_REFUSAL
    restore.assert_not_awaited()
    db.rollback.assert_awaited_once()
