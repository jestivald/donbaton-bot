# Handoff — слияние форка ДонБатона с upstream v5.0.0 (вместе с v4.16.0)

**Дата:** 2026-10-01
**Исполнитель:** Claude (субагент A «bot-merge» оркестратора `donbaton-da`), только локальная работа
**Репозиторий:** `/Users/admin/Documents/work/vpn/donbaton/forks/bedolaga-bot`
**Worktree:** `/Users/admin/Documents/work/_worktrees/donbaton-bot/upstream-5.0.0`
**Ветка:** `codex/upstream-5.0.0`
**База:** `d394b293` = `codex/lte-gate-off` (боевой образ `donbaton/bedolaga:u4150-d394b293-20260928`,
Alembic `0130` — леджер `STATE.md`, баннер 28.09).
**Собирать из `3cb5f0df`** — последний коммит с кодом и тестами. Прод-образ тот же, что из `f6eadc93`
(`3cb5f0df` меняет только `tests/`, а `tests/` и `docs/` исключены `.dockerignore`). Всё после
`3cb5f0df` — этот документ.
**Ничего не запушено, SSH не было, серверы и панель не тронуты, образ не собирался.**

Апстрим: `v4.15.0 877690a7 → v4.16.0 e31831f5 → v5.0.0 5bda3c56`, 74 коммита, линейно
(`merge-base(d394b293, v5.0.0)` = `v4.15.0`); **261 файл, +26 415/−2 362**. Конфликтов — **11 файлов**
(4 с кодом, 7 мест; 5 локалей; `uv.lock`; справочник структуры). Миграций — **четыре**, все
переномерованы. Правок форка после слияния — **две** (§8.2, §8.4).

---

## 1. Что сделано

```
<этот документ> docs(handoff): передача по слиянию с upstream v5.0.0
3cb5f0df test(donbaton): цикл LTE в SQLite-лаборатории идёт по одной подписке
f6eadc93 fix(donbaton): автопродление Cashera не оформляется для Wi-Fi/LTE — как Platega и Lava
98f4dfec Merge upstream bedolaga bot v5.0.0 into codex/upstream-5.0.0
d394b293 ← база: codex/lte-gate-off (боевой код)
```

---

## 2. Что принесли релизы

### 2.1 v4.16.0 (70 коммитов)

* **Cashera** — новая платёжная система: пополнение (СБП, карты, крипта, CryptoBot), свой экран
  оплаты с QR (H2H), автопродление подпиской Cashera (`sbp_recurring`), возвраты/чарджбэки
  списывают баланс, сверка по API, защита от двойного зачисления. Таблицы `cashera_payments`,
  `cashera_subscriptions`. **Выключено по умолчанию** (§4).
* **Живое главное меню** — фон раз в 15 мин (под нагрузкой до 6 ч) перерисовывает последнее
  rich-меню, если поменялись трафик/статус/устройства/баланс. **Выключено** (§5.2).
* **DPI//CHECKER** — проверки доступности из регионов через внешний `dpichecker.st`: раздел админки
  кабинета, мониторы, вебхук `/dpichecker/webhook` (подпись HMAC). Таблица `dpichecker_actions`,
  право `dpichecker:*`. **Выключено** (§5.5).
* **Рассылки по правилам** — аудитория из правил/фильтров/исключений, считается в момент отправки;
  колонка `broadcast_history.audience`. Доступно с имеющимся правом `broadcasts:*` (§5.3).
* **Внешний антифрод** (`ABUSE_API_*`) — бот только спрашивает внешний сервис о вердикте по
  `telegram_id` и показывает его. **Выключено** (§5.6).
* **Web API:** `GET /users/{id}/activity`, `POST /users/{id}/notify`, вложения обращений все
  сразу (§5.4).
* Исправления: Tribute (не терять/не зачислять дважды), мультитариф после сброса триала, пул Redis
  (`REDIS_MAX_CONNECTIONS=200`, ожидание вместо «Too many connections»), напоминания на PostgreSQL,
  документы кабинета на языке по умолчанию, ссылки на файлы за прокси (`app/utils/public_url.py`),
  ошибки stdlib-логгеров в админ-чат целиком (§5.7), алерты CodeQL.

### 2.2 v5.0.0 (4 коммита, `4b787516` + релиз)

* **PostgreSQL 18 в `docker-compose.yml` апстрима** + сторож в entrypoint контейнера postgres и
  скрипт переноса `scripts/pg-upgrade.sh` (`make pg-upgrade`) — §6.
* **Python 3.14** в образе (`python:3.14-slim`), `requires-python = '==3.14.*'`.
* **Зависимости:** pyjwt 2.13.0 → **2.15.1**, SQLAlchemy 2.0.52 → **2.1.1**, starlette 1.6.0 → 1.7.0,
  uvicorn 0.52.4 → 0.54.0, alembic 1.19.2 → 1.20.0 (§6.3).
