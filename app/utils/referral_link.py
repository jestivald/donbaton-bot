"""Извлечение реферального кода из того, что пользователь реально присылает.

Кабинет показывает партнёру ДВЕ ссылки (`app/cabinet/routes/referral.py`):
`https://<lk>/login?ref=<code>` и `https://t.me/<bot>?start=<code>`. Партнёр
пересылает ссылку целиком, приглашённый так же целиком её и вставляет — в поле
«введите реферальный код» на шаге регистрации в боте. До этого патча ссылка
резалась `validate_promo_format()` (в ней есть `:` и `/`), пользователь получал
«❌ Неверный реферальный код», жал «Пропустить» и регистрировался БЕЗ реферера.

Разбор намеренно терпимый к формату: схема необязательна, домен любой,
регистр параметра не важен, лишние параметры игнорируются.
"""

from urllib.parse import parse_qs, urlparse


__all__ = ['extract_referral_code']

# Параметры, в которых кабинет и бот передают код приглашения.
_CODE_QUERY_KEYS = ('ref', 'start', 'startapp', 'referral', 'referral_code')

# Разумный потолок: `users.referral_code` — VARCHAR(20), промокоды — до 50.
_MAX_CODE_LENGTH = 50


def extract_referral_code(raw: str | None) -> str:
    """Вернуть код из текста: голый код отдаётся как есть, ссылка — распаршенной.

    Никогда не бросает и никогда не возвращает None: на непонятном вводе
    отдаёт исходную строку без окружающих пробелов, чтобы вызывающий код
    прошёл штатную валидацию и показал штатную ошибку.
    """
    if not raw:
        return ''

    text = raw.strip()
    if not text:
        return ''

    # Голый код — самый частый случай, не трогаем его вовсе.
    if '/' not in text and '?' not in text and ':' not in text:
        return text

    # Берём последнее «слово»: пользователи присылают «вот моя ссылка <url>».
    candidate = text.split()[-1]

    # urlparse без схемы кладёт всё в path — подставим схему, чтобы получить query.
    to_parse = candidate if '//' in candidate else f'//{candidate}'
    try:
        parsed = urlparse(to_parse, scheme='https')
    except ValueError:
        return text

    query = parse_qs(parsed.query, keep_blank_values=False)
    lowered = {key.lower(): values for key, values in query.items()}
    for key in _CODE_QUERY_KEYS:
        values = lowered.get(key)
        if values and values[0].strip():
            return values[0].strip()[:_MAX_CODE_LENGTH]

    # Ссылка без параметров (например `t.me/bot/ref_uXXXX`) — последний сегмент пути.
    tail = parsed.path.rstrip('/').rsplit('/', 1)[-1]
    if tail:
        return tail[:_MAX_CODE_LENGTH]

    return text
