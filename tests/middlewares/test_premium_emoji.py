"""Премиум-эмодзи по карте PREMIUM_EMOJI_MAP на исходящих запросах бота.

Middleware сессии меняет обычные эмодзи на <tg-emoji> в HTML-текстах, подписях и
rich-сообщениях, а ведущий эмодзи inline-кнопки уносит в icon_custom_emoji_id.
Ссылки, код, готовые значки, тексты без HTML и с готовыми entities не трогаются;
отказ Telegram из-за значков — повтор исходного запроса. Проверки — перенос
test_premium_emoji.py ВнеПлены (патч №9, d853d006) на pytest, без сети.
"""

from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendPhoto, SendRichMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage, MessageEntity

from app.config import settings
from app.middlewares import premium_emoji as pe


MAP = '✅=111, 💰=222,💳=333,🆔=444,⚠️=555,👩=666,👍=777, кривая пара, ❌=abc,⬅=888'


@pytest.fixture(autouse=True)
def emoji_map(monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', MAP)
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_BUTTONS', True)


@pytest.fixture
def bot():
    return Bot(token='123456:TESTTOKEN', default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def tg(emoji_id: str, emoji: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'


def _markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text='💰 Мой баланс', callback_data='menu_balance')],
            [InlineKeyboardButton(text='✅ Уже с иконкой', callback_data='x', icon_custom_emoji_id='999')],
            [
                InlineKeyboardButton(text='⬅️', callback_data='prev'),
                InlineKeyboardButton(text='Далее ✅', callback_data='next'),
            ],
            [InlineKeyboardButton(text='  ⬅️ Назад', callback_data='back', style='primary')],
        ]
    )


def test_map_skips_broken_pairs_and_strips_vs16():
    mapping, _ = pe._premium_map()
    assert mapping == {
        '✅': '111',
        '💰': '222',
        '💳': '333',
        '🆔': '444',
        '⚠': '555',
        '👩': '666',
        '👍': '777',
        '⬅': '888',
    }


def test_payment_notification_gets_premium_emoji():
    topup = (
        '✅ <b>Пополнение успешно!</b>\n\n\U0001f4b0 Сумма: 10 ₽\n'
        '\U0001f4b3 Способ: СБП\n\U0001f194 Транзакция: 759\n\nБаланс пополнен автоматически!'
    )
    assert pe.apply_premium_emoji_html(topup) == (
        f'{tg("111", "✅")} <b>Пополнение успешно!</b>\n\n{tg("222", "💰")} Сумма: 10 ₽\n'
        f'{tg("333", "💳")} Способ: СБП\n{tg("444", "🆔")} Транзакция: 759\n\nБаланс пополнен автоматически!'
    )


def test_vs16_stays_in_fallback_emoji():
    assert pe.apply_premium_emoji_html('⚠️ Внимание') == f'{tg("555", "⚠️")} Внимание'


def test_links_code_and_ready_tg_emoji_are_left_alone():
    source = (
        '<a href="https://x/?q=✅">✅ ссылка</a> <code>💰</code> <pre>💳</pre> <tg-emoji emoji-id="9">✅</tg-emoji> ✅'
    )
    assert pe.apply_premium_emoji_html(source) == (
        '<a href="https://x/?q=✅">✅ ссылка</a> <code>💰</code> <pre>💳</pre> '
        f'<tg-emoji emoji-id="9">✅</tg-emoji> {tg("111", "✅")}'
    )


def test_nested_tags_inside_link():
    assert pe.apply_premium_emoji_html('<a href="x"><b>✅</b></a><b>✅</b>') == (
        f'<a href="x"><b>✅</b></a><b>{tg("111", "✅")}</b>'
    )


def test_rich_self_closing_tags_and_rich_buttons():
    source = '<h4>✅ Готово</h4><hr/><p>💰 10</p><tg-button-row><tg-button>💳 Оплатить</tg-button></tg-button-row>'
    assert pe.apply_premium_emoji_html(source) == (
        f'<h4>{tg("111", "✅")} Готово</h4><hr/><p>{tg("222", "💰")} 10</p>'
        '<tg-button-row><tg-button>💳 Оплатить</tg-button></tg-button-row>'
    )


def test_zwj_sequences_and_skin_tones_are_left_alone():
    assert pe.apply_premium_emoji_html('👩‍💻 👨‍👩‍👧 👍🏻 👍') == f'👩‍💻 👨‍👩‍👧 👍🏻 {tg("777", "👍")}'


def test_replacements_are_capped_per_message():
    many = pe.apply_premium_emoji_html('✅' * 60)
    assert many.count('<tg-emoji') == 50
    assert many.endswith('✅' * 10)


def test_text_without_mapped_emoji_is_unchanged():
    assert pe.apply_premium_emoji_html('Привет 🙂 <b>мир</b>') == 'Привет 🙂 <b>мир</b>'


def test_buttons_get_icons_from_leading_emoji():
    markup = _markup()
    rows = pe.apply_premium_emoji_markup(markup).inline_keyboard

    assert (rows[0][0].text, rows[0][0].icon_custom_emoji_id) == ('Мой баланс', '222')
    assert (rows[1][0].text, rows[1][0].icon_custom_emoji_id) == ('✅ Уже с иконкой', '999')
    # Кнопку из одного эмодзи Telegram без текста не примет; эмодзи в конце — не ведущий.
    assert [(b.text, b.icon_custom_emoji_id) for b in rows[2]] == [('⬅️', None), ('Далее ✅', None)]
    assert (rows[3][0].text, rows[3][0].icon_custom_emoji_id, rows[3][0].style) == ('Назад', '888', 'primary')
    assert markup.inline_keyboard[0][0].text == '💰 Мой баланс'