* Исправления: регистрация на шаге правил/политики с заголовками из редактора (пустые `<b></b>`),
  BSCHEK не запускает платную пробу дважды, массовое удаление подписок без 500.

### 2.3 Что из этого касается ДонБатона

| Изменение | Касается нас | Что делать |
|---|---|---|
| Cashera (по умолчанию выкл.) | только при включении | сторож Wi-Fi/LTE добавлен (§8.4); включение — решение владельца |
| Миграции `0131`–`0134` | да, при выкладке | только DDL новых таблиц/колонки, данные не трогают (§7) |
| Python 3.14, SQLAlchemy 2.1 | да — новый рантайм | полный pytest на PG 15 и 18 (§9); сборка на ядре |
| PostgreSQL 18 | **нет, если не трогать compose** | код работает на PG 15 (§6.1); перенос — отдельное решение |
| Живое меню (выкл.) | только при включении | §5.2 |
| `dpichecker:*` в системную роль Admin | да, на первом старте | роль Marketer не меняется (§5.1) |
| Рассылки по правилам | да — у Marketer (`broadcasts:*`) | списки людей и поиск — только с `users:read` (§5.3) |
| Web API notify/activity | только при включённом Web API | §5.4 |
| Ошибки логгеров в админ-чат целиком | да, если включены уведомления об ошибках | §5.7 |
| `/dpichecker/webhook` всегда смонтирован | да — новая публичная ручка бота | без ключа отвечает 401/503 (§5.5) |
| Документы кабинета на языке по умолчанию | да, визуально | — |

### 2.4 Что клиенты увидят без включения флагов

* Правила, политика и оферта в кабинете — на языке по умолчанию, если перевода нет или он пустой
  (`a31f0ec8`, `bf6236b8`).
* Регистрация больше не падает на шаге правил/политики, если текст оформлен заголовками в редакторе
  кабинета (5.0.0: `app/utils/telegram_html.py` снимает пустые пары тегов, `app/handlers/start.py`).
* Неверная сумма пополнения — бот объясняет, как ввести заново (`2cdbe86f`).
* Ссылки на медиа тикетов/аватарки в кабинете — с `https` и внешним хостом за прокси (`f733cc29`).
* Под всплеском нагрузки Redis не отвечает «Too many connections» — запрос ждёт соединение до 10 с.
* Tribute — платёж не теряется и не зачисляется дважды (если Tribute включён).
* Меню бота, покупка и LTE — без изменений: кнопки Cashera появляются, только если Cashera настроена,
  а сетевым подпискам не появляются вовсе (§8.4).

---

## 3. Шов форка цел

* **Все добавленные форком строки на месте.** Сверка по всем 85 файлам дельты форка
  (`git diff v4.15.0 d394b293 -- app main.py migrations`): каждая добавленная форком строка есть в
  файле слияния; «расхождения» — 5 строк, это последний ключ LTE в пяти локалях, получивший запятую.
  Удалённые форком строки не вернулись (6 «повторов» — синтаксис новых блоков Cashera апстрима:
  `InlineKeyboardButton(` и т. п.).
* **Счётчики символов шва** (`app/`, 56 символов: 27 из §8.3 handoff 4.15 + гейт LTE, окно оплаты,
  оплата заранее, отказы, модули форка) — **на слиянии `98f4dfec` все 56 равны базе**; после
  `f6eadc93` отличается один: `network_category` 213 → 216 (сторож Cashera, §8.4).
  Выборочно: `_adoptable_user_panel_id` 4, `is_managed` 53, `apply_managed_policy` 8,
  `validate_user_tariff_purchase` 23, `DONBATON_` 178, `DONBATON_LTE_TELEMETRY_GATE_ENABLED` 10,
  `DONBATON_PAYMENT_WINDOW` 29, `payment_window` 34, `legacy_terms_keep_devices` 7,
  `_refund_failed_charge` 2, `tariff_change_refusal` 12.
* Гейт LTE выключен флагом (`DONBATON_LTE_TELEMETRY_GATE_ENABLED=False`), окно оплаты, оплата
  заранее легаси, admin-sync fallback, fixes-purchase/fixes-admin — не тронуты; их тесты зелёные (§9).

---

## 4. Переменные окружения и настройки

