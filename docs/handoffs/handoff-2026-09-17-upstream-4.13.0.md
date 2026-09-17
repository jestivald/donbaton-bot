# Handoff — слияние форка ДонБатона с upstream v4.13.0

**Дата:** 2026-09-17
**Репозиторий:** `/Users/admin/Documents/work/vpn/donbaton/forks/bedolaga-bot`
**Worktree:** `/Users/admin/Documents/work/_worktrees/donbaton-bot/upstream-4.13.0`
**Ветка:** `codex/upstream-4.13.0`
**Код релиза:** `7640bfa0` — последний коммит, меняющий содержимое образа. Всё, что после
него на ветке, это правки самого документа передачи (актуальный HEAD — `git rev-parse HEAD`).
**База:** `64285f5d` (ветка `codex/referral-fix` = боевая линия `codex/upstream-4.12.1`
плюс реф-фикс, который выкатывался параллельно этой работе).
**Ничего не запушено, серверы не тронуты, образ не собирался. Рабочая копия чистая.**

Апстрим принёс **3 коммита, 103 файла, +2198/−495**. Конфликтов — **два**.
Правок тестов форка не потребовалось **ни одной**.

---

## 1. Что сделано

```
7640bfa0 style(start): привести перенос строки к ruff format
a0ef0a79 Merge upstream bedolaga bot v4.13.0 into codex/upstream-4.13.0
64285f5d ← база: codex/referral-fix
0891aa04    codex/upstream-4.12.1 (боевой код 0b9c28c3)
```

`style(start)` — не следствие слияния: строка пришла с реф-фиксом и не проходила
`ruff format --check` ещё на его собственной ветке. Поведение не меняется, но без
этой правки проверка формата красная и на нашей ветке тоже. **Если реф-фикс будут
править отдельно — эту же строку стоит поправить и там**, иначе конфликт форматирования
повторится при следующем слиянии.

---

## 2. Что принёс релиз v4.13.0

### 2.1 Срок подписки — по календарю, время — в поясе оператора

Новый модуль `app/utils/subscription_time.py` (123 строки) даёт три разных ответа
на три разных вопроса, которые раньше все считались как `(end_date - now).days`:

| Функция | Ответ на вопрос | Кто зовёт |
|---|---|---|
| `local_days_until` | «истекает сегодня / завтра / через N дн.» — календарные дни в `settings.TIMEZONE` | меню бота, карточки кабинета, админка |
| `days_left_rounded_up` | «N дн.» без даты рядом: 20 ч → 1, а не 0 | остаток докупок трафика в кабинете |
| `time_left` / `format_time_left` | точный остаток «1 дн. 23 ч.» | экран подписки, «Продлить» |
| `ends_within_days` | «пора ли продлевать за N дней» — сравнение длительностей | **автопродление** |

Симптом, который это лечит: целая часть суток у остатка 1 д 23 ч давала «1», и тексты
читали единицу как «истекает завтра», хотя истекала подписка послезавтра. Тот же
`.days` заставлял порог автопродления «за 3 дня» срабатывать уже при 3 д 23 ч остатка —
почти на сутки раньше.

Заодно `app/utils/formatters.py::format_datetime`/`format_date` перестали печатать
UTC голым `strftime` и пошли через `format_local_datetime`. **Это видимое изменение для
оператора: 74 вызова по `app/`, все даты и время на админских экранах и в уведомлениях
сдвигаются с UTC на `TIMEZONE` (у нас `Europe/Moscow`, +3 ч).** Для моментов между
21:00 и 24:00 UTC сдвигается не только время, но и ДАТА. Панель показывает МСК, так что
после выкладки бот и панель наконец сойдутся — но первые сутки это будет выглядеть как
«у всех поменялись сроки».

### 2.2 Тег панели: бот теперь владеет полем целиком

См. §5 — это главное поведенческое изменение релиза для нас.

### 2.3 Подписки, перенесённые из панели, получают `subscriptions.remnawave_id`

Импорт одиночного режима писал id аккаунта панели только в `users.remnawave_id`, а
`subscriptions.remnawave_id` оставлял пустым; экраны по выбранной подписке читают строго
id подписки — отсюда «0 устройств» у перенесённых людей. Исправлено в трёх точках
(`link_subscription_panel_identity` в `remnawave_service._create_subscription_from_panel_data`,
`._update_subscription_from_panel_data`, `admin_users.sync_user_from_panel`) плюс разовый
бэкфил миграцией — см. §4.

### 2.4 Вебхуки платёжных систем

Одно правило определения отправителя для ЮKassa и PayPear
(`app/external/yookassa_webhook.py::resolve_webhook_client_ip`) и **обязательное
fail-closed подтверждение платежа запросом в API ЮKassa в ЛЮБОМ режиме** — см. §6.

### 2.5 Кабинет (бэкенд; фронт — отдельный репозиторий)