def test_markup_without_changes_is_the_same_object():
    plain = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Ок', callback_data='ok')]])
    assert pe.apply_premium_emoji_markup(plain) is plain


def test_send_message_with_default_html_parse_mode(bot):
    markup = _markup()
    send = SendMessage(chat_id=1, text='✅ <b>Готово</b>', reply_markup=markup)

    patched = pe._patched(bot, send)

    assert patched.text == f'{tg("111", "✅")} <b>Готово</b>'
    assert patched.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id == '222'
    assert send.text == '✅ <b>Готово</b>'
    assert send.reply_markup is markup


def test_plain_text_entities_and_markdown_are_left_alone(bot):
    assert pe._patched(bot, SendMessage(chat_id=1, text='✅ Готово', parse_mode=None)) is None
    entities = [MessageEntity(type='bold', offset=2, length=1)]
    assert pe._patched(bot, SendMessage(chat_id=1, text='✅ x', entities=entities)) is None
    assert pe._patched(bot, SendMessage(chat_id=1, text='✅ x', parse_mode='MarkdownV2')) is None


def test_edit_text_and_photo_caption(bot):
    edit = pe._patched(bot, EditMessageText(chat_id=1, message_id=2, text='💳 Оплата', parse_mode='HTML'))
    assert edit.text == f'{tg("333", "💳")} Оплата'
    photo = pe._patched(bot, SendPhoto(chat_id=1, photo='file', caption='🆔 42'))
    assert photo.caption == f'{tg("444", "🆔")} 42'


def test_callback_answer_is_left_alone(bot):
    assert pe._patched(bot, AnswerCallbackQuery(callback_query_id='1', text='✅ Готово')) is None


def test_send_rich_message(bot):
    rich = InputRichMessage(html='<p>✅ Готово</p>', skip_entity_detection=True)
    patched = pe._patched(bot, SendRichMessage(chat_id=1, rich_message=rich))
    assert patched.rich_message.html == f'<p>{tg("111", "✅")} Готово</p>'
    assert patched.rich_message.skip_entity_detection is True


def test_buttons_switch_off_separately(bot, monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_BUTTONS', False)
    assert pe._patched(bot, SendMessage(chat_id=1, text='Без эмодзи', reply_markup=_markup())) is None


def test_empty_map_switches_everything_off(bot, monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', '')
    assert pe._patched(bot, SendMessage(chat_id=1, text='✅ x', reply_markup=_markup())) is None


def _fake_request(calls: list[str], fail_with: str | None = None):
    async def make_request(_bot, method):
        calls.append(method.text)
        if fail_with and '<tg-emoji' in method.text:
            raise TelegramBadRequest(method=method, message=fail_with)
        return 'sent'

    return make_request


@pytest.mark.asyncio
async def test_middleware_sends_request_with_icons(bot, monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', '✅=111')
    calls: list[str] = []

    result = await pe.PremiumEmojiMiddleware()(_fake_request(calls), bot, SendMessage(chat_id=1, text='✅ x'))

    assert (result, calls) == ('sent', [f'{tg("111", "✅")} x'])


@pytest.mark.asyncio
async def test_middleware_retries_without_icons_when_telegram_rejects_them(bot, monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', '✅=111')
    calls: list[str] = []
    make_request = _fake_request(calls, "Bad Request: can't parse entities: custom emoji")

    result = await pe.PremiumEmojiMiddleware()(make_request, bot, SendMessage(chat_id=1, text='✅ x'))

    assert (result, calls) == ('sent', [f'{tg("111", "✅")} x', '✅ x'])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'error'),
    [
        # Повтор без значков «изменил» бы сообщение и снял с него премиум-эмодзи.
        (
            EditMessageText(chat_id=1, message_id=1, text='✅ x'),
            'Bad Request: message is not modified: specified new message content and reply markup',
        ),
        (SendMessage(chat_id=1, text='✅ x'), 'Bad Request: chat not found'),
    ],
)
async def test_middleware_does_not_retry_other_errors(bot, monkeypatch, method, error):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', '✅=111')
    calls: list[str] = []

    with pytest.raises(TelegramBadRequest):
        await pe.PremiumEmojiMiddleware()(_fake_request(calls, error), bot, method)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_middleware_passes_untouched_request_through(bot, monkeypatch):
    monkeypatch.setattr(settings, 'PREMIUM_EMOJI_MAP', '✅=111')
    calls: list[str] = []

    result = await pe.PremiumEmojiMiddleware()(_fake_request(calls), bot, SendMessage(chat_id=1, text='без значков'))

    assert (result, calls) == ('sent', ['без значков'])


def test_create_bot_registers_middleware():
    from app.bot_factory import create_bot

    real_bot = create_bot('123456:TESTTOKEN')
    names = [type(middleware).__name__ for middleware in real_bot.session.middleware._middlewares]
    assert 'PremiumEmojiMiddleware' in names
