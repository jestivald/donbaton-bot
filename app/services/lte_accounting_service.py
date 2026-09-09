"""Pure arithmetic and calendar rules for preserving legacy LTE entitlements.

Historical weighted counters are never labelled as physical traffic. The exact
current budget remains a rational number; only its displayed/access capacity is
rounded up by less than one byte. Lifetime is an opaque epoch baseline.
"""

from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo


class LegacyAccountingError(ValueError):
    """The supplied snapshot cannot establish an unambiguous entitlement."""


def valid_telemetry_heartbeat(payload, *, epoch, node_ids, now, max_age_seconds):
    """Trusted producer evidence for both complete node→panel accounting paths."""
    if not isinstance(payload, dict) or type(payload.get('version')) is not int or payload['version'] != 1:
        return False
    if not isinstance(epoch, str) or not epoch or payload.get('epoch') != epoch:
        return False
    try:
        expected = {str(UUID(node_id)) for node_id in node_ids}
        observed_nodes = payload['node_ids']
        if not isinstance(observed_nodes, list) or len(observed_nodes) != 2 or len(expected) != 2:
            return False
        if {str(UUID(node_id)) for node_id in observed_nodes} != expected:
            return False
        observed = timestamp(payload['observed_at'])
        age = timestamp(now) - observed
        return timedelta(0) <= age <= timedelta(seconds=max_age_seconds)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _integer(value: int, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise LegacyAccountingError(f'{name} must be an integer >= {minimum}')
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LegacyAccountingError('Accounting timestamps must have an explicit timezone')
    return value.astimezone(UTC)


def ceil_units(units: int, divisor: int) -> int:
    """Project a nonnegative rational budget without losing the final byte."""
    _integer(units, 'units')
    _integer(divisor, 'divisor', minimum=1)
    return (units + divisor - 1) // divisor


@dataclass(frozen=True, slots=True)
class LegacyBudget:
    limit_units: int
    used_units: int
    divisor: int = 17

    def __post_init__(self):
        _integer(self.limit_units, 'limit_units')
        _integer(self.used_units, 'used_units')
        _integer(self.divisor, 'divisor', minimum=1)

    @property
    def remaining_units(self) -> int:
        return max(0, self.limit_units - self.used_units)

    @property
    def remaining_bytes(self) -> int:
        return ceil_units(self.remaining_units, self.divisor)

    @property
    def limit_bytes(self) -> int:
        return ceil_units(self.limit_units, self.divisor)

    @property
    def displayed_used_bytes(self) -> int:
        # Rounding limit and used independently can lose the last paid byte.
        return self.limit_bytes - self.remaining_bytes

    def consume(self, physical_bytes: int) -> 'LegacyBudget':
        """Only newly observed physical bytes are converted to legacy units."""
        _integer(physical_bytes, 'physical_bytes')
        return LegacyBudget(self.limit_units, self.used_units + physical_bytes * self.divisor, self.divisor)


def physical_counter_delta(
    *,
    previous_panel_id: int,
    current_panel_id: int,
    previous_counter: int,
    current_counter: int,
    expected_epoch: str,
    sample_epoch: str,
) -> int:
    """Subtract opaque counters only inside an explicitly established 1:1 epoch."""
    _integer(previous_panel_id, 'previous_panel_id', minimum=1)
    _integer(current_panel_id, 'current_panel_id', minimum=1)
    _integer(previous_counter, 'previous_counter')
    _integer(current_counter, 'current_counter')
    if not isinstance(expected_epoch, str) or not expected_epoch or expected_epoch != sample_epoch:
        raise LegacyAccountingError('A mixed or unknown counter epoch cannot be charged')
    if previous_panel_id != current_panel_id:
        raise LegacyAccountingError('Panel identity changed')
    if current_counter < previous_counter:
        raise LegacyAccountingError('Lifetime counter regressed')
    return current_counter - previous_counter


def _month_offset(value: datetime, offset: int, *, day: int, hour: int, minute: int) -> datetime:
    year, month_index = divmod(value.year * 12 + value.month - 1 + offset, 12)
    month = month_index + 1
    return datetime(year, month, min(day, monthrange(year, month)[1]), hour, minute, tzinfo=value.tzinfo)


def next_legacy_reset(
    *,
    created_at: datetime,
    after: datetime,
    strategy: str,
    timezone_name: str,
) -> datetime | None:
    """Next scheduled boundary from Remnawave 3.4.3, strictly after `after`.

    The caller supplies the verified scheduler/database timezone. A manual
    `lastTrafficResetAt` is deliberately not an anchor for MONTH_ROLLING.
    """
    _utc(created_at)
    _utc(after)
    timezone = ZoneInfo(timezone_name)
    created, current = created_at.astimezone(timezone), after.astimezone(timezone)
    if current < created:
        raise LegacyAccountingError('A reset cannot be planned before panel-user creation')
    if strategy == 'NO_RESET':
        return None
    if strategy in ('MONTH_ROLLING', 'MONTH'):
        day, minute = (created.day, 10) if strategy == 'MONTH_ROLLING' else (1, 20)
        candidate = _month_offset(current, 0, day=day, hour=0, minute=minute)
        # PostgreSQL: (created_at + interval '1 month')::date <= CURRENT_DATE.
        first_eligible = _month_offset(created, 1, day=created.day, hour=0, minute=0).date()
        if candidate <= current or (strategy == 'MONTH_ROLLING' and candidate.date() < first_eligible):
            candidate = _month_offset(current, 1, day=day, hour=0, minute=minute)
        return candidate.astimezone(UTC)
    if strategy == 'DAY':
        candidate = current.replace(hour=0, minute=5, second=0, microsecond=0)
        if candidate <= current:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)
    if strategy == 'WEEK':
        candidate = current.replace(hour=0, minute=15, second=0, microsecond=0)
        candidate += timedelta(days=(-candidate.weekday()) % 7)
        if candidate <= current:
            candidate += timedelta(days=7)
        return candidate.astimezone(UTC)
    raise LegacyAccountingError(f'Unsupported legacy reset strategy: {strategy}')


