# Handoff — ИИ-помощник поддержки на DeepSeek API и пересмотр тикет-системы

**Дата:** 2026-11-07
**Исполнитель:** Claude (оркестратор) + субагенты Codex (`codex exec`, codex-cli 0.167.0: разведка A1–A5, реализация I1–I4, ревью R1)
**Репозиторий:** `/Users/admin/Documents/work/vpn/donbaton/forks/bedolaga-bot`
**Worktree:** `/Users/admin/Documents/work/_worktrees/donbaton-bot/ai-support-20261107`
**Ветка:** `codex/ai-support-20261107` от `bfe9b96d` (= `codex/upstream-5.0.0`, боевой образ
`donbaton/bedolaga:u500-f8a10d5a-20261021`); параллельные ветки исполнителей влиты merge-коммитами:
`codex/ai-support-integ-20261107` (интеграции), `codex/ticket-fixes-20261107` (тикеты).
**Основание:** владелец 07.11.2026 — «давай пересмотрим систему тикетов, и внедрим дипсик апи, что бы у нас был бот для ответов
на вопросы». Задача — `vpn/donbaton/tasks/2026-11-07-ai-support-deepseek.md`; пакет (разведка, задания и отчёты субагентов,
база знаний, «золотой набор», скрипты выкладки) — `local-ops/_claude/tools/donbaton-ai-support-20261107/`.

## Что сделано

**ИИ-помощник** (новые файлы, апстрим их не содержит):

| Где | Что |
|---|---|
| `app/services/ai_support/` | ядро: `api.py` (контракт — единственный интерфейс для интеграций), `provider.py` (DeepSeek / OpenAI-совместимый Chat Completions, `thinking: disabled`, единый дедлайн, один повтор), `redaction.py` (обезличивание до отправки), `output_guard.py` (фильтр служебного в ответе), `knowledge.py` (база знаний из инфо-страниц), `prompts.py`, `tools.py` (только чтение и только текущий клиент), `limits.py` (бюджет/лимиты), `repository.py` (журнал), `retention.py`, `settings_view.py`, `pricing.py` |
| `app/handlers/ai_support.py` | свободный текст в личке бота → ответ ИИ (фоновая задача, ≤ 12 одновременно), кнопки «👍/👎/👤 Оператор», память диалога в Redis, передача оператору (тикет или дописывание в открытый) |
| `app/services/ai_support_tickets.py` | очередь по событиям `ticket.created` / `ticket.message_added` (+ прямой хук мобильного WS), режимы `off/draft/auto` |
| `app/services/ticket_reply_service.py` | публикация ответа поддержки с повторной проверкой под блокировкой строки тикета и доставкой во все каналы |
| `app/services/ai_support_notify.py`, `app/handlers/admin/ai_support.py` | уведомления админам (копии ответов, «🙋 нужен оператор», черновики с «Отправить клиенту»), экран «🤖 ИИ-помощник» в настройках поддержки бота |
| `migrations/alembic/versions/0135_ai_support.py` | таблица `ai_support_requests` (журнал, бюджет, идемпотентность по `trigger_message_id`, `handoff_done_at`) |
| `scripts/ai_support_seed_kb.py`, `scripts/ai_support_eval.py` | загрузка Markdown-базы знаний в скрытые FAQ-страницы `ai-*`; прогон «золотого набора» |

Правки существующих файлов: `app/config.py` и `app/services/system_settings_service.py` (категория `AI_SUPPORT`), `app/database/models.py`
(модель `AiSupportRequest` в конце), `app/bot.py` (регистрация перед `common`), `main.py` (старт/стоп), `app/cabinet/routes/support_ws.py`
(хук после commit ответа клиента), `message_id` в payload `ticket.created` (`crud/ticket.py`, `cabinet/routes/tickets.py`),
кнопка в `app/handlers/admin/support_settings.py`, локали.

**Тикет-система** (`d03f4528`, `7fe26e0a`): ответ клиента из кабинета теперь `open` + сброс SLA (раньше `pending` — мимо очереди
Telegram-админки и SLA); `is_user_reply_blocked` вместо несуществующего свойства; одно событие в Web API; правильный `old_status`;
перенос апстрима `37eee359` + `6151f60c` (видео и галереи вложений в Telegram-тикетах).