**Обязательных новых переменных нет. Все новые функции по умолчанию выключены** (`app/config.py`):

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `CASHERA_ENABLED` | `False` | Cashera (нужны ещё `CASHERA_API_KEY` и `CASHERA_API_SECRET`) |
| `CASHERA_API_KEY` / `CASHERA_API_SECRET` | `None` | ключ `pk_…` / секрет `sk_…`; вебхук смонтирован, только если оба заданы (`app/webserver/payments.py:2004`) |
| `CASHERA_H2H_ENABLED` | `False` | свой экран оплаты (QR) |
| `CASHERA_RECURRENT_ENABLED` | `False` | автопродление Cashera (`is_cashera_recurrent_enabled` = enabled и флаг) |
| `CASHERA_ACTIVE_METHODS` | `sbp,card` | методы; `CASHERA_INLINE_METHODS=False` |
| `CASHERA_MIN/MAX_AMOUNT_KOPEKS` | `10000` / `10000000` | пределы пополнения |
| `CASHERA_WEBHOOK_PATH` | `/cashera-webhook` | путь вебхука |
| `CASHERA_BASE_URL` | `https://api.cashera.cash/api/v1` | |
| `MAIN_MENU_LIVE_ENABLED` | `False` | живое меню (ещё нужен `MAIN_MENU_RICH_ENABLED`, по умолчанию `True`) |
| `DPICHECKER_ENABLED` | `False` | DPI//CHECKER (ещё нужен `DPICHECKER_API_KEY`) |
| `DPICHECKER_API_URL` | `https://dpichecker.st/api/v1` | |
| `DPICHECKER_REFERENCE_SUBSCRIPTION` | `None` | эталонная подписка для проверок |
| `ABUSE_API_ENABLED` | `False` | внешний антифрод (ещё `ABUSE_API_URL`, `ABUSE_API_KEY`) |
| `REDIS_MAX_CONNECTIONS` / `REDIS_POOL_TIMEOUT` | `200` / `10.0` | пул Redis ждёт свободное соединение до 10 с |

Cashera, живое меню и DPI//CHECKER редактируются в админке (`system_settings`); `.env.example` прямо
говорит не задавать их в `.env` — значение из `.env` блокирует правку из админки (жёсткое правило
проекта о ключах, управляемых из админки). Наши `DONBATON_*` не менялись.

---

## 5. Новые функции и ДонБатон

### 5.1 RBAC-бутстрап

На каждом старте `rbac_bootstrap_service._ensure_preset_roles` дописывает системным ролям
недостающие права из кода (`app/services/rbac_bootstrap_service.py:262-276`). В 4.16/5.0 добавлено
**одно** право — `dpichecker:*`, и только роли **Admin** (`:191`; реестр `permission_service.py:82`).
**Marketer не меняется** (`campaigns/broadcasts/promocodes/promo_offers/promo_groups:*`, `stats:read`,
`sales_stats:read`, `pinned_messages:*`, `user_reminders:*`, `wheel:*` — как в 4.15), Cashera прав
не добавляет. Дизайнер (`Marketer` с 22.09) новых разделов не получает, но рассылки по правилам ему
доступны через уже имеющийся `broadcasts:*` (§5.3).

### 5.2 Живое главное меню (`MAIN_MENU_LIVE_ENABLED=False`)

* Цикл стартует всегда (`monitoring_service.py:367-371`), но при выключенном флаге раз в 15 мин
  просыпается и засыпает (`live_menu_service.py:66-84`).
* Если включить: отслеживаются только чаты, где бот показал rich-меню после включения (ключ
  `live_menu:{chat_id}` с TTL); проход — `get_all_users_stream(size=1000)` всей панели одним
  запросом, затем для каждого чата `traffic_used_gb` из панели в БД (`:199-211`, `updated_at` не
  трогает) и **правка сообщения, только если изменился отпечаток** (`rich_menu.py:338`: язык,
  баланс, статус, срок, тариф, триал, **текст расхода трафика**, лимит устройств). Пауза 0,2 с между
  чатами (≤5 правок/с), при 429 панели, рассылке, очереди вебхука, занятом пуле БД, лаге цикла или
  `RetryAfter` Telegram — проход прерывается, интервал удваивается до 360 мин.
* **Шторма от нашего LTE-цикла не будет:** цикл квот (30 с) пишет в панель сквады/статус/лимиты, а
  не счётчики трафика, и меню он не трогает; живое меню само ходит раз в ≥15 мин. Но при включении
  меню почти всех активных клиентов будет перерисовываться каждые 15 мин (меняется текст расхода) —
  правки тихие, без уведомлений. Решение — за владельцем; держать выключенным, пока не нужно.

### 5.3 Рассылки по правилам

`app/cabinet/routes/admin_broadcasts.py:453-520`: предпросмотр аудитории — право `broadcasts:read`,
**список людей в предпросмотре — только при `users:read`** (у Marketer его нет — получит только
число); поиск людей `GET /audience/users` — `require_permission('broadcasts:read', 'users:read')`
(проверяются все, `dependencies.py:425`). Правила хранятся в `broadcast_history.audience` (миграция
`0132`), получатели пересчитываются в момент отправки.

### 5.4 Web API

