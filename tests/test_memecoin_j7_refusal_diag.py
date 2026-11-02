import ast
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services import memecoin_j7_refusal_diag as diag

NOW = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
SIG = '4m7RqswVGQ87ybyskrJAp7kaUQEw73CzdzvfdEjsDb8Pd6N1BNMZ4QeTKVoNKcfUshaJWsdUqqSVpnBQkXjkteT2'
WALLET = 'Ec2f5dnhzunrit1exqghsifdbp1wgrzktsrrzctu92mj'
MINT = '6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx'
UID = 'u1'


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'l.db')
    c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_wallets(source_user_id, real_chains, real_approved) VALUES (?, 'solana', 1)", (UID,))
    return c


def attempt(c, n=1, *, decided=NOW - timedelta(minutes=5), reason='j7_receipt_not_observed',
            sig=SIG, origin=None, uid=UID):
    c.execute(
        "INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, signal_source, network_id, "
        "mint, source_user_id, side, delivery_lane, decision_started_at, terminal_status, "
        "terminal_reason, completed_at) VALUES (?,?,'follow_activity','solana',?,?,'buy','j7_wallets',?,"
        "'refused',?,?)",
        (f'a{n}', origin or f'swap:raybot:{sig}:{WALLET.lower()}:{MINT.lower()}', MINT, uid,
         decided.isoformat(), reason, decided.isoformat()))


def detection(c, created, *, sig=SIG, uid=UID, address=WALLET, mint=MINT, block_time=None, state=None):
    c.execute(
        "INSERT INTO onchain_wallet_detections(signature, source_user_id, address, network_id, mint, "
        "block_time, detected_at, created_at, source_observation_state) VALUES (?,?,?,'solana',?,?,?,?,?)",
        (sig, uid, address, mint, block_time, (created - timedelta(milliseconds=50)).isoformat(),
         created.isoformat(), state))


def test_late_row_reports_write_lag_and_detected_lag_separately(conn):
    attempt(conn)
    detection(conn, NOW - timedelta(minutes=5) + timedelta(milliseconds=200))
    snap = diag.sample(conn, NOW)
    assert snap['receipt_states']['own_row_late'] == 1
    ex = snap['examples'][0]
    assert ex['write_lag_ms'] == 200.0 and ex['detected_lag_ms'] == 150.0


def test_row_before_decision_is_timing_unknown(conn):
    attempt(conn)
    detection(conn, NOW - timedelta(minutes=6))
    assert diag.sample(conn, NOW)['receipt_states']['own_row_present_timing_unknown'] == 1


def test_absent_then_late_on_later_sample(conn):
    attempt(conn)
    assert diag.sample(conn, NOW)['receipt_states']['own_row_absent'] == 1
    detection(conn, NOW - timedelta(minutes=4))
    assert diag.sample(conn, NOW + timedelta(minutes=1))['receipt_states']['own_row_late'] == 1


def test_zero_cohort_differs_from_read_failure(conn, tmp_path):
    ok = diag.sample(conn, NOW)
    assert ok['complete'] is True and ok['selected_count'] == 0
    bare = sqlite3.connect(tmp_path / 'empty.db')
    bad = diag.sample(bare, NOW)
    assert bad['complete'] is False and bad['error'] and 'selected_count' not in bad


def test_other_chain_roster_is_not_armed_current_basis(conn):
    conn.execute("UPDATE memecoin_wallets SET real_chains='4663'")
    attempt(conn)
    snap = diag.sample(conn, NOW)
    assert snap['chain_eligibility']['not_armed_on_chain'] == 1
    assert snap['eligibility_basis'] == 'current_registry'


@pytest.mark.parametrize('chains', ['solana,zzz_bogus', None, ''])
def test_malformed_or_missing_chains_are_unknown(conn, chains):
    conn.execute('UPDATE memecoin_wallets SET real_chains=?', (chains,))
    attempt(conn)
    assert diag.sample(conn, NOW)['chain_eligibility']['unknown'] == 1


@pytest.mark.parametrize('flag', ['paused=1', 'excluded=1', 'real_approved=0'])
def test_solana_chains_without_authority_are_not_armed(conn, flag):
    conn.execute(f'UPDATE memecoin_wallets SET {flag}')
    attempt(conn)
    assert diag.sample(conn, NOW)['chain_eligibility']['not_armed_on_chain'] == 1


