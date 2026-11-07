"""Проверка утечки служебных сведений и обычный текст для Telegram/кабинета."""

import re
from html import unescape

from app.services.ai_support.redaction import KEY_RE, URL_RE, contains_ip, private_url


INTERNAL_RE = re.compile(
    r'donbaton\.xyz|\bsubo\.|admin\.donbaton\.tech|grafitmedia|synqlane|remnawave|'
    r'\b(?:сквад\w*|балансир\w*|нод(?:а|ы|е|у|ой|ам|ами|ах)?)\b|'
    r'(?<![\w-])(?:msk|lon|waw|it|ams|sel-spb|bs|spb|fra|sel|hel|ru|de|nl|fi|us|uk|tr|kz|se|ch|fr)\d+[a-z]?(?![\w-])',
    re.IGNORECASE,
)


def guard(text: str) -> str | None:
    # Проверяем до очистки разметки, чтобы скрытая Markdown-ссылка не обошла фильтр.
    if INTERNAL_RE.search(text) or KEY_RE.search(text) or contains_ip(text):
        return None
    if any(private_url(match.group()) for match in URL_RE.finditer(text)):
        return None
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
    text = re.sub(r'```[^\n]*\n?', '', text)
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    text = unescape(re.sub(r'<[^>]*>', '', text)).strip()
    # Сущности и разметка могли скрывать запрещённые слова/адреса.
    if INTERNAL_RE.search(text) or contains_ip(text) or KEY_RE.search(text):
        return None
    if any(private_url(match.group()) for match in URL_RE.finditer(text)):
        return None
    if len(text) > 3500:
        prefix = text[:3500]
        boundaries = list(re.finditer(r'[.!?](?:\s|$)', prefix))
        text = prefix[: boundaries[-1].start() + 1] if boundaries else prefix.rsplit(' ', 1)[0]
    return text or None
