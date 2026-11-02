"""Am11/Am14 host rounds: the host re-sheets the newest unassigned admitted ticket between turns; both Judges see that
one item only while it is fresh at their cut; append-only round receipts."""
from types import SimpleNamespace

from sniper.office import judge_decisions as J, judge_rounds as R, store as S

E = 'office-paper-eval-1'


def item(h, feed, at):
    return {'receipt_row_hash': h * 64, 'feed': feed, 'known_at': at, 'sheet': {}, 'lenses': {}}


def decide(conn, slot, cand, at):
    ctx = SimpleNamespace(epoch_id=E, actor_id=slot, generation=3, decision_id='t-' + slot + str(at), cut={'knowledge_cut': at})
    J.record(conn, context=ctx, slot=slot, args={'decision': 'SKIP', 'candidate': cand, 'order_id': None, 'public_reason': 'x'},
             now=at, equity_snapshot_id='e' * 64, lesson_ids=[])


def host(conn, now, listed, sheets, made=None):
    made = made if made is not None else []
    def sheet(tid):
        made.append(tid)
        return sheets.get(tid)
    return R.advance(conn, epoch_id=E, now=now, judges=['j1', 'j2'], tickets=lambda n: listed, sheet=sheet)


LISTED = [{'ticket_id': 'np', 'feed': 'near_pass'}, {'ticket_id': 'p2', 'feed': 'pass'}, {'ticket_id': 'p1', 'feed': 'pass'}]