`POST /users/{user_id}/notify` (`app/webapi/routes/users.py:324`) — отправить клиенту сервисное
сообщение в Telegram и на почту от имени бота; `GET /users/{user_id}/activity` (`:827`) — лента
активности (транзакции, события подписки, промокоды, обращения, входы в кабинет…); вложения
обращений списком. Все — `Security(require_api_token)`, как остальной Web API. Держатель токена Web
API получает возможность писать любому клиенту от имени бота и читать его активность — экспозицию
Web API (включён ли, куда слушает) проверяет оркестратор на ядре.

### 5.5 DPI//CHECKER

Сервис работает только при `DPICHECKER_ENABLED` **и** ключе (`services/dpichecker/service.py:97-99`,
`monitor_watch.py:66`). Вебхук `POST /dpichecker/webhook` **смонтирован всегда**
(`webserver/unified_app.py:211-216`): без подписи `X-DPIChecker-Signature` — 401, без ключа сервиса —
503 (`webserver/dpichecker_webhook.py:41-62`), тело ≤ `MAX_BODY_BYTES`. При включении проверка
«из подписки пользователя» отправляет ключи/ссылки подписки во внешний сервис — раскрытие адресов
серверов третьей стороне; решение владельца.

### 5.6 Внешний антифрод (`ABUSE_API_ENABLED=False`)

`app/services/abuse_api_service.py`: при включении бот шлёт `GET {ABUSE_API_URL}/violations/summary`
и `/violations?telegram_id=…` с заголовком `X-API-Key` (адрес задаёт оператор; контракт совместим с
Remnawave Admin API v3). Наружу уходит только `telegram_id`; бот ничего не блокирует сам — показывает
клиенту присланное предупреждение (`GET /cabinet/abuse-status`) и оператору вердикт с историей
(`/cabinet/admin/users/{id}/abuse`, `users:read`). Недоступность сервиса = «претензий нет».

### 5.7 Ошибки логгеров в админ-чат целиком (`1b0433a3`)

`app/logging_handler.py:302-309`: в очередь отправки уходит снимок словаря события, поэтому ошибки
stdlib-логгеров (aiogram, APScheduler, SQLAlchemy) теперь приходят с текстом и traceback. Вырезаются
только токены Telegram (`_redact_telegram_secrets`, `:385-431`). **Ошибки SQLAlchemy могут нести
значения параметров** (почта, Telegram ID, внешние id платежей) — они окажутся в истории админ-чата,
если уведомления об ошибках включены. Секретов приложения в типовых ошибках нет; риск — ПДн в чате.

### 5.8 Ссылки на файлы за прокси (`f733cc29`)

`app/utils/public_url.py`: схема и хост внешней ссылки (CSV DPI//CHECKER, медиа тикетов и новостей,
медиа Web API) берутся из `X-Forwarded-Proto`/`X-Forwarded-Host`/`Host` запроса **без проверки
источника**. Ссылку получает сам запросивший (админ/клиент/держатель токена) — подменить её другому
человеку этим нельзя; на ядре стоит проверить, что nginx перезаписывает `X-Forwarded-Host`.

---

## 6. PostgreSQL 18, Python 3.14, зависимости

### 6.1 Нужен ли PostgreSQL 18 коду — нет

* В коде и миграциях нет возможностей PG 16+/17+/18 (поиск по `uuidv7`, `JSON_TABLE`, `MERGE`,
  `RETURNING OLD/NEW`, виртуальных колонок, `ANY_VALUE`, `NULLS NOT DISTINCT` и т. п. — пусто;
  «UUIDv7» встречается только в комментарии про id платежа cisPay). Новые миграции — обычный DDL.
* Проверено: полный pytest с **PostgreSQL 15.19** и `-m postgres` с **18.6** (§9); репетиция
  миграций на обоих (§7.3); сценарий цикла LTE с параллельностью 10 на обоих (§8.2).
* **Защита «без переноса база не запустится» живёт в `docker-compose.yml` апстрима**, а не в коде:
  entrypoint контейнера postgres = `docker/postgres/pg-upgrade-guard.sh` — если новый том PG 18
  пуст, а в старом томе (`postgres_data`, смонтирован `/mnt/pg-legacy-data:ro`) есть `PG_VERSION`,
  контейнер выходит с подсказкой `make pg-upgrade`. Бот версию сервера не проверяет. **Если на ядре
  оставить свой `/opt/bedolaga/docker-compose.yml` и менять в нём только `image:` бота/кабинета, PG 15
  продолжит работать, сторож не участвует.**

### 6.2 `scripts/pg-upgrade.sh` (`make pg-upgrade`) — что предполагает

