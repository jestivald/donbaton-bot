# Handoff — LTE: частичная телеметрия (lease v2) и оповещения владельцу

**Дата:** 2026-09-25
**Исполнитель:** Claude (субагент координатора), только локальная работа
**Репозиторий:** `/Users/admin/Documents/work/vpn/donbaton/forks/bedolaga-bot`
**Worktree:** `/Users/admin/Documents/work/_worktrees/donbaton-bot/lte-partial-telemetry`
**Ветка:** `codex/lte-partial-telemetry`
**База:** `465f26e6` (`codex/upstream-4.13.0` = бой 4.13.0 + docs)
**Коммиты:** `6fcf7121` — канарейка, `223ef7b7` — бот (гейт и оповещения), затем этот документ.
**Ничего не запушено, серверы не тронуты, SSH не было, образы не собирались.**

---

## 1. Зачем

Гейт LTE зависел от того, что канарейка доказывает **все** маршруты: при отказе
любого она удаляла lease, бот через grace (900 с) снимал LTE у всех, и делал это
молча — строки `LTE telemetry missing, within grace` просто прекращались, факт
снятия находили по всплеску `PATCH /api/users` в nginx ядра. Так было 12.09
(умер `cdn-vk-de`) и в ночь на 25.09 (spb-fra недоступен из-за рубежа: 9 снятий,
~11 тыс. PATCH). Теперь один упавший обход LTE не снимает, а владелец видит
деградацию в админ-чате.

## 2. Что сделано

### 2.1 Канарейка — `scripts/donbaton/lte_telemetry.py` (`6fcf7121`)

- Маршруты доказываются независимо (`gather(return_exceptions=True)`). Пока
  доказан хотя бы один, в тот же ключ `donbaton:lte:telemetry:<epoch>` пишется
  **lease v2**:
  `{"version":2,"epoch","observed_at","node_ids":[прошедшие],"failed":{node_id: code}}`,
  `observed_at` — самое старое из успешных наблюдений, TTL как раньше (≤180 с).
  Не доказан ни один, сбой Redis, остановка — ключ удаляется, как раньше.
- Коды без текста исключений: `unreachable` (туннель/прокси/TLS/таймаут),
  `asset` (не тот диапазон или хеш файла), `panel` (API панели или личность
  служебного юзера/ноды), `not_persisted` (счётчик не вырос за 30 с),
  `multiplier`, `wrong_node`, `regressed`. Каждый шаг проверки отображается в свой код.
- Лог: по строке на маршрут `{"event":"lte_telemetry_route","node_id","code","error_type"?}`
  (`error_type` — только имя класса), затем сводка: `lte_telemetry_ready` (все
  маршруты, прежний смысл), **`lte_telemetry_partial`** (lease на части, с
  `node_ids`/`failed`), `lte_telemetry_unavailable` (lease нет, с `failed`).
- `Config.load`: от 1 до 8 маршрутов, без повторов нод, юзеров и VLESS-кредов;
  `DONBATON_LTE_NODE_UUIDS` в env канарейки обязан совпадать с планом.
  `XrayClients.start` сверяет число кредов с `len(routes)`.
- `lease_status(payload, config, now=…)` — проверка lease v1/v2 против плана для
  хуков оператора внутри образа канарейки (см. §4.3).
- `--once` по-прежнему ненулевой, если доказаны не все маршруты.
- README `scripts/donbaton/README.lte-telemetry.md` переписан под v2.

### 2.2 Бот — гейт (`223ef7b7`)

`app/services/lte_accounting_service.py`: `telemetry_proof()` (структура lease
против настроенных нод, без возраста), `live_telemetry_proof()` (плюс возраст),
`valid_telemetry_heartbeat()`/`telemetry_within_grace()` получили `min_routes`.

- **v1** принимается только с полным набором `DONBATON_LTE_NODE_UUIDS` — старая
  канарейка + новый бот работают как сейчас.
- **v2** — если пересечение доказанных нод с настроенными не пустое и не меньше
  `DONBATON_LTE_TELEMETRY_MIN_ROUTES` (по умолчанию 1). Больше числа нод —
  ограничивается числом нод, одно предупреждение в лог на значение, LTE само
  по себе не закрывает. Ноды lease вне конфигурации игнорируются, одно
  предупреждение `LTE telemetry lease names unconfigured nodes, ignored`.