def test_host_resheets_the_newest_pass_ticket_and_both_judges_see_it_while_fresh(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    made = []
    rnd, attempts = host(conn, 100., LISTED, {'p2': item('b', 'pass', 99.)}, made)
    assert made == ['p2'] and rnd['ticket_id'] == 'p2' and rnd['feed'] == 'pass' and rnd['sheet_age_s'] == 1.
    assert R.frame_round(conn, cut=150.)['item']['receipt_row_hash'] == 'b' * 64
    assert R.frame_round(conn, cut=98.)['item'] is None                                    # not causal before the sheet
    assert R.frame_round(conn, cut=99. + R.JUDGE_FRESH_S + 1)['reason'] == 'sheet_not_fresh_at_cut'
    assert host(conn, 101., LISTED, {})[0]['round_id'] == rnd['round_id']                # open round is kept, no re-sheet


def test_a_stale_or_failed_sheet_is_not_assigned_and_the_next_ticket_is_tried(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    rnd, attempts = host(conn, 1000., LISTED, {'p2': item('b', 'pass', 1000. - R.JUDGE_FRESH_S - 1), 'p1': item('c', 'pass', 999.)})
    assert [a['state'] for a in attempts] == ['too_old', 'fresh'] and rnd['ticket_id'] == 'p1' and rnd['rule'] == 'am14'
    assert S.latest(conn, 'receipts', 'cache_key', 'judge.round.sheet_failed:p2') is not None
    conn2 = S.open_store(tmp_path / 'o2.db')
    assert host(conn2, 1000., LISTED, {})[0] is None                                        # nothing sheeted: no round


def test_round_closes_complete_or_incomplete_and_tickets_are_never_reassigned(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    r1, _ = host(conn, 100., LISTED, {'p2': item('b', 'pass', 99.)})
    decide(conn, 'judge_claude', 'b' * 64, 110.); decide(conn, 'judge_gpt', 'b' * 64, 111.)
    r2, _ = host(conn, 112., LISTED, {'p2': item('x', 'pass', 111.), 'p1': item('c', 'pass', 111.)})
    assert r2['ticket_id'] == 'p1'                                                           # p2 is never assigned again
    assert '"outcome":"complete"' in S.latest(conn, 'receipts', 'cache_key', 'judge.round.end:' + r1['round_id'])['payload_json']
    decide(conn, 'judge_gpt', 'c' * 64, 120.)
    r3, _ = host(conn, 112. + R.ROUND_MAX_S + 1, [{'ticket_id': 'np', 'feed': 'near_pass'}], {'np': item('d', 'near_pass', 230.)})
    end = S.latest(conn, 'receipts', 'cache_key', 'judge.round.end:' + r2['round_id'])['payload_json']
    assert '"outcome":"round_incomplete"' in end and '"reason":"one_judge:judge_gpt"' in end and r3['feed'] == 'near_pass'


def test_sheet_cost_is_bounded_per_host_pass(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    listed = [{'ticket_id': 't%d' % i, 'feed': 'pass'} for i in range(10)]
    made = []
    rnd, attempts = host(conn, 100., listed, {}, made)
    assert rnd is None and len(made) == R.PICK_TRIES


def test_near_pass_only_when_no_pass_ticket_exists_and_budget_bounds_the_pass(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    made = []
    assert host(conn, 100., LISTED, {'np': item('d', 'near_pass', 99.)}, made)[0] is None and 'np' not in made
    clock = iter([0., 0., R.SHEET_BUDGET_S + 1, 99.])
    conn2 = S.open_store(tmp_path / 'o2.db')
    made2 = []
    R.advance(conn2, epoch_id=E, now=100., judges=['j1', 'j2'], tickets=lambda n: LISTED,
              sheet=lambda tid: made2.append(tid), wall=lambda: next(clock))
    assert made2 == ['p2']                                                                  # budget spent after one build


import pytest


@pytest.mark.parametrize('exc', [OSError('desk gone'), AttributeError('ctx None'), RuntimeError('rpc')])
def test_a_failing_round_pass_never_escapes_flush_and_settlement_still_ran(exc):
    from sniper.office import runtime as RT
    ran = []
    def rounds(): raise exc
    out = RT.eval_housekeeping({'state': 'ok'}, settle=lambda: ran.append('settle'), rounds=rounds)
    assert ran == ['settle'] and out == {'state': 'partial', 'last_error': 'rounds_failed:' + type(exc).__name__}


def test_settle_failure_is_typed_and_rounds_still_run():
    from sniper.office import runtime as RT
    ran = []
    def settle(): raise ValueError('x')
    out = RT.eval_housekeeping({'state': 'ok'}, settle=settle, rounds=lambda: ran.append('rounds'))
    assert ran == ['rounds'] and out['last_error'] == 'eval_settle_failed'


def test_assignment_clock_is_sampled_after_the_sheet_build(tmp_path):
    conn=S.open_store(tmp_path/'o.db');at=[100.]
    def build(tid):
        at[0]=116.                 # real receipt was created while the host pass was blocked
        return item('b','pass',115.)
    rnd,_=R.advance(conn,epoch_id=E,now=100.,judges=['j1','j2'],
        tickets=lambda now:[{'ticket_id':'p','feed':'pass'}],sheet=build,clock=lambda:at[0])
    assert rnd['round_cut']==116. and rnd['known_at']==116. and rnd['sheet_age_s']==1.
    assert R.frame_round(conn,cut=117.)['sheet_age_s']==2.


def test_assignment_clock_failure_does_not_consume_a_ticket(tmp_path):
    import pytest
    for bad in (None,99.,float('nan')):
        conn=S.open_store(tmp_path/(str(bad)+'.db'))
        with pytest.raises(ValueError,match='round_clock'):
            R.advance(conn,epoch_id=E,now=100.,judges=['j1','j2'],
                tickets=lambda now:[{'ticket_id':'p','feed':'pass'}],sheet=lambda tid:item('b','pass',100.),clock=lambda:bad)
        assert S.latest(conn,'receipts','cache_key','judge.round.ticket:p') is None
        assert S.latest(conn,'receipts','cache_key','judge.round.sheet_failed:p') is None


def test_each_attempt_gets_a_new_clock_and_future_sheet_is_not_clamped(tmp_path):
    conn=S.open_store(tmp_path/'o.db');ticks=iter([201.,202.])
    rnd,attempts=R.advance(conn,epoch_id=E,now=200.,judges=['j1','j2'],
        tickets=lambda now:[{'ticket_id':'p2','feed':'pass'},{'ticket_id':'p1','feed':'pass'}],
        sheet=lambda tid:None if tid=='p2' else item('b','pass',201.5),clock=lambda:next(ticks))
    assert rnd['round_cut']==202. and rnd['sheet_age_s']==.5
    failed=S.latest(conn,'receipts','cache_key','judge.round.sheet_failed:p2')
    import json
    assert json.loads(failed['payload_json'])['known_at']==201.
    conn2=S.open_store(tmp_path/'future.db')
    rnd,_=R.advance(conn2,epoch_id=E,now=100.,judges=['j1','j2'],
        tickets=lambda now:[{'ticket_id':'p','feed':'pass'}],sheet=lambda tid:item('c','pass',102.),clock=lambda:101.)
    assert rnd is None                       # never invent a current sheet by clamping negative age


def test_every_round_marker_names_its_epoch(tmp_path):
    import json
    conn = S.open_store(tmp_path / 'o.db')
    listed = [{'ticket_id': 'p2', 'feed': 'pass'}, {'ticket_id': 'p1', 'feed': 'pass'}]
    rnd, _ = host(conn, 100., listed, {'p1': item('b', 'pass', 95.)})          # p2 has no sheet: sheet_failed, p1 assigned
    for key in ('judge.round.sheet_failed:p2', 'judge.round.ticket:p1', 'judge.round.assigned:' + 'b' * 64):
        row = S.latest(conn, 'receipts', 'cache_key', key)
        assert row is not None and json.loads(row['payload_json']).get('epoch_id') == E, key


def test_sheet_failed_keeps_the_sheet_tools_own_state_and_reason(tmp_path):
    import json
    conn = S.open_store(tmp_path / 'o.db')
    bad = {'unavailable': True, 'state': 'unknown', 'reason': 'refresh_failed:StaleSnapshot:latest_swap_not_buy'}
    rnd, attempts = host(conn, 100., LISTED, {'p2': bad, 'p1': item('c', 'pass', 99.)})
    assert rnd['ticket_id'] == 'p1' and [a['state'] for a in attempts] == ['unavailable', 'fresh']
    doc = json.loads(S.latest(conn, 'receipts', 'cache_key', 'judge.round.sheet_failed:p2')['payload_json'])
    assert doc['reason'] == 'sheet_failed:unavailable'
    assert doc['reason_detail'] == {'state': 'unknown', 'reason': 'refresh_failed:StaleSnapshot:latest_swap_not_buy'}
    host(conn, 100. + R.ROUND_MAX_S + 1, [{'ticket_id': 'p3', 'feed': 'pass'}],
         {'p3': item('d', 'pass', 100. + R.ROUND_MAX_S - R.JUDGE_FRESH_S - 5)})
    assert json.loads(S.latest(conn, 'receipts', 'cache_key', 'judge.round.sheet_failed:p3')['payload_json'])['reason_detail'] is None
