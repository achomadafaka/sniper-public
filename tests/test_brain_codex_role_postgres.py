"""Crew-role parity against a labelled, network-isolated disposable PostgreSQL."""
import json

import pytest

from test_brain_graph_postgres import ROOT, call, pg, pytestmark, row, sql


def upgrade(db):
    for suffix in ('037_brain_atomic_judgement', '039_brain_refines_closed', '043_brain_codex_role'):
        migration = ROOT / f'migrations/supabase_{suffix}.sql'
        if migration.exists():
            sql(db, migration.read_text())


def test_codex_writes_every_provenance_table_and_judges_a_pair(pg):
    upgrade(pg)
    a, b = row('Existing peer claim.'), row('Codex claim with preserved identity.')
    b['author'] = 'codex'
    sql(pg, call(a)); sql(pg, call(b))
    sql(pg, "SET ROLE service_role; INSERT INTO brain_episodes(kind,body,author) VALUES ('handover','Codex handover','codex');")
    assert sql(pg, f"SET ROLE service_role; SELECT brain_queue_candidate('{b['id']}','{a['id']}','codex');").stdout.strip() == 't'
    result = sql(pg, f"SET ROLE service_role; SELECT brain_judge_pair('{b['id']}','{a['id']}',2,1,'refines','codex');")
    assert json.loads(result.stdout)['verdict'] == 'refines'
    for table in ('brain_facts', 'brain_episodes', 'brain_links', 'brain_judgements'):
        assert int(sql(pg, f"SELECT count(*) FROM {table} WHERE author='codex';").stdout) > 0


def test_codex_can_verify_a_peer_but_not_itself_and_unknown_roles_still_refuse(pg):
    upgrade(pg)
    peer, own = row('Peer claim with evidence.'), row('Own claim must remain independently verified.')
    own['author'] = 'codex'
    sql(pg, call(peer)); sql(pg, call(own))
    sql(pg, f"SET ROLE service_role; UPDATE brain_facts SET status='verified',verified_by='codex' WHERE id='{peer['id']}';")
    bad = sql(pg, f"UPDATE brain_facts SET status='verified',verified_by='codex' WHERE id='{own['id']}';", ok=False)
    assert bad.returncode and 'brain_facts_check' in bad.stderr
    unknown = row('Unknown authors remain forbidden.'); unknown['author'] = 'untrusted'
    assert sql(pg, call(unknown), ok=False).returncode
    for role in ('anon', 'authenticated'):
        for request in (call(row('Public clients still cannot write.')),
                        f"SELECT brain_queue_candidate('{own['id']}','{peer['id']}','codex');",
                        f"SELECT brain_judge_pair('{own['id']}','{peer['id']}',1,2,'unrelated','codex');"):
            refused = sql(pg, f'SET ROLE {role}; '+request, ok=False)
            assert refused.returncode and 'permission denied' in refused.stderr


def test_atomic_codex_fact_actions_preserve_its_author_in_links_and_judgements(pg):
    upgrade(pg)
    prior, new = row('Peer baseline claim.'), row('A Codex refinement retaining its own provenance.')
    new['author'] = 'codex'
    sql(pg, call(prior))
    request = call(new, [{'id': prior['id'], 'version': 1, 'verdict': 'refines'}])
    sql(pg, 'SET ROLE service_role; '+request)
    sql(pg, 'SET ROLE service_role; '+request)
    assert sql(pg, "SELECT count(*) FROM brain_facts WHERE author='codex';").stdout.strip() == '1'
    for table in ('brain_links', 'brain_judgements'):
        assert sql(pg, f"SELECT count(*) FROM {table} WHERE author='codex';").stdout.strip() == '1'


def test_rollback_before_codex_data_restores_prior_roles_without_dropping_facts(pg):
    upgrade(pg)
    prior = row('Existing peer history survives rollback.')
    sql(pg, call(prior))
    sql(pg, (ROOT / 'migrations/supabase_043_rollback_brain_codex_role.sql').read_text())
    own = row('Codex is refused after explicit rollback.'); own['author'] = 'codex'
    assert sql(pg, call(own), ok=False).returncode
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '1'


def test_rollback_refuses_to_discard_codex_history_and_migration_can_repeat(pg):
    upgrade(pg)
    own = row('Recorded Codex history must survive.'); own['author'] = 'codex'
    sql(pg, call(own))
    bad = sql(pg, (ROOT / 'migrations/supabase_043_rollback_brain_codex_role.sql').read_text(), ok=False)
    assert bad.returncode and 'codex_history_exists' in bad.stderr
    sql(pg, (ROOT / 'migrations/supabase_043_brain_codex_role.sql').read_text())
    assert sql(pg, "SELECT count(*) FROM brain_facts WHERE author='codex';").stdout.strip() == '1'
