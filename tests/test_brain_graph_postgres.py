"""Opt-in real PostgreSQL checks; only a labelled, network-isolated disposable container.

Run with BRAIN_TEST_PG_CONTAINER=<name> pytest -n 0 tests/test_brain_graph_postgres.py.
Container must have label sniper.brain-test=1, --network none, and pgvector installed.
"""
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = os.getenv('BRAIN_TEST_PG_CONTAINER')
pytestmark = pytest.mark.skipif(not CONTAINER, reason='requires isolated disposable PostgreSQL')


def _command(db):
    return ['docker', 'exec', '-i', CONTAINER, 'psql', '-X', '-U', 'postgres', '-d', db,
            '-v', 'ON_ERROR_STOP=1', '-qAt']


def sql(db, statement, *, ok=True):
    r = subprocess.run(_command(db), input=statement, text=True, capture_output=True, timeout=30)
    if ok:
        assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def pg():
    info = json.loads(subprocess.check_output(['docker', 'inspect', CONTAINER], text=True))[0]
    assert info['Config'].get('Labels', {}).get('sniper.brain-test') == '1'
    assert info['HostConfig']['NetworkMode'] == 'none' and not info['HostConfig'].get('PortBindings')
    db = 'brain_test_' + uuid.uuid4().hex
    sql('postgres', f'CREATE DATABASE {db};')
    try:
        sql(db, """DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon; END IF;
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated; END IF;
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role BYPASSRLS; END IF;
        END $$;
        CREATE SCHEMA extensions; CREATE EXTENSION vector WITH SCHEMA extensions;""")
        for name in ('032_brain_graph', '034_brain_supersede_dispute'):
            sql(db, (ROOT / f'migrations/supabase_{name}.sql').read_text())
        upgrade = ROOT / 'migrations/supabase_035_brain_atomic_write.sql'
        if upgrade.exists():
            sql(db, upgrade.read_text())
        for name in ('033_brain_judgements', '036_brain_import_atomic'):
            upgrade = ROOT / f'migrations/supabase_{name}.sql'
            if upgrade.exists():
                sql(db, upgrade.read_text())
        yield db
    finally:
        sql('postgres', f'DROP DATABASE {db} WITH (FORCE);')


def row(statement, ident=None):
    return dict(id=ident or str(uuid.uuid4()), kind='rule', statement=statement, author='alpha',
                entity_ids=['10000000-0000-0000-0000-000000000001'],
                evidence=[dict(type='path', ref='CLAUDE.md')])


def call(new, actions=()):
    return "SELECT row_to_json(f) FROM public.brain_write_fact($new$" + json.dumps(new) + \
        "$new$::jsonb,$actions$" + json.dumps(list(actions)) + "$actions$::jsonb) f;"


def test_atomic_supersede_contradict_and_replay(pg):
    a, b = row('Use git stash for temporary work.'), row('Use git stash for urgent work.')
    sql(pg, call(a)); sql(pg, call(b))
    new = row('Never git stash in this checkout.')
    actions = [dict(id=a['id'], version=1, verdict='supersedes'),
               dict(id=b['id'], version=1, verdict='contradicts')]
    result = json.loads(sql(pg, 'SET ROLE service_role; ' + call(new, actions)).stdout)
    assert result['status'] == 'disputed'
    replay = json.loads(sql(pg, call(new, actions)).stdout)
    assert replay['id'] == result['id']
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '3'
    assert sql(pg, 'SELECT rel FROM brain_links ORDER BY rel;').stdout.splitlines() == ['contradicts', 'supersedes']
    assert sql(pg, f"SELECT status FROM brain_facts WHERE id='{a['id']}';").stdout.strip() == 'superseded'
    assert sql(pg, f"SELECT status FROM brain_facts WHERE id='{b['id']}';").stdout.strip() == 'disputed'