Запуск из папки бота; рядом `docker-compose.yml` и `.env`; `docker compose config` **уже** должен
показывать `image: postgres:18-alpine` (иначе выход «сначала обновите бота»); сервис базы
`postgres`, приложения `bot`; тома с ключами compose `postgres_data` (старый) и `postgres18_data`
(новый) — по меткам проекта или `<проект>_<ключ>`; учётка — из окружения сервиса postgres
(`POSTGRES_USER/PASSWORD/DB`). Делает: останавливает бота и базу (простой), поднимает временный PG
старой версии на старом томе, `pg_dump` в `./backups/postgres-upgrade-<время>/` (`chmod 700`, проверка
места ≈ размер базы ×2), восстанавливает в PG 18 на новом томе, сверяет число строк всех таблиц и
значения последовательностей, поднимает базу через compose, сверяет ещё раз, запускает бота. Старый
том не трогает (откат — вернуть compose 4.16). **К чужому compose применим, только если имена сервисов
и ключи томов совпадают** — макет `/opt/bedolaga` оркестратор сверяет на ядре; иначе перенос вручную
(`pg_dump -Fc` → новый контейнер 18 → `pg_restore` → сверка) или оставаться на 15 (поддержка до
ноября 2027).

### 6.3 Python 3.14 и зависимости

* `Dockerfile`: меняется только база `python:3.13-slim` → `python:3.14-slim` и `ARG VERSION`;
  пользователь `app` uid/gid 1000, `HEALTHCHECK` на `:8080/health`, `CMD python main.py` — как были.
  На ядре понадобится `docker pull python:3.14-slim` (и `ghcr.io/astral-sh/uv:0.10.8`, если не в кэше).
* **pyjwt 2.15.1:** 2.14.0 — усиление проверки HMAC-ключа против подсунутого публичного ключа (класс
  «путаница алгоритмов», GHSA-r6x4-923q-g947, GHSA-ffc3-869f-jxw9, GHSA-p4g4-x82p-q773 и др.);
  2.15.0 — `RecursionError` от глубоко вложенного payload заворачивается в `DecodeError`, разбор JWKS
  строже. У нас jwt — только в коде апстрима: `app/cabinet/auth/jwt_handler.py` (HS256 своего секрета)
  и `telegram_auth.py:178-315` (OIDC `id_token` Telegram по JWKS, алгоритм привязан к типу ключа).
  Форк `app/cabinet/auth/` не меняет. Правок не нужно.
* **SQLAlchemy 2.1:** `MissingGreenlet` теперь заворачивается в `StatementError` — апстрим завёл
  `app/database/errors.py::is_missing_greenlet`; в коде форка `except MissingGreenlet` нет. Задело нас
  только чередование параллельных сессий на одном соединении SQLite в тестах (§8.2).
* starlette 1.7, uvicorn 0.54, alembic 1.20 — правок форка не потребовали.
* В `uv.lock` апстрима осталась версия проекта `4.16.0` при `pyproject` `5.0.0` — `uv lock` поправил
  только эту строку; `uv lock --check` проходит.

---

## 7. Миграции Alembic

### 7.1 Переномеровка (правило §4а handoff 4.13.0: двигаем апстрим)

Наша голова — `0130` (`0130_lte_quota_notices`, на проде). Сдвиг апстрима теперь **+3**.

| У апстрима | У нас | Что делает |
|---|---|---|
| `0128_dpichecker_actions` | **`0131`**, `down_revision='0130'` | `CREATE TABLE dpichecker_actions` (если нет) + 3 индекса; downgrade — `DROP TABLE` (если есть) |
| `0129_broadcast_audience` | **`0132`**, `down='0131'` | `ADD COLUMN broadcast_history.audience JSON NULL` (если нет); downgrade — `DROP COLUMN` |
| `0130_create_cashera_payments` | **`0133`**, `down='0132'` | `CREATE TABLE cashera_payments` (FK users SET NULL, transactions) |
| `0131_add_cashera_subscriptions` | **`0134`**, `down='0133'` | `CREATE TABLE cashera_subscriptions` + индексы, 2 unique, частичный unique `uq_cashera_subscriptions_alive` |

Вслед: `tests/migrations/test_0128_dpichecker_actions.py` → `test_0131_…` (путь, имя модуля,
`revision/down_revision`), `docs/broadcast_audiences.md`, справочник структуры. `alembic heads` —
`0134` (одна), `alembic history -r 0118:` — линейна. **Данные существующих таблиц миграции не трогают.**

### 7.2 Откат схемы

* Старый образ на схеме `0134` — **crash-loop** (`Can't locate revision identified by '0134'`,
  воспроизведено, §7.3).
* `downgrade 0130` новым образом снимает 4 ревизии: таблицы `dpichecker_actions`, `cashera_payments`,
  `cashera_subscriptions`, колонку `broadcast_history.audience`. Пока Cashera/DPI выключены, таблицы
  пусты; `audience` заполнится, только если успеют создать рассылку по правилам.
* `SKIP_MIGRATION=true` со старым образом — схема остаётся `0134`, старой ORM лишние таблицы и
  nullable-колонка не мешают.