- `NODE_UUIDS` — от одной ноды (было ровно две).
- Якорь `…:last-valid` (EX grace+180) и grace — как раньше; частичный принятый
  lease тоже живая телеметрия и тоже пишется в якорь.
- `TelemetryEvidence` несёт `proven`, `missing`, `failed` (код канарейки или
  `unmonitored`, если свежий lease маршрут не упоминает), `state`
  (`live`/`grace`/`closed`) и `grace_remaining`.
- При закрытии гейта каждый цикл пишет warning
  **`LTE telemetry missing beyond grace, LTE closed`** (epoch, grace_seconds,
  last_valid_seen_at) — только при включённых `DONBATON_LTE_ENABLED`/`ACCOUNTING_READY`.

Итог: LTE закрывается, только когда нет ни одного принятого маршрута дольше
grace; упавший обход просто не работает, сквады не меняются.

### 2.3 Бот — оповещения владельцу (`app/services/lte_telemetry_alerts.py`)

Хук `report_cycle(evidence, bot=bot)` вызывается **один раз за цикл** сразу после
`_resolve_telemetry_evidence()` в `run_quota_cycle`; `bot` приходит из
`MonitoringService._lte_loop` параметром, по умолчанию `None`. Отправка —
`AdminNotificationService.send_admin_notification(text, category=INFRASTRUCTURE)`.

| Сообщение | Когда |
|---|---|
| 🟠 обход без подтверждения: имя, код, длительность | маршрута нет дольше `DONBATON_LTE_ROUTE_ALERT_DELAY_SECONDS` (300) |
| ✅ обход снова подтверждён | только после сообщённого отказа |
| 🟠 LTE держится на grace (осталось N) | ни одного маршрута дольше той же задержки |
| 🔴 LTE снят у всех | сразу при закрытии гейта (и при первом взгляде, если гейт уже закрыт) |
| ✅ LTE возвращён / подтверждение вернулось до конца grace | при возврате |
| 🔁 напоминание со сводкой | раз в `DONBATON_LTE_ROUTE_ALERT_REMIND_SECONDS` (21600, 0 — выкл) от последнего сообщения, пока не норма |

- Имена обходов по UUID ноды: `85950bae…` Финляндия (spb-fra), `97181e55…`
  Германия (cdn-de1), `516dd304…` Польша (cdn-pl1); неизвестная — «нода <префикс>».
- `not_persisted`/`multiplier`/`wrong_node` — «⚠️ Учёт под вопросом — проверьте ноду в панели».
- Коды бота: `no_lease` (lease нет; показывается последний известный код
  канарейки), `unmonitored` (нода в `NODE_UUIDS`, канарейка её не проверяет).
- Все изменения цикла — одним сообщением; блип короче задержки не сообщается.
- Состояние: Redis `donbaton:lte:telemetry:<epoch>:alert-state` (30 суток) и память
  процесса — рестарт не повторяет и не теряет сообщение, лежащий Redis не делает
  каждый цикл «первым».
- Сбой Telegram не ломает цикл: отправка ограничена 20 с (flood control
  отправителя может ждать до 30 с на попытку), недоставленное пересчитывается и
  отправляется в следующем цикле с его актуальным состоянием. Без бота или при
  выключенных админ-уведомлениях/категории — только лог `LTE telemetry alert`.

### 2.4 Конфиг

`app/config.py`, `.env.example`, подсказки в `app/services/system_settings_service.py`,
`docs/DONBATON.md` (подразделы «Телеметрия по маршрутам (lease v2)» и «Оповещения владельцу»):

```dotenv
DONBATON_LTE_TELEMETRY_MIN_ROUTES=1          # 1..16
DONBATON_LTE_ROUTE_ALERT_DELAY_SECONDS=300   # 0..86400
DONBATON_LTE_ROUTE_ALERT_REMIND_SECONDS=21600  # 0..604800, 0 — без напоминаний
```