## Как работает (кратко)

- Всё выключено, пока нет `AI_SUPPORT_ENABLED=true` и ключа `AI_SUPPORT_API_KEY` (секрет, маскируется; правится в админке
  или `PUT /settings/AI_SUPPORT_API_KEY` Web API). **В `.env` ключи `AI_SUPPORT_*` не класть** — env блокирует правку из админки.
- В провайдер уходит только обезличенный текст: почта, телефоны, ссылки подписки и ключи подключения, UUID/HWID, карты, IP,
  ники, токены → заглушки. База знаний и инструкции владельца — доверенный текст, не обезличиваются.
- База знаний: все активные FAQ-страницы кабинета + все страницы со слагом `ai-…` (их держат выключенными — клиенты не видят).
  Кэш 5 мин или кнопка «🔄 Обновить базу» в админке.
- Автор ответов ИИ в тикетах — существующий админ (`AI_SUPPORT_AUTHOR_TELEGRAM_ID`, по умолчанию первый из `ADMIN_IDS`, найденный
  в базе). Синтетического пользователя нет намеренно: `ticket_messages.user_id` — `ON DELETE CASCADE`, автоочистка неактивных
  удалила бы его вместе с ответами. Ответы ИИ отличаются по журналу (`reply_message_id`) и префиксу «🤖 ИИ-помощник».
- `auto`: ИИ отвечает, пока в тикете нет ответа человека и выполненной передачи, не более `AI_SUPPORT_TICKET_MAX_AI_REPLIES`;
  передача — тикет остаётся `open` (SLA работает), админам «🙋 Нужен оператор».

## Перенос при слиянии с апстримом

- Миграция `0135` — наша; при переносе v5.2.0 его `0132…0140` перенумеровать в `0136…0144` (правило — handoff 4.13.0/5.0.0).
- Конфликты возможны во вставках: `app/config.py`, `system_settings_service.py`, `models.py` (конец файла), `app/bot.py`, `main.py`,
  `support_ws.py`, `crud/ticket.py` (payload `ticket.created`, `old_status`), `cabinet/routes/tickets.py`, `webapi/routes/tickets.py`,
  локали. Перенос медиа `37eee359`/`6151f60c` при слиянии с 5.2.0 совпадёт с апстримом — брать апстрим, сверив патчи форка.
- После слияния: `pytest tests/services/ai_support tests/services/ai_support_integ tests/handlers/ai_support` и `make docs-structure`.

## Откат

- Без пересборки: `AI_SUPPORT_ENABLED=false` (всё прежнее поведение), или точечно `AI_SUPPORT_BOT_CHAT_ENABLED=false` / `AI_SUPPORT_TICKET_MODE=off`.
- Образ: вернуть `donbaton/bedolaga:u500-f8a10d5a-20261021`; **сначала** `alembic downgrade 0134` новым образом (старый не знает `0135`),
  затем смена образа — `rollback.sh` пакета. Таблица `ai_support_requests` при этом удаляется.
- База знаний: удалить инфо-страницы `ai-*`.

## Выкладка (07.11.2026, МСК)

| | было | стало |
|---|---|---|
| образ бота | `donbaton/bedolaga:u500-f8a10d5a-20261021` | **`donbaton/bedolaga:u500-3a868abe-20261107`** (id `caf81882…`), Alembic **0135** |
| образ кабинета | `donbaton/cabinet:u1801-8c737a56-20261001` | **`donbaton/cabinet:u1801ai-7cecb0ff-20261107`** (бандл `index-BIMa1Gpb.js`, старые ассеты сохранены) |
| база знаний | — | инфо-страницы 3–7 `ai-start`, `ai-tabs`, `ai-payments`, `ai-account`, `ai-trouble` (FAQ, выключены): 28 статей, ~4 тыс. токенов |

- 19:47–19:58 сборка и полный pytest на PostgreSQL 18.6 в тестовом образе: падения — только известные среды раннера
  (справочник структуры ×3 — нет git; `test_pg_upgrade_prompt` ×3 и `test_compose_postgres_service` ×2 — нет docker compose;
  `single_skip` ×2 — боевые `DONBATON_*`). Локально без PostgreSQL: 11034 passed, 0 failed.
