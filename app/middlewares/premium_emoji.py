"""Премиум-эмодзи во всех сообщениях бота (ДонБатон; перенос патча №9 ВнеПлены, d853d006).

Тексты бота — уведомления платёжек, мониторинга, рассылок, ответы обработчиков —
частью лежат в локалях, а частью зашиты в код у каждого провайдера отдельно. Поэтому
замена сделана не в текстах, а на исходящем запросе: middleware сессии aiogram видит
всё, что бот отправляет, через какой бы `create_bot()` оно ни шло. Ниже docstring'а
модуль совпадает с d853d006 побайтно — при переносе правок сверять с ним.

Карта — настройка PREMIUM_EMOJI_MAP («эмодзи=custom_emoji_id» через запятую),
меняется из админки без рестарта; пустая строка выключает замену целиком.

* Текст и подписи с parse_mode HTML (sendMessage, editMessageText, send*/
  editMessageCaption, copyMessage) и rich-HTML (sendRichMessage): эмодзи между
  тегами становится <tg-emoji>, сам эмодзи остаётся внутри запасным. Внутри <a>,
  <code>, <pre>, готового <tg-emoji> и <tg-button>, в атрибутах, в тексте без HTML
  и с готовыми entities ничего не меняется.
* Inline-кнопки (PREMIUM_EMOJI_BUTTONS): ведущий эмодзи из карты уходит в
  icon_custom_emoji_id и убирается из текста. Кнопки с уже заданной иконкой и
  кнопки из одного эмодзи не трогаются.

Если Telegram отверг изменённый запрос из-за разметки или кнопок, тот же запрос
уходит в исходном виде: значки не должны стоить доставки.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    CopyMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendRichMessage,
    SendVideo,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import InlineKeyboardMarkup

from app.config import settings


logger = structlog.get_logger(__name__)

_VS16 = '️'

_TEXT_METHODS = (SendMessage, EditMessageText)
_CAPTION_METHODS = (
    SendPhoto,
    SendVideo,
    SendAnimation,
    SendDocument,
    SendAudio,
    SendVoice,
    EditMessageCaption,
    CopyMessage,
)
_MARKUP_METHODS = (*_TEXT_METHODS, *_CAPTION_METHODS, EditMessageReplyMarkup, SendRichMessage)

# Telegram не вкладывает custom emoji в ссылку и код; в готовый <tg-emoji> и в
# подпись rich-кнопки своё тоже не вставляем.
_SKIP_TAGS = frozenset({'a', 'code', 'pre', 'tg-emoji', 'tg-button'})
# [^<>], а не [^>] — как в rich_notify: на строке из одних '<' без квадратичного перебора.
_TAG_RE = re.compile(r'<[^<>]+>')
_TAG_NAME_RE = re.compile(r'<\s*(/?)\s*([a-zA-Z][\w-]*)')
# Предел замен на одно сообщение: длинные админские списки остаются как есть дальше лимита.
_MAX_REPLACEMENTS = 50
# Ошибки, после которых имеет смысл повторить запрос без значков. «not modified» сюда не
# входит: повтор без значков «изменил» бы сообщение и снял с него премиум-эмодзи.
_RETRY_HINTS = ('entit', 'emoji', 'icon', 'button', 'markup', 'document_invalid', 'sticker')

_map_cache: tuple[str, dict[str, str], re.Pattern[str] | None] = ('', {}, None)


def _premium_map() -> tuple[dict[str, str], re.Pattern[str] | None]:
    """PREMIUM_EMOJI_MAP → ({эмодзи без VS16: id}, регулярка); кривые пары пропускаются."""
    global _map_cache
    raw = settings.PREMIUM_EMOJI_MAP or ''
    if raw == _map_cache[0]:
        return _map_cache[1], _map_cache[2]
    mapping: dict[str, str] = {}
    for pair in raw.split(','):
        emoji, sep, emoji_id = pair.partition('=')
        emoji = emoji.strip().replace(_VS16, '')
        emoji_id = emoji_id.strip()
        if sep and emoji and emoji_id.isdigit():
            mapping[emoji] = emoji_id
    pattern = None
    if mapping:
        alternatives = '|'.join(re.escape(key) for key in sorted(mapping, key=len, reverse=True))
        # Части ZWJ-последовательностей (👨‍👩‍👧) и эмодзи с тоном кожи не трогаем.
        pattern = re.compile(rf'(?<!‍)(?:{alternatives}){_VS16}?(?![‍\U0001F3FB-\U0001F3FF])')
    _map_cache = (raw, mapping, pattern)
    return mapping, pattern


def apply_premium_emoji_html(value: str) -> str:
    """Эмодзи из карты в тексте между тегами → <tg-emoji>; запасной эмодзи — внутри тега."""
    mapping, pattern = _premium_map()
    if not value or pattern is None:
        return value
    budget = _MAX_REPLACEMENTS

    def _replace(match: re.Match[str]) -> str:
        nonlocal budget
        if budget <= 0:
            return match.group(0)
        budget -= 1
        emoji = match.group(0)
        return f'<tg-emoji emoji-id="{mapping[emoji.replace(_VS16, "")]}">{emoji}</tg-emoji>'

    parts: list[str] = []
    skip_depth = 0
    position = 0
    for tag in _TAG_RE.finditer(value):
        text = value[position : tag.start()]
        parts.append(text if skip_depth else pattern.sub(_replace, text))
        parts.append(tag.group(0))
        name_match = _TAG_NAME_RE.match(tag.group(0))
        if name_match and name_match.group(2).lower() in _SKIP_TAGS and not tag.group(0).endswith('/>'):
            skip_depth = max(0, skip_depth - 1) if name_match.group(1) else skip_depth + 1
        position = tag.end()
    tail = value[position:]
    parts.append(tail if skip_depth else pattern.sub(_replace, tail))
    return ''.join(parts)


def _button_icon(text: str | None) -> tuple[str, str] | None:
    """(custom_emoji_id, текст без ведущего эмодзи) или None, если менять нечего."""
    mapping, pattern = _premium_map()
    if pattern is None or not text:
        return None
    stripped = text.lstrip()
    match = pattern.match(stripped)
    if match is None:
        return None
    rest = stripped[match.end() :].lstrip()
    if not rest:
        # Кнопка из одного эмодзи (стрелки пагинации): без текста Telegram её не примет.
        return None
    return mapping[match.group(0).replace(_VS16, '')], rest


def apply_premium_emoji_markup(markup: Any) -> Any:
    """Копия inline-клавиатуры с иконками вместо ведущих эмодзи; исходная — если менять нечего."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            icon = None if button.icon_custom_emoji_id else _button_icon(button.text)
            if icon is not None:
                button = button.model_copy(update={'icon_custom_emoji_id': icon[0], 'text': icon[1]})
                changed = True
            new_row.append(button)
        rows.append(new_row)
    return markup.model_copy(update={'inline_keyboard': rows}) if changed else markup


