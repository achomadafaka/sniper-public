"""Review witnesses for exact source SHA ebe9271078ec76fe06086c40ccb3a553736ec252."""
from decimal import Decimal

from scripts.research import desk_judge_speed_outcomes as O
import test_desk_judge_speed_outcomes as F
import pytest


def test_displaced_real_tokens_must_not_be_negative():
    rows = F.tape()
    rows[61]['real_token'] = 1
    result = O.grade_case(F.case(), rows, [], Decimal('100'))
    tokens = int(result['entry']['tokens_atomic'])
    outcome = result['outcomes']['60']
    assert 1 - tokens < 0
    assert outcome['state'] == 'censored', (
        f'Negative displaced real_token={1-tokens}; emitted {outcome}')


def test_one_transaction_index_cannot_name_two_signatures():
    rows = F.tape()
    for r in rows:
        r['tx_index'] = 10
    rows.append(F.state(1.2, 2, tx_index=10, ordinal=1, signature='different-tx'))
    result = O.grade_case(F.case(), rows, [], Decimal('100'))
    assert result['outcomes']['60']['reason'] == 'ambiguous_slot_order', result


def test_same_transaction_keeps_its_distinct_instruction_ordinals():
    rows = F.tape()
    rows.append(F.state(1.2, 2, tx_index=0, ordinal=1, signature='sig-2'))
    result = O.grade_case(F.case(), rows, [], Decimal('100'))
    assert result['outcomes']['60']['state'] == 'modeled'
    assert result['entry']['ordinal'] == 1


@pytest.mark.parametrize('remaining', [0, -1, 1])
def test_displaced_real_token_boundary(remaining):
    entry = O.buy_exact(F.state(), 250_000_000)
    endpoint = F.state(real_token=entry['tokens_atomic'] + remaining)
    if remaining <= 0:
        with pytest.raises(O.Censor, match='own_displacement_invalid'):
            O.sell_exact(endpoint, entry)
    else:
        assert O.sell_exact(endpoint, entry)['out_atomic'] > 0


def test_missing_later_day_must_not_censor_complete_short_horizon(tmp_path):
    import sqlite3
    rows = F.tape()
    origin = 86300  # 23:58:20 UTC; entry+60 exists today, entry+300 crosses midnight.
    c = F.case()
    c['call_started_at'] = '1970-01-01T23:58:20+00:00'
    swap_dir = tmp_path / 'swaps'
    gap_dir = tmp_path / 'gaps'
    swap_dir.mkdir(); gap_dir.mkdir()
    with sqlite3.connect(swap_dir / '1970-01-01.db') as db:
        db.execute('CREATE TABLE swaps(received_at REAL,slot INTEGER,tx_index INTEGER,ordinal INTEGER,signature TEXT,mint TEXT,actor TEXT,quote_mint TEXT,virtual_quote INTEGER,virtual_token INTEGER,real_quote INTEGER,real_token INTEGER,fee_bps INTEGER,creator_fee_bps INTEGER,cashback_fee_bps INTEGER)')
        for r in rows[:100]:
            r['received_at'] += origin
            db.execute('INSERT INTO swaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', [r[k] for k in O.COLUMNS])
    with sqlite3.connect(gap_dir / '1970-01-01.db') as db:
        db.execute('CREATE TABLE gaps(at REAL,cause TEXT)')
    start = origin + O.DELAY_S
    tape, gaps, read_error = O.load_window(tmp_path, 'M', start-O.SILENCE_S, start+max(O.HORIZONS)+O.SILENCE_S)
    assert read_error == 'missing_tape'
    assert O.grade_case(c,tape,gaps,Decimal('100'))['outcomes']['60']['state'] == 'modeled'
    import json
    proof, raw = F.fx_proof()
    manifest = tmp_path / 'manifest.json'
    provenance = tmp_path / 'fx-provenance.json'
    manifest.write_text(json.dumps({'cases': [c]}))
    provenance.write_text(json.dumps({'rows': [proof]}))
    calls = tmp_path / 'calls.db'
    with sqlite3.connect(calls) as db:
        db.execute('CREATE TABLE desk_judge_calls(call_id TEXT,ticket_id TEXT,call_started_at TEXT,result_json TEXT)')
        db.execute('INSERT INTO desk_judge_calls VALUES(?,?,?,?)',
                   (c['call_id'], c['ticket_id'], c['call_started_at'], raw))
    result = O.run(manifest, provenance, calls, tmp_path, tmp_path / 'outcomes.jsonl')[0]
    assert result['outcomes']['60']['state'] == 'modeled', result['outcomes']
    assert result['outcomes']['300']['reason'] == 'missing_tape'