* Сегмент «В грейсе» в списке пользователей (`in_grace`), сортировка в обе стороны
  (`sort_order=asc|desc`) и сортировка по концу временного доступа (`grace_until`).
* Манифест и иконки приложения по адресу бота: `/branding/manifest.webmanifest`,
  `/branding/app-icon/{size}.png`, `/branding/app-icon/maskable/{size}.png`
  (`app/cabinet/utils/app_icon.py`). Нужно, чтобы Chrome на Android ставил кабинет как
  WebAPK: Google скачивает манифест и иконки по обычным URL, `data:` ему недоступен.
  **Заработает только после обновления фронта кабинета** — в нашем боте это пока просто
  три новые ручки.

---

## 3. Переменные окружения

**НОВЫХ ПЕРЕМЕННЫХ НЕТ ВООБЩЕ.** Диff `app/config.py` и `.env.example` между v4.12.1 и
v4.13.0 — только комментарии; ни одного нового поля `Settings`. Зависимости не менялись
(`pyproject.toml` и `uv.lock` — только номер версии).

Но два **существующих** ключа стоит задать осознанно перед выкладкой (§5):

| Переменная | Сейчас (вероятно) | Что делает после 4.13.0 |
|---|---|---|
| `PAID_SUBSCRIPTION_USER_TAG` | не задан → `None` | тег, который бот пишет платному аккаунту панели. **Не задан = бот пишет `tag: null` и снимает тег, проставленный руками** |
| `TRIAL_USER_TAG` | не задан → `None` | то же для триала |

Альтернатива обеим — `tariffs.panel_tag` у каждого тарифа (колонка есть с миграции 0121,
редактируется в кабинете и в телеграм-редакторе); тег тарифа побеждает оба глобальных.

---

## 4. Цепочка миграций Alembic — правило переномеровки

**Столкновение (второе подряд).** Апстрим положил
`migrations/alembic/versions/0124_subscription_panel_identity_backfill.py`
(`revision = '0124'`, `down_revision = '0123'`). У нас номер `0124` занят ещё прошлым
слиянием (`0124_subscription_grace_overlay_expire_at`), боевая схема = `0125`.

**Решение.** Переномерована апстримовская миграция: файл, `revision`, `down_revision`
и строки `Revision ID`/`Revises` в докстроке → **`0126`**, `down_revision = '0125'`.
Ссылка на имя файла в `tests/database/test_panel_import_subscription_identity_postgres.py`
поправлена вслед (там же переименован `spec_from_file_location('migration_0126', …)`).

**Итоговая цепочка, ровно одна голова (`alembic heads` → `0126`, `alembic history` без
развилок):**

```
0118 (апстрим 4.8.0)
 → 0119 donbaton_lte_quotas               (наша, применена на проде)
 → 0120 legacy_lte_compatibility          (наша, применена на проде)
 → 0121 tariff_panel_tag_and_trial_days   ┐
 → 0122 subscription_grace_tail_expire_at │ переномерованы прошлым слиянием,
 → 0123 subscription_grace_session_open   │ применены на проде 16.09
 → 0124 subscription_grace_overlay_expire_at │
 → 0125 user_trial_reset_at               ┘  ← боевая схема СЕЙЧАС
 → 0126 subscription_panel_identity_backfill (head, переномерована этим слиянием)
```

### 4а. ПРАВИЛО ПЕРЕНОМЕРОВКИ ДЛЯ СЛЕДУЮЩИХ СЛИЯНИЙ

Переномеровывается **всегда апстрим, наши ревизии не трогаются никогда** — прод перестал
бы находить свою текущую ревизию в `alembic_version`. Порядок действий, по одной
апстримовской миграции за раз, в порядке её собственной цепочки:

1. `git mv migrations/alembic/versions/<NNNN>_<имя>.py migrations/alembic/versions/<MMMM>_<имя>.py`,
   где `<MMMM>` = наш текущий head + 1 (и далее +1 на каждую следующую).
2. В файле поправить **четыре** места: `Revision ID:` и `Revises:` в докстроке,
   `revision: str = '<MMMM>'`, `down_revision = '<предыдущая наша>'`.
3. `grep -rn '<NNNN>_<имя>' --include='*.py' --include='*.md'` — тесты апстрима
   загружают файлы миграций по ПУТИ (как `test_tariff_panel_tag_schema_parity.py` и
   `test_panel_import_subscription_identity_postgres.py`), поправить ссылки и имена
   модулей в `spec_from_file_location`.
4. `make docs-structure` — справочник структуры перечисляет файлы миграций поимённо.
5. Проверить: `alembic heads` = одна строка, `alembic history -r 0118:` без развилок.
   Локально это требует переменных окружения:
   `BOT_TOKEN=1:x ADMIN_IDS=1 DATABASE_URL="sqlite+aiosqlite:///./tmp.db" uv run alembic heads`.

