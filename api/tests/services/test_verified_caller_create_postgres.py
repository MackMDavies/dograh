"""Actual create_campaign method against disposable local PostgreSQL.

Run --noconftest: does not load the voice service's database/env fixtures.
The reduced model holds only the columns used by the actual method. No calls,
network listeners, production credentials or project dependency changes.
"""
import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.tests.services.test_verified_caller_gate import load_actual_function, HANDOFF


def test_concurrent_marked_create_is_idempotent_and_legacy_creates_stay_independent():
    sa = pytest.importorskip('sqlalchemy')
    pytest.importorskip('asyncpg')
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase, mapped_column

    binaries = Path('/usr/local/opt/postgresql@16/bin')
    if not (binaries / 'initdb').exists():
        pytest.skip('Local PostgreSQL 16 tools are required for the isolated proof')
    # Short socket path avoids macOS sockaddr_un's 104-byte path limit.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='crm-create-', dir='/private/tmp') as root:
        folder = Path(root)
        def pg(tool, *args):
            result = subprocess.run([str(binaries / tool), *map(str, args)], capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
        pg('initdb', '-D', folder / 'data', '-A', 'trust', '-U', 'postgres')
        pg('pg_ctl', '-D', folder / 'data', '-l', folder / 'pg.log', '-o', f'-F -k {folder} -p 55487 -c listen_addresses=', '-w', 'start')
        try:
            class Base(DeclarativeBase):
                pass
            class Campaign(Base):
                __tablename__ = 'campaigns'
                id = mapped_column(sa.Integer, primary_key=True)
                name = mapped_column(sa.Text)
                workflow_id = mapped_column(sa.Integer)
                source_type = mapped_column(sa.Text)
                source_id = mapped_column(sa.Text)
                created_by = mapped_column(sa.Integer)
                organization_id = mapped_column(sa.Integer)
                retry_config = mapped_column(JSONB)
                orchestrator_metadata = mapped_column(JSONB)
                telephony_configuration_id = mapped_column(sa.Integer, nullable=True)
                from_phone_number_id = mapped_column(sa.Integer, nullable=True)
                state = mapped_column(sa.Text)
                scheduled_start_at = mapped_column(sa.DateTime(timezone=True), nullable=True)
                scheduled_timezone = mapped_column(sa.Text, nullable=True)

            create = load_actual_function('db/campaign_client.py', 'create_campaign', {
                'CampaignModel': Campaign, 'text': sa.text, 'select': sa.select, 'CallerHandoffConflict': ValueError,
            }, 'CampaignClient')
            async def proof():
                engine = create_async_engine(f'postgresql+asyncpg://postgres@/postgres?host={folder}&port=55487')
                try:
                    async with engine.begin() as connection:
                        await connection.run_sync(Base.metadata.create_all)
                    sessions = async_sessionmaker(engine, expire_on_commit=False)
                    client = SimpleNamespace(async_session=sessions)
                    common = dict(name='Pool', workflow_id=10, source_type='csv', source_id='original.csv', user_id=1,
                                  organization_id=42, retry_config={'enabled': False, 'max_retries': 0},
                                  max_concurrency=2, verified_caller_handoff_id=HANDOFF)
                    one, two = await asyncio.gather(create(client, **common), create(client, **common))
                    assert one.id == two.id, 'Concurrent create made independently launchable duplicate drafts'
                    async with sessions() as session:
                        assert await session.scalar(sa.select(sa.func.count(Campaign.id))) == 1
                    for field, value in [('source_id', 'changed.csv'), ('workflow_id', 11), ('user_id', 2), ('max_concurrency', 3)]:
                        with pytest.raises(ValueError, match='original draft'):
                            await create(client, **{**common, field: value})
                    other_org = await create(client, **{**common, 'organization_id': 43})
                    assert other_org.id != one.id, 'Lookup crossed organization ownership'
                    legacy_one, legacy_two = await asyncio.gather(*[
                        create(client, **{**common, 'verified_caller_handoff_id': None}) for _ in range(2)
                    ])
                    assert legacy_one.id != legacy_two.id, 'Legacy unmarked create was unexpectedly deduplicated'
                    async with sessions() as session:
                        assert await session.scalar(sa.select(sa.func.count(Campaign.id))) == 4
                finally:
                    await engine.dispose()
            asyncio.run(proof())
        finally:
            pg('pg_ctl', '-D', folder / 'data', '-m', 'fast', '-w', 'stop')
