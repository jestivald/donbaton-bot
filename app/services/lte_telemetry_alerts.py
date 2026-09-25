"""Owner alerts about the LTE telemetry gate and its individual routes.

`run_quota_cycle` resolves the canary lease once per cycle and hands the result
here. This module turns that snapshot into at most one admin message per cycle,
and only on a change: a route unproven for longer than the delay and its
recovery, the gate holding on grace, closed for everyone and returned, plus a
periodic reminder while anything stays degraded.

What was announced is kept in Redis (`donbaton:lte:telemetry:<epoch>:alert-state`),
so a restart neither repeats nor forgets an alert. A failed delivery is retried
on the next cycle with that cycle's state, never as a stale message. Nothing
here may break or stall the quota cycle.
"""

import asyncio
import html
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Bypass names as the owner knows them, keyed by the LTE node UUID.
ROUTE_NAMES = {
    '85950bae-4671-409c-b090-bbc2adf6e7b3': 'Финляндия (spb-fra)',
    '97181e55-43ee-47c6-b45e-6ce188e57309': 'Германия (cdn-de1)',
    '516dd304-9a41-4507-8545-a46f92f6fe69': 'Польша (cdn-pl1)',
}
CODE_LABELS = {
    'unreachable': 'обход не пропускает запрос',
    'asset': 'через обход пришёл не тот контрольный файл',
    'panel': 'панель не подтвердила служебного пользователя или ноду',
    'not_persisted': 'трафик не записался в счётчик панели',
    'multiplier': 'множитель трафика ноды не равен 1',
    'wrong_node': 'трафик записан на другую ноду',
    'regressed': 'счётчик панели уменьшился',
    'unmonitored': 'канарейка этот маршрут не проверяет',
    'no_lease': 'канарейка не прислала подтверждения',
}
# The route may still carry traffic, but the panel is not counting it as sold.
ACCOUNTING_CODES = frozenset({'not_persisted', 'multiplier', 'wrong_node'})
STATE_TTL_SECONDS = 30 * 24 * 3600
SEND_TIMEOUT_SECONDS = 20
_REDIS_TIMEOUT_SECONDS = 2
# Titles in order of severity: the message takes the most severe one.
_TITLES = {
    'closed': '🔴 <b>LTE снят у всех</b>',
    'grace': '🟠 <b>LTE держится на grace</b>',
    'route_down': '🟠 <b>LTE: обход без подтверждения</b>',
    'reminder': '🔁 <b>LTE: напоминание</b>',
    'returned': '✅ <b>LTE возвращён</b>',
    'recovered': '✅ <b>LTE: подтверждение вернулось</b>',
    'route_up': '✅ <b>LTE: обход снова подтверждён</b>',
}
_RECOVERIES = frozenset({'returned', 'recovered', 'route_up'})

# The process keeps its own copy so a Redis outage cannot make every cycle look
# like the first one (which would repeat alerts); Redis carries it over restarts.
_memory: dict = {}


@dataclass(frozen=True, slots=True)
class Notice:
    kind: str
    lines: tuple[str, ...]
    apply: Callable[[dict], None]


def state_key(epoch):
    return f'donbaton:lte:telemetry:{epoch}:alert-state'


def route_name(node_id):
    return ROUTE_NAMES.get(node_id) or f'нода {node_id[:8]}'


async def report_cycle(evidence, *, bot=None, now=None):
    """Alert the owner about this cycle's changes; never raises into the quota cycle."""
    try:
        await _report(evidence, bot, _utc(now))
    except Exception as error:
        logger.warning('LTE telemetry alerts failed', error_type=type(error).__name__, error=str(error)[:200])


async def _report(evidence, bot, now):
    if getattr(evidence, 'state', None) not in ('live', 'grace', 'closed') or not (
        settings.DONBATON_LTE_ENABLED and settings.DONBATON_LTE_ACCOUNTING_READY
    ):
        return
    epoch = settings.DONBATON_LTE_ACCOUNTING_EPOCH
    if not epoch:
        return
    state = deepcopy(await _load(epoch))
    _observe(state, evidence, now)
    notices = _due(state, evidence, now)
    if notices and await _deliver(bot, _compose(notices, evidence)):
        for notice in notices:
            notice.apply(state)
            log = logger.info if notice.kind in _RECOVERIES else logger.warning
            log('LTE telemetry alert', kind=notice.kind, text=' '.join(notice.lines))
        state['notified_at'] = now.isoformat()
    await _store(epoch, state)