Альтернатива (перевести наши ревизии на нечисловые идентификаторы) требует ручной правки
`alembic_version` на проде и по-прежнему не делалась.

### 4б. Что именно бэкфиллит 0126 и безопасно ли это у нас

```sql
UPDATE subscriptions
SET remnawave_id = (SELECT u.remnawave_id FROM users u WHERE u.id = subscriptions.user_id)
WHERE subscriptions.remnawave_id IS NULL
  AND NOT (subscriptions.status = 'pending' AND subscriptions.is_trial)
  AND EXISTS (SELECT 1 FROM users u WHERE u.id = subscriptions.user_id AND u.remnawave_id IS NOT NULL)
  AND (SELECT count(*) FROM subscriptions s2 WHERE s2.user_id = subscriptions.user_id) = 1
  AND NOT EXISTS (SELECT 1 FROM subscriptions s3 JOIN users u ON u.id = subscriptions.user_id
                  WHERE s3.remnawave_id = u.remnawave_id)
```

Никаких DDL: одна `UPDATE`, копирующая `users.remnawave_id` в `subscriptions.remnawave_id`
там, где ответ однозначен. У нас это **до 534 из 612 активных подписок** с пустой колонкой.

**Разбор по нашей схеме — вредить нечему:**

* **Уникальность не нарушится.** `ix_users_remnawave_id` — UNIQUE (миграция 0104), то есть
  двум разным пользователям один и тот же id панели не принадлежит; условие
  «у человека ровно одна подписка» отсекает две строки одного пользователя; последнее
  условие отсекает id, который уже держит чья-то строка. Частично уникальный
  `uq_subscriptions_remnawave_id` (`WHERE remnawave_id IS NOT NULL`) в этой `UPDATE`
  сломаться не может.
* **Идемпотентна.** Повторный `upgrade` не находит строк (первое условие уже ложно).
  Таблиц может не быть — тогда ранний `return`.
* **Адресация панели у нас не меняется.** В одиночном режиме `subscription_service`
  по-прежнему берёт `user.remnawave_id`
  (`remnawave_id = subscription.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id`),
  а значение мы кладём то же самое. Покупка, продление, смена тарифа, LTE-политика —
  без изменений.
* **Что реально начнёт работать:** экраны, читающие id подписки БЕЗ проверки режима —
  список устройств (`app/cabinet/routes/admin_users.py:2743`, сейчас отдаёт пустой ответ)
  и сброс трафика по выбранной подписке (`:2909`, сейчас 400 «User has no panel account»).
  Массовые «отключить в панели» (`:3225`, `:3331`) режимом защищены и сейчас, поведение
  у них не меняется.
* **Наш fallback `_adoptable_user_panel_id` (ветка admin-sync-fallback) остаётся на месте**
  (`admin_users.py:410-432`, 4 ссылки) и продолжает страховать строки, которые миграция
  намеренно пропустит — у кого больше одной подписки. Для забэкфиленных строк он просто
  перестанет вызываться: условие входа — `not subscription.remnawave_id` (`:452`).
* **Legacy-подписки** миграция не отличает от остальных и не должна: она не трогает ни
  `original_paid_until`, ни леджер, ни квоты — только адрес аккаунта панели.
* **Долив продолжится и без миграции:** апстрим добавил `link_subscription_panel_identity`
  в путь импорта, так что оставшиеся пустые строки заполнит первый же проход
  синхронизации панель→бот (там страж `panel_id_is_free_for`, а не «ровно одна подписка»).

**`downgrade` — `pass` (комментарий: «снятая привязка вернула бы 0 устройств»).**
То есть `alembic downgrade 0125` проходит, но данные остаются. Для отката на боевой образ
4.12.1 это безвредно: колонка там уже была, и наш fallback читает её первой, подставляя то
же значение. **Но сам факт «downgrade ничего не откатывает» надо держать в голове: путь Б
отката (§9) от 0126 до 0125 — no-op, а не восстановление.**

---

## 5. 🔴 Теги панели — главное поведенческое изменение

### Что изменилось в коде

| Файл | Было | Стало |
|---|---|---|
| `app/services/panel_sync/payload.py:142-145` (`update_kwargs`) | `if self.tag is not None: kwargs['tag'] = self.tag` | `kwargs['tag'] = self.tag` — **всегда**, и `None` уезжает как `null` |
| `app/services/panel_sync/payload.py:91-93` (`create_kwargs`) | `if self.tag is not None` | `if self.tag` — у нового аккаунта снимать нечего |
| `app/external/remnawave_api.py:951,981` (`update_user`) | `tag: str \| None = None`; `if tag is not None` | `tag: str \| type(...) \| None = ...`; `if tag is not ...` — «не передан» и «снять» теперь разные вещи |
| `app/services/panel_sync/writer.py:306,330` (`patch_panel_account`) | то же | то же |

