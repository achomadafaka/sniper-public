"""Venue-isolated descriptive features, with no outcome fit or admission score."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_pumpswap_candidates as P
from test_memecoin_desk_pumpswap_candidates import facts, event, NOW, POOL, MINT, SIG

SPEC = importlib.util.spec_from_file_location('desk_pumpswap_features', Path(__file__).resolve().parents[1]
                                              / 'scripts/research/desk_pumpswap_features.py')
F = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(F)


def candidate():
    b = P.PoolBook(band=(30_000, 100_000), cooldown_s=300, sol_max_age_s=120)
    m, meta = facts()
    args = dict(mapping=m, metadata=meta, signature=SIG, ordinal=0, tx_index=None,
                sol_usd=100, sol_usd_at=NOW-1)
    b.on_swap(event=event(), slot=10, now=NOW, received_at=NOW, **args)
    return b.on_swap(event=event('40000000000'), slot=11, now=NOW+1, received_at=NOW+1, **args)


def swap(slot, at, *, buy=1, quote='1000000000', price_quote='40000000000', **kwargs):
    return {'pool': POOL, 'mint': MINT, 'quote_mint': facts()[0]['quote_mint'], 'slot': slot,
            'signature': SIG + str(slot), 'ordinal': 0, 'received_at': at, 'processed_at': at+.1,
            'map_known_at': NOW-100, 'map_status': 'mapped', 'is_buy': buy, 'actor': f'actor{slot}',
            'base_atomic': '1000000', 'quote_atomic': quote, 'pre_base_reserve_atomic': '100000000000000',
            'pre_quote_reserve_atomic': price_quote, 'lp_fee_bps': '20', 'protocol_fee_bps': '5',
            'creator_fee_bps': '5', 'cashback_fee_bps': '0', **kwargs}


def test_pool_features_use_sol_flow_strict_slot_and_both_availability_clocks():
    rows = [swap(12, NOW+2), swap(13, NOW+3, buy=0, quote='2000000000', price_quote='44000000000'),
            swap(14, NOW+4, price_quote='50000000000'), swap(16, NOW+6), swap(20, NOW+4)]
    rows[3]['processed_at'] = NOW+100
    got = F.extract(candidate(), rows, [], cutoff=NOW+10, cutoff_slot=20)
    assert got['status'] == 'descriptive' and got['score'] is None and got['shortlisted'] is None
    assert got['features']['n_swaps'] == 3 and got['features']['distinct_buyers'] == 2
    assert got['features']['buy_sol'] == 2 and got['features']['sell_sol'] == 2
    assert got['features']['log_return'] == pytest.approx(__import__('math').log(1.25))
    assert got['features']['price_basis'] == 'pre_reserves_plus_proven_virtual_quote'


@pytest.mark.parametrize('key,value,reason', [('lp_fee_bps', None, 'fee_unknown'),
    ('pre_quote_reserve_atomic', None, 'reserves_unknown'), ('map_status', 'unmapped', 'map_unknown'),
    ('map_known_at', None, 'map_unknown'), ('mint', POOL, 'identity_mismatch'),
    ('quote_mint', MINT, 'identity_mismatch'), ('received_at', None, 'availability_unknown')])
def test_unknowns_are_typed_unscored_never_zero(key, value, reason):
    row = swap(12, NOW+2); row[key] = value
    got = F.extract(candidate(), [row], [], cutoff=NOW+10, cutoff_slot=20)
    assert got == {'status': 'unscored', 'reason': reason, 'features': None, 'score': None, 'shortlisted': None}


def test_future_gap_excluded_and_in_window_gap_censors():
    args = dict(cutoff=NOW+10, cutoff_slot=20)
    rows = [swap(12, NOW+2)]
    got = F.extract(candidate(), rows, [{'at': NOW+11}], **args)
    assert got['status'] == 'descriptive' and got['features']['log_return'] is None
    assert F.extract(candidate(), rows, [{'at': NOW+9}], **args)['reason'] == 'tape_gap'
    assert F.extract(candidate(), [], [], **args)['reason'] == 'tape_empty'


def tape(tmp_path):
    day = F.utc_day(NOW)
    (tmp_path/'candidate_emissions_ps').mkdir()
    (tmp_path/'candidate_emissions_ps'/f'{day}.jsonl').write_text(json.dumps(candidate())+'\n')
    (tmp_path/'pumpswap').mkdir()
    c = sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    row = swap(12, NOW+2)
    c.execute('CREATE TABLE swaps_ps('+','.join(f'{k} '+ ('REAL' if k in ('received_at','processed_at','map_known_at') else 'INTEGER' if k in ('slot','ordinal','is_buy') else 'TEXT') for k in row)+')')
    c.execute('INSERT INTO swaps_ps VALUES('+','.join('?' for _ in row)+')', list(row.values()))
    c.execute('CREATE TABLE gaps_ps(at REAL)')
    c.commit(); c.close()
    return day


def test_cycle_writes_first_features_once_and_never_opens_curve_files(tmp_path):
    day = tape(tmp_path)
    (tmp_path/'candidate_emissions').mkdir()
    (tmp_path/'candidate_emissions'/f'{day}.jsonl').write_text('invalid curve file must not be opened')
    (tmp_path/'swaps').mkdir()
    curve = tmp_path/'swaps'/f'{day}.db'; curve.write_bytes(b'curve-db-unchanged')
    first = F.cycle(tmp_path, cutoff=NOW+10, cutoff_slot=20)
    assert first == {'n_candidates': 1, 'n_descriptive': 1, 'n_unscored': 0}
    registry = sqlite3.connect(tmp_path/'ranker_ps'/'candidates.db')
    original = registry.execute('SELECT features_json,first_rank_at,score,shortlisted FROM first_features').fetchone()
    c = sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    c.execute("UPDATE swaps_ps SET quote_atomic='999000000000'"); c.commit(); c.close()
    F.cycle(tmp_path, cutoff=NOW+11, cutoff_slot=21)
    assert registry.execute('SELECT features_json,first_rank_at,score,shortlisted FROM first_features').fetchone() == original
    assert registry.execute('SELECT COUNT(*) FROM cycle_rows').fetchone()[0] == 2
    assert original[-2:] == (None, None) and curve.read_bytes() == b'curve-db-unchanged'
    registry.close()


def test_default_off_cli_does_not_create_registry(tmp_path, monkeypatch):
    monkeypatch.delenv('MEMECOIN_DESK_PUMPSWAP_RANKER_ENABLED', raising=False)
    assert F.main(['--once', '--desk-dir', str(tmp_path)]) == 0
    assert not (tmp_path/'ranker_ps').exists()


def test_metadata_unavailable_at_a_swap_cannot_reprice_it_later():
    c = candidate()
    state = c['snapshot']['fields']['pool_state']['value']
    state['metadata_known_at'] = NOW+1
    row = swap(10, NOW-.1)
    # Keep the candidate's hash correct while checking original metadata availability.
    from test_memecoin_desk_contract import rehash
    rehash(c)
    assert F.extract(c, [row], [], cutoff=NOW+10, cutoff_slot=20)['reason'] == 'metadata_unknown'


def test_same_slot_unknown_transaction_order_is_not_a_return_path():
    a, b = swap(12, NOW+2), swap(12, NOW+3)
    b['signature'] += 'other'
    assert F.extract(candidate(), [a,b], [], cutoff=NOW+10, cutoff_slot=20)['reason'] == 'source_order_unknown'


def test_cycle_preserves_missing_clocks_as_unscored_even_with_other_good_rows(tmp_path):
    day = tape(tmp_path)
    db = sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    db.execute('UPDATE swaps_ps SET processed_at=NULL'); db.commit(); db.close()
    F.cycle(tmp_path, cutoff=NOW+10, cutoff_slot=20)
    db = sqlite3.connect(tmp_path/'ranker_ps'/'candidates.db')
    assert db.execute('SELECT status,reason FROM first_features').fetchone() == ('unscored','availability_unknown')
    db.close()


def test_defensive_event_dedup_ignores_exact_copy_and_refuses_conflicting_source_facts():
    row = swap(12, NOW+2)
    args = dict(cutoff=NOW+10, cutoff_slot=20)
    assert F.extract(candidate(), [row,dict(row)], [], **args)['features']['n_swaps'] == 1
    conflict = dict(row, quote_atomic='999')
    assert F.extract(candidate(), [row,conflict], [], **args)['reason'] == 'source_conflict'


def test_resource_window_is_bounded_without_an_admission_score():
    assert F.extract(candidate(), [], [], cutoff=NOW+10, cutoff_slot=20, window_s=3601)['reason'] == 'cutoff_unknown'


def test_cycle_replay_across_midnight_does_not_duplicate_one_source_event(tmp_path):
    day = tape(tmp_path)
    from datetime import datetime, timezone
    midnight = datetime.fromtimestamp(NOW, timezone.utc).replace(hour=0, minute=0, second=0).timestamp()+86400
    row = swap(12, midnight-.2)
    db = sqlite3.connect(tmp_path/'pumpswap'/'tape.db')
    for _ in range(2):
        db.execute('INSERT INTO swaps_ps VALUES('+','.join('?' for _ in row)+')', list(row.values()))
    db.commit(); db.close()
    # Old day files are invalid on purpose: this reader only opens the lifetime tape.
    (tmp_path/'swaps_ps').mkdir(); (tmp_path/'swaps_ps'/f'{day}.db').write_bytes(b'not a source')
    F.cycle(tmp_path, cutoff=midnight+.2, cutoff_slot=20)
    db = sqlite3.connect(tmp_path/'ranker_ps'/'candidates.db')
    got = json.loads(db.execute('SELECT features_json FROM first_features').fetchone()[0])
    assert got['n_swaps'] == 1
    db.close()
