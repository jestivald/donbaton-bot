"""Схема 0135 совпадает с моделью; уникальность, FK и откат проверяются отдельно."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.database.models import AiSupportRequest, Ticket, User
from tests.fixtures.postgres_db import postgres_engine


MIGRATION = Path('migrations/alembic/versions/0135_ai_support.py')


def migration():
    spec = importlib.util.spec_from_file_location('m0135', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(connection, step):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration(), step)()


def snapshot(connection):
    inspector = sa.inspect(connection)
    table = 'ai_support_requests'
    return {
        'columns': {c['name']: (str(c['type']), c['nullable']) for c in inspector.get_columns(table)},
        'indexes': {i['name'] for i in inspector.get_indexes(table)},
        'unique': {tuple(c['column_names']) for c in inspector.get_unique_constraints(table)},
        'fk': {
            (tuple(c['constrained_columns']), c['referred_table'], c['options'].get('ondelete'))
            for c in inspector.get_foreign_keys(table)
        },
    }


def test_sqlite_schema_parity_unique_and_downgrade():
    assert (migration().revision, migration().down_revision) == ('0135', '0134')
    engines = [sa.create_engine('sqlite:///:memory:') for _ in range(2)]
    try:
        for engine in engines:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
                connection.exec_driver_sql('CREATE TABLE tickets (id INTEGER PRIMARY KEY)')
        AiSupportRequest.__table__.create(engines[0])
        with engines[1].begin() as connection:
            run(connection, 'upgrade')
        with engines[0].connect() as fresh, engines[1].connect() as upgraded:
            assert snapshot(fresh) == snapshot(upgraded)
            assert (('user_id',), 'users', 'SET NULL') in snapshot(fresh)['fk']
            assert (('ticket_id',), 'tickets', 'SET NULL') in snapshot(fresh)['fk']
        with engines[1].begin() as connection:
            for trigger in (None, None, 123):
                connection.execute(
                    AiSupportRequest.__table__.insert().values(
                        channel='ticket',
                        model='deepseek-flash',
                        provider='deepseek',
                        trigger_message_id=trigger,
                    )
                )
        with pytest.raises(IntegrityError), engines[1].begin() as connection:
            connection.execute(
                AiSupportRequest.__table__.insert().values(
                    channel='ticket',
                    model='deepseek-flash',
                    provider='deepseek',
                    trigger_message_id=123,
                )
            )
        with engines[1].begin() as connection:
            run(connection, 'downgrade')
        assert 'ai_support_requests' not in sa.inspect(engines[1]).get_table_names()
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.postgres
async def test_postgres_upgrade_parity_downgrade(postgres_database):
    async with postgres_engine(postgres_database, [User.__table__, Ticket.__table__]) as engine:
        async with engine.begin() as connection:
            try:
                await connection.run_sync(lambda c: AiSupportRequest.__table__.drop(c, checkfirst=True))
                await connection.run_sync(run, 'upgrade')
                upgraded = await connection.run_sync(snapshot)
                await connection.run_sync(run, 'downgrade')
                assert not await connection.run_sync(lambda c: sa.inspect(c).has_table('ai_support_requests'))
                await connection.run_sync(lambda c: AiSupportRequest.__table__.create(c))
                fresh = await connection.run_sync(snapshot)
                assert fresh == upgraded
            finally:
                await connection.run_sync(lambda c: AiSupportRequest.__table__.create(c, checkfirst=True))