def timestamp(value) -> datetime:
    """Strict, serializable boundary used by the private import contract."""
    try:
        return _utc(datetime.fromisoformat(value) if isinstance(value, str) else value)
    except (ValueError, TypeError) as error:
        raise LegacyAccountingError('Invalid accounting timestamp') from error


def build_legacy_state(*, snapshot, migration_id, plan_sha256, counter_epoch, target_limit_bytes, now):
    """Validate an explicit final snapshot; no guesses from rounded bot usage."""
    if not isinstance(snapshot, dict) or type(snapshot.get('version')) is not int or snapshot['version'] != 1:
        raise LegacyAccountingError('Unsupported legacy snapshot version')
    for name, value in (('migration_id', migration_id), ('counter_epoch', counter_epoch)):
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise LegacyAccountingError(f'Invalid {name}')
    if (
        not isinstance(plan_sha256, str)
        or len(plan_sha256) != 64
        or any(character not in '0123456789abcdef' for character in plan_sha256)
    ):
        raise LegacyAccountingError('Invalid plan SHA256')
    source = deepcopy(snapshot)
    try:
        if source['divisor'] != 17 or type(source['divisor']) is not int:
            raise LegacyAccountingError('This import preserves the verified legacy divisor 17')
        if source['reset_timezone'] != 'UTC':
            raise LegacyAccountingError('The verified legacy panel scheduler timezone is UTC')
        if type(source['renewal_requires_expiry']) is not bool:
            raise LegacyAccountingError('renewal_requires_expiry must be explicit')
        panel = source['panel']
        _integer(panel['id'], 'panel.id', minimum=1)
        for name in ('limit_bytes', 'used_bytes', 'lifetime_bytes'):
            _integer(panel[name], f'panel.{name}')
        if panel['status'] not in ('ACTIVE', 'LIMITED', 'DISABLED', 'EXPIRED'):
            raise LegacyAccountingError('Unknown panel status')
        for name in ('created_at', 'expire_at'):
            panel[name] = timestamp(panel[name]).isoformat()
        panel['last_reset_at'] = timestamp(panel['last_reset_at']).isoformat() if panel['last_reset_at'] else None
        captured = timestamp(source['captured_at'])
        source['captured_at'] = captured.isoformat()
        now = timestamp(now)
        if captured > now:
            raise LegacyAccountingError('Snapshot was captured in the future')
        if panel['last_reset_at'] and timestamp(panel['last_reset_at']) > captured:
            raise LegacyAccountingError('Panel reset is newer than the snapshot')
        next_reset = next_legacy_reset(
            created_at=timestamp(panel['created_at']),
            after=captured,
            strategy=panel['reset_strategy'],
            timezone_name=source['reset_timezone'],
        )
        subscription = source['subscription']
        if subscription is not None:
            for name in ('id', 'user_id'):
                _integer(subscription[name], f'subscription.{name}', minimum=1)
            for name in ('device_limit', 'traffic_limit_gb', 'purchased_traffic_gb'):
                _integer(subscription[name], f'subscription.{name}')
            if type(subscription['is_trial']) is not bool:
                raise LegacyAccountingError('subscription.is_trial must be a boolean')
            if subscription['tariff_id'] is not None:
                _integer(subscription['tariff_id'], 'subscription.tariff_id', minimum=1)
            subscription['end_date'] = timestamp(subscription['end_date']).isoformat()
            subscription['traffic_reset_at'] = (
                timestamp(subscription['traffic_reset_at']).isoformat() if subscription['traffic_reset_at'] else None
            )
            paid_until = timestamp(subscription['end_date'])
            if abs(paid_until - timestamp(panel['expire_at'])) > timedelta(milliseconds=1):
                raise LegacyAccountingError('Bot and panel paid end disagree')
        else:
            paid_until = timestamp(panel['expire_at'])
        grants = source['grants']
        if not isinstance(grants, list) or (subscription is None and grants):
            raise LegacyAccountingError('Orphan snapshots cannot invent bot purchases')
        identifiers = set()
        for grant in grants:
            _integer(grant['id'], 'grant.id', minimum=1)
            _integer(grant['amount_units'], 'grant.amount_units', minimum=1)
            if grant['id'] in identifiers:
                raise LegacyAccountingError('Duplicate legacy grant')
            identifiers.add(grant['id'])
            for name in ('created_at', 'expires_at'):
                grant[name] = timestamp(grant[name]).isoformat()
            if timestamp(grant['created_at']) > captured:
                raise LegacyAccountingError('A grant was created after the snapshot')
        source['grants'] = sorted(grants, key=lambda grant: grant['id'])
        total_grants = sum(grant['amount_units'] for grant in grants)
        unlimited = panel['limit_bytes'] == 0
        if subscription is not None:
            if panel['limit_bytes'] != subscription['traffic_limit_gb'] * 1024**3:
                raise LegacyAccountingError('Bot and panel current limits disagree')
            if total_grants != subscription['purchased_traffic_gb'] * 1024**3:
                raise LegacyAccountingError('Installed purchases disagree with the complete grant snapshot')
        if not unlimited and total_grants > panel['limit_bytes']:
            raise LegacyAccountingError('Purchased capacity exceeds the installed current limit')
        _integer(target_limit_bytes, 'target_limit_bytes')
    except (KeyError, TypeError, AttributeError) as error:
        raise LegacyAccountingError('Incomplete legacy snapshot') from error
    return {
        'version': 1,
        'migration_id': migration_id,
        'plan_sha256': plan_sha256,
        'counter_epoch': counter_epoch,
        'prepared_at': now.isoformat(),
        'activated_at': None,
        'snapshot': source,
        'original_paid_until': paid_until.isoformat(),
        'unlimited': unlimited,
        'target_limit_bytes': target_limit_bytes,
        'runtime': {
            'phase': 'legacy',
            'base_units': max(0, panel['limit_bytes'] - total_grants),
            'limit_units': panel['limit_bytes'],
            'used_units': panel['used_bytes'],
            'base_expiry_credit_units': 0,
            'base_expiry_used_units': 0,
            'installed_grant_ids': sorted(identifiers),
            'base_ended': False,
            'next_reset_at': next_reset.isoformat() if next_reset else None,
            'last_reset_at': panel['last_reset_at'],
            'last_advanced_at': captured.isoformat(),
            'new_period_started_at': None,
        },
    }


