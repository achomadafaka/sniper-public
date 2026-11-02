"""Atomic judged-pair lifecycle checks against disposable PostgreSQL."""
import json
import pytest
from test_brain_graph_postgres import pytestmark, pg, sql, row, call, ROOT


@pytest.fixture
def judged_db(pg):
    for name in ('033_brain_judgements', '037_brain_atomic_judgement'):
        p = ROOT / f'migrations/supabase_{name}.sql'
        if p.exists():
            sql(pg, p.read_text())
    return pg


def judge(a,b,verdict='contradicts',av=1,bv=1):
    return f"SELECT public.brain_judge_pair('{b['id']}','{a['id']}',{bv},{av},'{verdict}','gamma');"


def facts(db):
    a,b=row('Never use the old entry policy.'),row('Use the new entry policy instead.')
    sql(db,call(a));sql(db,call(b))
    return a,b


def test_judgement_atomic_replay_and_conflicting_verdict(judged_db):
    db=judged_db;a,b=facts(db)
    sql(db,judge(a,b))
    assert sql(db,"SELECT count(*) FROM brain_facts WHERE status='disputed';").stdout.strip()=='2'
    versions=sql(db,'SELECT id,version FROM brain_facts ORDER BY id;').stdout
    sql(db,judge(a,b))
    assert sql(db,'SELECT id,version FROM brain_facts ORDER BY id;').stdout==versions
    refusal=sql(db,judge(a,b,'supersedes'),ok=False)
    assert refusal.returncode and 'judgement_conflict' in refusal.stderr


def test_conflict_or_failed_link_never_half_applies(judged_db):
    db=judged_db;a,b=facts(db)
    refusal=sql(db,judge(a,b,av=9),ok=False)
    assert refusal.returncode and 'version_conflict' in refusal.stderr
    sql(db,"""CREATE FUNCTION stop_link() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'test_failure'; END $$;
      CREATE TRIGGER stop_link BEFORE INSERT ON brain_links FOR EACH ROW EXECUTE FUNCTION stop_link();""")
    refusal=sql(db,judge(a,b),ok=False)
    assert refusal.returncode
    assert sql(db,"SELECT count(*) FROM brain_facts WHERE status='unverified';").stdout.strip()=='2'
    assert sql(db,'SELECT count(*) FROM brain_judgements;').stdout.strip()=='0'


def test_pending_clears_only_after_all_pairs_judged(judged_db):
    db=judged_db;a,b=facts(db);c=row('Third claim sharing the entry policy.')
    sql(db,call(c))
    sql(db,f"UPDATE brain_facts SET judge_pending=true WHERE id='{b['id']}';")
    for other in (a,c):
        sql(db,f"INSERT INTO brain_links(from_id,from_kind,to_id,to_kind,rel,author) VALUES ('{b['id']}','fact','{other['id']}','fact','candidate','cron');")
    sql(db,judge(a,b,'unrelated',bv=2))
    assert sql(db,f"SELECT judge_pending FROM brain_facts WHERE id='{b['id']}';").stdout.strip()=='t'
    sql(db,judge(c,b,'refines',bv=2))
    assert sql(db,f"SELECT judge_pending FROM brain_facts WHERE id='{b['id']}';").stdout.strip()=='f'
    for role in ('anon','authenticated'):
        assert sql(db,f'SET ROLE {role}; '+judge(a,b),ok=False).returncode


def test_directional_replay_cannot_reverse_supersession(judged_db):
    db=judged_db;a,b=facts(db)
    sql(db,judge(a,b,'supersedes'))
    result=sql(db,judge(b,a,'supersedes'),ok=False)
    assert result.returncode and 'orientation' in result.stderr
    assert sql(db,f"SELECT status FROM brain_facts WHERE id='{b['id']}';").stdout.strip()=='unverified'


def test_queue_repairs_half_written_link_and_never_reopens_judged(judged_db):
    db=judged_db;a,b=facts(db)
    sql(db,f"INSERT INTO brain_links(from_id,from_kind,to_id,to_kind,rel,author) VALUES ('{b['id']}','fact','{a['id']}','fact','candidate','cron');")
    queue=f"SELECT brain_queue_candidate('{b['id']}','{a['id']}','cron');"
    assert sql(db,'SET ROLE service_role; '+queue).stdout.strip()=='t'
    assert sql(db,queue).stdout.strip()=='f'
    sql(db,judge(a,b,'unrelated',bv=2))
    assert sql(db,queue).stdout.strip()=='f'
    assert sql(db,f"SELECT judge_pending FROM brain_facts WHERE id='{b['id']}';").stdout.strip()=='f'


