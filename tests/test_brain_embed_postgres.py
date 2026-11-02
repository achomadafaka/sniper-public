"""Atomic missing-only batch embeddings against disposable PostgreSQL."""
import json
from pathlib import Path

from test_brain_graph_postgres import pg, sql, row, call, pytestmark

ROOT = Path(__file__).resolve().parents[1]


def batch(items):
    return 'SELECT public.brain_set_embeddings($rows$' + json.dumps(items) + '$rows$::jsonb);'


def item(fact, value=1):
    return dict(table='brain_facts', id=fact['id'], embedding=json.dumps([value] * 384))


def test_batch_atomicity_resume_and_permissions(pg):
    # Supabase grants service_role usage of its extensions schema by default.
    sql(pg, 'GRANT USAGE ON SCHEMA extensions TO service_role;')
    sql(pg, (ROOT / 'migrations/supabase_038_brain_embedding_batch.sql').read_text())
    a, b = row('First embedding batch claim.'), row('Second embedding batch claim.')
    sql(pg, call(a)); sql(pg, call(b))
    invalid = dict(item(b), embedding='[1]')
    assert sql(pg, batch([item(a), invalid]), ok=False).returncode
    assert sql(pg, 'SELECT count(*) FROM brain_facts WHERE embedding IS NOT NULL;').stdout.strip() == '0'
    assert sql(pg, 'SET ROLE service_role; ' + batch([item(a), item(b)])).stdout.strip() == '2'
    assert sql(pg, batch([item(a, 2), item(b, 2)])).stdout.strip() == '0'
    assert sql(pg, f"SELECT embedding::text LIKE '[1,%' FROM brain_facts WHERE id='{a['id']}';").stdout.strip() == 't'
    for role in ('anon', 'authenticated'):
        assert sql(pg, f'SET ROLE {role}; ' + batch([]), ok=False).returncode
    assert sql(pg, batch([item(a)] * 65), ok=False).returncode
    assert sql(pg, batch([dict(item(a), table='memories')]), ok=False).returncode
    assert sql(pg, batch([dict(item(a), id=None)]), ok=False).returncode
    sql(pg, (ROOT / 'migrations/supabase_038_rollback_brain_embedding_batch.sql').read_text())
    assert sql(pg, 'SELECT count(*) FROM brain_facts WHERE embedding IS NOT NULL;').stdout.strip() == '2'
    assert sql(pg, batch([]), ok=False).returncode
