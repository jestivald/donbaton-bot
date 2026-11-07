"""Загрузка Markdown-базы ИИ в скрытые FAQ-страницы; по умолчанию только план."""

import argparse
import asyncio
import html
import json
import re
import sys
from pathlib import Path

from sqlalchemy import select


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database.database import AsyncSessionLocal
from app.database.models import InfoPage


def inline(text: str) -> str:
    value = html.escape(text)
    return re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', r'<a href="\2">\1</a>', value)


def to_html(lines: list[str]) -> str:
    chunks, paragraph = [], []
    in_list = False

    def flush():
        if paragraph:
            chunks.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()

    for line in lines:
        item = re.match(r'^\s*(?:[-*]|\d+\.)\s+(.+)', line)
        if item:
            flush()
            if not in_list:
                chunks.append('<ul>')
                in_list = True
            chunks.append('<li>' + inline(item[1]) + '</li>')
        else:
            if in_list:
                chunks.append('</ul>')
                in_list = False
            if line.strip():
                paragraph.append(line.strip())
            else:
                flush()
    flush()
    if in_list:
        chunks.append('</ul>')
    return ''.join(chunks)


def parse_markdown(text: str) -> list[dict]:
    pages = []
    page = None
    question = None
    lines = []

    def flush_question():
        if page is not None and question:
            page['items'].append({'q': question, 'a': to_html(lines)})
        lines.clear()

    for line in text.splitlines():
        heading = re.fullmatch(r'#\s+(.+?)\s+\{slug=(ai-[a-z0-9-]+)\}\s*', line)
        if heading:
            flush_question()
            page = {'title': heading[1], 'slug': heading[2], 'items': []}
            if any(p['slug'] == page['slug'] for p in pages):
                raise ValueError('Повторный slug в Markdown')
            pages.append(page)
            question = None
        elif line.startswith('## '):
            if page is None:
                raise ValueError('Вопрос находится перед заголовком страницы с {slug=ai-...}')
            flush_question()
            question = line[3:].strip()
        elif question:
            lines.append(line)
        elif line.startswith('# '):
            raise ValueError('Заголовок страницы должен содержать {slug=ai-...}')
    flush_question()
    if not pages or any(not p['items'] for p in pages):
        raise ValueError('В каждой странице должен быть хотя бы один вопрос ##')
    return pages


async def seed(pages, *, apply=False, update=False):
    async with AsyncSessionLocal() as db:
        existing = {
            p.slug: p
            for p in (await db.execute(select(InfoPage).where(InfoPage.slug.in_([p['slug'] for p in pages]))))
            .scalars()
            .all()
        }
        for order, page in enumerate(pages):
            row = existing.get(page['slug'])
            action = 'пропустить' if row is not None and not update else 'обновить' if row is not None else 'создать'
            print(f'{action}: {page["slug"]} — {len(page["items"])} вопросов; скрытая FAQ-страница')
            if not apply or action == 'пропустить':
                continue
            if row is None:
                row = InfoPage(slug=page['slug'])
                db.add(row)
            row.title = {'ru': page['title']}
            row.content = {'ru': json.dumps(page['items'], ensure_ascii=False)}
            row.page_type = 'faq'
            row.is_active = False
            row.sort_order = order
        if apply:
            await db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--apply', action='store_true', help='Записать изменения в БД')
    parser.add_argument('--update', action='store_true', help='Перезаписать существующие слаги')
    args = parser.parse_args()
    pages = parse_markdown(args.file.read_text(encoding='utf-8'))
    if not args.apply:
        print('Сухой прогон: изменения не записываются.')
    asyncio.run(seed(pages, apply=args.apply, update=args.update))


if __name__ == '__main__':
    main()
