"""Обезличивание каждого вида данных и запрет утечек в ответе."""

import pytest

from app.services.ai_support.output_guard import guard
from app.services.ai_support.redaction import redact


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
        'https://github.com/example/app',
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
    assert guard(url) is None


def test_url_paths_and_private_telegram_links_are_redacted():
    text = redact(
        'сброс https://example.org/reset/abcdefghijklmnop0123456789 и t.me/private_user, бот t.me/donbatonvpn_bot'
    )
    assert 'abcdefghijklmnop0123456789' not in text
    assert 'private_user' not in text
    assert 't.me/donbatonvpn_bot' in text


def test_guard_rejects_percent_encoded_internal_host():
    assert guard('Откройте https://admin%2edonbaton.tech/ в браузере') is None
    assert guard('Откройте https://admin.donbaton.tech/') is None
