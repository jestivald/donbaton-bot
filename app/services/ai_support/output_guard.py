"""Проверки служебных сведений, обещаний, ссылок и отдельного резюме оператору."""

import re
import unicodedata
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.ai_support.redaction import KEY_RE, contains_ip, redact


INTERNAL_RE = re.compile(
    r'donbaton\.xyz|\bsubo\.|admin\.donbaton\.tech|grafitmedia|synqlane|remnawave|'
    r'\b(?:сквад\w*|балансир\w*|нод(?:а|ы|е|у|ой|ам|ами|ах)?)\b|'
    r'(?<![\w-])(?:msk|lon|waw|it|ams|sel-spb|bs|spb|fra|sel|hel|ru|de|nl|fi|us|uk|tr|kz|se|ch|fr)\d+[a-z]?(?![\w-])',
    re.IGNORECASE,
)

CONTROL_LEAK_RE = re.compile(
    r'\b(?:handoff_to_human|get_account_overview|get_devices|ai_support_[a-z0-9_]+)\b|'
    r'текст\s+клиента,\s*тикета(?:\s+и\s+история)?|'
    r'дополнительные\s+инструкции\s+владельца\b|база\s+знаний\s*:',
    re.IGNORECASE,
)
PROMISE_RE = re.compile(
    r'\b(?:верн[её]м|возместим|компенсируем|начислим|выплатим|обязуемся вернуть|гарантируем возврат)\b|'
    r'\b(?:возврат|компенсация)(?:\s+[^.!?\n]{0,30})?\s+одобрен[аоы]?\b|'
    r'\b(?:деньги|дни|гигабайты)\s+(?:начислены|возвращены)\b|'
    r'\b(?:we|i)\s+(?:will|shall)\s+(?:refund|reimburse|compensate|credit)\b|'
    r'\brefund\s+(?:approved|guaranteed)\b',
    re.IGNORECASE,
)
DANGEROUS_TAG_RE = re.compile(r'<\s*/?\s*(?:script|iframe|object|embed|form)\b', re.IGNORECASE)
# Отдельный шаблон выхода: охватывает также произвольные схемы, почту и домены без схемы.
LINK_RE = re.compile(
    r'(?<![\w@])(?:[a-z][a-z0-9+.-]*://|mailto:|javascript:|data:)[^\s<>"«»]+|'
    r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+|'
    r'(?<![\w@.-])(?:[\w-]+\.)+[^\W\d_]{2,}(?:[^\s<>"«»]*)',
    re.IGNORECASE,
)
SUMMARY_PREFIX = '[авто, не проверено] '
SUMMARY_INSTRUCTION_RE = re.compile(
    r'игнориру\w*|выполни\w*|приказыва\w*|я (?:владелец|администратор)|'
    r'\b(?:ignore|execute|system override)\b',
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Раскрываем несколько слоёв кодирования и убираем невидимые управляющие символы."""
    value = text or ''
    for _ in range(4):
        decoded = unicodedata.normalize('NFKC', unescape(unquote(value)))
        decoded = ''.join(
            ch
            for ch in decoded
            if ch in '\n\t'
            or (
                unicodedata.category(ch) not in {'Cf', 'Cc', 'Cs'}
                and ch not in {'\u034f', '\u180b', '\u180c', '\u180d', '\ufe0e', '\ufe0f'}
            )
        )
        if decoded == value:
            break
        value = decoded
    return value


def allowed_link(value: str) -> bool:
    """Точные адресаты; у кабинета и Telegram дополнительно ограничены пути и параметры."""
    if '@' in value and not re.match(r'[a-z][a-z0-9+.-]*:', value, re.IGNORECASE):
        return value.lower() == 'support@donbaton.tech'
    try:
        parts = urlsplit(value if re.match(r'[a-z][a-z0-9+.-]*:', value, re.IGNORECASE) else 'https://' + value)
        host = (parts.hostname or '').lower()
        if parts.username is not None or parts.password is not None or parts.port is not None:
            return False
        # Даже пустой порт и неоднозначные разделители не принимаем.
        if ':' in parts.netloc or '\\' in value or parts.fragment:
            return False
        if parts.scheme == 'mailto':
            return parts.path.lower() == 'support@donbaton.tech' and not parts.query
        if parts.scheme == 'tg':
            return (
                host == 'resolve'
                and not parts.path
                and parse_qs(parts.query, keep_blank_values=True)
                in ({'domain': ['donbatonvpn_bot']}, {'domain': ['batonsupport']})
            )
        if parts.scheme not in {'http', 'https'}:
            return False
        path = parts.path.rstrip('/')
        if any(segment in {'.', '..'} for segment in parts.path.split('/')):
            return False
        if host == 't.me':
            return path.lower() in {'/donbatonvpn_bot', '/batonsupport'} and not parts.query
        if host == 'lk.donbaton.tech':
            return path in {'', '/support', '/login', '/subscription', '/balance', '/faq'} and not parts.query
        if host == 'github.com':
            return parts.path.startswith(('/INCY-DEV/', '/Happ-proxy/')) and not parts.query
        return host in {
            'donbaton.tech',
            'apps.apple.com',
            'play.google.com',
            'www.happ.su',
            'happ.su',
            'testflight.apple.com',
        }
    except ValueError:
        return False


def strip_links(text: str, *, all_links: bool = False) -> str:
    def replace(match):
        value = match.group()
        link = value.rstrip('.,;:!?')
        while link.endswith((')', ']', '}')) and any(
            link.count(right) > link.count(left) for left, right in [('(', ')'), ('[', ']'), ('{', '}')]
        ):
            link = link[:-1]
        suffix = value[len(link) :]
        return ('' if all_links else link if allowed_link(link) else '(ссылка удалена)') + suffix

    return LINK_RE.sub(replace, text)


def sentence_limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    boundaries = list(re.finditer(r'[.!?](?:\s|$)', prefix))
    return prefix[: boundaries[-1].start() + 1] if boundaries else prefix.rsplit(' ', 1)[0]


def has_control_leak(text: str) -> bool:
    decoded = normalize(text)
    plain = normalize(re.sub(r'<[^>]*>', '', decoded))
    plain = plain.replace('**', '').replace('__', '').replace('`', '')
    return any(CONTROL_LEAK_RE.search(value) for value in (text, decoded, plain))


def guard(text: str) -> str | None:
    if has_control_leak(text):
        return None
    decoded = normalize(text)
    for value in (text, decoded):
        if CONTROL_LEAK_RE.search(value) or DANGEROUS_TAG_RE.search(value) or KEY_RE.search(value):
            return None
        # Ссылки проверяются разрешительным списком; утечки в обычном тексте отклоняются целиком.
        plain = strip_links(value)
        if INTERNAL_RE.search(plain) or contains_ip(plain):
            return None
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', decoded)
    text = re.sub(
        r'<a\b[^>]*href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>', r'\2 (\1)', text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r'```[^\n]*\n?', '', text)
    text = re.sub(r'(?m)^\s{0,3}#{1,6}\s*', '', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    text = normalize(re.sub(r'<[^>]*>', '', text)).strip()
    # Повторяем проверки после снятия разметки: преобразования могут склеить служебное имя.
    if DANGEROUS_TAG_RE.search(text) or CONTROL_LEAK_RE.search(text) or KEY_RE.search(text):
        return None
    text = strip_links(text)
    if INTERNAL_RE.search(text) or contains_ip(text):
        return None
    return sentence_limit(text, 2000) or None


def operator_summary(text: str | None, reason) -> str:
    """Резюме — неподтверждённые данные, отдельные от клиентского ответа."""
    value = normalize(text or '')
    value = value.removeprefix(SUMMARY_PREFIX)
    invalid = len(value) > 500 - len(SUMMARY_PREFIX) or any(
        pattern.search(value)
        for pattern in (CONTROL_LEAK_RE, DANGEROUS_TAG_RE, INTERNAL_RE, KEY_RE, SUMMARY_INSTRUCTION_RE)
    )
    value = normalize(re.sub(r'<[^>]*>', '', strip_links(value, all_links=True)))
    if CONTROL_LEAK_RE.search(value) or contains_ip(value):
        invalid = True
    value = redact(value).strip()
    if invalid or not value:
        labels = {
            'money': 'Вопрос об оплате или начислении.',
            'refund': 'Запрос о возврате.',
            'client_request': 'Клиент просит оператора.',
            'no_knowledge': 'Нужна информация о сервисе.',
            'not_resolved': 'Проблема не решена предложенными шагами.',
        }
        value = labels.get(str(reason), 'Вопрос требует проверки оператором.')
    return SUMMARY_PREFIX + sentence_limit(value, 500 - len(SUMMARY_PREFIX))