def _is_html(bot: Bot, parse_mode: Any) -> bool:
    if isinstance(parse_mode, Default):
        parse_mode = bot.default[parse_mode.name]
    return isinstance(parse_mode, str) and parse_mode.lower() == 'html'


def _patched(bot: Bot, method: TelegramMethod[Any]) -> TelegramMethod[Any] | None:
    """Копия запроса со значками или None, если в нём менять нечего."""
    _, pattern = _premium_map()
    if pattern is None:
        return None
    update: dict[str, Any] = {}
    if isinstance(method, _TEXT_METHODS):
        if method.entities is None and _is_html(bot, method.parse_mode):
            text = apply_premium_emoji_html(method.text)
            if text != method.text:
                update['text'] = text
    elif isinstance(method, _CAPTION_METHODS):
        if method.caption and method.caption_entities is None and _is_html(bot, method.parse_mode):
            caption = apply_premium_emoji_html(method.caption)
            if caption != method.caption:
                update['caption'] = caption
    elif isinstance(method, SendRichMessage):
        # Лениво: rich_admin тянет rich_menu и БД, а этот модуль импортирует bot_factory.
        from app.utils.rich_admin import RICH_TEXT_LIMIT

        rich = method.rich_message
        if rich is not None and rich.html and not rich.markdown:
            rich_html = apply_premium_emoji_html(rich.html)
            # Предел rich считается по исходнику: теги значков не должны его перешагнуть.
            if rich_html != rich.html and len(rich_html) <= RICH_TEXT_LIMIT:
                update['rich_message'] = rich.model_copy(update={'html': rich_html})
    if settings.PREMIUM_EMOJI_BUTTONS and isinstance(method, _MARKUP_METHODS):
        markup = method.reply_markup
        new_markup = apply_premium_emoji_markup(markup)
        if new_markup is not markup:
            update['reply_markup'] = new_markup
    return method.model_copy(update=update) if update else None


class PremiumEmojiMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Bot,
        method: TelegramMethod[Any],
    ) -> Any:
        try:
            patched = _patched(bot, method)
        except Exception as error:
            logger.warning('Премиум-эмодзи: запрос ушёл без замены', method=type(method).__name__, error=str(error))
            patched = None
        if patched is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, patched)
        except TelegramBadRequest as error:
            message = str(error).lower()
            if 'not modified' in message or not any(hint in message for hint in _RETRY_HINTS):
                raise
            logger.warning(
                'Премиум-эмодзи отвергнуты Telegram, повтор без них',
                method=type(method).__name__,
                error=str(error),
            )
            return await make_request(bot, method)
