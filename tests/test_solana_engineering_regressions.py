"""Offline behavioral regressions for the two-day Solana engineering findings.

F17 correction (2026-10-10): the original audit's getTransaction=10 claim was wrong.
Current https://www.helius.dev/docs/billing/credits prices getTransaction/getBlock at 1
and getProgramAccounts at 10. The historical repro is preserved in the audit directory.
"""
import asyncio
import pytest
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / 'tests'), str(REPO / 'scripts/research')]



def test_missing_source_clock_cannot_produce_known_horizon_pnl():
    from test_s_landing_evidence import _replay_fixture
    import s_landing_replay as replay
    tape, ledger, send = _replay_fixture()
    tape.execute("UPDATE pool_swaps SET block_time=NULL WHERE signature='source'")
    row = replay.replay_send(tape, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row.get('censored'), row


def test_minute_meter_retains_both_process_fragments():
    from sniper.services import memecoin_sol_cohort as co
    store = co.connect_db(':memory:')
    co.record_minute(store, minute=100, bytes_=100_000, messages=50, rpc_calls=10, subscribed=1)
    co.record_minute(store, minute=100, bytes_=20_000, messages=10, rpc_calls=2, subscribed=1)
    row = dict(store.execute('SELECT * FROM meter').fetchone())
    assert row['bytes'] == 120_000 and row['rpc_calls'] == 12, row


@pytest.mark.parametrize('method, expected', [('getTransaction', 1), ('getProgramAccounts', 10)])
def test_rpc_meter_uses_current_provider_tariffs(method, expected):
    from sniper.services import memecoin_sol_cohort as co
    src = co.CohortSolTicks('https://unused/?api-key=fake', ledger_factory=lambda: None,
                           rpc_call=lambda *_: None)
    src.rpc(method, ['fake-signature'])
    store = co.connect_db(':memory:')
    rpc = sum(v for k, v in src.counters.items() if k.startswith('rpc:'))
    co.record_minute(store, minute=100, bytes_=0, messages=0, rpc_calls=rpc, subscribed=0,
                     rpc_method_counts={method: 1})
    assert store.execute('SELECT credits FROM meter').fetchone()[0] == expected


def test_unobserved_cohort_interval_is_unknown():
    from sniper.services import memecoin_sol_cohort as co
    import sol_cohort_canary_report as report
    store, ledger = co.connect_db(':memory:'), sqlite3.connect(':memory:')
    ledger.execute('CREATE TABLE memecoin_entry_attempts(network_id TEXT,mint TEXT,'
                   'decision_started_at TEXT,delivery_lane TEXT)')
    got = report.report(store, ledger, '2026-10-10T12:00:00+00:00',
                        '2026-10-10T12:05:00+00:00')
    assert got['stream']['live_share'] is None, got['stream']


def test_helius_feed_metrics_have_connection_state(monkeypatch, tmp_path):
    from sniper.services import memecoin_slot_signal as s, memecoin_shyft_signal as g
    stop, status = threading.Event(), {}
    async def helius(_on_buy, owners, url, observed, *args):
        observed.update(connected=True, frames=10, head_slot=100)
        stop.set()
    async def shyft(url, token, owners, offer, observed, stop):
        observed.update(connected=True, transactions=10, head_slot=100)
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(lambda _: None, dict, 'wss://unused', status, stop,
                              None, None, None, None,
                              ('fake', ('https://beta-grpc.fra.shyft.to',))))
    assert status['signal_feeds']['helius'].get('connected') is True, status
    from sniper.services import memecoin_runner as runner
    path = tmp_path / 'heartbeat.json'
    runner.write_heartbeat({}, True, path=str(path), slot_lane_status={'signal': status})
    actual = json.loads(path.read_text())['slot_lane']['signal_feeds']['helius']
    assert actual['connected'] is True and actual['head_slot'] == 100