- 19:58:56–19:59:23 `cutover.sh APPLY=1`: гейт тишины 0, дамп БД бота 3,0 МБ (`/root/backups/20261107-ai/bot-db-195856.dump`),
  healthy за 23 с, миграция `0134 → 0135` за 0,2 с, `/health/unified` 200, traceback 0, вебхук pending 0.
- 20:00:05 база знаний (`seed_kb.sh APPLY=1`); публичные `/api/cabinet/info-pages` страниц `ai-*` не отдают (404 / нет в списке).
- 20:00:16 кабинет (`deploy-cabinet.sh APPLY=1`): healthy, новый бандл на `lk` и сайте, старый — 200.
- ИИ после выкладки **выключен** (`AI_SUPPORT_ENABLED=false`, ключа нет). Включение — после ключа владельца и прогона «золотого набора».


## H1 — усиление после красной команды J1 (07.11.2026)

Изменения подготовлены в `codex/ai-vision-20261107` поверх `8f8bd795`; без сети, коммита и выкладки.

- Чат: максимум одна фоновая задача на клиента; повтор получает локализованное сообщение ожидания.
  Общий предел 12 задач сохранён. Передача учитывает блокировку активного тикета под `FOR UPDATE`;
  отдельная квота — 3 передачи за сутки МСК по `ai_support_requests.handoff_done_at` под блокировкой клиента.
  Резерв журнала коммитится вместе с записью тикета; повтор не обновляет время выполненной передачи.
- Тикеты: один слот очереди на тикет с обновлением до последнего `message_id`, максимум 3 слота на клиента
  (включая выполняемые). Источники событий и мобильный WS передают серверный `user_id` владельца.
  Актуальность проверяется запросом одного ID; в память выбирается хвост из 32 сообщений. Ответ человека
  в старой части истории по-прежнему выключает автоответ: отдельный SQL-поиск по всей переписке.
- Выход: нормализация NFKC/невидимых символов/HTML/percent-кодирования, разрешительный список ссылок,
  повторные проверки после снятия разметки, детекторы служебных утечек и финансовых обещаний.
  Запрещённые ссылки заменяются на «(ссылка удалена)», обещания — на передачу `money` с `HANDOFF_TEXT`.
  Ответ ограничен 2000 знаками. Резюме имеет отдельный валидатор, лимит 500 и пометку «[авто, не проверено]».
- Вход: короткие секреты после контекстных меток и percent-кодированная почта в URL маскируются;
  до обезличивания вопрос ограничивается 4000 знаками. Промпт `20261107-4` определяет границы доверия:
  посторонние поручения и джейлбрейки получают короткий отказ; проблемы сервиса сохраняют эскалацию.
- Контракт `api.py` и миграции не менялись. Мягкое ограничение бюджета (F05) не входит в H1 и сохраняется.
  Лимиты фоновых задач/очереди действуют внутри процесса; живая модель и PostgreSQL здесь не проверялись.

Проверки H1 в этой песочнице:

- Регрессии `-k h1` на исходном коде HEAD: **109 failed, 10 passed**; после восстановления исправлений
  весь целевой набор `tests/services/ai_support tests/handlers/ai_support tests/services/ai_support_integ`
  с `-m "not postgres"`: **425 passed, 3 deselected**, ошибок нет.
- Полный `.venv/bin/python -m pytest -q -p no:cacheprovider -m "not postgres and not bschek_live"`:
  **11215 passed, 7 failed, 5 skipped, 329 deselected**. Все 7 ошибок — `PermissionError: [Errno 1]
  Operation not permitted` на `socket.bind`: `test_clients_require_one_distinct_credential_per_route`
  в `tests/donbaton/test_lte_telemetry_producer.py`; в `tests/external/test_yookassa_webhook.py` —
  `test_handle_webhook_success`, `test_handle_webhook_rejects_cf_connecting_ip_alone_behind_local_peer`,
  `test_handle_webhook_with_optional_signature`, `test_handle_webhook_accepts_canceled_event`,
  `test_handle_webhook_rejects_non_yookassa_ip_by_default`, `test_handle_webhook_skip_ip_check_bypasses_ip_gate`.
- `.venv/bin/ruff check app tests` и `.venv/bin/ruff format --check app tests` — чисто (1558 файлов).
  `scripts/generate_structure_reference.py` пересобран; `--check` и `git diff --check` проходят.
