"""Детерминированная база знаний из инфо-страниц кабинета, с последним удачным кэшем."""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

import structlog
from sqlalchemy import and_, or_, select

from app.database.database import AsyncSessionLocal
from app.database.models import InfoPage
from app.services.ai_support.api import KbStatus


logger = structlog.get_logger(__name__)
TTL = 300


class TextParser(HTMLParser):
    """HTML в текст с абзацами, пунктами списка и адресами ссылок."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'ul', 'ol'):
            self.parts.append('\n')
        if tag == 'li':
            self.parts.append('\n- ')
        if tag == 'a':
            self.links.append(dict(attrs).get('href', ''))

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == 'a' and self.links:
            link = self.links.pop()
            if link:
                self.parts.append(f' ({link})')
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(value: str) -> str:
    parser = TextParser()
    parser.feed(value)
    return re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts)).strip()


def localized(value: dict, language: str) -> str:
    if not isinstance(value, dict):
        return str(value or '')
    return value.get(language) or value.get('ru') or next((v for v in value.values() if v), '')


@dataclass
class Knowledge:
    text: str
    status: KbStatus


_cache: dict[str, tuple[float, Knowledge]] = {}
_lock = asyncio.Lock()


def build(pages, language: str) -> Knowledge:
    chunks, slugs, items = [], [], 0
    pages = sorted(pages, key=lambda p: (not p.slug.startswith('ai-'), p.sort_order, p.id))
    for page in pages:
        if not (page.slug.startswith('ai-') or (page.is_active and page.page_type == 'faq')):
            continue
        title = html_text(localized(page.title, language))
        raw = localized(page.content, language)
        content = []
        if page.page_type == 'faq':
            try:
                faqs = json.loads(raw)
            except (ValueError, TypeError):
                faqs = []
            if isinstance(faqs, list):
                for faq in faqs:
                    if isinstance(faq, dict) and faq.get('q') and faq.get('a'):
                        content.append(f'Вопрос: {html_text(str(faq["q"]))}\nОтвет: {html_text(str(faq["a"]))}')
                        items += 1
        elif raw:
            content.append(html_text(raw))
            items += 1
        chunks.append(title + '\n' + '\n\n'.join(content))
        slugs.append(page.slug)
    # База знаний — доверенный текст владельца: не обезличиваем (иначе пропадут публичные контакты поддержки).
    text = '\n\n'.join(chunks)
    return Knowledge(
        text,
        KbStatus(
            pages=len(slugs),
            items=items,
            chars=len(text),
            approx_tokens=(len(text) + 3) // 4,
            version=hashlib.sha256(text.encode()).hexdigest()[:16],
            loaded_at=datetime.now(UTC),
            slugs=slugs,
        ),
    )


async def load(language: str = 'ru') -> Knowledge:
    async with _lock:
        previous = _cache.get(language)
        if previous and time.monotonic() - previous[0] < TTL:
            return previous[1]
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(InfoPage).where(
                        or_(
                            InfoPage.slug.startswith('ai-'),
                            and_(InfoPage.is_active.is_(True), InfoPage.page_type == 'faq'),
                        )
                    )
                )
                loaded = build(result.scalars().all(), language)
            _cache[language] = (time.monotonic(), loaded)
            return loaded
        except Exception:
            logger.warning('Не удалось обновить базу знаний ИИ; используется последний снимок')
            return previous[1] if previous else Knowledge('', KbStatus(0, 0, 0, 0, '', None))


def invalidate_kb_cache() -> None:
    # Старое содержимое сохраняется для отката при ошибке БД.
    for language, (_, value) in list(_cache.items()):
        _cache[language] = (float('-inf'), value)


async def kb_status() -> KbStatus:
    return (await load()).status