def legacy_budget(state) -> LegacyBudget:
    runtime = state['runtime']
    # Offset only the debt present when the base expired. Unused old base is
    # not a second spendable allowance after expiry; every later byte counts.
    boundary_used = runtime['base_expiry_used_units']
    used = max(0, boundary_used - runtime['base_expiry_credit_units'])
    used += max(0, runtime['used_units'] - boundary_used)
    return LegacyBudget(runtime['limit_units'], used, state['snapshot']['divisor'])


def legacy_is_unlimited(state) -> bool:
    return state['unlimited'] and not state['runtime']['base_ended']


def legacy_compatibility_end(state) -> datetime:
    original = timestamp(state['original_paid_until'])
    converted = state['runtime'].get('trial_converted_at')
    return min(original, timestamp(converted)) if converted else original


def legacy_pool_boundary(state) -> datetime:
    """The next loss/reset of aggregate capacity, for expiry-ordered spending."""
    runtime = state['runtime']
    boundaries = []
    if not runtime['base_ended']:
        boundaries.append(legacy_compatibility_end(state))
    if runtime['next_reset_at']:
        boundaries.append(timestamp(runtime['next_reset_at']))
    current = timestamp(runtime['last_advanced_at'])
    boundaries.extend(
        timestamp(grant['expires_at'])
        for grant in state['snapshot']['grants']
        if grant['id'] in runtime['installed_grant_ids'] and timestamp(grant['expires_at']) > current
    )
    return min(boundaries) if boundaries else timestamp(state['original_paid_until'])


