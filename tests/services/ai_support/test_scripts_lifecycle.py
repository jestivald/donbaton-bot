"""Парсер Markdown, сухой прогон, сравнение моделей и управление фоновыми задачами."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services.ai_support import api, knowledge, provider, retention
from scripts import ai_support_eval, ai_support_seed_kb


def test_markdown_to_hidden_faq():
    pages = ai_support_seed_kb.parse_markdown("""# Подключение {slug=ai-connect}
## Как начать?
Откройте [кабинет](https://lk.donbaton.tech).

- Первый шаг
- Второй шаг

## Что дальше?
Напишите оператору.
""")
    assert len(pages) == 1 and pages[0]['slug'] == 'ai-connect'
    assert len(pages[0]['items']) == 2
    html = pages[0]['items'][0]['a']
    assert '<ul><li>Первый шаг</li>' in html
    assert '<a href="https://lk.donbaton.tech">кабинет</a>' in html
    assert knowledge.html_text(html).startswith('Откройте кабинет (https://lk.donbaton.tech)')
    with pytest.raises(ValueError, match='slug'):
        ai_support_seed_kb.parse_markdown('# Неверный заголовок\n## Вопрос\nОтвет')


async def test_lifecycle_idempotent_and_closes(transport, monkeypatch):
    client = transport(lambda request: None)
    cleanup = AsyncMock()
    monkeypatch.setattr(retention, 'cleanup', cleanup)
    await api.startup()
    first = retention._task
    await asyncio.sleep(0)
    await api.startup()
    assert retention._task is first and cleanup.await_count == 1
    await api.shutdown()
    assert client.is_closed and retention._task is None and provider._client is None
    await api.shutdown()


async def test_eval_restores_model_and_disables_account_tools(monkeypatch, capsys):
    answer = AsyncMock(return_value=api.AiAnswer(api.AiOutcome.ANSWERED, reply_text='Откройте приложение'))
    monkeypatch.setattr(ai_support_eval.api, 'answer', answer)
    monkeypatch.setattr(ai_support_eval.api, 'startup', AsyncMock())
    monkeypatch.setattr(ai_support_eval.api, 'shutdown', AsyncMock())
    monkeypatch.setattr(ai_support_eval.bot_configuration_service, 'initialize', AsyncMock())
    original = settings.AI_SUPPORT_MODEL
    result = await ai_support_eval.evaluate(
        [{'id': 'one', 'question': 'Как?', 'expect': 'answer', 'must': ['приложение']}], model='custom'
    )
    assert result and original == settings.AI_SUPPORT_MODEL
    assert answer.call_args.args[2].allow_account_tools is False
    assert answer.call_args.args[2].channel == api.AiChannel.ADMIN_TEST
    assert '1/1' in capsys.readouterr().out


def test_seed_markdown_keeps_numbered_steps():
    import scripts.ai_support_seed_kb as seed

    html_value = seed.to_html(['Текст', '1. Раз', '2. Два', '- пункт'])
    assert html_value == '<p>Текст</p><ol><li>Раз</li><li>Два</li></ol><ul><li>пункт</li></ul>'