def test_grader_includes_confirmed_receipt_slot_sends(tmp_path, monkeypatch, capsys):
    import nozomi_ab_grade as grade
    path = tmp_path / 'grade.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE memecoin_entry_attempts(attempt_id TEXT, source_user_id TEXT,'
               'trace_json TEXT,network_id TEXT,delivery_lane TEXT,decision_started_at TEXT)')
    trace = {'substage_events': [
        {'name': 'slot_send_call', 'elapsed_ms': 2.0, 'tx_sig': 'fake-signed'},
        {'name': 'nozomi_ab', 'arm': 'B', 'rule': grade.ALL_B_RULE, 'tip_lamports': grade.NOZOMI_TIP},
        {'name': 'nozomi_ab_send', 'tip_lamports': grade.NOZOMI_TIP,
         'nozomi_state': 'accepted', 'nozomi_payload_sha_match': True, 'nozomi_sig_match': True}],
        'terminal_status': 'filled'}
    db.execute('INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?)',
               ('audit', 'uid', json.dumps(trace), 'solana', 'slot_lane_confirmed_receipt',
                '2026-10-10T12:00:00+00:00'))
    db.commit(); db.close()
    monkeypatch.setattr(grade, 'env_wallets', lambda _: {'uid': 'owner'})
    monkeypatch.setattr(grade, 'key_hits', lambda *_: None)
    monkeypatch.setattr(sys, 'argv', ['nozomi_ab_grade', '--db', str(path)])
    assert grade.main() == 0
    output = capsys.readouterr().out
    assert '1 signed sends' in output
    assert 'source lane slot_lane_confirmed_receipt arm B: n=1' in output


def test_key_leak_check_does_not_put_secret_in_child_argv(tmp_path, monkeypatch):
    import nozomi_ab_grade as grade
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('MEMECOIN_NOZOMI_API_KEY=audit-fake-sensitive-key\n')
    calls = []
    monkeypatch.setattr(grade.subprocess, 'run', lambda argv, **kw:
                        calls.append(argv) or SimpleNamespace(stdout=''))
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE memecoin_entry_attempts(decision_started_at TEXT, trace_json TEXT)')
    db.execute('CREATE TABLE memecoin_submission_attempts(created_at TEXT, reason_code TEXT, response_code TEXT)')
    grade.key_hits(db, '2026-10-10')
    assert all('audit-fake-sensitive-key' not in arg for argv in calls for arg in argv), calls


def test_minute_fragment_retry_is_idempotent_and_tariffs_are_weighted():
    from sniper.services import memecoin_sol_cohort as co
    store = co.connect_db(':memory:')
    args = dict(minute=100, bytes_=100_000, messages=1, rpc_calls=3, subscribed=2,
                fragment_id='run-one:1', started=6001., ended=6011.,
                rpc_method_counts={'getProgramAccounts': 1, 'getAccountInfo': 2})
    co.record_minute(store, **args)
    co.record_minute(store, **args)
    co.record_minute(store, **dict(args, fragment_id='run-two:1', bytes_=20_000,
                                 started=6011., ended=6016.))
    row = dict(store.execute('SELECT * FROM meter').fetchone())
    assert row['bytes'] == 120_000 and row['rpc_calls'] == 6
    assert row['credits'] == 26.4
    assert store.execute('SELECT SUM(elapsed_s) FROM meter_fragments').fetchone()[0] == 15


def test_unknown_rpc_tariff_preserves_raw_count_without_monthly_bill():
    from sniper.services import memecoin_sol_cohort as co
    import sol_cohort_canary_report as report
    store, ledger = co.connect_db(':memory:'), sqlite3.connect(':memory:')
    ledger.execute('CREATE TABLE memecoin_entry_attempts(network_id TEXT,mint TEXT,'
                   'decision_started_at TEXT,delivery_lane TEXT)')
    co.record_minute(store, minute=100, bytes_=0, messages=0, rpc_calls=1, subscribed=0,
                     fragment_id='run:1', started=6000., ended=6030.,
                     rpc_method_counts={'newPremiumMethod': 1})
    got = report.report(store, ledger, '1970-01-01T01:40:00+00:00', '1970-01-01T01:41:00+00:00')
    assert got['cost']['rpc_calls'] == 1
    assert got['cost']['rpc_credits_month'] is None
    assert got['cost']['unknown_rpc_calls'] == 1
    assert got['window']['metered_seconds'] == 30


