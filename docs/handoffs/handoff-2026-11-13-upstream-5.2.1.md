# Handoff — бот ДонБатона: апстрим 5.2.0 → 5.2.1 (13.11.2026)

Ветка `codex/upstream-5.2.1`, база — `cb4736e1` (боевой форк 5.2.0 + исправления поддержки 12–13.11).
Слияние — `c8974735`, правка теста — `1c19f05c`. Предыдущий handoff — `handoff-2026-11-08-upstream-5.2.0.md` (список патчей форка и мест защиты).

## Миграции
- Апстрим `0141_subscription_renewal_operations` → у нас **`0146_subscription_renewal_operations`** (`revision 0146`, `down_revision 0145`).
- Наши ревизии: 0129–0135 и 0145 — форк; 0136–0144 — апстрим 0132–0140; 0146 — апстрим 0141.
- **Следующий апстрим (0142+ у них) — от 0146**, так же перенумеровывать от нашей головы; проверка — `tests/database/test_migration_chain.py`.

## Конфликты (12 файлов) и решения
- `tariff_switch_policy.py`, `cabinet/routes/subscription_modules/tariff_switch.py` — обе функции: `should_reset_devices_on_switch` (форк) и `lift_traffic_limit_after_switch` (апстрим).
- `handlers/subscription/tariff_purchase.py` — продление по тарифу: лимит устройств из `_renewal_device_limit` форка (для триала — `paid_trial_renewal_terms`
  апстрима); `_reject_purchase_policy` и проверка сквадов по `network_category` — по тарифу, на который переходит триал; `client_purchase=True` + `connected_squads`.
  Суточный тариф — сначала блокировка строки подписки (апстрим), затем чтение исходного тарифа для сброса устройств (форк).
- `services/subscription_renewal_service.py` — `client_purchase=True` и `paid_trial_conversion`.
- `handlers/subscription/cispay_recurrent.py` — ввод своего срока автоплатежа закрыт для сетевых подписок (плюс отказ в сервисе цены).
- `database/models.py` — `AiSupportRequest` (форк) и `SubscriptionRenewalOperation` (апстрим).
- Локали (5) — ключи форка + новые апстрима; `docs/project_structure_reference.md` — пересобран.
- Сверх конфликтов: `database/crud/subscription.py` — переход с триала по `paid_trial_conversion` доходит до учёта LTE.

## Поведение для клиентов
- **Бот 5.2.1 — только вместе с кабинетом 1.82.1**: продление из кабинета требует `operation_id` и `expected_end_date`, без них 400.
  Старые корзины продления без идентификатора после пополнения не исполняются — клиент подтверждает покупку заново.
- Докупка трафика в грейсе после исчерпания — сетевые (Wi-Fi/LTE) по-прежнему отказ до новой ветки, в managed не переводятся.
- После смены тарифа снимается статус «трафик кончился» (апстрим; managed-подписки пропускаются).
- HWID: нового удаления устройств нет, `PANEL_SYNC_TRIM_EXCESS_HWID=False`.

## Тексты (смонтированы с хоста `/opt/bedolaga/locales`)
Хост перекрывает 2275 ключей; изменённые апстримом `ADMIN_REFERRALS_UI_001` (убран `{v5}`), `MONITOR_TRIAL_ENDING` (+`{hours}`) и новые
`ADMIN_BROADCAST_TEXT_REQUIRED`, `CISREC_ENTER_DAYS` на хосте не перекрыты — действуют умолчания образа. Сверка плейсхолдеров всех перекрытых
ключей с новыми умолчаниями 13.11 — расхождений нет. Удалены 74 ключа старого меню синхронизации и 4 пустые настройки (`NOTIFICATION_RETRY_ATTEMPTS`,
`NOTIFICATION_CACHE_HOURS`, `MONITORING_LOGS_RETENTION_DAYS`, `REFERRAL_NOTIFICATION_RETRY_ATTEMPTS`).

## Проверки
- Мак: полный pytest без PG — 15 298 passed, 3 366 skipped, 0 failed; ruff по изменённым файлам чисто; `uv.lock`/`pyproject.toml` = апстрим v5.2.1.
- Ядро: PG18 — зона форка + 15 новых PG-тестов апстрима; репетиция 0145 → 0146 на копии боевой БД (квитанции — приватный пакет `donbaton-u521-20261113`).