Умолчания рабочие. **В `.env` бота новые ключи не добавлять**, если не нужно
другое значение: ключ из `.env` молча блокирует правку из админки. Миграций нет.

### 2.5 Для сведения веток

- В `run_quota_cycle` три изменения: сигнатура `(subscription_service, bot=None)`,
  одна строка `await lte_telemetry_alerts.report_cycle(evidence, bot=bot)` сразу
  после `evidence = await _resolve_telemetry_evidence()`, модульный импорт
  `from app.services import lte_telemetry_alerts`. В `_lte_loop` —
  `run_quota_cycle(self.subscription_service, bot=self.bot)`. Если
  `lte-resilience-notify` тоже добавит `bot=None`, конфликт в одной строке тривиален.
- Докстринг `run_quota_cycle` («no Telegram, payment or email effects») **оставлен
  намеренно**, чтобы не создавать конфликт соседней строки; при сведении с
  `lte-resilience-notify` поправить (там тоже появится Telegram).
- После слияния с `upstream-4.15.0` пересобрать `docs/project_structure_reference.md`
  (`make docs-structure`) — тест `test_structure_reference_is_current` это ловит.

## 3. Проверки (локально, 25.09.2026)

| Проверка | Результат |
|---|---|
| `uv run pytest -q` (весь проект) | **9472 passed, 205 skipped, 0 failed** (база 9400 / 205 / 0) |
| `tests/donbaton/test_lte_telemetry_producer.py` | 37 passed |
| `tests/donbaton/test_lte_telemetry.py` | 62 passed |
| `tests/donbaton/test_lte_telemetry_alerts.py` (новый) | 23 passed |
| без правок: `conftest.py`, `test_paid_trial_cutover.py` (v1), `test_lte_legacy.py` (`run_quota_cycle(service)`) | проходят |
| `uv run ruff check .` / `uv run ruff format --check .` | чисто |
| `make docs-structure` | справочник пересобран, тест актуальности зелёный |
| мутационная проверка алертов (3 поломки) | тесты падают на каждой |

Тесты алертов покрывают задержку, одно сообщение на смену, склейку изменений
цикла, напоминание и `0`, восстановление, grace/закрытие/возврат, рестарт
(состояние из Redis), сбой и зависание Telegram, лежащий Redis, отсутствие бота,
выключенную категорию и передачу `bot` из `_lte_loop`.

## 4. Порядок выкладки

**Сначала бот, потом канарейка.** Бот до этой ветки v2 не принимает: новая
канарейка при старом боте закроет LTE у всех через grace.

### 4.1 Бот

Обычная выкладка образа бота с этой веткой (после сведения с
`lte-resilience-notify`/`upstream-4.15.0`, если они идут вместе). Старая
канарейка продолжает писать v1 с полным набором — гейт работает как сейчас.
После старта проверить:

- в логе `remnawave_bot` нет `LTE telemetry missing beyond grace, LTE closed`;
- в Redis появился `donbaton:lte:telemetry:lte-physical-20260909:alert-state`
  (`gate.announced = "live"`), сообщений в админ-чат нет;
- отличие от прежнего бота уже со старой канарейкой: если она снимет lease
  (любой маршрут упал), через 5 мин придёт «LTE держится на grace», при
  закрытии — «LTE снят у всех». Это ожидаемо.

### 4.2 Канарейка v2 (db-core)

Меняется **только** `runtime/lte_telemetry.py`; база образа прежняя
(`BOT_IMAGE=donbaton/bedolaga:lte-candidate-375e830f45d7-e33eb608`,
Id `sha256:995b0255…8a6c`), Xray прежний (`8ffa75a2…a2db`), Dockerfile тот же.
Хеши сверены локально по `telemetry-runtime-60ec6478111d.tar.gz` (09.09):
в бою `lte_telemetry.py` = `6aa02b10…e886` — это ровно база ветки.

