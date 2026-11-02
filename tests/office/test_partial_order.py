"""Paper orders on a partial sheet: the sells recompute, the partial receipt through the real verify path, the risk allowance, the entry wiring and the kill knob."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.office import eval_orders as EO, ledger as L, partial_order as PO, risk as R, store as S
from sniper.office.tools import sheet as SH

ROOT = Path(__file__).resolve().parents[2]
TICKET = json.loads((ROOT / 'tests/fixtures/office/partial_ticket_419P2.json').read_text())
POOL = TICKET['pool']
CUT = TICKET['stages'][0]['inputs']['evidence']['market_cut']['receipt_cut']
GOT = dict(state='unknown', reason='refresh_failed:StaleSnapshot:tape_gap_since_cut')
COHORT = dict(policy=dict(x=1))


def book_policy():
    return L.OfficeBookPolicy(**{**json.loads((ROOT / 'config/office/book_policy_pilot.json').read_text())['policy'], 'chains': ('solana',)})



SEL = ((0.0, None),)


def tape(tmp_path, rows=(), losses=(), points=(), intervals=((0.0, CUT + 1),), selection=None):
    d = tmp_path / 'pumpswap'; d.mkdir(exist_ok=True)
    c = sqlite3.connect(d / 'tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,received_at REAL,is_buy INTEGER)')
    c.execute('CREATE TABLE ps_loss_attribution(signature TEXT,scope TEXT,pools_json TEXT,received_at REAL)')
    c.execute('CREATE TABLE gaps_ps(at REAL,cause TEXT,last_slot INTEGER)')
    c.execute('CREATE TABLE ps_capture_intervals(session TEXT,start_at REAL,through_at REAL,protocol INTEGER)')
    for i, (at, buy) in enumerate(rows): c.execute('INSERT INTO swaps_ps VALUES(?,?,?)', (POOL, at, buy))
    for at, scope, pj in losses: c.execute('INSERT INTO ps_loss_attribution VALUES(?,?,?,?)', ('s', scope, pj, at))
    for at, cause in points: c.execute('INSERT INTO gaps_ps VALUES(?,?,1)', (at, cause))
    for i, (a, b) in enumerate(intervals): c.execute('INSERT INTO ps_capture_intervals VALUES(?,?,?,1)', (str(i), a, b))
    if selection is not None:
        c.execute('CREATE TABLE ps_pool_selection(pool TEXT,session TEXT,start_at REAL,end_at REAL,protocol INTEGER)')
        for i, (a, b) in enumerate(selection): c.execute('INSERT INTO ps_pool_selection VALUES(?,?,?,?,1)', (POOL, str(i), a, b))
    c.commit(); c.close()
    return tmp_path


def test_sells_seen_on_the_tape_before_the_cut_are_true_with_their_count(tmp_path):
    assert PO.sells_before_cut(tape(tmp_path, rows=[(CUT - 10, 1), (CUT - 5, 0), (CUT - 4, 0)]), POOL, CUT) == (True, 2, 'seen_on_tape')


def test_a_proven_zero_needs_rows_continuous_capture_and_no_loss_or_capture_point(tmp_path):
    assert PO.sells_before_cut(tape(tmp_path, rows=[(CUT - 10, 1)], selection=SEL), POOL, CUT) == (False, 0, 'proven_zero')


@pytest.mark.parametrize('kw,why', [
    (dict(rows=[]), 'no_rows_in_window'),                                                           # outside retention: unknown, never zero
    (dict(rows=[(CUT - 10, 1)], losses=[(CUT - 20, 'complete', json.dumps([POOL]))], selection=SEL), 'pool_loss_in_window'),
    (dict(rows=[(CUT - 10, 1)], losses=[(CUT - 20, 'complete', 'broken')], selection=SEL), 'loss_scope_unreadable'),
    (dict(rows=[(CUT - 10, 1)], losses=[(CUT - 20, None, '[]')], selection=SEL), 'loss_scope_unknown'),
    (dict(rows=[(CUT - 10, 1)], points=[(CUT - 30, None)], selection=SEL), 'capture_point_in_window'),
    (dict(rows=[(CUT - 10, 1)], intervals=((CUT - 30, None),), selection=SEL), 'capture_not_continuous'),
    (dict(rows=[(CUT - 10, 1)], intervals=((0.0, None),), selection=SEL), 'capture_not_continuous'),             # no frontier proves nothing
    (dict(rows=[(CUT - 10, 1)], intervals=((0.0, CUT - 1),), selection=SEL), 'capture_not_continuous'),
    (dict(rows=[(CUT - 10, 1)], selection=((CUT - 20, None),)), 'pool_not_selected_whole_window'),               # selected only part of the window
    (dict(rows=[(CUT - 10, 1)], selection=((0.0, CUT - 1),)), 'pool_not_selected_whole_window'),
    (dict(rows=[(CUT - 10, 1)], selection=()), 'pool_not_selected_whole_window'),
    (dict(rows=[(CUT - 10, 1)], selection=SEL, losses=[(CUT - 20, 'unknown', '[]')]), 'loss_scope_unknown'),
    (dict(rows=[(CUT - 10, 1)], selection=SEL, losses=[(CUT - 20, 'complete', '{}')]), 'loss_scope_unreadable'),
    (dict(rows=[(CUT - 10, 1)], selection=SEL, losses=[(CUT - 20, 'complete', '[]')]), 'loss_scope_unreadable'),
    (dict(rows=[(CUT - 10, 1)], selection=SEL, losses=[(CUT - 20, 'complete', None)]), 'loss_scope_unreadable')])
def test_anything_short_of_a_proven_zero_is_unknown_not_zero(tmp_path, kw, why):
    assert PO.sells_before_cut(tape(tmp_path, **kw), POOL, CUT) == (None, None, why)


def test_a_loss_naming_another_pool_does_not_block_a_proven_zero(tmp_path):
    assert PO.sells_before_cut(tape(tmp_path, rows=[(CUT - 10, 1)], losses=[(CUT - 20, 'complete', json.dumps(['other']))], selection=SEL), POOL, CUT)[0] is False


def test_an_unreadable_tape_is_unknown(tmp_path):
    assert PO.sells_before_cut(tmp_path / 'absent', POOL, CUT) == (None, None, 'tape_unreadable')


def store_for(tmp_path):
    c = S.open_store(tmp_path / 'o.db')
    return c


def build(tmp_path, ticket=TICKET, got=GOT, **tape_kw):
    st = store_for(tmp_path)
    desk_dir = tape(tmp_path, **tape_kw) if tape_kw is not None else tmp_path
    host = CUT + 4000.0
    return st, PO.candidate(st, ticket, COHORT, got, offset=0.5, sampled_at=host - 5, host=host, desk_dir=desk_dir), host


def test_a_partial_receipt_passes_the_real_verify_and_order_receipt_path_and_risk_allows_on_the_probe(tmp_path):
    st, out, host = build(tmp_path)                                            # tape has no rows: sells unknown, the stored sell probe passed
    assert out['state'] == 'ok' and out['order']['sells_basis'] == 'sell_probe' and out['order']['sheet_state'] == 'partial' and out['order']['sheet_class'] == 'tape_loss_since_cut'
    assert out['order']['probe_age_s'] > 3000 and out['order']['probe_loss_pct'] == pytest.approx(2.3711063142259596) and out['order']['sheet_ticket_age_s'] > 3000
    cand = out['candidate']
    hit = S.latest(st, 'receipts', 'cache_key', cand['cache_key'])
    raw = json.loads(hit['payload_json'])
    rec = SH._verify(hit, cand['cache_key'], raw['definition_bundle_hash'], raw['strategy_epoch'])
    assert rec['state'] == 'known' and rec['sheet_kind'] == 'partial' and rec['sheet']['liquidity_note'].startswith('No current-liquidity evidence')
    ctx = SimpleNamespace(cut=dict(knowledge_cut=host - 0.5 + 1))
    assert EO._receipt(st, ctx, cand, host + 5)['row_hash']                                # the candidate matches the persisted receipt exactly
    bundle = dict(decision='ENTER', decision_id='d', bundle_hash='b' * 64, network_id='solana', mint=cand['mint'], pool=cand['pool'])
    res = R.risk_check(book_policy(), bundle=bundle, safety_blocks=rec['sheet']['safety'])
    assert (res.decision, res.reason) == ('allow', None)


def test_seen_sells_use_the_tape_basis_and_a_proven_zero_still_vetoes(tmp_path):
    st, out, _ = build(tmp_path, rows=[(CUT - 10, 1), (CUT - 5, 0)])
    assert out['order']['sells_basis'] == 'cut_tape' and out['order']['sells_n'] == 1 and out['order']['probe_age_s'] is None
    st2, zero, _ = build(tmp_path / 'z' if (tmp_path / 'z').mkdir() is None else tmp_path, rows=[(CUT - 10, 1)], selection=SEL)
    safety = zero['candidate']['sheet']['safety']
    assert safety['sells_exist_60s'] is False and safety['sells_unknown_allowed_by_probe'] is False
    bundle = dict(decision='ENTER', decision_id='d', bundle_hash='b' * 64, network_id='solana', mint='m', pool='p')
    assert R.risk_check(book_policy(), bundle=bundle, safety_blocks=safety).reason == 'no_sells_60s'


def test_unknown_sells_without_a_passing_probe_still_vetoes():
    safety = dict(authority_mint='closed', authority_freeze='closed', sell_probe='ok', sells_exist_60s=None, sells_unknown_allowed_by_probe=False)
    bundle = dict(decision='ENTER', decision_id='d', bundle_hash='b' * 64, network_id='solana', mint='m', pool='p')
    assert R.risk_check(book_policy(), bundle=bundle, safety_blocks=safety).reason == 'sells_60s_unknown'


def test_classes_outside_the_allowed_set_and_ineligible_or_thin_tickets_are_refused(tmp_path):
    st = store_for(tmp_path)
    kw = dict(offset=0.5, sampled_at=1.0, host=CUT + 10, desk_dir=tmp_path)
    for reason, why in (('hard_filter_missing', 'ticket_incomplete'), ('not_near_pass_eligible', 'ticket_not_eligible'), ('clock_unknown', 'other')):
        out = PO.candidate(st, TICKET, COHORT, dict(state='refused', reason=reason), **kw)
        assert out == dict(state='refused', reason='partial_class_not_allowed:' + why)
    thin = json.loads(json.dumps(TICKET)); thin['stages'] = []
    assert PO.candidate(st, thin, COHORT, GOT, **kw) == dict(state='refused', reason='partial_evidence_missing')
    assert PO.candidate(st, TICKET, None, GOT, **kw) == dict(state='unknown', reason='partial_inputs_unknown')
    skip = json.loads(json.dumps(TICKET)); skip['stages'][0]['verdict'] = 'SKIP'; skip['stages'][0]['reason'] = 'kill:cluster'
    assert PO.candidate(st, skip, COHORT, GOT, **kw) == dict(state='refused', reason='not_near_pass_eligible')            # a SKIP outside the near-pass stratum is not orderable


def test_the_knob_defaults_on_and_zero_turns_it_off(monkeypatch):
    monkeypatch.delenv('OFFICE_PARTIAL_SHEET_ORDERS', raising=False)
    assert PO.enabled() is True
    monkeypatch.setenv('OFFICE_PARTIAL_SHEET_ORDERS', '0')
    assert PO.enabled() is False


def test_a_tape_without_the_selection_table_proves_no_zero(tmp_path):
    assert PO.sells_before_cut(tape(tmp_path, rows=[(CUT - 10, 1)]), POOL, CUT) == (None, None, 'tape_query_failed')


from office.test_judge_gpt_book import world  # noqa: E402,F401


def test_a_real_partial_receipt_order_lands_in_the_real_ledger_with_its_stratum(world, monkeypatch):
    import copy, sqlite3
    from sniper.office import ledger as L
    ticket = copy.deepcopy(TICKET); ticket['pool'] = 'P'
    monkeypatch.setattr(world.gpt, '_ticket', lambda _: ticket)
    monkeypatch.setattr(world.gpt, 'sheet', lambda *a, **k: GOT)
    monkeypatch.setattr(PO, 'sells_before_cut', lambda *a: (None, None, 'no_rows_in_window'))
    monkeypatch.setattr(world.gpt, '_partial', lambda t, g, **k: PO.candidate(k['store'], t, COHORT, g, offset=k['offset'], sampled_at=k['sampled_at'], host=k['host'], desk_dir=world.path))
    error, out = world.judge_gpt('paper_order', ticket_id='t1', reason='r', thesis='t', disproof='d', exit_plan='e')
    assert out['state'] == 'submitted', out
    c = sqlite3.connect(world.path / 'gpt.db')
    row = c.execute('SELECT sheet_state,sheet_class,sells_basis FROM orders WHERE order_id=?', (out['order_id'],)).fetchone()
    assert row == ('partial', 'tape_loss_since_cut', 'sell_probe')