def test_same_id_concurrency_and_identity_collision(pg):
    old = row('Use git stash for temporary work.')
    sql(pg, call(old))
    new = row('Never git stash in this checkout.')
    actions = [dict(id=old['id'], version=1, verdict='supersedes')]
    # Overlap identical transactions at the server, including while the first holds its locks.
    first = subprocess.Popen(_command(pg), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    first.stdin.write('BEGIN; ' + call(new, actions) + ' SELECT pg_sleep(0.4); COMMIT;')
    first.stdin.close()
    second = sql(pg, call(new, actions))
    first.wait(timeout=10)
    assert first.returncode == 0, first.stderr.read()
    assert json.loads(second.stdout)['id'] == new['id']
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '2'
    assert sql(pg, 'SELECT count(*) FROM brain_links;').stdout.strip() == '1'
    assert sql(pg, f"SELECT version FROM brain_facts WHERE id='{old['id']}';").stdout.strip() == '2'
    collision = sql(pg, call(dict(new, statement='An unrelated claim with the same UUID.')), ok=False)
    assert collision.returncode != 0 and 'operation_identity_conflict' in collision.stderr


def test_stale_version_is_atomic_and_can_be_retried(pg):
    old = row('Use git stash for temporary work.')
    sql(pg, call(old))
    sql(pg, f"UPDATE brain_facts SET verify_note='independent update' WHERE id='{old['id']}';")
    new = row('Never git stash in this checkout.')
    stale = [dict(id=old['id'], version=1, verdict='supersedes')]
    conflict = sql(pg, call(new, stale), ok=False)
    assert conflict.returncode != 0 and 'version_conflict' in conflict.stderr
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '1'
    assert sql(pg, 'SELECT count(*) FROM brain_links;').stdout.strip() == '0'
    assert sql(pg, f"SELECT status FROM brain_facts WHERE id='{old['id']}';").stdout.strip() == 'unverified'
    fresh = [dict(stale[0], version=2)]
    assert json.loads(sql(pg, call(new, fresh)).stdout)['id'] == new['id']


def test_link_failure_rolls_back_fact_and_judgements(pg):
    old = row('Use git stash for temporary work.')
    sql(pg, call(old))
    sql(pg, """CREATE FUNCTION refuse_link() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'test_link_failure'; END $$;
        CREATE TRIGGER refuse_link BEFORE INSERT ON brain_links FOR EACH ROW EXECUTE FUNCTION refuse_link();""")
    new = row('Never git stash in this checkout.')
    failed = sql(pg, call(new, [dict(id=old['id'], version=1, verdict='contradicts')]), ok=False)
    assert failed.returncode != 0 and 'test_link_failure' in failed.stderr
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '1'
    assert sql(pg, 'SELECT status FROM brain_facts;').stdout.strip() == 'unverified'
    assert sql(pg, 'SELECT count(*) FROM brain_links;').stdout.strip() == '0'


def test_rpc_permissions_and_additive_rollback(pg):
    for role in ('anon', 'authenticated'):
        refused = sql(pg, f'SET ROLE {role}; ' + call(row('Never expose private brain facts.')), ok=False)
        assert refused.returncode != 0 and 'permission denied' in refused.stderr
    assert sql(pg, "SELECT has_function_privilege('service_role', 'brain_write_fact(jsonb,jsonb)', 'EXECUTE');").stdout.strip() == 't'
    old = row('Use git stash for temporary work.')
    sql(pg, call(old))
    sql(pg, (ROOT / 'migrations/supabase_035_rollback_brain_atomic_write.sql').read_text())
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '1'
    # 034 remains usable by old clients after the additive RPC is removed.
    new = row('Never git stash in this checkout.')
    new['status'] = 'disputed'
    legacy = "SELECT row_to_json(f) FROM brain_supersede('" + old['id'] + "',1,$new$" + json.dumps(new) + "$new$::jsonb) f;"
    result = json.loads(sql(pg, legacy).stdout)
    assert result['status'] == 'disputed'
    sql(pg, (ROOT / 'migrations/supabase_034_rollback_brain_supersede_dispute.sql').read_text())
    again = dict(new, statement='Never use git stash in shared worktrees.')
    legacy = "SELECT row_to_json(f) FROM brain_supersede('" + result['id'] + "',1,$new$" + json.dumps(again) + "$new$::jsonb) f;"
    assert json.loads(sql(pg, legacy).stdout)['status'] == 'unverified'


def test_committed_rpc_response_loss_replays_one_real_graph(pg):
    from bot.brain_graph.store import BrainUnavailable, RestStore
    old = row('Use git stash for temporary work.')
    sql(pg, call(old))
    new = row('Never git stash in this checkout.')
    actions = [dict(id=old['id'], version=1, verdict='supersedes')]
    drop = True

    def request(method, path, body, headers, timeout):
        nonlocal drop
        assert method == 'POST' and path == 'rpc/brain_write_fact'
        result = json.loads(sql(pg, call(body['p_new'], body['p_actions'])).stdout)
        if drop:
            drop = False
            raise RuntimeError('simulated transport loss after PostgreSQL committed')
        return [result]

    store = RestStore(request=request)
    with pytest.raises(BrainUnavailable):
        store.write_fact(new, actions)
    assert store.write_fact(new, actions)['id'] == new['id']
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '2'
    assert sql(pg, 'SELECT count(*) FROM brain_links;').stdout.strip() == '1'