def test_concurrent_queue_waits_for_judge_and_does_not_reflag(judged_db):
    import subprocess
    from test_brain_graph_postgres import _command
    db=judged_db;a,b=facts(db)
    first=subprocess.Popen(_command(db),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    first.stdin.write('BEGIN; '+judge(a,b,'unrelated')+' SELECT pg_sleep(0.5); COMMIT;')
    first.stdin.close()
    assert json.loads(first.stdout.readline())['verdict']=='unrelated'
    result=sql(db,f"SELECT brain_queue_candidate('{b['id']}','{a['id']}','cron');")
    first.wait(timeout=10)
    assert first.returncode==0,first.stderr.read()
    assert result.stdout.strip()=='f'
    assert sql(db,'SELECT count(*) FROM brain_facts WHERE judge_pending;').stdout.strip()=='0'


def test_closed_peer_can_be_dismissed_but_never_reopened(judged_db):
    db=judged_db;a,b=facts(db)
    queue=f"SELECT brain_queue_candidate('{b['id']}','{a['id']}','cron');"
    sql(db,queue)
    sql(db,f"UPDATE brain_facts SET status='refuted',verified_by='beta' WHERE id='{a['id']}';")
    assert sql(db,queue).stdout.strip()=='f'
    assert sql(db,judge(a,b,av=2,bv=2),ok=False).returncode
    sql(db,judge(a,b,'unrelated',av=2,bv=2))
    assert sql(db,'SELECT count(*) FROM brain_facts WHERE judge_pending;').stdout.strip()=='0'
    assert sql(db,f"SELECT status FROM brain_facts WHERE id='{a['id']}';").stdout.strip()=='refuted'


# ---- 039: `refines` against a CLOSED older fact (2026-09-30) ---------------------------------
# A fact written with a judgement against an already closed fact stayed judge_pending forever:
# brain_judge_pair refused every verdict but `unrelated` with closed_fact, and `unrelated` is
# false when the new fact does build on the old one. 9282f2a8, fd4573d2 and 3b93e5d9 were stuck.
# `refines` only adds a derived_from link and changes no status, so it is safe against a closed
# OLDER fact. The newer fact must still be live, and every other verdict keeps its refusal.

@pytest.fixture
def refines_db(judged_db):
    sql(judged_db, (ROOT / 'migrations/supabase_039_brain_refines_closed.sql').read_text())
    return judged_db


def _pending_pair(db, close_sql):
    a, b = facts(db)
    sql(db, f"SELECT brain_queue_candidate('{b['id']}','{a['id']}','cron');")
    sql(db, close_sql.format(id=a['id']))
    return a, b


_REFUTE = "UPDATE brain_facts SET status='refuted',verified_by='beta' WHERE id='{id}';"
_EXPIRE = "UPDATE brain_facts SET valid_to=now() WHERE id='{id}';"


@pytest.mark.parametrize('close_sql', [_REFUTE, _EXPIRE])
def test_refines_links_a_live_fact_to_a_closed_older_one_and_changes_no_status(refines_db, close_sql):
    db = refines_db
    a, b = _pending_pair(db, close_sql)
    before = sql(db, f"SELECT status,valid_to,version FROM brain_facts WHERE id='{a['id']}';").stdout
    out = json.loads(sql(db, judge(a, b, 'refines', av=2, bv=2)).stdout)
    assert out == {'verdict': 'refines', 'replayed': False}
    assert sql(db, f"SELECT status,valid_to,version FROM brain_facts WHERE id='{a['id']}';").stdout == before
    assert sql(db, f"SELECT status,valid_to IS NULL,judge_pending FROM brain_facts WHERE id='{b['id']}';").stdout.strip() == 'unverified|t|f'
    assert sql(db, f"SELECT count(*) FROM brain_links WHERE from_id='{b['id']}' AND to_id='{a['id']}' AND rel='derived_from';").stdout.strip() == '1'
    assert sql(db, "SELECT verdict FROM brain_judgements;").stdout.strip() == 'refines'
    assert json.loads(sql(db, judge(a, b, 'refines', av=2, bv=2)).stdout)['replayed'] is True


def test_refines_is_still_refused_when_the_newer_fact_is_closed(refines_db):
    db = refines_db
    a, b = facts(db)
    sql(db, _REFUTE.format(id=b['id']))
    refusal = sql(db, judge(a, b, 'refines', bv=2), ok=False)
    assert refusal.returncode and 'closed_fact' in refusal.stderr
    assert sql(db, 'SELECT count(*) FROM brain_links;').stdout.strip() == '0'
    assert sql(db, 'SELECT count(*) FROM brain_judgements;').stdout.strip() == '0'


@pytest.mark.parametrize('verdict', ['duplicate', 'supersedes', 'contradicts'])
def test_every_other_verdict_still_refuses_a_closed_older_fact(refines_db, verdict):
    db = refines_db
    a, b = _pending_pair(db, _REFUTE)
    refusal = sql(db, judge(a, b, verdict, av=2, bv=2), ok=False)
    assert refusal.returncode and 'closed_fact' in refusal.stderr
    assert sql(db, f"SELECT status FROM brain_facts WHERE id='{a['id']}';").stdout.strip() == 'refuted'
    assert sql(db, f"SELECT status,judge_pending FROM brain_facts WHERE id='{b['id']}';").stdout.strip() == 'unverified|t'
    assert sql(db, 'SELECT count(*) FROM brain_judgements;').stdout.strip() == '0'


def test_the_039_rollback_restores_the_refusal_and_keeps_the_links(refines_db):
    db = refines_db
    a, b = _pending_pair(db, _REFUTE)
    sql(db, judge(a, b, 'refines', av=2, bv=2))
    c = row('A third claim about the entry policy.')
    sql(db, call(c))
    sql(db, (ROOT / 'migrations/supabase_039_rollback_brain_refines_closed.sql').read_text())
    refusal = sql(db, judge(a, c, 'refines', av=2), ok=False)
    assert refusal.returncode and 'closed_fact' in refusal.stderr
    assert sql(db, "SELECT count(*) FROM brain_links WHERE rel='derived_from';").stdout.strip() == '1'
    assert sql(db, 'SELECT count(*) FROM brain_judgements;').stdout.strip() == '1'
    for role in ('anon', 'authenticated'):
        assert sql(db, f'SET ROLE {role}; ' + judge(a, c, 'unrelated', av=2), ok=False).returncode
    assert sql(db, 'SET ROLE service_role; ' + judge(a, c, 'unrelated', av=2)).returncode == 0
