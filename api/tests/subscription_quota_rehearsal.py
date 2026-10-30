"""Exercise the actual merged quota code on disposable PostgreSQL, never production."""
import asyncio
import io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres@sha256:00bc86618629af00d2937fdc5a5d63db3ff8450acf52f0636ec813c7f4902929'
NAME = 'sysevo-voice-quota-' + uuid.uuid4().hex[:12]
checks = 0


def command(args, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=90)


def check(condition, label):
    global checks
    assert condition, label
    checks += 1
    print('PASS ' + label, flush=True)


async def prove(expected_database_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from api.constants import DATABASE_URL
    assert DATABASE_URL == expected_database_url, "Quota proof must use its own localhost test database"
    from api.db.models import CampaignModel, VoiceLibraryModel
    from api.db.voice_library_client import VoiceCloneLimitExceeded, VoiceLibraryClient
    from api.db.campaign_client import ActiveCampaignLimitExceeded, CampaignClient, _reserve_active_campaign_slot
    if '--negative-control' in sys.argv:
        async def disabled_guard(*_args, **_kwargs):
            return None
        _reserve_active_campaign_slot = disabled_guard
    from loguru import logger
    logger.remove()
    engine = create_async_engine(DATABASE_URL, pool_size=15, max_overflow=10)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = VoiceLibraryClient()
    campaigns = CampaignClient()
    client.async_session = campaigns.async_session = sessions
    try:
        async with engine.begin() as connection:
            version = await connection.scalar(text('SHOW server_version'))
            check(version.startswith('17.6'), 'PostgreSQL matches the pinned 17.6 rehearsal version')
            await connection.execute(text('CREATE TABLE users(id integer PRIMARY KEY)'))
            await connection.execute(text('CREATE TABLE organizations(id integer PRIMARY KEY)'))
            await connection.execute(text('INSERT INTO users VALUES(1)'))
            await connection.execute(text('INSERT INTO organizations SELECT generate_series(1,100)'))
            await connection.run_sync(lambda sync: VoiceLibraryModel.__table__.create(sync))
            await connection.run_sync(lambda sync: CampaignModel.__table__.c.state.type.create(sync))
            await connection.execute(text("CREATE TABLE campaigns(id serial PRIMARY KEY, organization_id integer NOT NULL REFERENCES organizations(id),state campaign_state NOT NULL,is_standing boolean NOT NULL DEFAULT false,started_at timestamptz,updated_at timestamptz,source_sync_status text,source_last_synced_at timestamptz)"))

        for organization_id, cap in enumerate((1, 3, 5, 10, 15), 1):
            outcomes = await asyncio.gather(*[
                client.create_clone_voice_with_limit(user_id=1, organization_id=organization_id,
                    name=f'quota fixture {index}', provider='dograh_clone' if index % 2 else 'xai', clone_limit=cap)
                for index in range(cap + 5)], return_exceptions=True)
            errors = [item for item in outcomes if isinstance(item, BaseException) and not isinstance(item, VoiceCloneLimitExceeded)]
            if errors:
                raise errors[0]
            async with sessions() as session:
                actual = await session.scalar(text('SELECT count(*) FROM voice_library WHERE organization_id=:org'), {'org': organization_id})
            check(actual == cap and sum(isinstance(item, VoiceCloneLimitExceeded) for item in outcomes) == 5,
                  f'concurrent clone reservations stop at the {cap} clone tier cap')
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE voice_library SET status='failed' WHERE organization_id=1"))
        try:
            await client.create_clone_voice_with_limit(user_id=1, organization_id=1, name='blocked failed slot', provider='dograh_clone', clone_limit=1)
            raise AssertionError('failed clone did not hold its reserved slot')
        except VoiceCloneLimitExceeded:
            check(True, 'failed provider clone retains its slot until removed')
        async with engine.begin() as connection:
            await connection.execute(text('DELETE FROM voice_library WHERE organization_id=1'))
        voice = await client.create_clone_voice_with_limit(user_id=1, organization_id=1, name='freed slot', provider='dograh_clone', clone_limit=1)
        check(voice.organization_id == 1, 'removing a clone frees exactly its organization slot')
        legacy = await asyncio.gather(*[client.create_clone_voice_with_limit(user_id=1, organization_id=50,
            name=f'legacy fixture {index}', provider='dograh_clone', clone_limit=None) for index in range(4)])
        check(len(legacy) == 4, 'an explicit legacy uncapped clone policy remains uncapped')

        async def reserve(organization_id, cap, index):
            async with sessions() as session:
                async with session.begin():
                    await _reserve_active_campaign_slot(session, organization_id, cap)
                    await session.execute(text('INSERT INTO campaigns(organization_id,state) VALUES(:org,:state)'),
                        {'org': organization_id, 'state': ('scheduled', 'syncing', 'running')[index % 3]})

        for organization_id, cap in enumerate((0, 1, 3, 5, 10), 20):
            async with engine.begin() as connection:
                await connection.execute(text("INSERT INTO campaigns(organization_id,state,is_standing) VALUES(:org,'running',true)"), {'org': organization_id})
            outcomes = await asyncio.gather(*[reserve(organization_id, cap, index) for index in range(cap + 5)], return_exceptions=True)
            errors = [item for item in outcomes if isinstance(item, BaseException) and not isinstance(item, ActiveCampaignLimitExceeded)]
            if errors:
                raise errors[0]
            async with sessions() as session:
                actual = await session.scalar(text('SELECT count(*) FROM campaigns WHERE organization_id=:org AND NOT is_standing'), {'org': organization_id})
            check(actual == cap and sum(isinstance(item, ActiveCampaignLimitExceeded) for item in outcomes) == 5,
                  f'concurrent active-campaign reservations stop at the {cap} campaign tier cap')
        await asyncio.gather(*[reserve(organization_id=51, cap=None, index=index) for index in range(4)])
        async with sessions() as session:
            legacy_count = await session.scalar(text('SELECT count(*) FROM campaigns WHERE organization_id=51'))
        check(legacy_count == 4, 'an explicit legacy uncapped campaign policy remains uncapped')
        async with sessions() as session:
            own_id = await session.scalar(text('SELECT id FROM campaigns WHERE organization_id=21 AND NOT is_standing'))
            async with session.begin_nested():
                await _reserve_active_campaign_slot(session, 21, 1, exclude_campaign_id=own_id)
            check(True, 'starting an already-reserved campaign does not consume a second slot')
        async with engine.begin() as connection:
            created = (await connection.execute(text("INSERT INTO campaigns(organization_id,state) SELECT 40,'created' FROM generate_series(1,8) RETURNING id"))).scalars().all()
        outcomes = await asyncio.gather(*[campaigns.claim_campaign_start(item, active_campaign_limit=1) for item in created], return_exceptions=True)
        check(sum(item is True for item in outcomes) == 1 and sum(isinstance(item, ActiveCampaignLimitExceeded) for item in outcomes) == 7,
              'actual campaign start claims reserve only one concurrent execution slot')
        async with engine.begin() as connection:
            paused = (await connection.execute(text("INSERT INTO campaigns(organization_id,state) SELECT 41,'paused' FROM generate_series(1,8) RETURNING id"))).scalars().all()
        outcomes = await asyncio.gather(*[campaigns.claim_campaign_resume(item, active_campaign_limit=1) for item in paused], return_exceptions=True)
        check(sum(item is True for item in outcomes) == 1 and sum(isinstance(item, ActiveCampaignLimitExceeded) for item in outcomes) == 7,
              'actual campaign resume claims reserve only one concurrent execution slot')
        print(json.dumps({'checks': checks, 'production_modified': False, 'scope': 'actual merged quota methods against minimal isolated tables; no provider calls or full platform migrations'}), flush=True)
    finally:
        await engine.dispose()
        await client.engine.dispose()
        await campaigns.engine.dispose()


if __name__ == '__main__':
    started = False
    environment_file = ROOT / 'api/.env.test'
    previous_environment = environment_file.read_bytes()
    from dotenv import dotenv_values
    test_values = dotenv_values(stream=io.StringIO(previous_environment.decode()))
    template_values = dotenv_values(ROOT / "api/.env.test.example")
    assert test_values.get("DATABASE_URL") == template_values.get("DATABASE_URL"), "Use the committed example test database URL; no existing database is touched"
    assert "localhost:5432/test_db" in test_values["DATABASE_URL"], "Expected the example localhost test_db fixture"
    try:
        command(['docker', 'run', '--detach', '--rm', '--name', NAME, '--memory', '512m', '--cpus', '1',
                 '--publish', '127.0.0.1::5432', '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'POSTGRES_DB=test_db', IMAGE])
        started = True
        config = json.loads(command(['docker', 'inspect', NAME]).stdout)[0]
        binding = config['NetworkSettings']['Ports']['5432/tcp'][0]
        assert binding['HostIp'] == '127.0.0.1'
        port = binding['HostPort']
        test_environment = previous_environment.replace(b'localhost:5432/test_db', f'localhost:{port}/test_db'.encode())
        deadline = time.monotonic() + 60
        while command(['docker', 'exec', NAME, 'pg_isready', '-U', 'postgres', '-d', 'test_db'], check=False).returncode:
            if time.monotonic() > deadline:
                raise RuntimeError('Isolated Postgres readiness timeout')
            time.sleep(.2)
        sys.path[:0] = [str(ROOT), str(ROOT / 'sdk/python/src')]
        from dotenv import load_dotenv
        load_dotenv(stream=io.StringIO(test_environment.decode()), override=True)
        expected_database_url = test_values["DATABASE_URL"].replace("localhost:5432/test_db", f"localhost:{port}/test_db")
        asyncio.run(prove(expected_database_url))
    finally:
        if started:
            command(['docker', 'rm', '--force', NAME])
            print('Removed own disposable voice-quota database ' + NAME, flush=True)