| Файл | sha256 |
|---|---|
| боевой `runtime/lte_telemetry.py` (`aa1b3c98`) | `6aa02b101053e2c84643f1acfd18d09a0723f7bb89b953f38135b1d53301e886` |
| v2 `scripts/donbaton/lte_telemetry.py` @ `6fcf7121` | `9d8d1c7722711223b965b6159588950cedde568ac79905fb6c31bca24ff31d6b` |
| `manifest_sha256` v2 при `source_commit=6fcf7121…` | `23da7fd1656b58d5c97e7d9454bb6f09c17f7d1d313869294b9263cc0585d7c1` |

Всё на ядре — в `/root/codex-staging/donbaton-lte-telemetry-20260909/`
(далее `$S`). Доступ — **только одним ControlMaster-сокетом**
(`local-ops/_claude/tools/donbaton-autopay-20260916/dbcore_ssh_cm.sh`), один
исполнитель на ядро. Шаги (как в `4-dbcore-apply.sh` 25.09):

1. Сверить на ядре: `sha256sum $S/runtime/lte_telemetry.py` = `6aa02b10…`;
   `telemetry_service.py` = `86973c93…` (иначе сначала понять, что меняли).
2. Положить v2 в `$S/incoming/lte_telemetry.py` (scp тем же сокетом), сверить sha.
3. Бэкап `$S/runtime/`, `compose.json`, `SERVICE.json` в `/root/backups/lte-canary-v2-<ts>/`.
4. Заменить `runtime/lte_telemetry.py`; в `runtime/SOURCE.json` —
   `files_sha256["lte_telemetry.py"]`, `source_commit`, пересчитать
   `manifest_sha256` = sha256 от `json.dumps(manifest_без_него, sort_keys=True, separators=(',', ':'))`
   (формула из `telemetry_service.source()`, сверена на боевом манифесте).
5. Собрать без сети, как `stage()`: `docker build --network=none --pull=false
   --platform=linux/amd64 --build-arg BOT_IMAGE=… --build-arg XRAY_SHA256=…
   --label org.donbaton.telemetry.owner=donbaton-lte-telemetry:20260909
   --label org.donbaton.telemetry.source=<manifest> -t donbaton/lte-telemetry:<manifest[:12]> $S/runtime`.
6. `compose.json`: `services.telemetry.image` = новый Id, метка
   `org.donbaton.telemetry.source` = новый манифест; `docker compose -f compose.json config --quiet`.
7. `SERVICE.json`: `image_id`, `image_tag`, `source_manifest_sha256`,
   `source_commit`, `compose_sha256` (= sha256 нового `compose.json`);
   `private_config_sha256` не меняется.
8. `docker compose -f $S/compose.json up -d --no-deps --no-build telemetry`
   (старый контейнер при остановке удаляет lease; окно ≤1,5 мин покрывает grace 900 с,
   алерта не будет — задержка 300 с).
9. Наблюдение каждые 15 с до ~7 мин: `docker logs --since $T0 donbaton-lte-telemetry`
   — считать **`lte_telemetry_ready` и `lte_telemetry_partial` вместе** (оба =
   lease записан), смотреть строки `lte_telemetry_route`, в Redis
   `GET donbaton:lte:telemetry:lte-physical-20260909` → `version: 2`, `failed`.
   Приёмка — 5 записанных lease подряд и нет `beyond grace` в логе бота.
10. **Автооткат:** за ~4 мин ни одного `ready|partial` — вернуть `runtime/`,
    `compose.json`, `SERVICE.json` из бэкапа и `docker compose … up -d` (старый
    образ остаётся локально, не делать `docker image prune`).

Черновик скрипта с этими шагами — приложение А. **На ядре не запускался**: проверены
`bash -n` и оба Python-блока на копиях в песочнице (манифест из архива 09.09 даёт
`23da7fd1…`, формула `source()` его принимает). Первый прогон — без `APPLY=1`.

`telemetry_service.py proof` до правки §4.3 на v2 **ложно покажет FAIL** — не
использовать его как признак готовности; признак — логи и Redis (шаг 9).

### 4.3 Правка `telemetry_service.py` (файл не в Git — не менял)