### 7.3 Репетиция (локально, синтетика — не копия прода)

На **PostgreSQL 15.19 и 18.6**: схема базы `d394b293` её же кодом (`run_alembic_upgrade` на пустой
базе = `create_all` + сторожа + `stamp head` → `0130`, 128 таблиц), затем код слияния:
`upgrade head` — 4 шага `0130→0134`, таблицы/колонка/частичный индекс на месте; повтор — 0 шагов;
**старый код на `0134` — `Can't locate revision identified by '0134'`**; `downgrade 0130` — 4 шага,
ничего не осталось; старый код на `0130` — `0130 (head)`; снова `upgrade head` — 4 шага.

`alembic check` (модели слияния против схемы после миграций) — расхождения только в таблицах Cashera
**апстрима**: модель объявляет `unique/index` (индексы `ix_*`), миграция создаёт unique-ограничения
`uq_*` и нет индекса на `id`; `cashera_payments.is_paid` в БД `NOT NULL DEFAULT false`, в модели
nullable. Уникальность обеспечена в обоих вариантах; так у любой установки апстрима; фича выключена.
Базовая линия (`d394b293`) — расхождений нет.

---

## 8. Конфликты и правки форка

### 8.1 Конфликты (подробно — в сообщении `98f4dfec`)

1. **`app/cabinet/routes/admin_users.py` ×2** — апстрим добавил отмену привязки Cashera. `change_tariff`:
   у нас поиск тарифа и отказ `tariff_change_refusal` стоят до отмены привязок — порядок сохранён,
   отмена Cashera третьей внутри `if tariff_id != subscription.tariff_id`, дубль поиска тарифа снят.
   `reset`: отмена Cashera после Platega/Lava, затем наш admin-sync fallback и отказ без id панели.
2. **`app/handlers/subscription/autopay.py` ×2** — наши `_managed_subscription`/`_sbp_autopay_rows` +
   `_cashera_recurring_rows` апстрима; суточный экран — обе группы строк.
3. **`app/handlers/subscription/tariff_purchase.py` ×2** — экраны подтверждения: наш хелпер
   `_sbp_purchase_rows(category=…)` (кнопка Cashera вошла в него автослиянием).
4. **`app/services/monitoring_service.py`** — `_lte_notice_tasks` (наш) + `_live_menu_task`.
5. **Локали ×5** — наши `LTE_QUOTA_*` и `CASHERA_*` апстрима (по 2344 ключа, без дублей).
6. **`uv.lock`** — сторона апстрима + `uv lock` (только версия проекта); **справочник** — пересобран.

### 8.2 Правка теста: SQLite-лаборатория цикла LTE (`3cb5f0df`)

После слияния детерминированно падал `tests/donbaton/test_lte_quota_notices.py::
test_first_pass_over_the_limit_marks_everyone_and_sends_nothing`: из трёх `low_notice_sent_at`
сохранялись две. На базе (Python 3.13, SQLAlchemy 2.0.52) — зелёный.

* Причина — лаборатория: `memory_session` (`sqlite+aiosqlite:///:memory:`, `StaticPool` в обеих
  версиях) отдаёт **всем сессиям одно соединение**, а `run_quota_cycle` гоняет подписки 10
  параллельными сессиями (`lte_quota_service.py:1877`) — они делят одну транзакцию SQLite, и на 2.1
  чередование коммитов теряет отметку. С шириной семафора 1 — все три отметки на месте.
* **Прод не задет:** тот же сценарий (3 клиента, лимит 2, боевая параллельность 10, отдельные
  соединения) на настоящих **PostgreSQL 15.19 и 18.6 — 5/5 прогонов каждый**: все отметки на месте,
  второй проход пуст (одноразовый тест, в ветку не входит).
* Правка: в `cycle_lab` модулю `lte_quota_service` подставляется `asyncio` с семафором ширины 1
  (класс `_SerialCycleAsyncio`, всё остальное — настоящий модуль). Код цикла и утверждения теста не
  менялись.

### 8.3 Правки тестов апстрима

Только переименование `test_0128_dpichecker_actions.py` → `test_0131_…` (§7.1).

### 8.4 🔴 Семантический шов: Cashera обходила запрет внешней автооплаты Wi-Fi/LTE (`f6eadc93`)

Форк запрещает внешнее автопродление тарифам и подпискам Wi-Fi / Wi-Fi + LTE (их продление — с
баланса через леджер LTE): `_ensure_legacy_recurring` в `payment/platega.py` и `payment/lava.py` в трёх
точках входа + кнопки бота. Cashera — третий провайдер, без сторожа она при включённом
`CASHERA_RECURRENT_ENABLED` оформила бы сетевой подписке внешнее автосписание мимо леджера.