Тег считает `app/services/panel_sync/tags.py:19-35::resolve_panel_user_tag`:

```
tariff.panel_tag (нормализованный)  →  победил
иначе is_trial  →  settings.get_trial_user_tag()          (TRIAL_USER_TAG)
иначе           →  settings.get_paid_subscription_user_tag() (PAID_SUBSCRIPTION_USER_TAG)
```

### Ответ на вопрос «пишет ли он пустой тег поверх ручного»

**Да, пишет.** Если у тарифа `panel_tag` пуст (у нас он пуст: колонка появилась миграцией
0121 только вчера, никто её не заполнял) И глобальные `TRIAL_USER_TAG` /
`PAID_SUBSCRIPTION_USER_TAG` не заданы, то `resolve_panel_user_tag` возвращает `None`,
и в PATCH уходит `"tag": null` — панель снимает тег, проставленный администратором руками.
Это не побочный эффект, а намеренный контракт апстрима; он закреплён тестами
`tests/services/panel_sync/test_payload.py::test_update_always_carries_the_tag_so_a_stale_one_is_cleared`
и `test_tags.py::test_without_a_configured_tag_the_account_gets_none`.

### Где именно это сработает (и как быстро)

`tag` уезжает только там, где PATCH идёт полным набором полей либо где `tag` явно внесён
в узкий набор (`only_fields`):

| Путь | `only_fields` | Тег уедет? | Когда срабатывает |
|---|---|---|---|
| `panel_sync/runner.py:134` ← `remnawave_service.sync_users_to_panel` — кнопка **«Из бота в панель»** | нет (полный PATCH) | **ДА, по ВСЕМ подпискам разом** | одно нажатие админа |
| `subscription_service.py:638-648` `update_remnawave_user` | `{…, 'tag', …}` | **ДА** | каждая покупка, продление, смена тарифа |
| `subscription_service.py:454` `_create_or_update_remnawave_user` | нет | **ДА** | создание/обновление аккаунта подписки |
| `cabinet/routes/admin_users.py:500` `_sync_subscription_to_panel` | нет | **ДА** | «в панель» и «продлить» из кабинета |
| `handlers/admin/users.py:4343` поштучный синк | `fields \| PANEL_ACCOUNT_METADATA_FIELDS` (`tag` внутри) | **ДА** | кнопка в боте |
| `handlers/admin/users.py:5145` | нет | **ДА** | массовые операции админки |
| `monitoring_service.py:905-917` рутинный проход | набор без `tag` | нет | каждые несколько минут |
| `panel_sync/managed.py:109` политика Wi-Fi/LTE | `POLICY_FIELDS` (без `tag`) | нет | каждый пересчёт квоты |
| `patch_panel_account` из `middlewares/auth.py` | `tag` не передан (`...`) | нет | на каждом апдейте |
| **Полная синхронизация панель→бот** | — | нет: с 4.10.0 она только читает | расписание |

То есть без нажатия «Из бота в панель» теги будут осыпаться постепенно — по мере покупок,
продлений и админских действий. **С нажатием — все ~2400 аккаунтов за один проход.**

### Рекомендация

Настройки-выключателя у этого поведения нет: оно безусловное. Варианты, по убыванию
предпочтительности:

1. **Задать `tariffs.panel_tag` у всех четырёх управляемых тарифов** (7 Wi-Fi Личный,
   8 Wi-Fi Семейный, 9 Wi-Fi+LTE Личный, 10 Wi-Fi+LTE Семейный) через кабинет.
   Формат: до 16 символов, `A-Z 0-9 _` (например `WIFI_PERSONAL`, `WIFI_FAMILY`,
   `LTE_PERSONAL`, `LTE_FAMILY`). Тогда бот пишет осмысленный тег вместо `null`, а
   администратор видит в списке панели, кто на каком тарифе, — ровно то, ради чего это
   и делалось. `validate_tariff_definition` нашего форка `panel_tag` не запрещает.
2. **Дополнительно задать `PAID_SUBSCRIPTION_USER_TAG` и `TRIAL_USER_TAG`** в `.env` —
   страховка для подписок без тарифа (legacy, классические) и для триалов.
3. **ДО выкладки снять инвентарь тегов из панели** (`GET /api/users`, поле `tag`; либо
   ручка тегов панели) и сохранить рядом с релизным пакетом. Если ручные теги что-то
   значат для эксплуатации, восстановить их потом будет неоткуда.
4. Если ручные теги надо сохранить как есть — это локальный патч
   (вернуть `if self.tag is not None` в `payload.update_kwargs`), но тогда теряется
   исправление «триальный тег снимается после покупки». Не рекомендуется: расхождение с
   апстримом придётся тащить через все следующие слияния.

**Пока пункты 1–2 не сделаны, кнопку «Из бота в панель» нажимать нельзя.**

