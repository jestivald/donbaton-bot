"""Upgrade an existing schema without touching legacy paid values."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.database.models import Base, LteOrphanQuota, LteQuota, LteTopup
from app.services.backup_service import backup_service


def migration(filename='0119_donbaton_lte_quotas.py'):
    path = Path(__file__).resolve().parents[2] / 'migrations/alembic/versions' / filename
    spec = importlib.util.spec_from_file_location(f'donbaton_{filename[:4]}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preserves_legacy_rows_and_matches_new_table_metadata():
    engine = sa.create_engine('sqlite:///:memory:')
    fresh = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE tariffs(id INTEGER PRIMARY KEY, traffic_limit_gb INTEGER)'))
        conn.execute(
            sa.text(
                'CREATE TABLE subscriptions(id INTEGER PRIMARY KEY, traffic_limit_gb INTEGER, purchased_traffic_gb INTEGER)'
            )
        )
        conn.execute(sa.text('INSERT INTO tariffs VALUES (4,1024),(5,2048)'))
        conn.execute(sa.text('INSERT INTO subscriptions VALUES (1,1524,500)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration().upgrade()
            migration().upgrade()  # create_all + stamp / retried upgrade remain compatible
            migration('0120_legacy_lte_compatibility.py').upgrade()
            migration('0120_legacy_lte_compatibility.py').upgrade()
        assert conn.execute(
            sa.text('SELECT traffic_limit_gb, purchased_traffic_gb, network_category FROM subscriptions')
        ).all() == [(1524, 500, None)]
        assert conn.execute(sa.text('SELECT COUNT(*) FROM lte_quotas')).scalar_one() == 0
    Base.metadata.create_all(fresh, tables=[LteQuota.__table__, LteTopup.__table__, LteOrphanQuota.__table__])
    for table in ('lte_quotas', 'lte_topups', 'lte_orphan_quotas'):
        existing = {c['name']: str(c['type']) for c in sa.inspect(engine).get_columns(table)}
        expected = {c['name']: str(c['type']) for c in sa.inspect(fresh).get_columns(table)}
        assert existing == expected
        assert {i['name'] for i in sa.inspect(engine).get_indexes(table)} == {
            i['name'] for i in sa.inspect(fresh).get_indexes(table)
        }
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration('0120_legacy_lte_compatibility.py').downgrade()
            migration().downgrade()
        assert conn.execute(sa.text('SELECT traffic_limit_gb, purchased_traffic_gb FROM subscriptions')).all() == [
            (1524, 500)
        ]
    engine.dispose()
    fresh.dispose()


def test_backup_round_trips_lte_bytes_and_dates():
    models = backup_service._get_models_for_backup(True)
    assert LteQuota in models and LteTopup in models and LteOrphanQuota in models
    assert next(i for i, m in enumerate(models) if m.__tablename__ == 'subscriptions') < models.index(LteQuota)
    restored = backup_service._process_record_data(
        {
            'subscription_id': 1,
            'limit_bytes': '64424509440',
            'counter_bytes': '987654321000',
            'period_ends_at': '2026-10-09T12:00:00+00:00',
            'accounting_state': 'ready',
        },
        LteQuota,
        'lte_quotas',
    )
    assert restored['limit_bytes'] == 64424509440
    assert restored['counter_bytes'] == 987654321000
    assert restored['period_ends_at'].isoformat() == '2026-10-09T12:00:00+00:00'


def test_0120_does_not_convert_existing_0119_balances_and_protects_imported_rights():
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE subscriptions(id INTEGER PRIMARY KEY)'))
        conn.execute(sa.text('INSERT INTO subscriptions VALUES (1)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration().upgrade()
        conn.execute(
            sa.text(
                'INSERT INTO lte_quotas(subscription_id,limit_bytes,used_bytes,base_used_bytes,period_days,'
                'period_starts_at,period_ends_at,counter_bytes) VALUES '
                "(1,64424509440,123,123,30,'2026-09-09','2026-10-09',9123456789012345)"
            )
        )
        before = conn.execute(
            sa.text('SELECT limit_bytes,used_bytes,base_used_bytes,counter_bytes FROM lte_quotas')
        ).all()
        with Operations.context(MigrationContext.configure(conn)):
            migration('0120_legacy_lte_compatibility.py').upgrade()
        assert (
            conn.execute(sa.text('SELECT limit_bytes,used_bytes,base_used_bytes,counter_bytes FROM lte_quotas')).all()
            == before
        )
        assert conn.execute(
            sa.text('SELECT legacy_state,trial_ends_at,panel_access_cleared_at FROM lte_quotas')
        ).one() == (None, None, None)
        assert conn.execute(sa.text('SELECT COUNT(*) FROM lte_orphan_quotas')).scalar_one() == 0
        conn.execute(sa.text("UPDATE lte_quotas SET legacy_state='{}'"))
        with (
            Operations.context(MigrationContext.configure(conn)),
            pytest.raises(RuntimeError, match='forward recovery'),
        ):
            migration('0120_legacy_lte_compatibility.py').downgrade()
    engine.dispose()
