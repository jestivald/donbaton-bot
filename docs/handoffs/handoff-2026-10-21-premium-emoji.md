# Handoff — премиум-эмодзи во всех сообщениях и кнопках бота (перенос патча №9 ВнеПлены)

**Дата:** 2026-10-21
**Исполнитель:** Claude, сессия ДонБатона `donbaton-64`
**Репозиторий:** `/Users/admin/Documents/work/vpn/donbaton/forks/bedolaga-bot`
**Worktree:** `/Users/admin/Documents/work/_worktrees/donbaton-bot/premium-emoji-20261021`
**Ветка:** `codex/premium-emoji-20261021` от `9efae345` (= `codex/upstream-5.0.0`, боевой образ
`donbaton/bedolaga:u500-4943e3b6-20261001`).
**Основание:** владелец 21.10.2026 — «давай освежим колво кнопок, и эмодзи, можешь посмотреть как у внеплена»;
выбор: эмодзи «везде, как у ВнеПлены». Задача — `vpn/donbaton/tasks/2026-10-21-bot-menu-premium-emoji.md`,
пакет выкладки — `local-ops/_claude/tools/donbaton-premium-emoji-20261021/`.

## Что сделано

Патч №9 ВнеПлены (`d853d006`, образ `vneplena-bot:5.0.0-brands.4`) перенесён на линию 5.0.0 ДонБатона:

| Файл | Что |
|---|---|
| `app/middlewares/premium_emoji.py` | middleware сессии aiogram. Ниже docstring'а модуль совпадает с `d853d006` побайтно |
| `app/bot_factory.py` | `create_bot()` регистрирует `PremiumEmojiMiddleware` после `StaleCallbackAnswerMiddleware` |
| `app/config.py` | `PREMIUM_EMOJI_MAP: str = ''`, `PREMIUM_EMOJI_BUTTONS: bool = True` (после `MAIN_MENU_RICH_LOGO_URL`) |
| `app/services/system_settings_service.py` | обе настройки в категории `INTERFACE` + описания для админки |
| `tests/middlewares/test_premium_emoji.py` | 33 проверки ВнеПлены, переписанные на pytest (24 теста, без сети) |
| `docs/project_structure_reference.md` | пересобран `make docs-structure` (только две новые записи) |

**Поведение.** Карта `PREMIUM_EMOJI_MAP` — пары `эмодзи=custom_emoji_id` через запятую; меняется из админки
(категория «Интерфейс») или `PUT /settings/PREMIUM_EMOJI_MAP` Web API без рестарта, пустая строка выключает всё.
В HTML-текстах, подписях и rich-HTML эмодзи из карты становится `<tg-emoji>` (обычный эмодзи остаётся внутри
запасным); внутри `<a>`, `<code>`, `<pre>`, готовых `<tg-emoji>` и `<tg-button>`, в тексте без HTML и с готовыми
entities — без замены; не больше 50 замен на сообщение. У inline-кнопок без иконки ведущий эмодзи из карты
уходит в `icon_custom_emoji_id` (`PREMIUM_EMOJI_BUTTONS`); кнопки меню с иконкой из `CABINET_BUTTON_STYLES`
не трогаются. Отказ Telegram с намёком на entities/emoji/кнопки — повтор исходного запроса без значков;
«message is not modified» и прочие ошибки не повторяются.

**Отличие от ВнеПлены.** Патча №7 (`MAIN_MENU_RICH_PREMIUM_EMOJI`, карта rich-меню) у ДонБатона нет: rich-меню
выключено (`MAIN_MENU_RICH_ENABLED=false` в `.env` — с 04.09 ради баннера главного меню). Поэтому якоря вставок —
`MAIN_MENU_RICH_LOGO_URL`, а из описания настройки убрана фраза про карту rich-меню. Если rich-меню включат,
его текст закроет та же `PREMIUM_EMOJI_MAP` (middleware видит `sendRichMessage`).

## Условия работы

- Премиум-эмодзи в тексте работают, пока у владельца бота есть Telegram Premium; иначе клиент видит обычный эмодзи.
  Проверка — по ответу Telegram: в `entities`/`caption_entities` отправленного сообщения есть `custom_emoji`.
- Иконки кнопок `icon_custom_emoji_id` бот ДонБатона показывает с 04.09 (кнопки меню).

## Перенос при слиянии с апстримом

Патч свой, апстрим его не содержит. Конфликт возможен только в трёх вставках (`bot_factory.py`, `config.py`,
`system_settings_service.py`); модуль и тест — новые файлы. После слияния: `pytest tests/middlewares` и
`make docs-structure`. Сверка модуля с ВнеПленой — по `d853d006` (их worktree на `deeeway-core`
`/opt/remnawave-bedolaga-worktrees/vne-5.0.0-20261001`, копия — `vpn/vneplena/implementation/bot-premium-emoji-20261021/`).

## Откат

- Без пересборки: `PREMIUM_EMOJI_MAP` = пустая строка (замена выключена целиком), только кнопки —
  `PREMIUM_EMOJI_BUTTONS=false`.
- Образ: вернуть `donbaton/bedolaga:u500-4943e3b6-20261001` — миграций нет, схема та же.
