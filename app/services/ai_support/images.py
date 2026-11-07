"""Изображения ИИ: ограниченная загрузка из Telegram только в оперативную память."""

import asyncio
from io import BytesIO
from pathlib import PurePosixPath

import structlog

from app.services.ai_support import settings_view
from app.services.ai_support.api import AiImage
from app.services.ai_support.settings_view import Config


logger = structlog.get_logger(__name__)
DOWNLOAD_TIMEOUT = 10
DEFAULT_IMAGE_QUESTION = 'Клиент прислал скриншот без текста: что на нём и как решить проблему?'
UNAVAILABLE_IMAGE_NOTE = '(клиент приложил изображение, его не удалось посмотреть)'
MIMES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}
EXTENSIONS = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}


def unavailable_question(question: str) -> str:
    return (question.strip() or DEFAULT_IMAGE_QUESTION) + '\n\n' + UNAVAILABLE_IMAGE_NOTE


def usable_images(images: list[AiImage], config: Config) -> list[AiImage]:
    """Повторная проверка границ перед отправкой, включая вызовы API без загрузчика."""
    if not config.vision:
        return []
    return [
        image
        for image in images[: config.max_images]
        if image.mime in MIMES and 0 < len(image.data) <= config.max_image_bytes
    ]


def telegram_image_refs(message) -> list[str]:
    """Для фото выбираем самый большой доступный размер, помещающийся в лимит."""
    config = settings_view.snapshot()
    photos = getattr(message, 'photo', None) or []
    if photos:
        candidates = [photo for photo in photos if (photo.file_size or 0) <= config.max_image_bytes]
        if candidates:
            return [max(candidates, key=lambda photo: (photo.width * photo.height, photo.file_size or 0)).file_id]
        return []
    document = getattr(message, 'document', None)
    if document and (document.mime_type or '').startswith('image/'):
        if (document.file_size or 0) <= config.max_image_bytes:
            return [document.file_id]
    return []


def ticket_image_refs(messages) -> list[str]:
    """Только вложения клиента после последнего ответа поддержки, без дубля первого элемента галереи."""
    limit = settings_view.snapshot().max_images
    if not limit:
        return []
    last_support = max((i for i, message in enumerate(messages) if message.is_from_admin), default=-1)
    refs = []
    seen = set()
    for message in messages[last_support + 1 :]:
        items = [{'type': getattr(message, 'media_type', None), 'file_id': getattr(message, 'media_file_id', None)}]
        gallery = getattr(message, 'media_items', None)
        if isinstance(gallery, list):
            items.extend(gallery)
        for item in items:
            if not isinstance(item, dict) or item.get('type') not in {'photo', 'document'}:
                continue
            ref = item.get('file_id')
            if isinstance(ref, str) and ref and ref not in seen:
                refs.append(ref)
                seen.add(ref)
                if len(refs) >= limit:
                    return refs
    return refs


class _LimitedBuffer(BytesIO):
    """Размер проверяется и по реальным байтам, даже если Telegram не прислал file_size."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValueError('Изображение превышает лимит')
        return super().write(data)


def _mime(data: bytes, path: str) -> str | None:
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower())


async def fetch_telegram_images(bot, refs) -> list[AiImage]:
    """Не бросает ошибок загрузки; таймаут охватывает get_file и скачивание одного файла."""
    result = []
    try:
        config = settings_view.snapshot()
        if bot is None or not config.vision or not config.max_images or not config.max_image_bytes:
            return []
        seen = set()
        for ref in refs[: config.max_images]:
            if not isinstance(ref, str) or not ref or ref in seen:
                continue
            seen.add(ref)
            try:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                    file = await bot.get_file(ref)
                    if not file.file_path or (file.file_size or 0) > config.max_image_bytes:
                        continue
                    with _LimitedBuffer(config.max_image_bytes) as destination:
                        await bot.download_file(file.file_path, destination=destination, timeout=DOWNLOAD_TIMEOUT)
                        data = destination.getvalue()
                    mime = _mime(data, file.file_path)
                    if data and mime:
                        result.append(AiImage(data, mime))
            except Exception:
                # Исключение может содержать URL с токеном бота, путь или file_id: в лог его не передаём.
                continue
        logger.debug('Загружены изображения для ИИ', image_count=len(result))
    except Exception:
        logger.warning('Не удалось подготовить изображения для ИИ')
    return result