@pytest.mark.parametrize('origin', [f'swap::{SIG}::', f'swap:raybot:{SIG}::', f'swap:raybot:{SIG}:w:m',
                                    f'swap::{SIG}:{WALLET.lower()}:{MINT.lower()}'])
def test_malformed_origin_is_unknown_never_absent(conn, origin):
    attempt(conn, origin=origin)
    assert diag.sample(conn, NOW)['receipt_states']['unknown'] == 1


def test_missing_profile_is_unknown(conn):
    attempt(conn, uid='ghost')
    assert diag.sample(conn, NOW)['chain_eligibility']['unknown'] == 1


def test_paused_excluded_reported_separately(conn):
    conn.execute('UPDATE memecoin_wallets SET paused=1, excluded=1, real_approved=0')
    attempt(conn)
    assert diag.sample(conn, NOW)['wallet_flags'] == {'paused': 1, 'excluded': 1, 'unapproved': 1}


def test_foreign_identity_is_mismatch_and_case_preserved(conn):
    attempt(conn)
    detection(conn, NOW, uid='other')
    assert diag.sample(conn, NOW)['receipt_states']['identity_mismatch'] == 1
    assert diag.exact_signature(f'swap:x:{SIG}:{WALLET.lower()}:{MINT.lower()}')[0] == SIG


def test_opaque_id_is_unknown_without_proximity_lookup(conn):
    attempt(conn, origin='swap:3f2b8c1a-0000-0000-0000-000000000000')
    detection(conn, NOW - timedelta(minutes=4))
    snap = diag.sample(conn, NOW)
    assert snap['receipt_states']['unknown'] == 1 and snap['distinct_signatures'] == 0


def test_retracted_and_missing_block_time_stay_diagnostic(conn):
    attempt(conn)
    detection(conn, NOW - timedelta(minutes=4), state='retracted')
    snap = diag.sample(conn, NOW)
    assert snap['examples'][0]['retracted'] is True
    assert snap['examples'][0]['block_time_present'] is False
    assert conn.execute('SELECT terminal_reason FROM memecoin_entry_attempts').fetchone()[0] == 'j7_receipt_not_observed'


def test_other_terminal_reasons_counted_not_renamed(conn):
    attempt(conn, 1, reason='j7_source_time_unavailable')
    snap = diag.sample(conn, NOW)
    assert snap['terminal_reasons'] == {'j7_source_time_unavailable': 1}
    assert snap['classified_count'] == 0


def test_future_timestamp_is_unknown(conn):
    attempt(conn)
    detection(conn, NOW + timedelta(minutes=5))
    assert diag.sample(conn, NOW)['receipt_states']['unknown'] == 1


def test_slow_statement_hits_per_statement_cap(conn, monkeypatch):
    attempt(conn)
    monkeypatch.setattr(diag, '_ATTEMPTS', 'WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) '
                        'SELECT * FROM n, (SELECT ? a, ? b, ? c)')
    t = __import__('time').monotonic()
    snap = diag.sample(conn, NOW, budget_s=5)
    assert snap['error'] == 'budget_exhausted' and __import__('time').monotonic() - t < 0.5


def test_future_detected_at_is_unknown(conn):
    attempt(conn, decided=NOW - timedelta(seconds=1))
    detection(conn, NOW)
    conn.execute('UPDATE onchain_wallet_detections SET detected_at=?', ((NOW + timedelta(hours=1)).isoformat(),))
    snap = diag.sample(conn, NOW)
    assert snap['receipt_states']['unknown'] == 1 and snap['examples'][0]['reason'] == 'invalid_timestamp'


def test_snapshot_read_refuses_fifo_symlink_and_oversize(tmp_path):
    import os, time as _t
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    t0 = _t.monotonic()
    assert diag.read_snapshot(fifo)['state'] == 'unreadable' and _t.monotonic() - t0 < 0.1
    big = tmp_path / 'big.json'
    big.write_text('{"a": "' + 'x' * diag.MAX_BYTES + '"}')
    assert diag.read_snapshot(big)['state'] == 'unreadable'
    good = tmp_path / 'good.json'
    good.write_text('{}')
    link = tmp_path / 'link.json'
    link.symlink_to(good)
    assert diag.read_snapshot(link)['state'] == 'unreadable'