`/Users/admin/Documents/work/vpn/donbaton/implementation/donbaton-lte-cutover-20260909/telemetry_service.py`
(sha `86973c93…` = хелпер из передачи 09.09) в Git не лежит, поэтому правка только
описана. Причина FAIL: `READ_PROOF` выполняется внутри образа канарейки и
импортирует `valid_telemetry_heartbeat` из **базового** образа бота (код 09.09,
знает только v1), а `proof()` требует lease «от обоих маршрутов» и множитель 1 у
всех нод. Правка (проверено только `py_compile` и компиляцией строки `READ_PROOF`;
имена импорта есть в v2-модуле):

```diff
@@ READ_PROOF
-from scripts.donbaton.lte_telemetry import Config,PanelAPI,read_outbound,counter
-from app.services.lte_accounting_service import valid_telemetry_heartbeat
+from scripts.donbaton.lte_telemetry import Config,PanelAPI,read_outbound,counter,lease_status
@@
-    valid=valid_telemetry_heartbeat(payload,epoch=config.epoch,node_ids=config.node_ids,now=now,max_age_seconds=180)
+    status=lease_status(payload,config,now=now,max_age_seconds=180)
+    valid=status['valid']
     result={'result':'PASS','epoch':config.epoch,'nodes':nodes,'users':users,
       'private_panel_read':'PASS','redis_read':'PASS','lease_present':value is not None,
-      'lease_valid':valid,'lease_ttl_seconds':ttl}
+      'lease_valid':valid,'lease_complete':status['complete'],'lease_ttl_seconds':ttl}
     if valid:
         result['lease']={key:payload[key] for key in ('version','epoch','observed_at','node_ids')}
+        result['lease']['failed']=status['failed']
@@ def proof():
     if not value['lease_valid'] or not 0 < value['lease_ttl_seconds'] <= 180:
-        raise RuntimeError('No valid current telemetry lease from both LTE routes')
-    if any(node['multiplier'] != 1 for node in value['nodes']):
+        raise RuntimeError('No valid current telemetry lease from any LTE route')
+    # A failed route is named in lease.failed (code 'multiplier' included);
+    # only the routes the lease claims as proven must be at 1:1 right now.
+    proven = set(value['lease']['node_ids'])
+    if any(node['multiplier'] != 1 for node in value['nodes'] if node['node_id'] in proven):
         raise RuntimeError('Telemetry lease does not belong to current physical node policy')
```

Правку применить на ядре к `$S/telemetry_service.py` **после** выкладки образа v2
(старый образ не содержит `lease_status`). Новая sha хелпера после правки —
`b44a4705d0df035860542130efc159a1334528504a70d4c0e1c996f87366b12d` (если исходник
на ядре = `86973c93…`). `stage()` для v2 устарел (ищет старую строку `reason` при ×17) —
не использовать. `policy()` (в `start`) по-прежнему требует множитель 1 у **всех**
нод из `SPECS` — это осознанный предохранитель, но `start` при сломанном множителе
одной ноды не поднимет канарейку; перезапуск делать `docker compose up`/`docker restart`.

## 5. Откат

**Сначала канарейка, потом бот** (обратный порядок выкладки).

1. Канарейка: вернуть `runtime/`, `compose.json`, `SERVICE.json` из бэкапа §4.2,
   `docker compose -f $S/compose.json up -d --no-deps --no-build telemetry`.
   Снова v1: любой упавший маршрут опять снимает lease.
2. Подождать хотя бы один цикл (~1–2 мин): новый бот перепишет якорь `last-valid`
   на v1 (якорь с v2-payload старый бот не примет как grace).
3. Бот: прежний образ. Ключ `…:alert-state` старому боту не мешает (не читается).

## 6. Третий маршрут — cdn-pl1 (`516dd304-9a41-4507-8545-a46f92f6fe69`)

Только после того, как и бот, и канарейка v2 в бою и стабильны. **При v1-канарейке
не делать:** v1 требует точного набора, лишняя нода в боте закроет LTE через grace.