def test_current_connected_epoch_is_visible_before_disconnect(monkeypatch):
    from sniper.services import memecoin_sol_cohort as co
    from sniper.services import memecoin_solana_ws
    import websockets
    from contextlib import asynccontextmanager
    stop = asyncio.Event()
    entered = asyncio.Event()
    @asynccontextmanager
    async def connect(*args, **kw):
        yield object()
    async def run_stream(vaults, on_row, connect, **kw):
        async with connect('wss://offline'):
            entered.set()
            await stop.wait()
    monkeypatch.setattr(websockets, 'connect', connect)
    monkeypatch.setattr(memecoin_solana_ws, 'run_vault_stream', run_stream)
    epochs = []
    now = [6000.]
    stream = co._metered_stream(co.ByteMeter(), epochs, lambda: now[0])
    async def check():
        task = asyncio.create_task(stream({}, lambda _: None))
        await entered.wait()
        assert epochs and epochs[0]['connected_at'] == 6000.
        assert epochs[0]['ended'] is None
        now[0] = 6020.
        stop.set()
        await task
        assert len(epochs) == 1 and epochs[0]['ended'] == 6020.
    asyncio.run(check())




def test_replay_can_enforce_horizon_exit_freshness():
    from test_s_landing_evidence import _replay_fixture
    import s_landing_replay as replay
    tape, ledger, send = _replay_fixture()
    row = replay.replay_send(tape, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger,
                            max_exit_age_s=5.)
    assert row['censored'] == 'stale_horizon_exit:30'


def test_active_epoch_reports_only_its_observed_tail():
    from sniper.services import memecoin_sol_cohort as co
    import sol_cohort_canary_report as report
    store, ledger = co.connect_db(':memory:'), sqlite3.connect(':memory:')
    ledger.execute('CREATE TABLE memecoin_entry_attempts(network_id TEXT,mint TEXT,'
                   'decision_started_at TEXT,delivery_lane TEXT)')
    co.record_epoch(store, started=6000., ended=None, vaults=2, error=None,
                    connected_at=6005., connection_observed=True, epoch_key='one', observed_until=6020.)
    got = report.report(store, ledger, '1970-01-01T01:40:00+00:00', '1970-01-01T01:41:00+00:00')['stream']
    assert got['live_share'] is None
    assert got['observed_live_seconds'] == 15.
    assert got['unknown_seconds'] == 40.
    assert got['observation_coverage'] == round(1/3, 4)
    co.record_epoch(store, started=6000., ended=6060., vaults=2, error=None,
                    connected_at=6005., connection_observed=True, epoch_key='one', observed_until=6060.)
    assert store.execute('SELECT COUNT(*) FROM epochs').fetchone()[0] == 1


def test_key_check_counts_chunk_boundary_secret_and_bound_sql_values(tmp_path, monkeypatch):
    import nozomi_ab_grade as grade
    monkeypatch.chdir(tmp_path)
    secret = 'fake-boundary-key'
    (tmp_path / '.env').write_text('MEMECOIN_NOZOMI_API_KEY='+secret+'\n')
    logs = tmp_path / 'logs'; logs.mkdir()
    (logs / 'one').write_bytes(b'x'*(1024*1024-3)+secret.encode())
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE memecoin_entry_attempts(decision_started_at TEXT, trace_json TEXT)')
    db.execute('CREATE TABLE memecoin_submission_attempts(created_at TEXT, reason_code TEXT, response_code TEXT)')
    db.execute('INSERT INTO memecoin_entry_attempts VALUES (?,?)', ('2026-10-10', secret))
    got = grade.key_hits(db, '2026-10-10')
    assert got == dict(log_files=1, unreadable_log_files=0, traces=1, submission_rows=0)