def _observe(state, evidence, now):
    """Record what the cycle shows; announcements are recorded only once delivered."""
    gate = state['gate']
    if gate['state'] != evidence.state:
        gate['state'], gate['since'] = evidence.state, now.isoformat()
    routes = state['routes']
    configured = set(evidence.proven) | set(evidence.missing)
    for node_id in [node_id for node_id in routes if node_id not in configured]:
        del routes[node_id]  # taken out of the configuration: nothing left to report
    for node_id in evidence.proven:
        if node_id in routes:
            routes[node_id]['up'] = True
    for node_id in evidence.missing:
        entry = routes.setdefault(node_id, {'since': now.isoformat(), 'code': None, 'announced': False, 'up': False})
        entry['up'] = False
        code = evidence.failed.get(node_id)
        # Without a current lease the last code the canary gave stays the best clue.
        if code or not entry['code']:
            entry['code'] = code or 'no_lease'


def _due(state, evidence, now):
    delay = settings.DONBATON_LTE_ROUTE_ALERT_DELAY_SECONDS
    gate = state['gate']
    notices = []
    current, announced = gate['state'], gate['announced']
    if current == 'closed' and announced != 'closed':
        notices.append(Notice('closed', (_closed_line(),), _announce_gate('closed')))
    elif current == 'grace' and announced != 'grace':
        # A short canary restart must not page anyone; a return from closed must.
        if announced == 'closed' or _seconds_since(gate['since'], now) >= delay:
            notices.append(Notice('grace', (_grace_line(evidence, announced),), _announce_gate('grace')))
    elif current == 'live' and announced != 'live':
        if announced == 'closed':
            notices.append(Notice('returned', (_returned_line(evidence),), _announce_gate('live')))
        elif announced == 'grace':
            notices.append(Notice('recovered', (_recovered_line(evidence),), _announce_gate('live')))
        else:
            gate['announced'] = 'live'  # first sight of a healthy gate: nothing to say
    for node_id, entry in sorted(state['routes'].items()):
        if entry['up']:
            if entry['announced']:
                notices.append(Notice('route_up', (_route_up_line(node_id, entry, now),), _forget(node_id)))
            else:
                del state['routes'][node_id]  # a blip shorter than the delay stays silent
        elif not entry['announced'] and _seconds_since(entry['since'], now) >= delay:
            notices.append(Notice('route_down', (_route_down_line(node_id, entry, now),), _announce_route(node_id)))
    if not notices:
        reminder = _reminder(state, evidence, now)
        if reminder:
            notices.append(Notice('reminder', reminder, lambda state: None))
    return notices


def _reminder(state, evidence, now):
    remind = settings.DONBATON_LTE_ROUTE_ALERT_REMIND_SECONDS
    if remind <= 0 or _seconds_since(state['notified_at'], now, default=0) < remind:
        return ()
    gate = state['gate']
    lines = []
    if gate['announced'] == 'closed' and gate['state'] == 'closed':
        lines.append(f'LTE снят у всех уже {_duration(_seconds_since(gate["since"], now))}.')
    elif gate['announced'] == 'grace' and gate['state'] == 'grace':
        lines.append(_grace_line(evidence, 'grace'))
    down = [
        _route_down_line(node_id, entry, now)
        for node_id, entry in sorted(state['routes'].items())
        if entry['announced'] and not entry['up']
    ]
    if not lines and not down:
        return ()
    return ('Всё ещё не в норме:', *lines, *down)


def _announce_gate(value):
    def apply(state):
        state['gate']['announced'] = value

    return apply


def _announce_route(node_id):
    def apply(state):
        state['routes'][node_id]['announced'] = True

    return apply


def _forget(node_id):
    def apply(state):
        state['routes'].pop(node_id, None)

    return apply


def _closed_line():
    grace = settings.DONBATON_LTE_TELEMETRY_GRACE_SECONDS
    window = f'дольше grace ({_duration(grace)})' if grace > 0 else '(grace выключен)'
    return (
        f'Ни один обход не подтверждён {window}: бот снимает LTE-сквад со всех подписок '
        'до первого нового подтверждения. Смотрите <code>docker logs donbaton-lte-telemetry</code> на ядре.'
    )


def _grace_line(evidence, announced):
    remaining = evidence.grace_remaining
    left = f'осталось {_duration(remaining)}' if isinstance(remaining, int) else 'срок неизвестен'
    if announced == 'closed':
        return f'Подтверждение было, LTE снова открыт, но держится на grace: {left}.'
    return f'Ни один обход не подтверждён. LTE пока открыт на grace: {left}, потом снимется у всех.'


