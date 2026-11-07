# Handoff: слияние апстрима v5.2.0 в форк ДонБатона (07–08.11.2026)

Поручение владельца 07.11 ~22:00 МСК: «Давай наши форки и лк всё до последних версий обновим». Исполнитель — Claude (оркестратор),
конфликты и интеграция — субагенты Codex U1–U9 (отчёты: `local-ops/_claude/tools/donbaton-upstream-20261107/codex/out/`).
Задача — `vpn/donbaton/tasks/2026-11-07-upstream-update.md`.

## Что в бою (08.11.2026 00:25:36 МСК)

| | Было | Стало |
|---|---|---|
| Ветка | `codex/upstream-5.0.0` = `f48f0c42` | **`codex/upstream-5.2.0` = `f1a26d3b`** (merge v5.2.0 + защита форка), в origin |
| Образ | `donbaton/bedolaga:u500-f48f0c42-20261107` | **`donbaton/bedolaga:u520-f1a26d3b-20261107`** |
| Alembic | `0135` (`0135_ai_support`) | **`0144`** |
| Кабинет | `u1801ai-b80d14c1-20261107` (1.80.1) | **`u1820-fc664249-20261107`** (1.82.0, ветка `codex/upstream-1.82.0`) |

## Миграции — главное правило при следующих слияниях

У форка номера 0129–0135 заняты своими миграциями (0129 user_reminders, 0130 lte_quota_notices, 0131 dpichecker_actions,
0132–0134 — апстримовские 0129–0131 broadcast_audience/cashera, перенумерованы ещё при слиянии 5.0.0; 0135 ai_support).
Апстримовские **0132–0140 (v5.1–5.2) перенумерованы в 0136–0144** (+4), `down_revision` — цепочкой от `0135`; ссылки в тестах
(`tests/database/*_postgres.py`, `tests/migrations/test_0139*`, `test_0140*` — имена файлов тестов не менялись) и в документах обновлены.
Следующий апстрим (0141+ у них) — перенумеровывать так же, от нашей головы. Проверка: `tests/database/test_migration_chain.py`,
репетиция на копии боевой БД (`core/rehearsal.sh` пакета) — 08.11 0135→0144 за 2 с, данные совпали, откат на 0135 работает.

## Защита форка в новых механизмах апстрима

| Механизм 5.1/5.2 | Что сделано у нас | Где |
|---|---|---|
| Выбор доп. устройств при покупке | сетевые тарифы (Wi-Fi, Wi-Fi+LTE) исключены на сервере во всех сценариях; лимит — по LTE-политике | `app/services/tariff_purchase_devices.py`, `tariff_purchase_quote.py`, `cabinet/.../purchase.py`; тесты `tests/donbaton/test_upgrade520_network_devices.py` |
| CisPay-рекурренты | запрет для сетевых до quote/резерва/POST; поздний платёж — на баланс один раз + уведомление админам; отмена соглашения сохранена | `app/services/cispay_recurrent_*.py`; `test_upgrade520_cispay_network.py` |
| Обычная докупка трафика (`traffic_purchase_service`) | сетевым — отказ (LTE только пакетами), повторно под блокировкой | `app/services/traffic_purchase_service.py` |
| Управляемый учёт трафика (`TrafficAccountingWorker`) | Wi-Fi/LTE никогда не переводятся в `managed` | `traffic_accounting_worker.py`, `traffic_accounting_service.py`; `test_upgrade520_lte_accounting.py` |
| Удаление «лишних» HWID при записи лимита | **новая настройка `PANEL_SYNC_TRIM_EXCESS_HWID=False`** (по умолчанию выключено — поведение 5.0); при `True` окно прежних условий берёт лимит из LTE-политики | `app/services/panel_sync/writer.py`, `app/config.py`; `test_upgrade520_hwid_squads.py` |
| Восстановление записи с частью сквадов | сетевым — отсутствие любого обязательного сквада = отказ до списания (`validate_checkout_squads(..., network_category=...)` во всех точках), grace — полный набор | `checkout_squad_validation.py`, `grace_access_runtime.py` |

Бой 08.11: у 2512 подписок общий лимит 0 → кандидатов в managed нет; в панели у 15 пользователей устройств больше лимита (54 лишних) —
поэтому HWID-удаление выключено до решения владельца.

## Тексты (смонтированные локали)

`/opt/bedolaga/locales` перекрывают умолчания образа; новые ключи 5.2 приходят из образа. При выкладке `core/locales_sync.py` поправил
5 ключей (подстановка `COUNTRY_CHANGES_CHARGED {months}→{days}` и 4 неизменённых старых текста апстрима); бэкап — `/root/backups/20261107-up/locales-002529.tgz`.

## Откат

`core/rollback.sh` пакета (`SHA=f1a26d3b LTS=002529 APPLY=1`): `alembic downgrade 0135` новым образом → тексты из бэкапа → образ
`u500-f48f0c42`. Downgrade откажет, если в новых таблицах появились данные (например, покупки по новому checkout) — тогда решать вручную;
дамп перед миграцией — `/root/backups/20261107-up/bot-db-002529.dump`. Кабинет — `MODE=rollback` в `deploy-cabinet.sh`.

## Что знать дальше

- 5.2 публикует меню команд бота (`CommandMenuSync`, раз в 60 с), ежедневную сводку превышений HWID админам, проверку CisPay-соглашений.
- `rich_admin`: rich-уведомление в админ-чат с картинкой падает «failed to get HTTP URL content», бот откатывается на обычное (08.11 00:25).
- Полный pytest на PG18 на ядре идёт ~2–3 ч (tests/database поднимает миграции на каждый случай); перед выкладкой гонялись зона форка на
  PG18 (exit 0) и полный набор без PG на маке (exit 0).
- Ноды 3.4.4 (Xray 26.10.31) **не выкатывались** — новая REALITY отклоняет ClientHello без X25519MLKEM768; см. задачу.
