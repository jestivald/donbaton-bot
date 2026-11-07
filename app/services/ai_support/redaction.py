"""Обезличивание клиентского текста перед отправкой провайдеру."""

import ipaddress
import re
from datetime import datetime
from urllib.parse import unquote, urlsplit


KEY_RE = re.compile(
    r'\b(?:vless|vmess|trojan|ssr?|hy2|hysteria2?|tuic|happ|incy|v2raytun|streisand|sing-box|wireguard|wg|sn)://[^\s<>"\u00ab\u00bb]+',
    re.IGNORECASE,
)
URL_RE = re.compile(
    r'(?:https?://|\b(?:[\w-]+\.)+(?:tech|xyz|com|ru|org|me|net)(?:[/?#]|\b))[^\s<>"\u00ab\u00bb]*', re.IGNORECASE
)
EMAIL_RE = re.compile(r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+', re.IGNORECASE)
UUID_RE = re.compile(r'\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b', re.IGNORECASE)
PHONE_RE = re.compile(r'(?<!\w)(?:\+7|8|7)[\s().-]*\d{3}[\s().-]*\d{3}[\s().-]*\d{2}[\s().-]*\d{2}(?!\d)')
IPV4_RE = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6_RE = re.compile(
    r'(?<![\w:])(?:[0-9a-f]{0,4}:){2,}(?:[0-9a-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})(?:%[\w.-]+)?(?!\w)', re.IGNORECASE
)
TOKEN_RE = re.compile(r'(?<![\w-])[A-Za-z0-9_-]{24,}(?![\w-])')
ID_RE = re.compile(r'(?<![\w.])\d{7,}(?![\w.])')
CARD_RE = re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)')


def luhn(value: str) -> bool:
    digits = [int(digit) for digit in value if digit.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            digit = digit - 9 if digit > 9 else digit
        total += digit
    return total % 10 == 0


def private_url(value: str) -> bool:
    try:
        parts = urlsplit(value if '://' in value else 'https://' + value)
        host = unquote(parts.hostname or '').lower().rstrip('.')
        if host == 'sub.donbaton.tech' or host == 'donbaton.xyz' or host.endswith('.donbaton.xyz'):
            return True
        if host == 'lk.donbaton.tech':
            # Корень и обычные разделы кабинета публичны. Произвольный путь или query — нет.
            return bool(
                parts.query
                or parts.fragment
                or parts.path.rstrip('/') not in ('', '/support', '/login', '/subscription', '/balance', '/faq')
            )
    except ValueError:
        return True
    return False


def contains_ip(text: str) -> bool:
    for pattern in (IPV4_RE, IPV6_RE):
        for match in pattern.finditer(text):
            try:
                ipaddress.ip_address(match.group().split('%')[0])
                return True
            except ValueError:
                continue
    return False


def _ip(match):
    try:
        ipaddress.ip_address(match.group().split('%')[0])
        return '[IP]'
    except ValueError:
        return match.group()


def _amount_or_date(text: str, match) -> bool:
    suffix = text[match.end() : match.end() + 12]
    value = match.group()
    compact_date = False
    if re.fullmatch(r'(?:19|20)\d{6}', value):
        try:
            datetime.strptime(value, '%Y%m%d')
            compact_date = True
        except ValueError:
            pass
    return bool(re.match(r'\s*(?:₽|руб\b|рублей\b|RUB\b|доллар|\$|€)', suffix, re.IGNORECASE) or compact_date)


def redact(text: str) -> str:
    text = KEY_RE.sub('[ключ подключения]', str(text or ''))
    text = EMAIL_RE.sub('[почта]', text)
    text = re.sub(r'\bHWID\s*[:=]\s*[^\s,;]+', '[идентификатор]', text, flags=re.IGNORECASE)
    text = UUID_RE.sub('[идентификатор]', text)
    # URL временно вынимаются: длинный путь публичной статьи не является токеном.
    urls = []

    def hide_url(match):
        value = match.group()
        safe = '[ссылка подписки]' if private_url(value) else redact_url(value)
        urls.append(safe)
        return f'\x00{len(urls) - 1}\x00'

    text = URL_RE.sub(hide_url, text)
    text = CARD_RE.sub(
        lambda m: '[номер карты]' if luhn(m.group()) and not _amount_or_date(text, m) else m.group(), text
    )
    text = PHONE_RE.sub(lambda m: m.group() if _amount_or_date(text, m) else '[телефон]', text)
    text = IPV4_RE.sub(_ip, text)
    text = IPV6_RE.sub(_ip, text)
    text = re.sub(r'(?<![\w@])@[A-Za-z][A-Za-z0-9_]{2,31}\b', '[ник]', text)
    text = TOKEN_RE.sub('[токен]', text)
    original = text
    text = ID_RE.sub(lambda m: m.group() if _amount_or_date(original, m) else '[идентификатор]', text)
    return re.sub(r'\x00(\d+)\x00', lambda m: urls[int(m[1])], text)


def redact_url(value: str) -> str:
    # Публичный идентификатор приложения — не идентификатор клиента.
    parts = urlsplit(value if '://' in value else 'https://' + value)
    if parts.username or parts.password or contains_ip(value):
        return '[токен]'
    query = unquote(parts.query + '&' + parts.fragment)
    public_app = parts.hostname == 'play.google.com' and parts.path == '/store/apps/details'
    if re.search(r'(?:^|[?&#])(?:token|key|auth|password|secret|email)=', query, re.IGNORECASE):
        return '[токен]'
    if not public_app and (TOKEN_RE.search(query) or re.search(r'(?:^|&)id=\d{7,}', query)):
        return '[токен]'
    return value