def test_overflow_sets_truncated(conn):
    for n in range(4):
        attempt(conn, n, sig=SIG[:-1] + 'ABCD'[n])
    snap = diag.sample(conn, NOW, limit=3)
    assert snap['truncated'] is True and snap['selected_count'] == 3


def test_budget_exhaustion_is_incomplete(conn):
    attempt(conn)
    snap = diag.sample(conn, NOW, budget_s=-1)
    assert snap == {**snap, 'complete': False, 'error': 'budget_exhausted'}


def test_attempt_query_uses_source_index(conn):
    plan = ' '.join(r[3] for r in conn.execute('EXPLAIN QUERY PLAN ' + diag._ATTEMPTS, ('a', 'b', 1)))
    assert 'idx_mc_entry_attempt_source' in plan
    plan = ' '.join(r[3] for r in conn.execute(
        'EXPLAIN QUERY PLAN SELECT * FROM onchain_wallet_detections WHERE signature = ?', ('x',)))
    assert 'USING INDEX' in plan


def test_cron_pass_writes_snapshot_and_stale_absent_are_not_healthy(conn, tmp_path):
    attempt(conn)
    conn.commit()
    out = tmp_path / 'snap.json'
    diag.run_once(tmp_path / 'l.db', out)
    fresh = diag.read_snapshot(out)
    assert fresh['complete'] is True and fresh['stale'] is False
    assert diag.read_snapshot(out, datetime.now(timezone.utc) + timedelta(seconds=diag.STALE_S + 1))['stale'] is True
    assert diag.read_snapshot(tmp_path / 'none.json') == {'state': 'absent', 'stale': True}
    (tmp_path / 'bad.json').write_text('{')
    assert diag.read_snapshot(tmp_path / 'bad.json')['state'] == 'unreadable'


def test_cron_pass_on_unreadable_ledger_publishes_error(tmp_path):
    snap = diag.run_once(tmp_path / 'missing.db', tmp_path / 's.json')
    assert snap['complete'] is False and snap['error']


def test_runner_starts_no_diag_thread_and_never_samples():
    loop = (Path(__file__).resolve().parents[1] / 'scripts' / 'memecoin_runner_loop.py').read_text()
    imports = [line.strip() for line in loop.splitlines() if 'memecoin_j7_refusal_diag' in line]
    assert imports == ['from sniper.services.memecoin_j7_refusal_diag import read_snapshot, snapshot_path']


def test_restart_repeats_same_cohort_without_writes(conn):
    attempt(conn)
    before = conn.total_changes
    assert diag.sample(conn, NOW)['receipt_states'] == diag.sample(conn, NOW)['receipt_states']
    assert conn.total_changes == before


def test_classifier_not_called_from_entry_or_exit_code():
    root = Path(__file__).resolve().parents[1]
    for path in (root / 'sniper').rglob('*.py'):
        if path.name == 'memecoin_j7_refusal_diag.py':
            continue
        text = path.read_text()
        if path.name == 'memecoin_runner.py':   # heartbeat whitelist constant only
            text = text.replace('from sniper.services.memecoin_j7_refusal_diag import HEARTBEAT_KEYS', '')
        assert 'memecoin_j7_refusal_diag' not in text, path
    loop = (root / 'scripts' / 'memecoin_runner_loop.py').read_text()
    uses = [n for n in ast.walk(ast.parse(loop)) if isinstance(n, ast.Attribute)
            and n.attr in ('sample', 'refresh') and 'refusal' in ast.unparse(n.value)]
    assert uses == []


def test_heartbeat_whitelists_snapshot_and_absent_is_compatible(tmp_path):
    import json
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / 'hb'
    write_heartbeat({}, True, path=str(path), j7_refusal_diag={'complete': True, 'secret': 'x'})
    assert json.loads(path.read_text())['j7_refusal_diag'] == {'complete': True}
    write_heartbeat({}, True, path=str(path))
    assert 'j7_refusal_diag' not in json.loads(path.read_text())