1. **Служебный юзер** в панели ДонБатона (её REST API; **не** `mcp__remnawave__*`):
   `db_lte_canary_pl_<дата>`, тег `LTE_TELEMETRY`, описание
   `donbaton-lte-telemetry:20260909`, `telegramId=null`, лимит 0, `NO_RESET`,
   ACTIVE, срок до 2036, только сквад `DonBaton LTE`, свой VLESS-кред; вне
   managed/orphan-леджеров и census. Инбаунд тот же, что у Германии
   (`02d65c58-a013-419c-aba9-0d02f21713ad`), поэтому маршрут закрепляется
   **хостом** на cdn-pl1 (включённый Host этой ноды), а `lastConnectedNodeUuid`
   проверит, что трафик лёг именно на PL.
   **`telemetry_control.py prepare` не запускать:** он требует множители ×17,
   перезаписывает `lte-telemetry.json`/`telemetry.env` и заново закрепляет файл
   кабинета. Нужно узкое действие (или руками по его образцу `owned_user` →
   подписка юзера с UA Happ и **одним фиксированным** `X-Hwid`
   `db-lte-canary-pl-20260909` → `select_outbound` по PL-хосту →
   `secrets/pl-client.json`, владелец UID 1000, 0400).
2. **`telemetry_control.py`:** `SPECS['pl'] = {node_id: 516dd304…, inbound_uuid:
   02d65c58…, host_id: <Host cdn-pl1>, networks: ('xhttp', 'splithttp')}`;
   `ownership.json` получит `users['pl']` (через `owned_user`).
   `telemetry_service.py`: в `private_hashes()` добавить `'secrets/pl-client.json'`.
3. **Бот раньше канарейки:** добавить PL в `DONBATON_LTE_NODE_UUIDS` бота (в `.env`
   → пересоздание контейнера, не рестарт). До шага 4 бот показывает PL как
   `unmonitored`; если шаг 4 займёт дольше 5 мин — придёт одно сообщение
   «Польша (cdn-pl1) — канарейка этот маршрут не проверяет» и затем восстановление.
   В обратном порядке бот игнорировал бы PL («unconfigured nodes, ignored»), и
   отказы PL не были бы видны. `MIN_ROUTES` не должен превышать число маршрутов,
   которые канарейка уже проверяет.
4. **Канарейка:** в `secrets/lte-telemetry.json` добавить маршрут
   `{"node_id":"516dd304-…","user_id":<id PL>,"client_config":"/run/secrets/pl-client.json"}`
   (origin и sha не трогать), в `telemetry.env` — PL в `DONBATON_LTE_NODE_UUIDS`,
   обновить `SERVICE.json.private_config_sha256`, `docker restart donbaton-lte-telemetry`.
   Приёмка: `lte_telemetry_route` для `516dd304…` с `code: ok`, события `ready`.
   Нагрузка: +64 КиБ/мин (~90 МиБ/сутки).

## 7. Риски и что не проверено

- **Неучтённый LTE на сломанном маршруте до реакции владельца.** При
  `MIN_ROUTES=1` LTE открыт, пока жив хоть один обход. Если у упавшего обхода
  сломан именно учёт (`not_persisted`, `multiplier`, `wrong_node`), клиенты через
  него могут тратить LTE без списаний до реакции: сообщение через 5 мин с пометкой
  «учёт под вопросом», напоминания раз в 6 ч. Меры: снять Host этого обхода в
  панели или поставить `MIN_ROUTES` = число нод (прежнее «все или никто»).
- `unreachable`/`asset` учёту не вредят: обход просто недоступен клиентам.
- Канарейка целиком (контейнер, релей msk-hel к spb-fra, Redis) по-прежнему
  закрывает LTE у всех через grace — но теперь с сообщением и строкой в логе.
  Предохранителя от массового PATCH при закрытии по-прежнему нет (вне задачи).
- **Не проверено:** реальный прогон канарейки v2 (нет стенда; только юнит-тесты),
  доставка и вид сообщений в боевом админ-чате (rich-рендер), правка
  `telemetry_service.py` на ядре, скрипт приложения А на ядре (только `bash -n` и
  Python-блоки на копиях), сборка образов.
- Хеши боевых файлов взяты из локальных копий 09.09; на ядре перед правкой сверить.

## 8. Следующий шаг

Ревью ветки → сведение с `lte-resilience-notify` (§2.5) → выкладка бота (§4.1) →
канарейка v2 по §4.2 (сначала сухой прогон приложения А) → правка
`telemetry_service.py` (§4.3) → cdn-pl1 по §6 отдельным шагом.

