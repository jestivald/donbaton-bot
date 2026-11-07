"""Загрузка и ограничения изображений без обращений к Telegram."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings
from app.services.ai_support import api, images, settings_view
from app.services.system_settings_service import BotConfigurationService


PNG = b'\x89PNG\r\n\x1a\nprivate-screenshot'


@pytest.fixture(autouse=True)
def vision(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_ENABLED', True)


def bot_with(data=PNG, path='photos/file.png', size=None):
    async def download(path, destination, **kwargs):
        destination.write(data)

    return SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(file_path=path, file_size=size)),
        download_file=AsyncMock(side_effect=download),
    )


def test_vision_settings_defaults_and_category():
    for key, default in {
        'AI_SUPPORT_VISION_ENABLED': False,
        'AI_SUPPORT_VISION_MAX_IMAGES': 3,
        'AI_SUPPORT_VISION_MAX_MB': 5,
    }.items():
        assert Settings.model_fields[key].default == default
        assert BotConfigurationService._resolve_category_key(key) == 'AI_SUPPORT'
        assert BotConfigurationService.SETTING_HINTS[key]['description']


@pytest.mark.parametrize(
    ('enabled', 'provider', 'model', 'expected'),
    [
        (False, 'deepseek', 'deepseek-flash', False),
        (True, 'deepseek', 'deepseek-flash', True),
        (True, 'deepseek', 'DeepSeek-FLASH', True),
        (True, 'deepseek', 'deepseek-v4-pro', False),
        (True, 'openai_compatible', 'owner-model', True),
        (True, 'unknown', 'flash', False),
    ],
)
def test_vision_capability(monkeypatch, enabled, provider, model, expected):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_ENABLED', enabled)
    monkeypatch.setattr(settings, 'AI_SUPPORT_PROVIDER', provider)
    monkeypatch.setattr(settings, 'AI_SUPPORT_MODEL', model)
    assert api.vision_enabled() is expected
    assert settings_view.snapshot().vision is expected


async def test_fetch_keeps_bytes_in_memory_and_limits_count(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_IMAGES', 2)
    bot = bot_with()
    result = await images.fetch_telegram_images(bot, ['one', 'two', 'three'])
    assert result == [api.AiImage(PNG, 'image/png')] * 2
    assert [call.args[0] for call in bot.get_file.await_args_list] == ['one', 'two']
    assert bot.download_file.call_args.kwargs['timeout'] == 10
    assert 'private-screenshot' not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result[0].mime = 'image/gif'


@pytest.mark.parametrize(
    ('data', 'path', 'expected'),
    [
        (PNG, 'file.bin', 'image/png'),
        (b'\xff\xd8\xffcontent', 'file', 'image/jpeg'),
        (b'GIF89aimage', 'file', 'image/gif'),
        (b'GIF87aimage', 'file', 'image/gif'),
        (b'RIFF1234WEBPimage', 'file', 'image/webp'),
        (b'content', 'file.JPEG', 'image/jpeg'),
        (b'content', 'file.svg', None),
        (b'%PDF', 'file.pdf', None),
        (b'', 'file.png', None),
    ],
)
async def test_mime_by_signature_or_extension(data, path, expected):
    result = await images.fetch_telegram_images(bot_with(data, path), ['file'])
    assert [image.mime for image in result] == ([expected] if expected else [])


async def test_declared_oversize_skipped_before_download(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 1)
    bot = bot_with(size=1024 * 1024 + 1)
    assert await images.fetch_telegram_images(bot, ['large']) == []
    bot.download_file.assert_not_awaited()


async def test_real_size_limit_even_without_file_size(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 1)
    bot = bot_with(data=PNG + bytes(1024 * 1024))
    assert await images.fetch_telegram_images(bot, ['large']) == []
    bot.download_file.assert_awaited_once()


async def test_exact_size_is_allowed(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 1)
    data = PNG + bytes(1024 * 1024 - len(PNG))
    assert (await images.fetch_telegram_images(bot_with(data), ['fits']))[0].data == data


@pytest.mark.parametrize('stage', ['get_file', 'download_file'])
async def test_download_error_skips_and_continues(stage):
    bot = bot_with()
    original = getattr(bot, stage).side_effect
    if stage == 'get_file':
        bot.get_file.side_effect = [RuntimeError('secret URL'), bot.get_file.return_value]
    else:
        calls = 0

        async def download(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('secret URL')
            return await original(*args, **kwargs)

        bot.download_file.side_effect = download
    assert await images.fetch_telegram_images(bot, ['broken', 'good']) == [api.AiImage(PNG, 'image/png')]


@pytest.mark.parametrize('stage', ['get_file', 'download_file'])
async def test_timeout_includes_both_telegram_operations(monkeypatch, stage):
    monkeypatch.setattr(images, 'DOWNLOAD_TIMEOUT', 0.01)
    bot = bot_with()

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    setattr(bot, stage, AsyncMock(side_effect=slow))
    assert await asyncio.wait_for(images.fetch_telegram_images(bot, ['slow']), 0.2) == []


async def test_disabled_no_bot_or_zero_limits_do_not_download(monkeypatch):
    bot = bot_with()
    assert await images.fetch_telegram_images(None, ['file']) == []
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_ENABLED', False)
    assert await images.fetch_telegram_images(bot, ['file']) == []
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_ENABLED', True)
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_IMAGES', 0)
    assert await images.fetch_telegram_images(bot, ['file']) == []
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_IMAGES', 3)
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 0)
    assert await images.fetch_telegram_images(bot, ['file']) == []
    bot.get_file.assert_not_awaited()


def test_photo_uses_largest_size_that_fits(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 1)
    photos = [
        SimpleNamespace(file_id='small', file_size=100, width=10, height=10),
        SimpleNamespace(file_id='fits', file_size=1000, width=100, height=100),
        SimpleNamespace(file_id='large', file_size=2 * 1024 * 1024, width=1000, height=1000),
    ]
    assert images.telegram_image_refs(SimpleNamespace(photo=photos)) == ['fits']
    assert images.telegram_image_refs(SimpleNamespace(photo=photos[-1:])) == []


def test_image_document_selection():
    document = SimpleNamespace(file_id='doc', file_size=100, mime_type='image/png')
    assert images.telegram_image_refs(SimpleNamespace(document=document)) == ['doc']
    document.mime_type = 'application/pdf'
    assert images.telegram_image_refs(SimpleNamespace(document=document)) == []


def test_api_images_rechecked_for_size_mime_and_count():
    config = replace(settings_view.snapshot(), max_images=2, max_image_bytes=len(PNG))
    image = api.AiImage(PNG, 'image/png')
    assert images.usable_images([image, image, image], config) == [image, image]
    assert images.usable_images([api.AiImage(PNG + b'x', 'image/png'), api.AiImage(PNG, 'text/plain')], config) == []
    assert images.usable_images([api.AiImage(b'', 'image/png')], config) == []


def test_provider_hard_limit_32_mib(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_VISION_MAX_MB', 100)
    assert settings_view.snapshot().max_image_bytes == 32 * 1024 * 1024