def test_feed_stage_counts_separate_native_from_routed_duplicates(monkeypatch):
    from sniper.services import memecoin_slot_signal as s, memecoin_shyft_signal as g
    stop, status, delivered = threading.Event(), {}, []
    # The production sink requires a frozen dataclass; the exact decode is covered by signal tests.
    from dataclasses import make_dataclass
    Event = make_dataclass('Event', [('signature', str), ('owner', str), ('recv_monotonic', float),
                                   ('signal_feed', str, ''), ('refusal', str, None),
                                   ('receipt_evidence', object, None)], frozen=True)
    native, routed = Event('same', 'owner', 1.), Event('route', 'owner', 2.)
    async def helius(on_buy, owners, url, observed, stop, connect, on_sell, on_routed, baseline):
        try:
            on_buy(native)
            on_routed(native)
            on_routed(routed)
        finally:
            stop.set()
    async def shyft(*a, **k):
        pass
    monkeypatch.setattr(s, '_run', helius)
    monkeypatch.setattr(g, 'run_feed', shyft)
    asyncio.run(s._run_signals(delivered.append, dict, 'wss://unused', status, stop,
        None, None, delivered.append, None, ('fake', ('https://beta-grpc.fra.shyft.to',))))
    feed = status['signal_feeds']['helius']
    assert feed['native_wins'] == 1 and feed['routed_wins'] == 1
    assert feed['native_duplicates'] == 0 and feed['routed_duplicates'] == 1
    assert len(delivered) == 2
    assert [buy.recv_monotonic for buy in delivered] == [1., 2.]


def test_heartbeat_exposes_unknown_head_and_stale_owner_ack(tmp_path):
    from sniper.services import memecoin_runner as runner
    path = tmp_path / 'heartbeat.json'
    runner.write_heartbeat({}, True, path=str(path), slot_lane_status={'signal': {'signal_feeds': {
        'helius': {'connected': True, 'owners': 1, 'requested_owners': ('new',),
                   'acknowledged_owners': ('old',), 'head_slot': 123, 'last_frame_mono': time.monotonic()}}}})
    got = json.loads(path.read_text())['slot_lane']['feed_readiness']['helius']
    assert got['state'] == 'blocked' and 'owner_ack_pending' in got['blockers']
    assert 'head_clock_unknown' in got['unknown']


def test_cache_churn_reasons_count_expiry_once_without_new_reads():
    from sniper.services.memecoin_slot_lane import MissLearner
    from test_slot_lane_miss_learn import Timers
    timers, reads, handed = Timers(), [], []
    pools = SimpleNamespace(learn=lambda pool: reads.append(pool) or False)
    learner = MissLearner(pools=pools, put=lambda _: None, handoff_fn=lambda *a: handed.append(a),
                          log=lambda _: None, timer=timers, max_waiting=1)
    buy = SimpleNamespace(signature='one', pool='pool', venue='pump_amm')
    assert learner.offer(buy)
    assert not learner.offer(buy)
    assert not learner.offer(SimpleNamespace(signature='two', pool='other', venue='pump_amm'))
    timers.fire(0)
    timers.fire(0)
    assert learner.stats['expired'] == 1 and learner.stats['duplicates'] == 1
    assert learner.stats['queue_full'] == 1 and learner.stats['waiting'] == 0
    assert reads == [] and len(handed) == 1






def test_partial_minute_report_uses_actual_elapsed_and_request_counts():
    from sniper.services import memecoin_sol_cohort as co
    import sol_cohort_canary_report as report
    store, ledger = co.connect_db(':memory:'), sqlite3.connect(':memory:')
    ledger.execute('CREATE TABLE memecoin_entry_attempts(network_id TEXT,mint TEXT,'
                   'decision_started_at TEXT,delivery_lane TEXT)')
    co.record_minute(store, minute=100, bytes_=100_000, messages=1, rpc_calls=1, subscribed=1,
                     fragment_id='short:1', started=6000., ended=6020.,
                     rpc_method_counts={'getTransaction': 1})
    got = report.report(store, ledger, '1970-01-01T01:40:00+00:00', '1970-01-01T01:40:30+00:00')
    assert got['cost']['rpc_calls'] == 1
    assert got['window']['metered_seconds'] == 20.
    assert got['cost']['rpc_credits_month'] == 129600
