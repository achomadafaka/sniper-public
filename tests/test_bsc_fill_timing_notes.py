import json
import sqlite3
from pathlib import Path

from sniper.services import memecoin_fill_timing as ft
from scripts.research import fill_timing_resolver as resolver

TX = '0x' + 'ab' * 32
MINT = '0x' + 'cd' * 20


def test_watcher_note_survives_and_keeps_exact_source_block(tmp_path):
    path = tmp_path / 'watch.log'
    note = dict(chain='56', tx_hash=TX, mint=MINT, reason='tax_unknown', source_block=100)
    path.write_text('[bsc-lane-note] ' + json.dumps(note) + '\n')
    conn = ft.connect(tmp_path / 'timing.db')
    resolver.ingest_bsc_notes(conn, path)
    path.unlink()
    fast = ft.fast_of({}, '56', TX, timing=conn, mint=MINT)
    assert fast['reason'] == 'tax_unknown'
    f = ft.facts(dict(network_id='56', terminal_status='filled', mint=MINT), fast=fast,
                 timing={'landing_slot': 105})
    assert ft.gap_of(f) == 5
    assert 'tax_unknown' in ft.landed_text(f)
    assert ft.fast_of({}, '56', TX, timing=conn, mint='0x' + 'ef' * 20) is None
    assert ft.fast_of({}, '4663', TX, timing=conn, mint=MINT) is None


def test_partial_wrong_chain_and_invalid_block_stay_unknown(tmp_path):
    path = tmp_path / 'watch.log'
    notes = [dict(chain='4663', tx_hash=TX, mint=MINT, reason='wrong', source_block=100),
             dict(chain='56', tx_hash=TX, mint=MINT, reason='wrong', source_block=False)]
    path.write_text(''.join('[bsc-lane-note] ' + json.dumps(n) + '\n' for n in notes)
                    + '[bsc-lane-note] ' + json.dumps(dict(chain='56', tx_hash=TX, mint=MINT,
                                                        reason='partial', source_block=100)))
    conn = ft.connect(tmp_path / 'timing.db')
    resolver.ingest_bsc_notes(conn, path)
    assert ft.fast_of({}, '56', TX, timing=conn, mint=MINT) is None


def test_runner_verdict_wins_over_watcher_note(tmp_path):
    path = tmp_path / 'watch.log'
    path.write_text('[bsc-lane-note] ' + json.dumps(dict(chain='56', tx_hash=TX, mint=MINT,
                    reason='would_send', source_block=100)) + '\n')
    timing = ft.connect(tmp_path / 'timing.db')
    resolver.ingest_bsc_notes(timing, path)
    lane = sqlite3.connect(':memory:')
    lane.execute('CREATE TABLE bsc_lane_events (id INTEGER, tx_hash TEXT, mint TEXT, reason TEXT, written_at TEXT)')
    lane.execute('INSERT INTO bsc_lane_events VALUES (1, ?, ?, ?, NULL)',
                 (TX, MINT, 'hook:rejected:wallet_initiation_unproved'))
    fast = ft.fast_of({'bsc': lane}, '56', TX, timing=timing, mint=MINT)
    assert fast['reason'] == 'hook:rejected:wallet_initiation_unproved'
    assert fast['source_slot'] == 100


def test_failed_s_plus_one_is_price_failure():
    f = ft.facts(dict(network_id='solana', signal_source='slot_lane', terminal_status='failed',
                      terminal_reason='slot_pump_slippage',
                      trace_json=json.dumps(dict(source_evidence={'source_slot': 100},
                                      submission_events=[dict(stage='chain_confirmation', landing_slot=101)]))))
    assert ft.gap_of(f) == 1
    assert ft.classify(f)[0] == 'price'


def test_existing_capture_recovers_gme_block_without_rpc(tmp_path):
    capture = Path(__file__).parent / 'fixtures/memecoin/gme_bsc_source_20261005.json'
    frame = json.loads(capture.read_text())
    path = tmp_path / 'capture.jsonl'
    path.write_text(json.dumps(frame) + '\n')
    conn = ft.connect(tmp_path / 'timing.db')
    resolver.ingest_bsc_notes(conn, tmp_path / 'missing.log', capture_paths=(path,))
    note = ft.fast_of({}, '56', frame['tx_hash'], timing=conn,
                      mint='0x3e7d61d4ac560fcfc1902a5c54d93172dafb19de')
    assert note['source_slot'] == int(frame['receipt']['blockNumber'], 16)
    assert note['reason'] == 'source_receipt_unsupported'


def test_watcher_existing_log_write_contains_full_identity(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watcher
    lines = []
    monkeypatch.setattr(watcher, '_say', lines.append)
    watcher._signal_bsc_lane(lambda *a, **k: dict(reason='tax_unknown', sent=False, decide_ms=.2),
                            dict(tx_hash=TX, buy={'mint': MINT}),
                            {'_receipt': {'blockNumber': '0x64'}}, 600, .1)
    note = json.loads(lines[0].split('[bsc-lane-note] ', 1)[1])
    assert (note['tx_hash'], note['mint'], note['chain'], note['source_block']) == (TX, MINT, '56', 100)
    assert 'quote' not in note


def test_a_lane_note_carries_the_quote_when_the_decision_names_one(monkeypatch):
    """F2: a stale hop note names the quote whose WBNB pair the tape could not vouch for."""
    from scripts.research import onchain_evm_wallet_watch as watcher
    lines = []
    monkeypatch.setattr(watcher, '_say', lines.append)
    quote = '0x' + '9b' * 20
    watcher._signal_bsc_lane(lambda *a, **k: dict(reason='hop_state_stale:untracked', quote=quote,
                                                  sent=False, decide_ms=.2),
                            dict(tx_hash=TX, buy={'mint': MINT}), {'_receipt': {'blockNumber': '0x64'}}, 600, .1)
    note = json.loads(lines[0].split('[bsc-lane-note] ', 1)[1])
    assert note['reason'] == 'hop_state_stale:untracked' and note['quote'] == quote
