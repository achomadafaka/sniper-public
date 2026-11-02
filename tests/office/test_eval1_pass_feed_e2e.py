"""End-to-end witness for the eval-1 pass feed with a REAL SH.sheet receipt (live receipt seq 726, captured byte-exact):
receipt -> Judge frame candidates -> Am11 round -> both Judges' judge.decide + memo -> host settlement -> ledger order.
No provider call, no tape read; the only synthetic inputs are the two Judge answers, the FX sample and the clock."""
import json
from pathlib import Path
from types import SimpleNamespace

from sniper.office import eval_settle as ES, eval_transition as X, judge_decisions as J, judge_rounds as JR, ledger as L
from sniper.office import runtime as R, store as S
from sniper.office.supervisor import _put, action_key

from .eval1_pass_sheet_fixture import RECEIPT_JSON
REPO = Path(__file__).resolve().parents[2]


def test_real_pass_sheet_reaches_a_round_and_a_ledger_order(tmp_path):
    raw = RECEIPT_JSON
    rec = json.loads(raw)
    assert 'feed' not in rec['request']                               # the live shape: SH.sheet omits feed for the pass feed
    at = rec['clock_sample']['corrected_utc']
    conn = S.open_store(tmp_path / 'office.db')
    row = S.append(conn, 'receipts', rec)
    conn.execute('INSERT INTO epochs VALUES(?,?)', (X.NEW, json.dumps({'epoch_id': X.NEW, 'first_order_at': None}))); conn.commit()
    ledger = L.open_ledger(tmp_path / 'ledger.db')
    assert X.provision_ledger(ledger, now=at - 3600) is None

    cut = at + 10
    # Am14: the host pass "re-sheets" the ticket (here: the persisted real receipt) and opens the round
    rnd, attempts = JR.advance(conn, epoch_id=X.NEW, now=at + 5, judges=['jc', 'jg'],
                               tickets=lambda now: [{'ticket_id': 't726', 'feed': 'pass'}], sheet=lambda tid: R.candidate_by_row(conn, row))
    assert rnd['item']['receipt_row_hash'] == row and rnd['item']['feed'] == 'pass' and rnd['rule'] == 'am14'
    framed = JR.frame_round(conn, cut=cut)
    assert framed['item']['receipt_row_hash'] == row and framed['sheet_age_s'] == 10

    for actor, slot, model in (('jc', 'judge_claude', 'claude-opus-5-5'), ('jg', 'judge_gpt', 'gpt-6-luna')):
        ctx = SimpleNamespace(epoch_id=X.NEW, actor_id=actor, generation=3, decision_id='turn-' + actor, cut={'knowledge_cut': cut})
        J.record(conn, context=ctx, slot=slot, args={'decision': 'ENTER', 'candidate': row, 'order_id': None, 'public_reason': 'fresh buyers'},
                 now=cut + 1, equity_snapshot_id='e' * 64, lesson_ids=[])
        _put(conn, 'messages', action_key('turn-' + actor, 'memo.commit'),
             {'actor_id': actor, 'request': {'memo': 'Call: ENTER. Disproof: buyers stop', 'served_model': model,
                                             'served_model_basis': 'echoed' if slot == 'judge_claude' else 'requested_pinned'}})
    conn.commit()
    book = json.loads((REPO / 'config/office/book_policy_pilot.json').read_text())
    policy = L.OfficeBookPolicy(**{**book['policy'], 'chains': ('solana',)})
    now = cut + 5
    out = ES.settle(conn, ledger, policy=policy, now=now, sol_usd=150., sol_at=now,
                    candidate_of=lambda h: R.candidate_by_row(conn, h))
    assert len(out) == 1 and out[0]['label'] == 'ENTER', out
    print('SETTLE', out)
    assert out[0]['state'] == 'submitted', (out[0]['state'], out[0]['reason'], out[0]['outcome'])
    order = ledger.execute('SELECT book_id,epoch_id,mint,pool,state FROM orders').fetchone()
    assert tuple(order)[:4] == (X.BOOK, X.NEW, rec['request']['mint'], rec['request']['pool'])
    assert json.loads(conn.execute('SELECT payload_json FROM epochs WHERE id=?', (X.NEW,)).fetchone()[0])['first_order_at'] == now
    assert S.latest(conn, 'receipts', 'cache_key', 'judge.settle:' + out[0]['decision_id']) is not None
    assert ES.settle(conn, ledger, policy=policy, now=now + 1, sol_usd=150., sol_at=now + 1,
                     candidate_of=lambda h: R.candidate_by_row(conn, h)) == []          # settled once


def test_required_facts_declaration_is_the_sheet_contract_and_is_hashed_with_the_policy():
    from sniper.office.tools import sheet as SH
    decl = R.required_facts('b' * 64)
    assert decl['version'] == R.REQUIRED_FACTS_VERSION and decl['facts'] == list(SH.REQUIRED)
    assert decl['missing'].endswith('sheet.missing_required') and R.required_facts('c' * 64)['sha256'] != decl['sha256']
    assert not {'age_s', 'curve_pct', 'roster', 'crawler_flags', 'creator_prior_launches'} & set(decl['facts'])
    real = json.loads(RECEIPT_JSON)['sheet']
    assert all(k in real for k in SH.REQUIRED)             # every required fact exists as a key on a real pass sheet