---

## 6. Вебхуки платёжных систем: что проверено

### 6.1 Определение адреса отправителя — наша схема работает без правок nginx

Новая `resolve_webhook_client_ip(remote, forwarded_for=, real_ip=, cf_connecting_ip=)`
(`app/external/yookassa_webhook.py:152-203`) заменила `resolve_yookassa_ip`. Правила:

* пир публичный и не из доверенных прокси → отправитель он сам, заголовки не читаются;
* пир из сетей Cloudflare → верим `Cf-Connecting-Ip`;
* пир локальный/приватный/доверенный прокси → `X-Forwarded-For` справа налево, первый
  недоверенный хоп; `Cf-Connecting-Ip` здесь **игнорируется** (за локальным прокси его
  мог выставить сам клиент); `X-Real-IP` — только если `X-Forwarded-For` нет вовсе;
* иначе `None` → 403.

**Наша схема:** `gw.synqlane.top` за Cloudflare, в server-блоке подключён
`snippets/real-ip-cloudflare.conf` (`set_real_ip_from` сети CF + `real_ip_header
CF-Connecting-IP`), затем `snippets/proxy-backend.conf` ставит
`X-Real-IP $remote_addr` и `X-Forwarded-For $remote_addr` (именно `$remote_addr`, а не
`$proxy_add_x_forwarded_for` — правка F11 из аудита 05.09), `proxy_pass` на
`127.0.0.1:8080`. Значит пир для бота — loopback (или адрес docker-моста), а в
`X-Forwarded-For` ровно один адрес — настоящий отправитель.

Прогнано на самой функции из ветки:

| Вход | Результат | Allowlist ЮKassa |
|---|---|---|
| `remote=127.0.0.1`, `XFF=185.71.76.5` | `185.71.76.5` | ✅ |
| `remote=172.18.0.1` (docker-мост), `XFF=185.71.76.5` | `185.71.76.5` | ✅ |
| `XFF` отсутствует, `X-Real-IP=185.71.76.5` | `185.71.76.5` | ✅ |
| `XFF="1.2.3.4, 185.71.76.5"` (подделка клиента + наш хоп) | `185.71.76.5` | ✅ |
| `remote=8.8.8.8` (мимо nginx) | `8.8.8.8` | ❌ 403 — как и надо |
| `XFF` только из приватных | `None` | 403 |

**Вывод: правки nginx не нужны, вебхуки ЮKassa проходить будут.** `location =
/yookassa-webhook` со своим `allow`-списком остаётся первым барьером и работает как
раньше. CryptoBot (HMAC `Crypto-Pay-API-Signature`) и Telegram Stars этим изменением не
затронуты вовсе; `_resolve_proxied_client_ip` в `app/webserver/payments.py` теперь зовёт
ту же функцию, но используется он только вебхуком PayPear (`payments.py:1354`), который
у нас не включён.

### 6.2 🔴 Подтверждение платежа в API ЮKassa стало ОБЯЗАТЕЛЬНЫМ

Было (`app/services/payment/yookassa.py`, 4.12.1): перезапрос платежа в API —
best-effort при включённом IP-гейте; таймаут/ошибка → берём статус из тела вебхука.
Обязательным он был только при `YOOKASSA_SKIP_IP_CHECK=true`.

Стало: `if remote_data is None: return False` **без условия на `YOOKASSA_SKIP_IP_CHECK`**
(`yookassa.py:1448-1460`). Бюджет вызова — 8 секунд. Нет ответа (404, таймаут, ошибка,
или клиент API не сконфигурирован) → обработка отклонена, ручка отвечает **500**, ЮKassa
повторит уведомление позже.

Требования, которые из этого следуют:

* **исходящий доступ с бокса к `api.yookassa.ru:443`** — в нашем `na_filter.nft`
  `chain output` с `policy accept`, egress открыт, отдельного правила не нужно;
* **`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` в `.env` должны быть валидны** — если
  клиент API не поднимется, зачислений не будет вовсе (в лог пойдёт «клиент API YooKassa
  не сконфигурирован — подтвердить платёж нечем»);
* при деградации API ЮKassa зачисление теперь откладывается до ретрая, а не проходит по
  телу уведомления. Это строго безопаснее, но означает задержку пополнения в такие
  минуты. **После выкладки посмотреть `/var/log/nginx/yookassa-webhook.log` и лог бота
  на предмет 500 и строки «API YooKassa не подтвердил платёж».**

---

## 7. Конфликты и как разрешены

### 7.1 `app/services/monitoring_service.py::_process_autopayments` (строка 1731)

Единственный содержательный конфликт, и он ровно в шве нашего хотфикса автопродления.

* **Апстрим:** заменил `days_before_expiry = (sub.end_date - current_time).days;
  if days_before_expiry <= min(sub.autopay_days_before or 3, 3): autopay_subscriptions.append(sub)`
  на `if ends_within_days(sub.end_date, min(...), current_time): append`.
