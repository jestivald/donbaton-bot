"""Приглашение переживает вход в кабинет через Telegram и вставленную ссылку.

Инцидент 16.09.2026 (ДонБатон). Партнёр 493 (`ref_uI7GP6KNW8`) раздал ссылку
кабинета `https://lk.donbaton.tech/login?ref=<code>`. Приглашённый открыл её,
нажал «Войти через Telegram» (виджет Telegram в РФ не грузится, кабинет всегда
падает в deep-link), попал в бота без аккаунта — и упёрся в тупик «сначала
зарегистрируйтесь в боте»: вход в кабинет не состоялся, приглашение исчезло,
потому что код жил только в localStorage браузера. Дальше он зарегистрировался
заново и заплатил 1299 ₽ — реферер не получил ничего.

Второй сценарий того же инцидента (07.09, приглашённый forza_69): на шаге
«введите реферальный код» пользователь вставил ССЫЛКУ целиком, она не прошла
`validate_promo_format` и была отвергнута как «неверный код».

Тесты закрывают обе дыры:
1. `extract_referral_code` достаёт код из любой формы ссылки;
2. токен deep-link'а переносит `?ref=` из кабинета в бота;
3. `/start webauth_<token>` от незнакомого пользователя больше не тупик:
   токен запоминается, код приглашения уходит в обычный конвейер регистрации.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.utils.referral_link import extract_referral_code


class TestExtractReferralCode:
    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            # Ровно то, что случилось на бою 07.09: вставлена кабинетная ссылка.
            ('https://lk.donbaton.tech/login?ref=ref_uA1IYZ623U', 'ref_uA1IYZ623U'),
            # Ботовая ссылка из того же экрана «Рефералы».
            ('https://t.me/donbatonvpn_bot?start=ref_uI7GP6KNW8', 'ref_uI7GP6KNW8'),
            # Без схемы — так их копируют из превью Telegram.
            ('t.me/donbatonvpn_bot?start=refZHOEWx9Z', 'refZHOEWx9Z'),
            # С текстом вокруг.
            ('вот ссылка https://lk.donbaton.tech/login?ref=refkwqSIlpZ', 'refkwqSIlpZ'),
            # Порядок параметров не важен.
            ('https://lk.donbaton.tech/login?campaign=x&ref=ABC123', 'ABC123'),
            # Голый код не трогаем вовсе.
            ('ref_uA1IYZ623U', 'ref_uA1IYZ623U'),
            ('  PROMO2026  ', 'PROMO2026'),
            # Пустой ввод не должен падать.
            ('', ''),
            (None, ''),
        ],
    )
    def test_extracts(self, raw, expected):
        assert extract_referral_code(raw) == expected

    def test_never_raises_on_garbage(self):
        for raw in ['http://[', '://', '?', '/', 'ref=', 'https://', 'a' * 500]:
            assert isinstance(extract_referral_code(raw), str)

    def test_caps_length(self):
        code = extract_referral_code('https://lk.example/login?ref=' + 'z' * 200)
        assert len(code) <= 50


class TestDeepLinkTokenCarriesReferral:
    @pytest.mark.asyncio
    async def test_token_stores_referral_code(self):
        """`?ref=` кабинета должен доехать до бота внутри токена."""
        from app.services import web_auth_service

        stored: dict = {}

        async def fake_set(key, value, expire=None):
            stored[key] = value
            return True

        with patch.object(web_auth_service.cache, 'set', side_effect=fake_set):
            token = await web_auth_service.create_web_auth_token(referral_code='ref_uI7GP6KNW8')

        assert token
        (payload,) = stored.values()
        assert payload['status'] == 'pending'
        assert payload['referral_code'] == 'ref_uI7GP6KNW8'

    @pytest.mark.asyncio
    async def test_token_without_referral_has_no_key(self):
        from app.services import web_auth_service

        stored: dict = {}

        async def fake_set(key, value, expire=None):
            stored[key] = value
            return True

        with patch.object(web_auth_service.cache, 'set', side_effect=fake_set):
            await web_auth_service.create_web_auth_token()

        (payload,) = stored.values()
        assert 'referral_code' not in payload

    @pytest.mark.asyncio
    async def test_referral_code_survives_linking(self):
        """Линковка токена не должна затирать код: он ещё нужен для аудита."""
        from app.services import web_auth_service

        data = {'status': 'pending', 'created_at': 'x', 'referral_code': 'ref_uABC'}
        saved: dict = {}

        async def fake_getdel(key):
            return data

        async def fake_set(key, value, expire=None):
            saved.update(value)
            return True

        with (
            patch.object(web_auth_service.cache, 'getdel', side_effect=fake_getdel),
            patch.object(web_auth_service.cache, 'set', side_effect=fake_set),
        ):
            linked = await web_auth_service.link_web_auth_token('tok', 111, 222)

        assert linked is True
        assert saved['status'] == 'linked'
        assert saved['user_id'] == 222
        assert saved['referral_code'] == 'ref_uABC'


class TestWebAuthDeadEnd:
    """`/start webauth_<token>` от незарегистрированного больше не тупик."""

    def test_handler_no_longer_refuses_unregistered_user(self):
        """Строка-тупик удалена, а токен и код уходят в регистрацию."""
        from pathlib import Path

        source = Path('app/handlers/start.py').read_text(encoding='utf-8')
        assert 'Сначала зарегистрируйтесь в боте' not in source
        assert 'pending_web_auth_token=web_auth_token' in source
        assert '_link_pending_web_auth_after_registration' in source

    @pytest.mark.asyncio
    async def test_link_helper_is_noop_without_token(self):
        from types import SimpleNamespace

        from app.handlers.start import _link_pending_web_auth_after_registration

        state = SimpleNamespace(get_data=AsyncMock(return_value={}), update_data=AsyncMock())
        answer = AsyncMock()
        user = SimpleNamespace(id=1, telegram_id=1, language='ru')

        await _link_pending_web_auth_after_registration(state, user, answer)

        answer.assert_not_awaited()
        state.update_data.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_helper_links_and_clears(self):
        from types import SimpleNamespace

        from app.handlers import start as start_module

        state = SimpleNamespace(
            get_data=AsyncMock(return_value={'pending_web_auth_token': 'tok-1234567890123456'}),
            update_data=AsyncMock(),
        )
        answer = AsyncMock()
        user = SimpleNamespace(id=7, telegram_id=77, language='ru')

        with patch.object(start_module, 'link_web_auth_token', AsyncMock(return_value=True)) as linker:
            await start_module._link_pending_web_auth_after_registration(state, user, answer)

        linker.assert_awaited_once_with('tok-1234567890123456', 77, 7)
        state.update_data.assert_awaited_once_with(pending_web_auth_token=None)
        answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_link_helper_swallows_errors(self):
        """Регистрация уже прошла — падать из-за входа в кабинет нельзя."""
        from types import SimpleNamespace

        from app.handlers import start as start_module

        state = SimpleNamespace(
            get_data=AsyncMock(return_value={'pending_web_auth_token': 'tok-1234567890123456'}),
            update_data=AsyncMock(),
        )
        user = SimpleNamespace(id=7, telegram_id=77, language='ru')

        with patch.object(start_module, 'link_web_auth_token', AsyncMock(side_effect=RuntimeError('redis down'))):
            await start_module._link_pending_web_auth_after_registration(state, user, AsyncMock())