def _returned_line(evidence):
    return f'Учёт снова подтверждён ({_names(evidence.proven)}): бот возвращает LTE-сквад подписчикам.'


def _recovered_line(evidence):
    return f'Учёт снова подтверждён ({_names(evidence.proven)}) до конца grace — LTE не снимался.'


def _route_down_line(node_id, entry, now):
    code = entry['code'] or 'no_lease'
    label = CODE_LABELS.get(code, 'неизвестная причина')
    line = (
        f'• {html.escape(route_name(node_id))} — нет подтверждения {_duration(_seconds_since(entry["since"], now))}: '
        f'{label} (<code>{html.escape(code)}</code>).'
    )
    if code in ACCOUNTING_CODES:
        line += ' ⚠️ Учёт под вопросом — проверьте ноду в панели.'
    return line


def _route_up_line(node_id, entry, now):
    return (
        f'• {html.escape(route_name(node_id))} — снова подтверждён '
        f'(не было {_duration(_seconds_since(entry["since"], now))}).'
    )


def _names(node_ids):
    return ', '.join(html.escape(route_name(node_id)) for node_id in node_ids) or 'нет'


def _compose(notices, evidence):
    kinds = {notice.kind for notice in notices}
    title = next(text for kind, text in _TITLES.items() if kind in kinds)
    lines = [title, '']
    for notice in notices:
        lines.extend(notice.lines)
    if kinds & {'route_down', 'route_up', 'reminder'} and evidence.state == 'live':
        lines += ['', f'LTE открыт: подтверждены {_names(evidence.proven)}. Сквады не менялись.']
    return '\n'.join(lines)


async def _deliver(bot, text):
    """True once the owner has it, or when there is nowhere to send; False means retry."""
    if bot is None:
        return True
    from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

    category = NotificationCategory.INFRASTRUCTURE
    service = AdminNotificationService(bot)
    if not service.is_enabled or not service.category_enabled.get(category, True):
        return True
    try:
        # Flood control inside the sender may wait up to 30 s per attempt; the
        # quota cycle must not stall behind it.
        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
            sent = await service.send_admin_notification(text, category=category)
    except Exception as error:
        logger.warning('LTE telemetry alert not delivered, will retry', error_type=type(error).__name__)
        return False
    if not sent:
        logger.warning('LTE telemetry alert not delivered, will retry')
    return bool(sent)


def _fresh_state():
    return {
        'version': 1,
        'gate': {'state': None, 'since': None, 'announced': None},
        'routes': {},
        'notified_at': None,
    }


def _restore(stored):
    """A stored state from Redis, or a fresh one if it is absent or unreadable."""
    try:
        if stored['version'] != 1:
            return _fresh_state()
        gate = stored['gate']
        return {
            'version': 1,
            'gate': {key: gate.get(key) for key in ('state', 'since', 'announced')},
            'routes': {
                str(node_id): {
                    'since': entry['since'],
                    'code': entry.get('code'),
                    'announced': bool(entry.get('announced')),
                    'up': bool(entry.get('up')),
                }
                for node_id, entry in stored['routes'].items()
            },
            'notified_at': stored.get('notified_at'),
        }
    except (KeyError, TypeError, AttributeError):
        return _fresh_state()


async def _load(epoch):
    if _memory.get('epoch') == epoch:
        return _memory['state']
    from app.utils.cache import cache

    stored = None
    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            stored = await cache.get(state_key(epoch))
    except TimeoutError:
        logger.warning('LTE telemetry alert state read timed out')
    state = _restore(stored)
    _memory.clear()
    _memory.update(epoch=epoch, state=state, persisted=deepcopy(state) if stored is not None else None)
    return state


async def _store(epoch, state):
    _memory.update(epoch=epoch, state=state)
    if _memory.get('persisted') == state:
        return
    from app.utils.cache import cache

    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            saved = await cache.set(state_key(epoch), state, expire=STATE_TTL_SECONDS)
    except TimeoutError:
        saved = False
    if saved:
        _memory['persisted'] = deepcopy(state)


def _utc(value):
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _seconds_since(value, now, *, default=None):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return float('inf') if default is None else default
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((now - moment).total_seconds()))


def _duration(seconds):
    seconds = int(seconds) if seconds != float('inf') else 0
    if seconds < 60:
        return f'{seconds} с'
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes} мин'
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours} ч {minutes} мин' if minutes else f'{hours} ч'
    days, hours = divmod(hours, 24)
    return f'{days} д {hours} ч' if hours else f'{days} д'