def _retire_legacy_grants(state, at, *, reset=False):
    runtime = state['runtime']
    installed = [grant for grant in state['snapshot']['grants'] if grant['id'] in runtime['installed_grant_ids']]
    expired = [grant for grant in installed if timestamp(grant['expires_at']) <= at]
    base = 0 if runtime['base_ended'] else runtime['base_units']
    desired = base + sum(grant['amount_units'] for grant in installed if grant not in expired)
    if reset:
        runtime['used_units'] = 0
        runtime['base_expiry_credit_units'] = 0
        runtime['base_expiry_used_units'] = 0
    deferred = (
        expired
        and not reset
        and desired > 0
        and legacy_budget(state).used_units > desired
        and state['snapshot']['panel']['reset_strategy'] != 'NO_RESET'
        and at - max(timestamp(grant['expires_at']) for grant in expired) <= timedelta(days=40)
    )
    if expired and not deferred:
        runtime['installed_grant_ids'] = [grant['id'] for grant in installed if grant not in expired]
        if desired > 0 and legacy_budget(state).used_units > desired:
            # Preserve the old daily housekeeping's forced clamp for NO_RESET
            # and its 40-day fallback. This never resets new purchased buckets.
            runtime['used_units'] = 0
            runtime['base_expiry_credit_units'] = 0
            runtime['base_expiry_used_units'] = 0
    runtime['limit_units'] = base + sum(
        grant['amount_units'] for grant in installed if grant['id'] in runtime['installed_grant_ids']
    )
    runtime['phase'] = (
        'legacy' if not runtime['base_ended'] else ('sidecar' if runtime['installed_grant_ids'] else 'finished')
    )
    if runtime['phase'] == 'finished':
        runtime['next_reset_at'] = None


def advance_legacy_state(state, now):
    """Replay calendar/paid-end boundaries once, without inventing bucket usage.

    An observed delta is allocated before calling this function. When a sample
    crosses a boundary its timing is unknown; the service deliberately charges
    that delta to the preceding interval, matching ordinary LTE accounting.
    """
    result = deepcopy(state)
    runtime = result['runtime']
    now = timestamp(now)
    if now < timestamp(runtime['last_advanced_at']):
        return result
    paid_until = legacy_compatibility_end(result)
    while True:
        boundaries = []
        if not runtime['base_ended'] and paid_until <= now:
            boundaries.append(paid_until)
        reset_at = timestamp(runtime['next_reset_at']) if runtime['next_reset_at'] else None
        if reset_at and reset_at <= now:
            boundaries.append(reset_at)
        if not boundaries:
            break
        event_at = min(boundaries)
        if event_at == paid_until:
            runtime['base_ended'] = True
            # Old per-grant usage was never recorded. Preserve the greatest
            # possible remaining paid pool instead of pretending base usage
            # consumed the topups: min(T, max(0, B + T - U)). Raw U is retained
            # and this explicit credit expires at the next old calendar reset.
            runtime['base_expiry_credit_units'] = (
                runtime['used_units'] if result['unlimited'] else runtime['base_units']
            )
            runtime['base_expiry_used_units'] = runtime['used_units']
        resetting = reset_at == event_at
        if resetting:
            runtime['last_reset_at'] = event_at.isoformat()
            next_reset = next_legacy_reset(
                created_at=timestamp(result['snapshot']['panel']['created_at']),
                after=event_at,
                strategy=result['snapshot']['panel']['reset_strategy'],
                timezone_name=result['snapshot']['reset_timezone'],
            )
            runtime['next_reset_at'] = next_reset.isoformat() if next_reset else None
        _retire_legacy_grants(result, event_at, reset=resetting)
    _retire_legacy_grants(result, now)
    runtime['last_advanced_at'] = now.isoformat()
    return result
