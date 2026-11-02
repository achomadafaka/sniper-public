"""Actual main/loop acceptance: durable tickets, byte cursors and host-clock shutdown."""
import fcntl
import json
import signal
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from scripts import memecoin_desk_intake_assembler as IA
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from test_memecoin_desk_contract import policy
from test_memecoin_desk_intake_integrated import B, I, MintRpc, MINT, NOW, POOL, SLOT, PRINT_AT, fixture


class Clock:
    def __init__(self, now=NOW + .2):
        self.now = now
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


@pytest.fixture
def run(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    desk_dir, _ = fixture(root / 'data')
    tape = sqlite3.connect(desk_dir / 'pumpswap/tape.db')
    [(signature,)] = tape.execute('SELECT signature FROM swaps_ps WHERE slot=?', (SLOT,)).fetchall()
    tape.execute('DELETE FROM pools')
    tape.execute("UPDATE swaps_ps SET map_status='unmapped',map_known_at=NULL,mint=NULL,quote_mint=NULL")
    tape.commit()
    tape.close()
    (desk_dir / 'pumpswap/metadata.db').unlink()
    out = I.open_day(desk_dir / 'intake', 'pumpfun', PRINT_AT - 1)
    I.record_poll(out, 'pumpfun', 'active', PRINT_AT - 1,
                  [dict(network_id='solana', chain_raw='solana', mint=MINT, venue='pumpswap', raw={})], offset_s=25200)
    out.close()
    book = B.IntakeBook(intake_dir=desk_dir / 'intake', db_path=root / 'emitter.db')
    book.reload(PRINT_AT - .5)
    mapping = dict(pool=POOL, mint=MINT, quote_mint=K.WSOL_MINT, known_at=PRINT_AT, slot=SLOT, canonical=True,
                   map_source='intake_canonical_pda')
    event = dict(kind='buy', pool=POOL, post_base_reserve_atomic='100000000000000',
                 post_quote_reserve_atomic='40000000000', lp_fee_bps=20, protocol_fee_bps=5, creator_fee_bps=5,
                 cashback_fee_bps=0, virtual_quote_atomic='0', virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote')
    [(tid, body)] = book.on_print(event=event, mapping=mapping, metadata=None, slot=SLOT, signature=signature,
                                 ordinal=0, tx_index=None, now=PRINT_AT, received_at=PRINT_AT,
                                 sol_usd=100, sol_usd_at=PRINT_AT - 1)
    book.db.close()
    db = root / 'desk.db'
    conn = D.connect(db)
    hard = {**policy(), 'intake': {'version': 'intake-v1', 'mcap_safety': [5000, 50_000_000], 'liq_min_quote_sol': 40}}
    D.register_cohort(conn, cohort_id='intake', producer='U', prompt_version='desk-judge-v26',
                      policy=dict(clip_usd=25, venue='pumpswap', hard_filter=hard, evidence_wait_s=30),
                      created_at=iso(NOW - 3600))
    conn.close()
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_ASSEMBLER_ENABLED', '1')
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_COHORT_ID', 'intake')
    monkeypatch.setenv('MEMECOIN_DESK_INTAKE_CALLS_PER_DAY', '2000')
    source = desk_dir / 'candidate_emissions_intake'
    source.mkdir()
    path = source / (IA.A.utc_day(NOW) + '.jsonl')
    path.write_text(body + '\n')
    clock = Clock()
    args = ['--desk-db', str(db)]
    def invoke(*extra, rpc=None, **kw):
        return IA.main(args + list(extra), rpc=rpc or MintRpc(), env_root=root,
                       clock=clock, sleep=clock.sleep, **kw)
    return dict(root=root, desk=desk_dir, db=db, path=path, tid=tid, cand=json.loads(body),
                body=body, clock=clock, invoke=invoke)


def cursor(run):
    return json.loads((run['desk'] / 'assembler_intake/cursor.json').read_text())


def stages(run):
    conn = D.connect(run['db'])
    try:
        return conn.execute("SELECT verdict,reason FROM desk_tickets WHERE stage='hard_filter'").fetchall()
    finally:
        conn.close()


def test_actual_main_persists_filter_and_restart_replay_is_idempotent(run):
    rpc = MintRpc()
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 1
    assert [tuple(r) for r in stages(run)] == [('PASS', 'pass')]
    assert cursor(run)['offset'] == run['path'].stat().st_size
    with run['path'].open('a') as stream:
        stream.write(run['body'] + '\n')
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 1 and len(stages(run)) == 1
    assert cursor(run)['offset'] == run['path'].stat().st_size


def test_crash_after_ingest_resumes_open_ticket_and_charges_before_rpc(run):
    class Crash(MintRpc):
        def __call__(self, *args, **kwargs):
            raise RuntimeError('process died after dispatch')
    with pytest.raises(RuntimeError, match='process died'):
        run['invoke'](rpc=Crash())
    assert not stages(run) and cursor(run)['offset'] == 0
    rpc = MintRpc()
    assert run['invoke']('--calls-per-day', '1', rpc=rpc) == 0
    assert rpc.calls == 0  # pre-dispatch charge survives the crash and restart
    assert [r[0] for r in stages(run)] == ['SKIP']
    assert cursor(run)['offset'] == run['path'].stat().st_size


def test_crash_after_filter_before_cursor_does_not_read_mint_again(run, monkeypatch):
    original = IA.os.replace
    target = run['desk'] / 'assembler_intake/cursor.json'
    def crash(source, dest):
        if dest == target and json.loads(source.read_text())['offset']:
            raise RuntimeError('cursor crash')
        return original(source, dest)
    with monkeypatch.context() as patch:
        patch.setattr(IA.os, 'replace', crash)
        with pytest.raises(RuntimeError, match='cursor crash'):
            run['invoke']()
    assert len(stages(run)) == 1
    rpc = MintRpc()
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 0 and len(stages(run)) == 1


def test_partial_line_and_loop_stop_preserve_offset_then_complete_on_restart(run):
    run['path'].write_text(run['body'])
    stop = threading.Event()
    run['clock'].on_sleep = stop.set
    before = signal.getsignal(signal.SIGTERM)
    assert run['invoke']('--loop', stop=stop) == 0
    assert cursor(run)['offset'] == 0 and not stages(run)
    assert signal.getsignal(signal.SIGTERM) == before
    with run['path'].open('a') as stream:
        stream.write('\n')
    run['clock'].on_sleep = None
    assert run['invoke']() == 0
    assert len(stages(run)) == 1


def test_rollover_does_not_skip_intermediate_days_or_partial_old_tail(run):
    source = run['path'].parent
    run['path'].write_text('null\n')
    old_day = IA.A.utc_day(NOW - 2 * 86400)
    mid_day = IA.A.utc_day(NOW - 86400)
    (source / (old_day + '.jsonl')).write_text('null\n{')
    (source / (mid_day + '.jsonl')).write_text('null\n')
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    IA.A.save_cursor(out / 'cursor.json', old_day, 0)
    for _ in range(3):
        assert run['invoke']() == 0
    assert cursor(run)['day'] == IA.A.utc_day(NOW)
    assert cursor(run)['pending_tails'] == [{'day': old_day, 'offset': 5, 'tries': 0}]
    (source / (old_day + '.jsonl')).write_text('null\n{}\n')
    assert run['invoke']() == 0
    assert cursor(run)['pending_tails'][0]['offset'] == 8
    # A producer restart can append its stored claim after completing the damaged old line.
    with (source / (old_day + '.jsonl')).open('a') as stream:
        stream.write('null\n')
    assert run['invoke']() == 0
    assert cursor(run)['pending_tails'][0]['offset'] == 13
    logs = [json.loads(line) for p in out.iterdir() if p.suffix == '.jsonl' for line in p.read_text().splitlines()]
    refused = [r for r in logs if r.get('stage') == 'ingest' and r.get('verdict') == 'REFUSED']
    assert len(refused) == 5  # original old line, intermediate/today, completed old tail, later append


@pytest.mark.parametrize('value', ['{bad', '{"day":"bad","offset":0}', '{"day":"2027-01-01","offset":-1}'])
def test_corrupt_cursor_is_not_silently_reset(run, value):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    (out / 'cursor.json').write_text(value)
    with pytest.raises(ValueError, match='cursor'):
        run['invoke']()
    assert not stages(run) and (out / 'cursor.json').read_text() == value


def test_default_off_does_not_open_state_or_construct_rpc(tmp_path, monkeypatch):
    monkeypatch.delenv('MEMECOIN_DESK_INTAKE_ASSEMBLER_ENABLED', raising=False)
    root = tmp_path / 'missing'
    assert IA.main([], env_root=root) == 0
    assert not root.exists()



def test_sigterm_during_capture_wait_closes_resources_without_advancing(run):
    heartbeat = run['desk'] / 'universe.heartbeat'
    beat = json.loads(heartbeat.read_text())
    beat['pumpswap_capture'] = False
    heartbeat.write_text(json.dumps(beat))
    run['clock'].on_sleep = lambda: signal.raise_signal(signal.SIGTERM)
    previous = signal.getsignal(signal.SIGTERM)
    rpc = MintRpc()
    assert run['invoke']('--loop', rpc=rpc) == 0
    assert rpc.calls == 0 and not stages(run) and cursor(run)['offset'] == 0
    assert signal.getsignal(signal.SIGTERM) == previous
    with (run['desk'] / 'assembler_intake/assembler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_second_owner_does_not_touch_cursor_or_open_a_ticket(run):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    with (out / 'assembler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rpc = MintRpc()
        assert run['invoke'](rpc=rpc) == 0
        assert rpc.calls == 0 and not (out / 'cursor.json').exists()
    assert not stages(run)


def test_environment_file_defaults_are_loaded_before_argument_defaults(run, monkeypatch):
    monkeypatch.delenv('MEMECOIN_DESK_INTAKE_COHORT_ID')
    (run['root'] / '.env').write_text('MEMECOIN_DESK_INTAKE_COHORT_ID=intake\n')
    assert run['invoke']() == 0
    assert len(stages(run)) == 1


@pytest.mark.parametrize('reason', ['timeout', 'rate_limited', 'http_403'])
def test_provider_failure_is_a_durable_skip_without_a_retry(run, reason):
    class Failed(MintRpc):
        def __call__(self, method, params, deadline=None):
            self.calls += 1
            return None, reason
    rpc = Failed()
    assert run['invoke'](rpc=rpc) == 0
    assert [r[0] for r in stages(run)] == ['SKIP']
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 1 and len(stages(run)) == 1


def test_loop_finishes_inflight_filter_then_stops_with_durable_cursor(run):
    stop = threading.Event()
    class Final(MintRpc):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            stop.set()
            return result
    rpc = Final()
    assert run['invoke']('--loop', rpc=rpc, stop=stop) == 0
    assert rpc.calls == 1 and [r[0] for r in stages(run)] == ['PASS']
    assert cursor(run)['offset'] == run['path'].stat().st_size


def test_repeated_crash_guard_terminalizes_without_new_rpc(run):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    IA.A.save_cursor(out / 'cursor.json', IA.A.utc_day(NOW), 0, 3)
    rpc = MintRpc()
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 0 and [r[0] for r in stages(run)] == ['SKIP']
    log = out / (IA.A.utc_day(NOW) + '.jsonl')
    assert any(json.loads(line).get('reason') == 'crash_loop' for line in log.read_text().splitlines())
    assert cursor(run)['offset'] == run['path'].stat().st_size


def test_bounded_pass_does_not_consume_a_second_line(run):
    run['path'].write_text('null\nnull\n')
    assert run['invoke']() == 0
    assert cursor(run)['offset'] == 5


def test_truncated_source_is_not_replayed_from_zero(run):
    assert run['invoke']() == 0
    saved = cursor(run)
    run['path'].write_text('')
    with pytest.raises(ValueError, match='cursor source'):
        run['invoke']()
    assert cursor(run) == saved


@pytest.mark.parametrize('tails', [None, [{'day': '../bad', 'offset': 0, 'tries': 0}],
                                  [{'day': '2027-01-14', 'offset': True, 'tries': 0}],
                                  [{'day': '2027-01-14', 'offset': -1, 'tries': 0}]])
def test_malformed_deferred_journal_does_not_reset_any_offsets(run, tails):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    saved = dict(day=IA.A.utc_day(NOW), offset=0, tries=0, pending_tails=tails)
    (out / 'cursor.json').write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='cursor'):
        run['invoke']()
    assert cursor(run) == saved and not stages(run)


def test_missing_deferred_source_is_a_named_gap_without_progress(run):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    saved = dict(day=IA.A.utc_day(NOW), offset=0, tries=0,
                 pending_tails=[dict(day=IA.A.utc_day(NOW-86400), offset=0, tries=0)])
    (out / 'cursor.json').write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='cursor source'):
        run['invoke']()
    assert cursor(run) == saved and not stages(run)
    log = out / (IA.A.utc_day(NOW) + '.jsonl')
    assert json.loads(log.read_text().splitlines()[-1])['reason'] == 'source_missing_or_truncated'


def test_utf8_deferred_tail_then_replayed_claim_uses_bytes_and_one_filter(run):
    assert run['invoke']() == 0
    out = run['desk'] / 'assembler_intake'
    old = IA.A.utc_day(NOW-86400)
    oldfile = run['path'].parent / (old + '.jsonl')
    oldfile.write_text('"é')
    state = cursor(run)
    state['pending_tails'] = [dict(day=old, offset=0, tries=0)]
    (out / 'cursor.json').write_text(json.dumps(state))
    assert run['invoke']() == 0
    with oldfile.open('a') as stream:
        stream.write('"\n' + run['body'] + '\n')
    assert run['invoke']() == 0
    assert cursor(run)['pending_tails'][0]['offset'] == 5  # quote, two UTF8 bytes, quote, newline
    rpc = MintRpc()
    assert run['invoke'](rpc=rpc) == 0
    assert rpc.calls == 0 and len(stages(run)) == 1
    assert cursor(run)['pending_tails'][0]['offset'] == oldfile.stat().st_size


def test_rollover_ignores_noncanonical_day_names(run):
    out = run['desk'] / 'assembler_intake'
    out.mkdir()
    old = IA.A.utc_day(NOW-86400)
    (run['path'].parent / (old + '.jsonl')).write_text('')
    (run['path'].parent / (old + '-junk.jsonl')).write_text('null\n')
    run['path'].write_text('null\n')
    IA.A.save_cursor(out / 'cursor.json', old, 0)
    assert run['invoke']() == 0
    assert cursor(run)['day'] == IA.A.utc_day(NOW)


def test_stop_after_durable_charge_does_not_dispatch_or_advance(run, monkeypatch):
    stop = threading.Event()
    original = IA.os.fsync
    log = run['desk'] / 'assembler_intake' / (IA.A.utc_day(NOW) + '.jsonl')
    def stopped_after_charge(fd):
        original(fd)
        if log.exists() and json.loads(log.read_text().splitlines()[-1]).get('stage') == 'mint_read':
            stop.set()
    monkeypatch.setattr(IA.os, 'fsync', stopped_after_charge)
    rpc = MintRpc()
    assert run['invoke']('--loop', rpc=rpc, stop=stop) == 0
    assert rpc.calls == 0 and not stages(run) and cursor(run)['offset'] == 0


def test_no_terminal_filter_after_backwards_clock_does_not_advance_cursor(run):
    class Backwards(MintRpc):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            run['clock'].now = PRINT_AT - 1
            return result
    with pytest.raises(ValueError, match='hard_filter'):
        run['invoke'](rpc=Backwards())
    assert not stages(run) and cursor(run)['offset'] == 0


def test_actual_main_spends_no_mint_read_on_a_pool_that_fails_the_free_check(run):
    """Alpha 2026-10-28: the free pool check runs before the paid read; the failure is persisted with its own
    reason, no mint_read row is charged and the RPC is never called."""
    tape = sqlite3.connect(run['desk'] / 'pumpswap/tape.db')
    tape.execute("UPDATE swaps_ps SET payload_json='{}'")                  # the tip's virtual-quote proof is gone
    tape.commit()
    tape.close()
    rpc = MintRpc()
    assert run['invoke'](rpc=rpc) == 0
    [(verdict, reason)] = stages(run)
    assert verdict == 'SKIP' and reason.startswith('unknown:market_data:') and 'mint_clock' not in reason
    rows = [json.loads(l) for l in (run['desk'] / 'assembler_intake' / (IA.A.utc_day(NOW) + '.jsonl')).read_text().splitlines()]
    assert rpc.calls == 0 and not [r for r in rows if r.get('stage') == 'mint_read']


def test_charge_lands_in_the_day_the_cap_was_checked_across_midnight(run, monkeypatch):
    # The cap was compared against day D's count; the charge must be written to day D's journal even when
    # the clock crosses 00:00Z between that check and the paid-read charge (it used to resample now()).
    import sniper.services.memecoin_desk_ps_evidence as E
    midnight = (int(NOW) // 86400 + 1) * 86400
    day = IA.A.utc_day(NOW)
    real = E.pool_refusal
    def crossing(*args, **kwargs):
        run['clock'].now = midnight + 1
        return real(*args, **kwargs)
    monkeypatch.setattr(E, 'pool_refusal', crossing)
    assert run['invoke'](rpc=MintRpc()) == 0
    out = run['desk'] / 'assembler_intake'
    charges = {f.name: [json.loads(l) for l in f.read_text().splitlines() if '"mint_read"' in l and '"stage": "mint_read"' in l]
               for f in out.glob('20*.jsonl')}
    assert [n for n, rows in charges.items() if rows] == [day + '.jsonl']
    assert all(r['ts'] < midnight for r in charges[day + '.jsonl'])