* `app/services/payment/cashera.py:23-31` — тот же `_ensure_legacy_recurring`; вызовы в
  `create_cashera_recurrent_subscription` (`:712`, до БД), `enable_cashera_recurring` (`:1132`, до
  флага), `purchase_tariff_with_cashera_recurring` (`:1149`). Кабинетная ручка
  `/cashera-recurrent/purchase` отвечает 400 с текстом отказа — проверено тестом; `/enable` — тот же
  `except ValueError → 400` (по коду, `subscription_modules/cashera_recurrent.py:74-76`).
* `app/handlers/subscription/autopay.py` — `_cashera_recurring_rows` не предлагает сетевой подписке
  «Автопродление через Cashera» (живую привязку отключить можно); колбэк `cashera_recurring_enable`
  для сетевой — отказ `DONBATON_RECURRING_UNAVAILABLE` до сервиса.
* Кнопка `tariff_cashera:` на экранах подтверждения уже в `_sbp_purchase_rows(category=…)`.
* Отмена привязок Cashera стоит во всех местах, где форк/апстрим отменяют Platega и Lava (сверено по
  файлам: `admin_users.py` 6/5/5 — шестая Platega-отмена у отдельной СБП-ручки, как у апстрима).
* Тесты: Cashera в параметризации `tests/donbaton/test_recurring_guards.py` (22 → 33), новый
  `tests/donbaton/test_recurring_ui_cashera.py` (17). **Мутации:** без сторожа сервиса падают 10,
  без сторожа меню — 2, без сторожа колбэка — 4.
* `docs/DONBATON.md` («Правила»): где стоит сторож и что новый провайдер апстрима получает его при слиянии.

---

## 9. Результаты проверок

Всё из корня worktree, uv 0.12.12. База — независимый `git clone --shared` на `d394b293` в
scratchpad (CPython 3.13.15, SQLAlchemy 2.0.52). Слияние — CPython **3.14.7**, SQLAlchemy **2.1.1**.
PostgreSQL — одноразовые кластеры Homebrew `postgresql@15` (15.19) и `postgresql@18` (18.6), keg-only,
данные во временном каталоге scratchpad, только `127.0.0.1` (TCP, без unix-сокета), `fsync=off`.
**Не боевые 15 и не боевой `.env`** — это не замена прогону на ядре.
Кластеры остановлены после прогонов. Формулы Homebrew оставлены (keg-only, служб нет); убрать —
`brew uninstall postgresql@15 postgresql@18 krb5` и `rm -rf /opt/homebrew/var/postgresql@{15,18}`
(кластеры по умолчанию, которые создаёт сама формула).

| Проверка | База `d394b293` | HEAD |
|---|---|---|
| `uv run pytest -q` (без PG) | **10106 passed, 247 skipped, 0 failed** (exit 0) | **10608 passed, 328 skipped, 0 failed** (exit 0) |
| `pytest` целиком, `TEST_DATABASE_URL` → PG 15.19 | — | **10918 passed, 17 skipped**, 1 failed* |
| `pytest -m postgres`, PG 18.6 | — | **313 passed, 10623 deselected, 0 failed** (exit 0) |
| `uv run ruff check .` | — | All checks passed |
| `uv run ruff format --check .` | — | 1547 files already formatted |
| `generate_structure_reference --check` | — | актуален |
| `uv lock --check` | — | проходит |
| `alembic heads` / `history -r 0118:` | `0130` / линейна | **`0134`** / линейна |
| `git diff --check d394b293 HEAD` | — | чисто |
| Репетиция миграций PG 15 / PG 18 (§7.3) | — | обе зелёные |

\* единственное падение — `test_structure_reference_is_current`: во время прогона в дереве появился
этот документ (ещё не в справочнике); после `make docs-structure` тест зелёный, справочник входит в
коммит документа. 17 пропусков с PG — состав не разбирался (в 4.15 было столько же: BSCHEK без
живого ключа, ручные, `apple_iap`).

Первый прогон на коммите слияния (до правок §8.2/§8.4): 10578 passed, 328 skipped, **2 failed** —
`test_first_pass_over_the_limit…` (§8.2) и `test_structure_reference_is_current` (во время прогона в
дереве появился новый тестовый файл; после `make docs-structure` — актуален).

---

## 10. Что осталось на серверную часть

SSH к ядру — только обёрткой с одним ControlMaster-сокетом (`dbcore_ssh_cm.sh`). `mcp__remnawave__*` —
чужая панель.

1. **Сборка:** `git archive 3cb5f0df` → бокс; `chown -R 1000:1000` дерева; `bot-full-build.sh`
   (`TAG_PROD=donbaton/bedolaga:u500-3cb5f0df-2026MMDD`, `TAG_TEST=…-test-…`). Нужен `python:3.14-slim`.
2. **Тесты на ядре** — `run_tests_full.py` (одноразовый PG 15 боевой версии); ожидаемые падения —
   известные 5 класса 17.09/27.09 (боевые `DONBATON_*` в окружении раннера, нет git в образе).
