"""Host settlement of published Judge decisions into eval_orders (stubbed library)."""
from types import SimpleNamespace

import pytest

from sniper.office import eval_settle as ES, judge_decisions as J, store as S
from sniper.office.supervisor import _put, action_key

C = 'c' * 64


def ctx(actor, view=9.):
    return SimpleNamespace(epoch_id='office-paper-eval-1', actor_id=actor, generation=2, decision_id='turn-' + actor,
                           cut={'knowledge_cut': view})


def decide(conn, actor, slot, decision='ENTER', at=10., order_id=None, memo=True, view=9.):
    args = {'decision': decision, 'candidate': None if decision == 'EXIT' else C, 'order_id': order_id, 'public_reason': 'why'}
    J.record(conn, context=ctx(actor, view), slot=slot, args=args, now=at, equity_snapshot_id='e' * 64, lesson_ids=[])
    if memo:
        _put(conn, 'messages', action_key('turn-' + actor, 'memo.commit'),
             {'actor_id': actor, 'request': {'memo': 'Disproof: x', 'served_model': 'claude-x' if slot == 'judge_claude' else 'gpt-x'}})
        conn.commit()


def run(conn, monkeypatch, now=20., result='submitted'):
    calls = []
    def enter(store, ledger, policy, **kw):
        calls.append(('enter', kw)); return {'state': result, 'reason': None, 'order_id': 'o1', 'bundle_hash': 'b', 'outcome': 'ENTER'}
    def exit(ledger, **kw):
        calls.append(('exit', kw)); return {'state': 'submitted', 'reason': None, 'order_id': kw['order_id'], 'bundle_hash': 'b', 'outcome': 'EXIT'}
    monkeypatch.setattr(ES.EO, 'enter', enter); monkeypatch.setattr(ES.EO, 'exit', exit)
    monkeypatch.setattr(ES.X, 'mark_first_order', lambda c, at: calls.append(('first_order', at)))
    out = ES.settle(conn, None, policy=None, now=now, sol_usd=200., sol_at=now, candidate_of=lambda h: {'receipt_row_hash': h})
    return out, calls


