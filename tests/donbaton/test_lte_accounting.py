"""Exact migration primitives: current purchasing power, epoch and old calendar."""

from datetime import UTC, datetime

import pytest

from app.services.lte_accounting_service import (
    LegacyAccountingError,
    LegacyBudget,
    next_legacy_reset,
    physical_counter_delta,
)


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


@pytest.mark.parametrize('remaining', [0, 1, 16, 17, 18, 2**53 + 17, 1024**4 - 999])
def test_snapshot_remaining_conserves_exact_units_with_customer_favouring_final_byte(remaining):
    budget = LegacyBudget(remaining + 123456789012345678, 123456789012345678)
    assert budget.remaining_units == remaining
    assert budget.remaining_bytes * 17 >= remaining
    assert budget.remaining_bytes * 17 - remaining < 17
    assert budget.limit_bytes - budget.displayed_used_bytes == budget.remaining_bytes
    exhausted = budget.consume(budget.remaining_bytes)
    assert exhausted.remaining_bytes == 0
    assert budget.remaining_units == remaining  # the immutable snapshot was not mutated


def test_overshoot_is_retained_and_cannot_create_negative_remaining():
    budget = LegacyBudget(1024**4, 1024**4 + 3)
    assert budget.used_units > budget.limit_units
    assert budget.remaining_bytes == 0
    assert budget.consume(2).used_units == 1024**4 + 37


@pytest.mark.parametrize('bad', [-1, 1.5, True, '17'])
def test_weighted_and_physical_counters_reject_float_boolean_or_negative_values(bad):
    with pytest.raises(LegacyAccountingError):
        LegacyBudget(bad, 0)
    with pytest.raises(LegacyAccountingError):
        LegacyBudget(100, 0).consume(bad)


def test_lifetime_is_only_subtracted_inside_same_confirmed_epoch():
    baseline = 9_223_372_036_854_000_001  # deliberately not divisible by 17 or safe as JS Number
    arguments = dict(
        previous_panel_id=22,
        current_panel_id=22,
        previous_counter=baseline,
        current_counter=baseline + 3,
        expected_epoch='cutover:confirmed',
        sample_epoch='cutover:confirmed',
    )
    assert physical_counter_delta(**arguments) == 3
    assert LegacyBudget(170, 17).consume(physical_counter_delta(**arguments)).remaining_units == 102
    for change in (
        {'current_panel_id': 23},
        {'current_counter': baseline - 1},
        {'sample_epoch': 'old17'},
        {'expected_epoch': '', 'sample_epoch': ''},
    ):
        with pytest.raises(LegacyAccountingError):
            physical_counter_delta(**dict(arguments, **change))


@pytest.mark.parametrize(
    ('created', 'after', 'expected'),
    [
        ('2026-01-31T17:24:00', '2026-02-01T12:00:00', '2026-02-28T00:10:00'),
        ('2024-01-31T17:24:00', '2024-02-01T12:00:00', '2024-02-29T00:10:00'),
        ('2026-01-31T17:24:00', '2026-02-28T00:10:00', '2026-03-31T00:10:00'),
        ('2026-01-30T10:00:00', '2026-02-28T00:10:00', '2026-03-30T00:10:00'),
        ('2026-08-09T23:59:00', '2026-09-09T00:09:59', '2026-09-09T00:10:00'),
        ('2026-08-09T23:59:00', '2026-09-09T00:10:00', '2026-10-09T00:10:00'),
        ('2026-08-09T23:59:00', '2026-08-10T00:10:00', '2026-09-09T00:10:00'),
    ],
)
def test_month_rolling_uses_original_creation_day_and_short_month_clamp(created, after, expected):
    assert next_legacy_reset(
        created_at=stamp(created), after=stamp(after), strategy='MONTH_ROLLING', timezone_name='UTC'
    ) == stamp(expected)


@pytest.mark.parametrize(
    ('strategy', 'expected'),
    [
        ('DAY', '2026-09-10T00:05:00'),
        ('WEEK', '2026-09-14T00:15:00'),
        ('MONTH', '2026-10-01T00:20:00'),
        ('NO_RESET', None),
    ],
)
def test_other_existing_reset_strategies_have_explicit_boundaries(strategy, expected):
    actual = next_legacy_reset(
        created_at=stamp('2026-08-09T00:00:00'),
        after=stamp('2026-09-09T12:00:00'),
        strategy=strategy,
        timezone_name='UTC',
    )
    assert actual == (stamp(expected) if expected else None)


def test_naive_or_unknown_calendar_inputs_do_not_silently_assume_a_reset():
    values = dict(
        created_at=stamp('2026-08-09T00:00:00'),
        after=stamp('2026-09-09T12:00:00'),
        strategy='MONTH_ROLLING',
        timezone_name='UTC',
    )
    for change in ({'strategy': '30_days'}, {'after': datetime(2026, 9, 9)}, {'after': stamp('2026-08-01T12:00:00')}):
        with pytest.raises(LegacyAccountingError):
            next_legacy_reset(**dict(values, **change))