3. **Репетиция на копии боевой БД** (`pg_dump -Fc` → `postgres:15-alpine` во internal-сети, бот против
   копии не стартует): `0130 → 0134`, повтор no-op, `downgrade 0130`, старый образ на `0134` —
   crash-loop. Сосчитать до/после: строки во всех таблицах не меняются (миграции — только DDL).
4. **Сверить на ядре:** макет `/opt/bedolaga/docker-compose.yml` (сервис/том/образ базы) — для решения
   по PG 18; не стоит ли в `.env` что-то из `CASHERA_*`/`DPICHECKER_*`/`MAIN_MENU_LIVE_ENABLED`/
   `ABUSE_API_*`; включён ли Web API и куда слушает; включены ли уведомления об ошибках в админ-чат;
   проксируется ли наружу `/dpichecker/webhook`; перезаписывает ли nginx `X-Forwarded-Host`.
5. **Выкладка** — `bot-deploy-v2.sh` (дамп БД перед выкладкой); контейнер сам сделает
   `alembic upgrade head` → `0131…0134`.
6. **Приёмка:** `alembic current` = `0134`; в логе одна строка `LTE telemetry gate disabled…`, нет
   traceback; RBAC — в логе `Added new permissions to system role` с `dpichecker:*` только для Admin;
   LTE-сквад и PATCH-фон как до выкладки; вебхук ЮKassa проходит; в админ-чат — «остановка/запуск».
7. **Кабинет 1.80.x** — после бота (кабинет 1.80 ждёт бота 4.16 для Cashera/DPI/рассылок по правилам).
8. **PostgreSQL 18** — отдельной операцией с простоем, по решению владельца (§6.1–6.2).

---

## 11. Порядок отката

* **Путь Б (основной):** пока новый образ запущен —
  `docker compose -f /opt/bedolaga/docker-compose.yml run --rm --no-deps --entrypoint /app/.venv/bin/alembic bot downgrade 0130`,
  проверить `current` = `0130`, затем `image:` → `donbaton/bedolaga:u4150-d394b293-20260928` и
  `up -d --no-deps --no-build --pull never bot`. Теряются только новые таблицы/колонка (§7.2).
  🔴 Боевой образ `d394b293` — с выключенным гейтом LTE; откатываться ещё дальше (на `0f5e6a4a`) —
  только после возврата таймера якоря (README `tools/donbaton-lte-gate-off-20260928/`).
* **Путь А:** восстановить дамп, снятый перед выкладкой (теряет всё после выкладки, включая платежи).
* **Путь В (аварийный):** старый образ с `SKIP_MIGRATION=true`; потом снять переменную.

---

## 12. Риски

1. 🟡 **Новый рантайм** (Python 3.14, SQLAlchemy 2.1) — локально всё зелёное, но боевой прогон и
   поведение под нагрузкой видны только на ядре. Первый же признак — traceback в логе бота.
2. 🟡 **`dpichecker:*` у роли Admin** на первом старте; админам виден раздел DPI//CHECKER (без ключа не
   работает).
3. 🟡 **Рассылки по правилам у Marketer** — через `broadcasts:*`; без `users:read` только числа.
4. 🟡 **Ошибки логгеров в админ-чат с полным текстом** — возможны ПДн в истории чата (§5.7).
5. 🟡 **Новая всегда смонтированная ручка** `/dpichecker/webhook` (401/503 без ключа).
6. 🟡 **Включение Cashera/DPI/антифрода/живого меню** — отдельные решения владельца: внешние сервисы
   получают данные (платежи; ключи подписок; `telegram_id`).
7. **PostgreSQL 15** — поддержка до ноября 2027; апстрим дальше может начать требовать 18.
8. **Следующее слияние снова упрётся в номера миграций** — сдвиг теперь +3.

---

## 13. Вопросы владельцу

1. **PostgreSQL 18:** переносить сейчас (простой ~1–2 мин + дамп/сверка) или остаться на 15 с этим
   релизом (рекомендую остаться и перенести отдельной операцией).
2. **Cashera / DPI//CHECKER / внешний антифрод / живое меню** — включать ли что-то (по умолчанию всё
   выключено; при включении — §5).

---

## 14. Не проверено

* Сборка образа на Python 3.14 и прогон тестов **на ядре** (локально — macOS arm64).
* Репетиция на **копии боевой БД** — только синтетика (§7.3).
* PG 18 — только `-m postgres` и репетиция; полный прогон на 18 не делался.
* Живое поведение новых функций (Cashera, DPI, живое меню, антифрод) — выключены и не включались.
* Кабинетная сторона Cashera для Wi-Fi/LTE во фронте 1.80 — бэкенд отвечает 400, как это покажет
  фронт, не смотрелось.
