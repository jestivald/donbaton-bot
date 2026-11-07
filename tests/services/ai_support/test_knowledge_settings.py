"""FAQ/HTML, порядок кэша и редактирование настроек с маской ключа."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings, settings
from app.services.ai_support import api, knowledge, prompts, settings_view
from app.services.system_settings_service import BotConfigurationService


def page(slug, *, active=False, kind='faq', order=0, id=1, content=None, title=None):
    return SimpleNamespace(
        slug=slug,
        is_active=active,
        page_type=kind,
        sort_order=order,
        id=id,
        content=content or {'ru': '[{"q":"Вопрос","a":"<p>Ответ &amp; текст</p>"}]'},
        title=title or {'ru': 'Заголовок'},
    )


def test_kb_selection_order_language_and_html():
    pages = [
        page('faq', active=True, order=-1),
        page('hidden'),
        page('ai-b', order=2, id=2),
        page('ai-a', order=2, id=1),
        page('not-faq', active=True, kind='page'),
        page(
            'ai-page',
            kind='page',
            order=3,
            content={'en': '<p>Article</p><ul><li>First</li><li><a href="https://donbaton.tech">Link</a></li></ul>'},
        ),
    ]
    loaded = knowledge.build(pages, 'de')
    assert loaded.status.slugs == ['ai-a', 'ai-b', 'ai-page', 'faq']
    assert loaded.status.pages == 4 and loaded.status.items == 4
    assert 'Ответ & текст' in loaded.text and '- First' in loaded.text
    assert 'Link (https://donbaton.tech)' in loaded.text
    assert knowledge.build(list(reversed(pages)), 'de').status.version == loaded.status.version
    assert knowledge.localized({'en': 'English', 'ru': 'Русский'}, 'en') == 'English'
    assert knowledge.localized({'en': 'English'}, 'fr') == 'English'
    assert knowledge.html_text('<script>secret()</script><p>Safe</p>') == 'Safe'


async def test_cache_reload_and_db_fallback(monkeypatch):
    execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [page('ai-first')])))

    class DB:
        async def __aenter__(self):
            return SimpleNamespace(execute=execute)

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(knowledge, 'AsyncSessionLocal', DB)
    first = await knowledge.load()
    second = await knowledge.load()
    assert first is second and execute.await_count == 1
    knowledge.invalidate_kb_cache()
    execute.side_effect = RuntimeError('database unavailable')
    assert (await knowledge.load()).text == first.text
    knowledge._cache.clear()
    assert (await knowledge.load()).status.loaded_at is None


def test_settings_category_masks_and_textarea(monkeypatch):
    assert Settings.model_fields['AI_SUPPORT_ENABLED'].default is False
    keys = [key for key in Settings.model_fields if key.startswith('AI_SUPPORT_')]
    for key in keys:
        assert BotConfigurationService._resolve_category_key(key) == 'AI_SUPPORT'
        assert key in BotConfigurationService.SETTING_HINTS
    assert BotConfigurationService.CATEGORY_TITLES['AI_SUPPORT'] == '🤖 ИИ-помощник'
    assert [c.value for c in BotConfigurationService.CHOICES['AI_SUPPORT_TICKET_MODE']] == ['off', 'draft', 'auto']
    assert (
        BotConfigurationService.mask_secret_value('AI_SUPPORT_API_KEY', 'secret') == BotConfigurationService.SECRET_MASK
    )
    assert (
        BotConfigurationService.format_value_human('AI_SUPPORT_API_KEY', 'secret')
        == BotConfigurationService.SECRET_MASK
    )
    assert BotConfigurationService.mask_secret_value('AI_SUPPORT_API_KEY', None) is None
    assert all('_TEMPLATE' in key for key in keys if 'INSTRUCTIONS' in key or 'STATUS_NOTE' in key)
    assert 'test-only-key' not in json.dumps(api.describe_settings())
    monkeypatch.setattr(settings, 'AI_SUPPORT_API_KEY', None)
    assert not api.is_enabled() and not api.bot_chat_enabled() and api.ticket_mode() == 'off'
    assert api.describe_settings()['api_key_configured'] is False


def test_normalization_and_prompt_order(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_TICKET_MODE', 'bad')
    monkeypatch.setattr(settings, 'AI_SUPPORT_HISTORY_TURNS', -1)
    assert api.ticket_mode() == 'off' and api.history_turns() == 6
    monkeypatch.setattr(settings, 'AI_SUPPORT_HISTORY_TURNS', 0)
    config = settings_view.snapshot()
    ctx = api.AiRequestContext(api.AiChannel.TICKET, 99999999, ticket_title='Email a@b.com')
    kb = knowledge.build([page('ai-test')], 'ru')
    messages = prompts.messages('вопрос +7 999 123 45 67', [api.ChatTurn('user', 'old')], ctx, kb, config)
    # Контекст канала — в системном сообщении, тема тикета — в вопросе; история длиной 0 не передаётся.
    assert [m['role'] for m in messages] == ['system', 'system', 'system', 'user']
    assert messages[1]['content'].startswith('База знаний:')
    assert 'Текущая дата МСК:' in messages[2]['content'] and 'Канал: обращение' in messages[2]['content']
    assert '99999999' not in json.dumps(messages)
    assert 'a@b.com' not in json.dumps(messages)
    assert messages[-1]['content'] == 'Тема обращения: Email [почта]\n\nвопрос [телефон]'


def test_prompt_merges_consecutive_roles_and_keeps_owner_contacts(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_HISTORY_TURNS', 6)
    monkeypatch.setattr(
        settings, 'AI_SUPPORT_OWNER_INSTRUCTIONS_TEMPLATE', 'Контакт: @batonsupport, support@donbaton.tech'
    )
    config = settings_view.snapshot()
    ctx = api.AiRequestContext(api.AiChannel.BOT_CHAT, 1)
    kb = knowledge.build([page('ai-test')], 'ru')
    history = [
        api.ChatTurn('user', 'первое'),
        api.ChatTurn('user', 'второе'),
        api.ChatTurn('assistant', 'ответ'),
    ]
    messages = prompts.messages('ещё вопрос', history, ctx, kb, config)
    roles = [m['role'] for m in messages if m['role'] != 'system']
    assert roles == ['user', 'assistant', 'user']
    assert messages[3]['content'] == 'первое\n\nвторое'
    # Инструкции владельца — доверенный текст: публичные контакты не обезличиваются.
    assert '@batonsupport' in messages[2]['content'] and 'support@donbaton.tech' in messages[2]['content']


def test_html_text_numbers_ordered_lists():
    text = knowledge.html_text('<p>Шаги:</p><ol><li>Раз</li><li>Два</li></ol><ul><li>пункт</li></ul>')
    assert '1. Раз' in text and '2. Два' in text and '- пункт' in text


def test_long_question_keeps_latest_clarification():
    config = settings_view.snapshot()
    ctx = api.AiRequestContext(api.AiChannel.TICKET, 1)
    kb = knowledge.build([page('ai-test')], 'ru')
    question = 'Длинное описание. ' * 200 + 'Уточняю: проблема на Android, на iPhone работает'
    messages = prompts.messages(question, [], ctx, kb, config)
    assert messages[-1]['content'].endswith('Уточняю: проблема на Android, на iPhone работает')
    assert len(messages[-1]['content']) < 2100


def test_assistant_history_without_ai_prefix_and_footer(monkeypatch):
    monkeypatch.setattr(settings, 'AI_SUPPORT_HISTORY_TURNS', 6)
    config = settings_view.snapshot()
    ctx = api.AiRequestContext(api.AiChannel.TICKET, 1)
    kb = knowledge.build([page('ai-test')], 'ru')
    footer = settings_view.reply_footer_text()
    history = [
        api.ChatTurn('user', 'привет'),
        api.ChatTurn('assistant', f'{api.AI_REPLY_PREFIX}\n\nЗдравствуйте!\n\n{footer}'),
    ]
    messages = prompts.messages('ещё вопрос', history, ctx, kb, config)
    assistant = next(m for m in messages if m['role'] == 'assistant')['content']
    assert assistant == 'Здравствуйте!'
