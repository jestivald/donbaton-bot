"""Обезличивание каждого вида данных и запрет утечек в ответе."""

import re

import pytest

from app.services.ai_support.output_guard import guard
from app.services.ai_support.redaction import KEY_RE, redact


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('a.person+test@example.org', '[почта]'),
        ('+7 (999) 123-45-67', '[телефон]'),
        ('8 999 123 45 67', '[телефон]'),
        ('79991234567', '[телефон]'),
        ('https://sub.donbaton.tech/secret', '[ссылка подписки]'),
        ('abc.donbaton.xyz/private', '[ссылка подписки]'),
        ('https://lk.donbaton.tech/auth?token=foo', '[ссылка подписки]'),
        ('lk.donbaton.tech/abcdefghijklmnop0123456789', '[ссылка подписки]'),
        ('965ad117-a166-40db-9a3d-61395471daef', '[идентификатор]'),
        ('HWID: my-private-device', '[идентификатор]'),
        ('4111 1111 1111 1111', '[номер карты]'),
        ('192.168.1.1', '[IP]'),
        ('2001:db8::1234', '[IP]'),
        ('::1', '[IP]'),
        ('@myusername', '[ник]'),
        ('abcdefghijklmnop0123456789', '[токен]'),
        ('Клиент 123456789', 'Клиент [идентификатор]'),
    ],
)
def test_redaction(raw, expected):
    assert redact(raw) == expected


@pytest.mark.parametrize(
    'scheme',
    ['vless', 'vmess', 'trojan', 'ss', 'hy2', 'hysteria2', 'tuic', 'happ', 'incy', 'v2raytun', 'streisand', 'sing-box'],
)
def test_connection_schemes(scheme):
    assert redact(scheme + '://private-secret') == '[ключ подключения]'


@pytest.mark.parametrize(
    'text',
    [
        '1299 ₽',
        '10000000 рублей',
        '07.11.2026',
        '2026-11-07',
        '20261107',
        'Happ 4.11.0',
        'Xray core 4.7.4',
        'https://github.com/example/abcdefghijklmnopqrstuvwx',
        'https://apps.apple.com/app/id1234567890',
        'https://lk.donbaton.tech',
        'https://donbaton.tech',
        'https://play.google.com/store/apps/details?id=com.example.app',
    ],
)
def test_public_text_and_urls_unchanged(text):
    assert redact(text) == text


@pytest.mark.parametrize(
    'text',
    [
        'Адрес 10.0.0.1',
        'Адрес ::1',
        'https://a.donbaton.xyz',
        'https://subo.example.org',
        'admin.donbaton.tech',
        'Grafitmedia',
        'Synqlane',
        'Remnawave',
        'сквады',
        'балансировщик',
        'нода',
        'msk01',
        'lon1',
        'waw1',
        'it1',
        'ams4',
        'sel-spb2',
        'bs1',
        'vless://secret',
        'https://sub.donbaton.tech/secret',
        'https://lk.donbaton.tech/?token=secret',
        'Remna<b>wave</b>',
        'https://admin&#46;donbaton.tech',
    ],
)
def test_filtered(text):
    # По H1 запрещённую ссылку удаляем, служебный текст по-прежнему отклоняем.
    if (re.match(r'https?://|admin\.', text) and not KEY_RE.search(text)) or text == 'https://admin&#46;donbaton.tech':
        assert guard(text) == '(ссылка удалена)'
    else:
        assert guard(text) is None


@pytest.mark.parametrize(
    'url',
    [
        'https://t.me/donbatonvpn_bot',
        'https://t.me/batonsupport',
        'https://lk.donbaton.tech',
        'https://donbaton.tech',
        'https://testflight.apple.com/join/public',
        'https://apps.apple.com/app/id1234567890',
        'https://play.google.com/store/apps/details?id=app',
        'https://github.com/INCY-DEV/app',
    ],
)
def test_allowed_output_links(url):
    assert guard('Откройте ' + url) == 'Откройте ' + url


def test_plain_text_and_sentence_limit():
    assert guard('# Заголовок\n**Ответ** __текст__ <b>ещё</b>') == 'Заголовок\nОтвет текст ещё'
    text = guard('Готово. ' * 600)
    assert len(text) <= 3500
    assert text.endswith('.')