def test_unanimous_entry_settles_once_with_both_judgements(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jc', 'judge_claude'); decide(conn, 'jg', 'judge_gpt', at=11.)
    out, calls = run(conn, monkeypatch)
    kind, kw = calls[0]
    assert calls[1] == ('first_order', 20.)
    assert kind == 'enter' and kw['context'].role == 'trader' and kw['context'].book_id == 'office-paper-eval-1'
    assert set(kw['judgements']) == {'judge_claude', 'judge_gpt'} and kw['judgements']['judge_gpt']['served_model'] == 'gpt-x'
    assert kw['judgements']['judge_claude']['receipt_row_hash'] == C and kw['context'].cut['knowledge_cut'] == 11.
    assert run(conn, monkeypatch)[1] == []                                   # settled once


def test_missing_memo_retries_then_expires(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jc', 'judge_claude'); decide(conn, 'jg', 'judge_gpt', at=11., memo=False)
    out, calls = run(conn, monkeypatch)
    assert calls == [] and out[0]['state'] == 'unknown'
    assert run(conn, monkeypatch)[0][0]['state'] == 'unknown'                 # retried, not settled
    late, _ = run(conn, monkeypatch, now=11. + ES.SETTLE_WINDOW_S)
    assert late[0]['state'] == 'unknown' and run(conn, monkeypatch, now=500.)[0] == []   # window closed: settled


def test_single_exit_settles_through_exit(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jg', 'judge_gpt', decision='EXIT', order_id='o7')
    out, calls = run(conn, monkeypatch)
    assert calls[0][0] == 'exit' and calls[0][1]['order_id'] == 'o7' and out[0]['label'] == 'EXIT'
    assert calls[0][1]['asker'] == {'slot': 'judge_gpt', 'decision_id': out[0]['decision_id'],
                                    'answers': {'judge_claude': None, 'judge_gpt': 'EXIT'}}
    decide(conn, 'jc', 'judge_claude', decision='EXIT', order_id='o7', at=14., view=4.)   # second Judge: an additional asker
    out2, calls2 = run(conn, monkeypatch)
    assert [c[1]['asker']['slot'] for c in calls2] == ['judge_claude']
    assert calls2[0][1]['asker']['answers'] == {'judge_claude': 'EXIT', 'judge_gpt': 'EXIT'}
    assert calls2[0][1]['context'].cut == {'knowledge_cut': 4.}          # the view it saw, not its commit time
    assert run(conn, monkeypatch)[1] == []


def test_unpublished_single_entry_is_not_settled(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jc', 'judge_claude')
    assert run(conn, monkeypatch) == ([], [])


def test_first_order_mark_precedes_the_settle_receipt(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jc', 'judge_claude'); decide(conn, 'jg', 'judge_gpt', at=11.)
    monkeypatch.setattr(ES.EO, 'enter', lambda *a, **k: {'state': 'submitted', 'reason': None, 'order_id': 'o1', 'bundle_hash': 'b', 'outcome': 'ENTER'})
    def boom(c, at): raise RuntimeError('crash')
    monkeypatch.setattr(ES.X, 'mark_first_order', boom)
    with pytest.raises(RuntimeError):
        ES.settle(conn, None, policy=None, now=20., sol_usd=1., sol_at=20., candidate_of=lambda h: {})
    assert conn.execute("SELECT count(*) FROM receipts WHERE cache_key LIKE 'judge.settle:%'").fetchone()[0] == 0   # retried next pass


def test_reversible_pause_is_retried_not_lost(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    decide(conn, 'jc', 'judge_claude'); decide(conn, 'jg', 'judge_gpt', at=11.)
    out, _ = run(conn, monkeypatch, result='refused')                       # stub returns reason None: final refusal
    assert out[0]['state'] == 'refused'
    conn2 = S.open_store(tmp_path / 'office2.db')
    decide(conn2, 'jc', 'judge_claude'); decide(conn2, 'jg', 'judge_gpt', at=11.)
    monkeypatch.setattr(ES.EO, 'enter', lambda *a, **k: {'state': 'refused', 'reason': 'epoch_clock_regressed', 'order_id': None, 'bundle_hash': 'b', 'outcome': 'ENTER'})
    monkeypatch.setattr(ES.X, 'mark_first_order', lambda c, at: None)
    first = ES.settle(conn2, None, policy=None, now=20., sol_usd=1., sol_at=20., candidate_of=lambda h: {})
    assert first[0]['state'] == 'unknown' and ES.settle(conn2, None, policy=None, now=21., sol_usd=1., sol_at=21., candidate_of=lambda h: {})


def test_halt_latch_becomes_one_epoch_end_receipt(tmp_path, monkeypatch):
    conn = S.open_store(tmp_path / 'office.db')
    from sniper.office import ledger as L
    latch = {'state': 'unknown', 'reason': 'epoch_safety_not_sampled'}
    monkeypatch.setattr(L, 'evaluation_safety_state', lambda c, *, epoch_id: latch)
    assert ES.record_epoch_end(conn, None, now=1.) is None
    latch = {'halted_at': 5., 'halt_kind': 'config_drift', 'halt_reason': 'epoch_config_drift', 'halt_actor': 'safety'}
    assert ES.record_epoch_end(conn, None, now=6.) == 'BROKEN' and ES.record_epoch_end(conn, None, now=7.) is None
    latch2 = dict(latch, halt_kind='drawdown')
    conn2 = S.open_store(tmp_path / 'office2.db')
    monkeypatch.setattr(L, 'evaluation_safety_state', lambda c, *, epoch_id: latch2)
    assert ES.record_epoch_end(conn2, None, now=6.) == 'FAILED'