---

## Приложение А. Черновик `canary-v2-apply.sh` (НЕ запускался)

Положить в `local-ops/_claude/tools/<дата>-lte-canary-v2/`, прочитать целиком.
Сухой прогон: `dbcore_ssh_cm.sh 'bash -s' < canary-v2-apply.sh`;
боевой: `dbcore_ssh_cm.sh 'APPLY=1 bash -s' < canary-v2-apply.sh`.

```bash
#!/usr/bin/env bash
# db-core: LTE-канарейка -> lease v2. Тот же BOT_IMAGE и Xray, меняется только runtime/lte_telemetry.py.
# Без APPLY=1 только проверки. Заранее: v2-файл в $S/incoming/lte_telemetry.py (scp тем же сокетом).
# Автооткат: ни одного lte_telemetry_ready|partial за ~4 мин -> бэкап и прежний образ. Выкл: AUTO_ROLLBACK=0.
set -uo pipefail
S=/root/codex-staging/donbaton-lte-telemetry-20260909
C=donbaton-lte-telemetry
KEY=donbaton:lte:telemetry:lte-physical-20260909
BASE_REF=donbaton/bedolaga:lte-candidate-375e830f45d7-e33eb608
BASE_ID=sha256:995b02553e483b523f72c792ca8af9c1ce971eb24ef7614431bb48be1bc28a6c
XRAY=8ffa75a22131a8db7daa751ee1b12c5cba84cc48d41bccef49e12bb8efcfa2db
MARKER=donbaton-lte-telemetry:20260909
OLD=6aa02b101053e2c84643f1acfd18d09a0723f7bb89b953f38135b1d53301e886
NEW=9d8d1c7722711223b965b6159588950cedde568ac79905fb6c31bca24ff31d6b
COMMIT=6fcf7121e7be06991086d2927ff583019bf01d03
BOT=${BOT_CONTAINER:-remnawave_bot}
fail() { echo "СТОП: $*"; exit 1; }
sha() { sha256sum < "$1" | cut -d' ' -f1; }

echo "### $(date '+%F %T %Z') APPLY=${APPLY:-0}"
[ "$(sha $S/runtime/lte_telemetry.py)" = "$OLD" ] || fail "в runtime/ не боевая v1"
[ "$(sha $S/incoming/lte_telemetry.py)" = "$NEW" ] || fail "incoming/lte_telemetry.py не v2 из $COMMIT"
[ "$(docker image inspect -f '{{.Id}}' $BASE_REF)" = "$BASE_ID" ] || fail "база образа сменилась"
docker exec "$BOT" test -f /app/app/services/lte_telemetry_alerts.py || fail "бот ещё без v2-гейта: сначала бот"
OLD_IMAGE=$(python3 -c "import json;print(json.load(open('$S/SERVICE.json'))['image_id'])")
[ "$(docker inspect -f '{{.Image}}' $C)" = "$OLD_IMAGE" ] || fail "работает не образ из SERVICE.json"
echo "проверки OK, сейчас $OLD_IMAGE"
[ "${APPLY:-0}" = 1 ] || { echo "DRY: дальше бэкап, сборка, compose.json/SERVICE.json, пересоздание $C"; exit 0; }

B=/root/backups/lte-canary-v2-$(date +%Y%m%d-%H%M%S)
mkdir -p "$B" && chmod 700 "$B" && cp -a "$S/runtime" "$B/runtime" && cp -p "$S/compose.json" "$S/SERVICE.json" "$B/" \
  || fail "бэкап не удался"
echo "бэкап: $B"
restore() {
  rm -rf "${S:?}/runtime" && cp -a "$B/runtime" "$S/runtime" && cp -p "$B/compose.json" "$B/SERVICE.json" "$S/"
}
rollback() {
  echo "### АВТООТКАТ $(date '+%T')"; restore
  docker compose -f "$S/compose.json" up -d --no-deps --no-build telemetry && echo "откачено на $OLD_IMAGE"
}
cp "$S/incoming/lte_telemetry.py" "$S/runtime/lte_telemetry.py"
MANIFEST=$(python3 - "$S" "$NEW" "$COMMIT" <<'PY'
import hashlib, json, sys
root, new, commit = sys.argv[1:]
path = f'{root}/runtime/SOURCE.json'
with open(path) as stream:
    manifest = json.load(stream)
manifest.pop('manifest_sha256')
manifest['files_sha256']['lte_telemetry.py'] = new
manifest['source_commit'] = commit
canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
manifest['manifest_sha256'] = hashlib.sha256(canonical).hexdigest()
with open(path, 'w') as stream:
    stream.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
print(manifest['manifest_sha256'])
PY
) || { restore; fail "SOURCE.json не пересчитан"; }
TAG=donbaton/lte-telemetry:${MANIFEST:0:12}
docker build --network=none --pull=false --platform=linux/amd64 \
  --build-arg BOT_IMAGE=$BASE_REF --build-arg XRAY_SHA256=$XRAY \
  --label org.donbaton.telemetry.owner=$MARKER --label org.donbaton.telemetry.source=$MANIFEST \
  -t "$TAG" "$S/runtime" > "$B/build.log" 2>&1 || { restore; fail "сборка не удалась, см. $B/build.log"; }
NEW_IMAGE=$(docker image inspect -f '{{.Id}}' "$TAG")
python3 - "$S" "$NEW_IMAGE" "$TAG" "$MANIFEST" "$COMMIT" <<'PY' || { restore; fail "compose.json/SERVICE.json не обновлены"; }
import hashlib, json, sys
root, image, tag, manifest, commit = sys.argv[1:]
compose_path, receipt_path = f'{root}/compose.json', f'{root}/SERVICE.json'
with open(compose_path) as stream:
    compose = json.load(stream)
service = compose['services']['telemetry']
service['image'] = image
service['labels']['org.donbaton.telemetry.source'] = manifest
with open(compose_path, 'w') as stream:
    stream.write(json.dumps(compose, indent=2, sort_keys=True) + '\n')
with open(receipt_path) as stream:
    receipt = json.load(stream)
with open(compose_path, 'rb') as stream:
    compose_sha = hashlib.sha256(stream.read()).hexdigest()
receipt.update(image_id=image, image_tag=tag, source_manifest_sha256=manifest, source_commit=commit,
               compose_sha256=compose_sha)
with open(receipt_path, 'w') as stream:
    stream.write(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
PY
docker compose -f "$S/compose.json" config --quiet || { restore; fail "compose.json не валиден"; }
T0=$(date -u +%FT%TZ); echo "### up $T0: $NEW_IMAGE"
docker compose -f "$S/compose.json" up -d --no-deps --no-build telemetry
for i in $(seq 1 28); do
  sleep 15
  L=$(docker logs --since "$T0" $C 2>&1)
  R=$(grep -c '"lte_telemetry_ready"' <<<"$L"); P=$(grep -c '"lte_telemetry_partial"' <<<"$L")
  U=$(grep -c '"lte_telemetry_unavailable"' <<<"$L")
  V=$(docker exec remnawave_bot_redis redis-cli GET $KEY | python3 -c \
    "import json,sys;d=sys.stdin.read().strip();print(json.loads(d).get('version'),json.loads(d).get('failed')) if d else print('нет')")
  echo "$(date '+%T') ready=$R partial=$P unavail=$U lease=$V"
  if [ $((R + P)) -ge 5 ]; then echo "OK: lease пишется"; break; fi
  if [ $((R + P)) = 0 ] && [ "$i" -ge 16 ] && [ "${AUTO_ROLLBACK:-1}" = 1 ]; then rollback; break; fi
done
docker logs --since "$T0" $C 2>&1 | grep lte_telemetry_route | tail -4
echo "бот, закрытия с T0: $(docker logs --since "$T0" "$BOT" 2>&1 | grep -c 'LTE telemetry missing beyond grace')"
echo "ОТКАТ вручную: восстановить $B/{runtime,compose.json,SERVICE.json} в $S и docker compose -f $S/compose.json up -d --no-deps --no-build telemetry"
```
