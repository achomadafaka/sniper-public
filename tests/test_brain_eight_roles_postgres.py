"""Crew-role parity against a labelled, network-isolated disposable PostgreSQL."""
import json

import pytest

from test_brain_graph_postgres import ROOT, call, pg, pytestmark, row, sql


def upgrade(db):
    for suffix in ('037_brain_atomic_judgement', '039_brain_refines_closed', '043_brain_codex_role', '044_brain_eight_roles'):
        migration = ROOT / f'migrations/supabase_{suffix}.sql'
        if migration.exists():
            sql(db, migration.read_text())


@pytest.mark.parametrize('crew_role', ('delta', 'epsilon', 'zeta'))
def test_eight_writes_every_provenance_table_and_judges_a_pair(pg, crew_role):
    upgrade(pg)
    a, b = row('Existing peer claim.'), row('New-role claim with preserved identity.')
    b['author'] = crew_role
    sql(pg, call(a)); sql(pg, call(b))
    sql(pg, f"SET ROLE service_role; INSERT INTO brain_episodes(kind,body,author) VALUES ('handover','New-role handover','{crew_role}');")
    assert sql(pg, f"SET ROLE service_role; SELECT brain_queue_candidate('{b['id']}','{a['id']}','{crew_role}');").stdout.strip() == 't'
    result = sql(pg, f"SET ROLE service_role; SELECT brain_judge_pair('{b['id']}','{a['id']}',2,1,'refines','{crew_role}');")
    assert json.loads(result.stdout)['verdict'] == 'refines'
    for table in ('brain_facts', 'brain_episodes', 'brain_links', 'brain_judgements'):
        assert int(sql(pg, f"SELECT count(*) FROM {table} WHERE author='{crew_role}';").stdout) > 0


@pytest.mark.parametrize('crew_role', ('delta', 'epsilon', 'zeta'))
def test_eight_can_verify_a_peer_but_not_itself_and_unknown_roles_still_refuse(pg, crew_role):
    upgrade(pg)
    peer, own = row('Peer claim with evidence.'), row('Own claim must remain independently verified.')
    own['author'] = crew_role
    sql(pg, call(peer)); sql(pg, call(own))
    sql(pg, f"SET ROLE service_role; UPDATE brain_facts SET status='verified',verified_by='{crew_role}' WHERE id='{peer['id']}';")
    bad = sql(pg, f"UPDATE brain_facts SET status='verified',verified_by='{crew_role}' WHERE id='{own['id']}';", ok=False)
    assert bad.returncode and 'brain_facts_check' in bad.stderr
    unknown = row('Unknown authors remain forbidden.'); unknown['author'] = 'untrusted'
    assert sql(pg, call(unknown), ok=False).returncode
    for role in ('anon', 'authenticated'):
        for request in (call(row('Public clients still cannot write.')),
                        f"SELECT brain_queue_candidate('{own['id']}','{peer['id']}','{crew_role}');",
                        f"SELECT brain_judge_pair('{own['id']}','{peer['id']}',1,2,'unrelated','{crew_role}');"):
            refused = sql(pg, f'SET ROLE {role}; '+request, ok=False)
            assert refused.returncode and 'permission denied' in refused.stderr


@pytest.mark.parametrize('crew_role', ('delta', 'epsilon', 'zeta'))
def test_atomic_eight_role_fact_actions(pg, crew_role):
    upgrade(pg)
    prior, new = row('Peer baseline claim.'), row('A New-role refinement retaining its own provenance.')
    new['author'] = crew_role
    sql(pg, call(prior))
    request = call(new, [{'id': prior['id'], 'version': 1, 'verdict': 'refines'}])
    sql(pg, 'SET ROLE service_role; '+request)
    sql(pg, 'SET ROLE service_role; '+request)
    assert sql(pg, f"SELECT count(*) FROM brain_facts WHERE author='{crew_role}';").stdout.strip() == '1'
    for table in ('brain_links', 'brain_judgements'):
        assert sql(pg, f"SELECT count(*) FROM {table} WHERE author='{crew_role}';").stdout.strip() == '1'


@pytest.mark.parametrize('crew_role', ('delta', 'epsilon', 'zeta'))
def test_rollback_before_new_data_preserves_old_roles(pg, crew_role):
    upgrade(pg)
    prior = row('Existing peer history survives rollback.')
    sql(pg, call(prior))
    sql(pg, (ROOT / 'migrations/supabase_044_rollback_brain_eight_roles.sql').read_text())
    own = row('New-role is refused after explicit rollback.'); own['author'] = crew_role
    assert sql(pg, call(own), ok=False).returncode
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '1'


@pytest.mark.parametrize('crew_role', ('delta', 'epsilon', 'zeta'))
def test_rollback_refuses_new_history_and_migration_repeats(pg, crew_role):
    upgrade(pg)
    own = row('Recorded New-role history must survive.'); own['author'] = crew_role
    sql(pg, call(own))
    bad = sql(pg, (ROOT / 'migrations/supabase_044_rollback_brain_eight_roles.sql').read_text(), ok=False)
    assert bad.returncode and 'eight_role_history_exists' in bad.stderr
    sql(pg, (ROOT / 'migrations/supabase_044_brain_eight_roles.sql').read_text())
    assert sql(pg, f"SELECT count(*) FROM brain_facts WHERE author='{crew_role}';").stdout.strip() == '1'


@pytest.mark.parametrize('surface,crew_role', [('episode', 'delta'), ('link', 'epsilon'),
                                               ('judgement', 'zeta'), ('verifier', 'delta')])
def test_rollback_also_preserves_non_author_provenance(pg, surface, crew_role):
    upgrade(pg)
    a, b = row('First preexisting peer fact.'), row('Second preexisting peer fact.')
    sql(pg, call(a)); sql(pg, call(b))
    if surface == 'episode':
        request = f"INSERT INTO brain_episodes(kind,body,author) VALUES ('handover','Role history','{crew_role}');"
    elif surface == 'link':
        request = f"INSERT INTO brain_links(from_id,from_kind,to_id,to_kind,rel,author) VALUES ('{a['id']}','fact','{b['id']}','fact','supports','{crew_role}');"
    elif surface == 'judgement':
        request = f"SELECT brain_judge_pair('{a['id']}','{b['id']}',1,1,'unrelated','{crew_role}');"
    else:
        request = f"UPDATE brain_facts SET status='verified',verified_by='{crew_role}' WHERE id='{a['id']}';"
    sql(pg, 'SET ROLE service_role; '+request)
    refused = sql(pg, (ROOT/'migrations/supabase_044_rollback_brain_eight_roles.sql').read_text(), ok=False)
    assert refused.returncode and 'eight_role_history_exists' in refused.stderr
    assert sql(pg, 'SELECT count(*) FROM brain_facts;').stdout.strip() == '2'