* **Мы (`codex/autopay-legacy-renewal`):** развернули то же условие в guard-форму
  (`if days_before_expiry > порог: continue`), чтобы следом встала проверка
  `autopay_blocked_by_legacy` с уведомлением `_notify_autopay_legacy_terms`.

**Разрешение — новое сравнение в нашей форме:**

```python
if not ends_within_days(sub.end_date, min(sub.autopay_days_before or 3, 3), current_time):
    continue
# ... наш блок «перенесённые индивидуальные условия» без изменений ...
```

Почему так, а не наоборот. Обе стороны правят разные вещи: апстрим — **границу окна**,
мы — **что происходит внутри окна**. Смысл `ends_within_days` — сравнивать длительности,
а не целые сутки; в guard-форме это ровно отрицание. Уже истёкшие подписки (отрицательная
разница ≤ `timedelta(days=N)`) попадают в окно и в старой, и в новой версии — двухчасовое
окно «недавно истёкших», на которое опирается наш хотфикс, сохранено. Сужение окна на
сутки означает только, что до проверки `autopay_blocked_by_legacy` доезжает меньше
подписок; смысл запрета не меняется, а уведомление «перенесённые индивидуальные условия»
теперь приходит человеку на сутки позже — что вернее, потому что и само продление теперь
происходит в свой день.

Та же замена независимо приехала в `app/database/crud/subscription.py:1911`
(`get_subscriptions_for_autopay`) — там конфликта не было.

### 7.2 `docs/project_structure_reference.md`

Генерируется скриптом. Взята сторона апстрима, затем `make docs-structure`
(4956 строк); сторож актуальности зелёный и в `pytest`, и в CI-варианте
`uv run python -m scripts.generate_structure_reference --check`.

### 7.3 Чего конфликта НЕ было, хотя ждали

* **Реф-фикс (`64285f5d`).** `app/handlers/start.py`, `app/cabinet/schemas/auth.py`,
  `app/services/web_auth_service.py` апстрим не трогал совсем. В
  `app/cabinet/routes/auth.py` его правка легла в `_sync_subscription_from_panel_by_email`
  — другая функция, автослияние. `tests/donbaton/test_referral_link_handoff.py` — зелёный.
* **`app/services/lte_quota_service.py`, `pricing_engine.py`, `donbaton_catalog.py`,
  `legacy_*`, `tariff_squad_sync.py`, `panel_sync/managed.py`** — ни один из этих файлов
  слияние не изменило. LTE-циклы по 30 суток считает `lte_quota_service` своей
  арифметикой, календарные помощники апстрима туда не заходят.

### 7.4 Шов форка цел

Счётчики вызовов в `app/` совпадают с базой `64285f5d` **строка в строку по всем 21
проверенным символам**: `_adoptable_user_panel_id` 4, `is_managed` 48,
`needs_managed_policy` 13, `apply_panel_policy` 9, `apply_managed_policy` 8,
`patch_panel_policy` 6, `network_category` 165, `reproject_after_create` 6,
`initialize_quota` 7, `update_entitlement` 13, `validate_legacy_renewal` 15,
`is_paid_tariff_switch` 9, `_prepare_donbaton_subscription` 5, `_uses_lte_accounting` 4,
`autopay_blocked_by_legacy` 7, `legacy_purchase_recorded` 4, `describe_tariff_switch` 3,
`should_reset_devices_on_switch` 12, `validate_tariff_for_sale` 15,
`validate_subscription_transition` 6, `validate_user_tariff_purchase` 23.

---

## 8. Результаты проверок

Всё из корня worktree, venv собран `uv sync` (uv 0.12.12, CPython 3.13).
База мерялась в этой же среде — на одноразовом detached-worktree от `64285f5d`
(чужие worktree не трогались), потом он удалён.

| Проверка | База `64285f5d` | Код релиза `7640bfa0` |
|---|---|---|
| `uv run pytest -q` | **9292 passed, 163 skipped, 0 failed** | **9400 passed, 205 skipped, 0 failed** |
| `uv run ruff check .` | All checks passed | All checks passed |
| `uv run ruff format --check .` | **1 файл требовал правки** (`app/handlers/start.py`, пришло с реф-фиксом) | 1389 files already formatted |
| `alembic heads` | `0125` (одна голова) | **`0126` (одна голова)** |
| `alembic history -r 0118:` | линейна | линейна, без развилок |
| `generate_structure_reference --check` | актуален | актуален |

**+108 проходящих тестов, ни одного падающего, ни одного изменённого или ослабленного.**
Правок тестов форка не потребовалось вовсе — впервые за серию слияний.

