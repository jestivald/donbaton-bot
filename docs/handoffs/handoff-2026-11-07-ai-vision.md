# Handoff V1 — скриншоты для ИИ-помощника

Дата: 07.11.2026. Исполнитель: Codex, задача V1 оркестратора Claude.
Рабочая копия: `donbaton-bot/ai-vision-20261107`, ветка `codex/ai-vision-20261107`, база `160be9da`.
Изменения оставлены без коммита. Сеть и серверы не использовались; боевые БД, `.env` и секреты не затрагивались.
Устройство исходного ИИ описано в [handoff ИИ-поддержки](handoff-2026-11-07-ai-support.md).

## Поведение

- Новые настройки категории `AI_SUPPORT`: `AI_SUPPORT_VISION_ENABLED=false`,
  `AI_SUPPORT_VISION_MAX_IMAGES=3`, `AI_SUPPORT_VISION_MAX_MB=5` (МиБ на картинку, верхняя граница 32).
  Зрение доступно у DeepSeek с `flash` в имени модели и у `openai_compatible`, где совместимость выбирает владелец.
  `deepseek-v4-pro` получает только текст с пометкой о недоступном изображении.
- `api.AiImage(data: bytes, mime: str)` — неизменяемый объект, байты исключены из `repr`.
  `api.answer(question, history, ctx, images=None)` сохраняет совместимость со старыми вызовами.
  Добавлена функция `api.vision_enabled()`. Существующие поля публичных dataclass не менялись.
- `images.py` загружает Telegram file_id через `get_file` и `download_file` только в память.
  На одну картинку — общий таймаут 10 секунд; размер проверяется до скачивания и при записи каждого блока.
  JPEG/PNG/GIF/WebP определяются по подписи или расширению, остальные форматы пропускаются.
  Ошибка файла не прерывает обработку остальных выбранных файлов и текстовый ответ.
  Проверка количества, размера и MIME повторяется перед формированием запроса модели.
- В личке фото и документы `image/*` без FSM проходят тем же фоновым путём, что текст (не более 12 задач).
  У фото выбирается самое большое разрешение, помещающееся в лимит размера.
  При выключенном зрении или ИИ вызывается прежний обработчик медиа с прежним ответом и клавиатурой.
  Память чата хранит подпись и ответ, без байтов и file_id. Для фото без подписи ядро использует вопрос по умолчанию.
  Альбом обрабатывается по одному сообщению: отдельный ответ на каждый элемент допустим по заданию.
- В тикетах используются одиночные `media_type`/`media_file_id` и галерея `media_items` (бот, REST, WS).
  Берутся только сообщения клиента после последнего ответа поддержки, повтор первого файла галереи исключается.
  Скачивание — через `maintenance_service._bot`. Тикет без текста с картинкой обрабатывается в `draft`/`auto`;
  действующие запреты после ответа человека/передачи и проверка устаревшего сообщения сохранены.
- В последнем user-сообщении — текст и `image_url` с `data:<mime>;base64,...`, `detail=high`.
  Системное правило запрещает исполнять инструкции на изображении и пересказывать ссылки, QR-коды и личные данные.
  `PROMPT_VERSION=20261107-3`. Байты, base64 и число изображений не добавляются в журнал запросов;
  число доступно только в structlog.

## Проверки

Команды из задания выполняются локально, без PostgreSQL и живых API.

- `.venv/bin/python -m pytest -q -p no:cacheprovider tests/services/ai_support tests/services/ai_support_integ tests/handlers/ai_support -m "not postgres and not bschek_live"`:
  **306 passed, 0 failed, 3 deselected**.
- `.venv/bin/ruff check .`: чисто; `.venv/bin/ruff format --check .`: 1600 файлов уже отформатированы.
- `.venv/bin/python -m pytest -q -p no:cacheprovider -m "not postgres and not bschek_live"`:
  **11096 passed, 7 failed, 5 skipped, 329 deselected** (161,93 с).
  Все 7 падений — `PermissionError: [Errno 1] Operation not permitted` при `socket.bind` на localhost в песочнице:
  - `tests/donbaton/test_lte_telemetry_producer.py::test_clients_require_one_distinct_credential_per_route`;
  - в `tests/external/test_yookassa_webhook.py`: `test_handle_webhook_success`,
    `test_handle_webhook_rejects_cf_connecting_ip_alone_behind_local_peer`,
    `test_handle_webhook_with_optional_signature`, `test_handle_webhook_accepts_canceled_event`,
    `test_handle_webhook_rejects_non_yookassa_ip_by_default`, `test_handle_webhook_skip_ip_check_bypasses_ip_gate`.
  Эти файлы и код их подсистем V1 не меняет; для успешного полного прогона нужна среда с разрешённым localhost bind.
  Логи запуска: `/tmp/ai-vision-targeted-pytest.log`, `/tmp/ai-vision-full-pytest.log`.

После изменения файлов справочник `docs/project_structure_reference.md` пересобирается
командой `.venv/bin/python scripts/generate_structure_reference.py`.

## Приватность и ограничения

Текст по-прежнему обезличивается до отправки. Изображения отправляются провайдеру **целиком и без обезличивания**:
провайдер видит ссылки подписки, QR-коды, ключи, данные оплаты, имена и контакты, если они присутствуют на скриншоте,
а у документов может получить и встроенные метаданные. Промпт ограничивает ответ, но не скрывает данные от провайдера
и не гарантирует защиту от всех инструкций, написанных на картинке. Локальный OCR/маскирование в V1 не реализованы.
Поэтому зрение по умолчанию выключено, риск указан в описании настройки.

Изображения не сохраняются подсистемой ИИ, не входят в память диалога и при передаче чата оператору не копируются в тикет.
Исходные вложения тикета по-прежнему хранятся существующим механизмом тикетов в Telegram/file_id.
В повторном вопросе модель помнит текст подписи и свой прошлый ответ, но не само изображение.
Проверка реального DeepSeek/Telegram и включение настройки остаются оркестратору: в V1 сеть запрещена.

Откат без пересборки: `AI_SUPPORT_VISION_ENABLED=false`. Миграций БД и изменений соседних рабочих копий нет.