@pytest.mark.parametrize(
    'url', ['https://lk.donbaton.tech./private-token', 'https://sub.donbaton.tech./secret', 'node.donbaton.xyz']
)
def test_subscription_domain_with_trailing_dot_or_bare_host(url):
    assert redact(url) == '[ссылка подписки]'
    assert guard(url) == '(ссылка удалена)'


def test_url_paths_and_private_telegram_links_are_redacted():
    text = redact(
        'сброс https://example.org/reset/abcdefghijklmnop0123456789 и t.me/private_user, бот t.me/donbatonvpn_bot'
    )
    assert 'abcdefghijklmnop0123456789' not in text
    assert 'private_user' not in text
    assert 't.me/donbatonvpn_bot' in text


def test_guard_rejects_percent_encoded_internal_host():
    assert guard('Откройте https://admin%2edonbaton.tech/ в браузере') == 'Откройте (ссылка удалена) в браузере'
    assert guard('Откройте https://admin.donbaton.tech/') == 'Откройте (ссылка удалена)'


@pytest.mark.parametrize(
    'text',
    [
        'handoff_to_human',
        'get_account_overview',
        'get_devices',
        'ai_support_api_key',
        'База знаний: секрет',
        'Дополнительные инструкции владельца: секрет',
        'Текст клиента, тикета и история',
        'hаndoff_to_human'.replace('а', 'a'),
        'get_аccount_overview'.replace('а', 'a'),
        'get_\u200bdevices',
        'Дополнительные инструкции владельца',
        'Дополнительные\nинструкции владельца',
        '＆ｌｔ；script＆ｇｔ；bad＆ｌｔ；/script＆ｇｔ；',
        '&lt;script&gt;bad&lt;/script&gt;',
        '%253Ciframe%253Ebad%253C/iframe%253E',
        'Remna\u200bwave',
    ],
)
def test_h1_encoded_control_and_markup_filtered(text):
    assert guard(text) is None


@pytest.mark.parametrize(
    'url',
    [
        'https://login-check.example/verify',
        'https://t.me/attacker',
        'tg://resolve?domain=attacker',
        'https://t.me/donbatonvpn_bot/extra',
        'https://lk.donbaton.tech/admin',
        'https://lk.donbaton.tech/login?token=x',
        'https://donbaton.tech.evil.example',
        'https://user@donbaton.tech',
        'https://donbaton.tech:443',
        'https://github.com/example/app',
        'https://github.com/INCY-DEV/../evil',
        'javascript:alert(1)',
        'support@evil.example',
        'www.evil.su/path',
        'https://evil.中国/path',
        'evil.中国/path',
        'https://t.me/batonsupport?start=evil',
    ],
)
def test_h1_unapproved_links_removed_without_losing_answer(url):
    assert guard('Помощь: ' + url) == 'Помощь: (ссылка удалена)'


@pytest.mark.parametrize(
    'url',
    [
        'https://github.com/INCY-DEV/app',
        'https://github.com/Happ-proxy/app',
        'https://happ.su',
        'https://www.happ.su',
        'tg://resolve?domain=batonsupport',
        'support@donbaton.tech',
        'https://lk.donbaton.tech/support',
    ],
)
def test_h1_approved_links_preserved(url):
    assert guard('Помощь: ' + url) == 'Помощь: ' + url


def test_h1_answer_limit_is_2000_on_sentence_boundary():
    text = guard('Готово. ' * 600)
    assert len(text) <= 2000 and text.endswith('.')
    assert len(guard('а' * 2500)) <= 2000


@pytest.mark.parametrize(
    'label', ['пароль', 'password', 'pass', 'код', 'code', 'otp', 'pin', 'cvv', 'cvc', 'одноразовый код', 'одноразовый']
)
@pytest.mark.parametrize('separator', [': ', '=', ' '])
def test_h1_short_context_secrets(label, separator):
    assert redact(label + separator + 'Abc123!') == label + separator + '[секрет]'


def test_h1_encoded_email_and_otp_in_url():
    text = redact('https://example.org/a%2540example.com?otp=123456')
    assert '123456' not in text and 'example.com' not in text