**Пропущено 205, из них не прогнано из-за отсутствия контейнерного рантайма на маке —
188**: «`TEST_DATABASE_URL` не задан — тесты на настоящем PostgreSQL пропущены».
Рост 146 → 188 (+42) объясняется тремя новыми postgres-файлами апстрима:
`tests/database/test_panel_import_subscription_identity_postgres.py` (в нём и проверка
самой миграции 0126), `test_users_list_grace_filter_postgres.py`,
`test_users_list_sort_direction_postgres.py`. Остальные пропуски прежние:
12 — «`BSCHEK_LIVE_API_KEY` не задан», 5 — структурные (`apple_iap`,
«файл разобран вручную»).

Отдельно прогнано и зелено (2362 passed, 188 skipped): весь `tests/donbaton/`
(включая `test_autopay_legacy_renewal.py`, `test_lte_quota.py`, `test_lte_accounting.py`,
`test_tariff_switch.py`, `test_referral_link_handoff.py`), весь `tests/services/panel_sync/`,
весь `tests/database/`, `tests/webserver/`, `tests/external/test_yookassa_webhook.py`,
`tests/services/test_autopay_period.py`, `test_autopay_free_period.py`,
`test_expiry_asks_the_panel_first.py`, `tests/utils/test_subscription_time.py`.

---

## 9. Что осталось на серверную часть

Локально сделать нельзя — тут ничего из этого не делалось и делаться не должно было:
на боксе в это время шла выкладка предыдущего фикса.

1. **Полный прогон с PostgreSQL** — `run_tests_full.py` по рецепту
   `local-ops/_claude/tools/donbaton-autopay-20260916/README.md`. Закрывает 188 пропусков,
   в том числе единственную настоящую проверку миграции 0126
   (`test_panel_import_subscription_identity_postgres.py::test_migration_binds_only_unambiguous_rows`)
   и наши `test_lte_lock_postgres.py`, `test_lte_legacy_postgres.py`.
2. **Репетиция миграции на одноразовой копии боевой БД** — по образцу
   `local-ops/_claude/tools/donbaton-u4121-20260916/rehearse-migrations-u4121.sh`
   (свежий `pg_dump -Fc` → `postgres:15-alpine` во внутренней сети; бот против копии НЕ
   стартует, отобрал бы вебхук). Что проверить:
   * `alembic current` до = `0125`, после `upgrade head` = `0126`;
   * **сколько строк реально обновила 0126** — до и после:
     `SELECT count(*) FROM subscriptions WHERE remnawave_id IS NULL;`
     (ожидание: было ~534, станет заметно меньше; разница = число забэкфиленных);
   * повторный `upgrade head` — no-op, без ошибок;
   * `downgrade 0125` проходит (он no-op) и `alembic current` = `0125`;
   * `downgrade 0120` — полный откат схемы, как в прошлый раз;
   * боевой образ 4.12.1 на схеме 0126 — ожидается crash-loop
     `Can't locate revision identified by '0126'` (см. §10).
3. **Инвентарь тегов панели ДО выкладки** (§5, п. 3) и решение по `panel_tag` тарифов /
   `PAID_SUBSCRIPTION_USER_TAG` / `TRIAL_USER_TAG`.
4. **Сборка образа и выкладка** — `bot-full-build.sh` → `run_tests_full.py` →
   `bot-deploy-v2.sh`, тег вида `donbaton/bedolaga:u4130-7640bfa0-2026MMDD`.
5. **Резервная копия БД до выкладки** — основной путь отката опирается на неё.

---

## 10. Порядок отката

**«Просто вернуть старый образ» НЕ работает и даёт crash-loop** — как и в прошлый раз.
Контейнер на старте сам зовёт `alembic upgrade head` (`main.py:187-205` →
`app/database/migrations.py:161`), и при `ALLOW_MIGRATION_FAILURE != true` исключение
пробрасывается. В образе `u4121-0b9c28c3` нет файла ревизии `0126`, поэтому Alembic не
найдёт в `alembic_version` запись `0126`:

```
Can't locate revision identified by '0126'
```

Скрипт `0126` есть **только у нового образа** — значит, откатывать схему должен он, пока
ещё запущен.

### Путь А (основной) — восстановить резервную копию

```bash
docker compose -f /opt/bedolaga/docker-compose.yml stop bot
# восстановить дамп, снятый перед выкладкой (alembic_version вернётся к '0125')
# image: → donbaton/bedolaga:u4121-0b9c28c3-20260916
docker compose -f /opt/bedolaga/docker-compose.yml up -d --no-deps --no-build --pull never bot
```

### Путь Б — откатить схему новым образом, потом сменить образ

