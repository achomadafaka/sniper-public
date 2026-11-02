"""judge.decide validation, records and publication."""
from types import SimpleNamespace

import pytest

from sniper.office import judge_decisions as J, store as S

C = 'c' * 64


def ctx(actor, epoch='office-paper-eval-1'):
    return SimpleNamespace(epoch_id=epoch, actor_id=actor, generation=2, decision_id='turn-' + actor, cut={'knowledge_cut': 1.})


def a(decision='ENTER', candidate=C, order_id=None, reason='fresh buyers'):
    return {'decision': decision, 'candidate': candidate, 'order_id': order_id, 'public_reason': reason}


@pytest.mark.parametrize('args', [a(candidate='d' * 64), a(order_id='o1'), a(decision='EXIT', candidate=None, order_id='o9'),
                                  a(decision='EXIT'), a(reason=''), a(reason='x' * 161), a(decision='BUY'), {'decision': 'ENTER'}])
def test_invalid_targets_are_refused(args):
    with pytest.raises(ValueError):
        J.validate(args, candidates={C}, open_orders={'o1'})


def test_valid_targets_pass():
    J.validate(a(), candidates={C}, open_orders=set())
    J.validate(a(decision='EXIT', candidate=None, order_id='o1'), candidates=set(), open_orders={'o1'})


def put(conn, actor, slot, args, at):
    return J.record(conn, context=ctx(actor), slot=slot, args=args, now=at, equity_snapshot_id='e' * 64, lesson_ids=['l1'])


def test_entry_publishes_only_when_both_judges_committed_and_split_is_its_own_label(tmp_path):
    conn = S.open_store(tmp_path / 'office.db')
    did = J.decision_id('office-paper-eval-1', a())
    put(conn, 'jc', 'judge_claude', a(), 10.)
    assert J.published(J.rows_for(conn, did)) is None and J.recent_published(conn, now=20.) == []
    put(conn, 'jg', 'judge_gpt', a(decision='SKIP'), 12.)
    pub = J.published(J.rows_for(conn, did))
    assert pub['decision'] == 'split' and pub['published_at'] == 12.
    assert J.recent_published(conn, now=11.) == []                    # causal: not published before the second commit
    assert J.recent_published(conn, now=20.)[0]['decision_id'] == did


def test_single_exit_publishes_and_unanimous_enter_labels_enter(tmp_path):
    conn = S.open_store(tmp_path / 'office.db')
    ex = a(decision='EXIT', candidate=None, order_id='o1')
    put(conn, 'jg', 'judge_gpt', ex, 5.)
    assert J.published(J.rows_for(conn, J.decision_id('office-paper-eval-1', ex)))['decision'] == 'EXIT'
    put(conn, 'jc', 'judge_claude', a(), 6.); put(conn, 'jg', 'judge_gpt', a(), 7.)
    assert J.published(J.rows_for(conn, J.decision_id('office-paper-eval-1', a())))['decision'] == 'ENTER'


def test_disproof_line_is_extracted_or_none():
    assert J.disproof('Thesis: x\nDisproof: top10 > 40%\n') == 'Disproof: top10 > 40%'
    assert J.disproof('no line') is None and J.disproof(None) is None


def test_published_reader_one_indexed_query_even_with_many_unpublished_targets(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    for i in range(80):put(conn,'jc','judge_claude',a(candidate=f'{i:064x}'),float(i+1))
    sql=[];conn.set_trace_callback(sql.append)
    assert J.recent_published(conn,now=100.)==[]
    conn.set_trace_callback(None)
    reads=[s for s in sql if s.lstrip().upper().startswith(('SELECT','WITH'))]
    assert len(reads)==1
    plan=' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN '+reads[0]))
    assert 'receipts_judge_decide_seq' in plan
    assert 'receipts_cache_key_seq' in plan


def test_published_reader_keeps_first_commits_outside_newest_window(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    put(conn,'jc','judge_claude',a(reason='original'),10.)
    put(conn,'jg','judge_gpt',a(decision='SKIP'),12.)
    for i in range(410):put(conn,'jc','judge_claude',a(decision='WAIT',reason='later'),float(20+i))
    pub=J.recent_published(conn,now=1000.)[0]
    assert pub['decision']=='split' and pub['rows'][0]['public_reason']=='original'
    assert J.recent_published(conn,now=11.)==[]


def test_published_reader_refuses_selected_hash_tamper(tmp_path):
    conn=S.open_store(tmp_path/'office.db')
    put(conn,'jc','judge_claude',a(),10.);put(conn,'jg','judge_gpt',a(),11.)
    conn.execute('DROP TRIGGER receipts_no_update')
    conn.execute("UPDATE receipts SET payload_json=replace(payload_json,'fresh buyers','fabrication') WHERE seq=1")
    conn.commit()
    with pytest.raises(ValueError,match='integrity'):
        J.recent_published(conn,now=20.)


def test_published_reader_missing_index_refuses_without_creating_it(tmp_path):
    import sqlite3
    conn=S.open_store(tmp_path/'office.db')
    conn.execute('DROP INDEX receipts_judge_decide_seq')
    before=conn.total_changes
    with pytest.raises(sqlite3.OperationalError,match='no such index'):
        J.recent_published(conn,now=20.)
    assert conn.total_changes==before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='receipts_judge_decide_seq'").fetchone()