```bash
# 1. НОВЫМ образом (у него есть скрипт 0126):
docker compose -f /opt/bedolaga/docker-compose.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/alembic bot downgrade 0125
docker compose -f /opt/bedolaga/docker-compose.yml run --rm --no-deps \
  --entrypoint /app/.venv/bin/alembic bot current        # ожидается 0125
# 2. только теперь image: → donbaton/bedolaga:u4121-0b9c28c3-20260916 и
docker compose -f /opt/bedolaga/docker-compose.yml up -d --no-deps --no-build --pull never bot
```

🔴 **`downgrade 0126 → 0125` ничего не откатывает** (тело `downgrade` — `pass`):
забэкфиленные `subscriptions.remnawave_id` останутся. Для кода 4.12.1 это безвредно
(колонка там уже есть, значение то же, что он берёт из `users.remnawave_id`), но «схема
на 0125» после этого НЕ означает «данные как до выкладки». Если нужны именно прежние
данные — только путь А.

Для отката дальше, до образа `autopay-a21c7c55-20260916` и схемы `0120`, действует
прежний порядок из handoff 16.09 (§11) — `downgrade 0120` новым образом.

### Путь В (аварийный) — старый образ со `SKIP_MIGRATION`

```bash
SKIP_MIGRATION=true          # именно она, не ALLOW_MIGRATION_FAILURE
# image: → donbaton/bedolaga:u4121-0b9c28c3-20260916
docker compose -f /opt/bedolaga/docker-compose.yml up -d --no-deps --no-build --pull never bot
```

Схема остаётся на `0126`. Коду 4.12.1 она не мешает: 0126 не добавляет ни колонок, ни
таблиц, ни индексов — только `UPDATE`. **Не забыть снять `SKIP_MIGRATION=true`** перед
следующей выкладкой.

### Ветка

`codex/upstream-4.13.0` никуда не пушилась. `64285f5d` на месте в `codex/referral-fix`,
`0891aa04` / `0b9c28c3` — в `codex/upstream-4.12.1`, `a21c7c55` — в
`codex/autopay-legacy-renewal`.

---

## 11. Риски для прода

1. 🔴 **Теги панели** (§5). ~2400 аккаунтов, ручные теги затираются. До решения по
   `panel_tag`/`*_USER_TAG` кнопку «Из бота в панель» не трогать.
2. 🔴 **Подтверждение платежа в API ЮKassa стало обязательным** (§6.2). Проверить
   `YOOKASSA_SHOP_ID`/`YOOKASSA_SECRET_KEY` и доступность `api.yookassa.ru`;
   после выкладки смотреть на 500 в `yookassa-webhook.log`.
3. 🟡 **Все даты и время на админских экранах и в уведомлениях сдвигаются с UTC на МСК**
   (§2.1). Данные те же, цифры другие; у моментов 21:00–24:00 UTC меняется и дата.
   Объяснить оператору заранее.
4. 🟡 **Автопродление начнёт срабатывать на сутки позже** — теперь в свой день, а не при
   3 д 23 ч остатка. Для клиента это ближе к сроку; для нас — сдвиг волны списаний.
   На подписках с `autopay_days_before = 1` окно сжимается до реальных суток.
5. 🟡 **Миграция 0126 трогает данные** — первая за серию слияний, которая не просто
   добавляет колонку. Репетиция на копии (§9, п. 2) обязательна.
6. 🟡 **Новые публичные ручки `/branding/manifest.webmanifest` и `/branding/app-icon/*`**
   отдают иконки и цвета темы без аутентификации. Это задумано (Google должен их
   скачать), но на `lk.donbaton.tech` стоит убедиться, что они попадают в проксируемый
   набор путей, а на `gw.synqlane.top` — что НЕ попадают (там принцип «ничего, кроме
   перечисленных путей»).
7. 🟡 **GEO-проверка достижимости (4.10.0) по-прежнему не опробована** — в форке не
   используется, но код в образе есть.
8. **Следующее слияние снова упрётся в номера миграций** — правило в §4а.

---

## 12. Незакрытые вопросы

1. **Postgres-тесты не прогонялись** (188 пропусков) — контейнерного рантайма на маке нет.
2. **Откат не репетировался**, как и в прошлый раз: §10 написан по коду и по прошлой
   репетиции `rehearse-migrations-u4121.sh`, но на живой копии 0125→0126 не проигрывался.
3. **Сколько строк реально забэкфиллит 0126 — неизвестно.** Оценка «до 534» взята из
   постановки задачи; сколько из них у людей ровно с одной подпиской, можно узнать только
   на копии боевой БД (запрос в §9, п. 2).
4. **Ручные теги в панели не инвентаризированы** — на бокс и в API панели в этой работе
   не ходили вовсе. Без инвентаря решение по §5 принимать вслепую.
5. **`tariffs.panel_tag` у наших тарифов не заполнялся** — это правка данных в кабинете,
   не в коде, и делается она на проде.
6. **Фронт кабинета под манифест приложения не обновлялся** — это отдельный репозиторий
   (`forks/bedolaga-cabinet`), в этой задаче он не трогался.
