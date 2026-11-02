"""The slot receipt accounts for the handler work before its durable attempt exists."""
import json

import pytest

from sniper.services import memecoin_addon_guard as addon
from sniper.services import memecoin_real_runner as runner
from sniper.services import memecoin_slot_lane as lane
from test_memecoin_slot_lane import _Clock, _buy, _live_lane, _snap, conn


def _measured_lane(conn, tmp_path, monkeypatch, *, cpu=True):
    ln, sent = _live_lane(conn, tmp_path, monkeypatch)
    clock = _Clock()
    clock.cpu = 10.0
    clock.perf_counter = clock.monotonic
    if cpu:
        clock.thread_time = lambda: clock.cpu
    ln.clock = clock
    ln.snapshot_fn = lambda: _snap(clock.t)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', False)

    def advance(obj, name, milliseconds):
        original = getattr(obj, name)

        def measured(*args, **kwargs):
            clock.t += milliseconds / 1000
            clock.cpu += milliseconds / 4000
            return original(*args, **kwargs)

        monkeypatch.setattr(obj, name, measured)

    advance(ln, '_entry_opportunity', 1)
    advance(lane, 'admission_refusal', 3)
    advance(addon.RING, 'note_buy', 4)
    advance(lane.m, 'unresolved_withdraws', 6)

    def now_iso():
        clock.t += .008
        clock.cpu += .002
        return '2026-09-26T20:30:00+00:00'

    def momentum(*args):
        clock.t += .007
        clock.cpu += .00175
        return None

    ln.now_iso_fn = now_iso
    monkeypatch.setattr(runner, '_start_momentum_read', momentum)
    return ln, clock, sent


def _receipt(conn):
    raw = conn.execute('SELECT trace_json FROM memecoin_entry_attempts '
                       'ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    return next(event for event in json.loads(raw)['substage_events']
                if event['name'] == 'slot_receipt')


def test_pretrace_phases_cover_the_gap_and_keep_queue_time_separate(conn, tmp_path, monkeypatch):
    ln, clock, sent = _measured_lane(conn, tmp_path, monkeypatch)
    result = ln.handle(_buy(recv_monotonic=clock.t - .02), enqueued_mono=clock.t - .01)
    assert result['status'] == 'filled' and sent == [1]
    receipt = _receipt(conn)
    assert receipt['queue_ms'] == pytest.approx(10)
    phases = receipt['pretrace_phase_ms']
    assert phases == pytest.approx(dict(source_proof=0, routing=0, entry_opportunity=1,
        admission=3, addon=4, withdrawal=6, metadata=8, momentum_setup=7))
    assert receipt['pretrace_total_ms'] == pytest.approx(29)
    assert receipt['pretrace_cpu_ms'] == pytest.approx(7.25)
    assert sum(phases.values()) == pytest.approx(receipt['pretrace_total_ms'])


@pytest.mark.parametrize('cpu_mode', ['missing', 'raises', 'nonfinite'])
def test_missing_thread_cpu_stays_unknown_without_changing_dispatch(
        conn, tmp_path, monkeypatch, cpu_mode):
    ln, clock, sent = _measured_lane(conn, tmp_path, monkeypatch, cpu=False)
    if cpu_mode == 'raises':
        def unreadable():
            raise RuntimeError('unavailable')
        clock.thread_time = unreadable
    elif cpu_mode == 'nonfinite':
        clock.thread_time = lambda: float('nan')
    result = ln.handle(_buy(recv_monotonic=clock.t))
    assert result['status'] == 'filled' and sent == [1]
    receipt = _receipt(conn)
    assert receipt['pretrace_cpu_ms'] is None
    assert receipt['pretrace_total_ms'] == pytest.approx(29)


def test_pretrace_marks_reset_for_each_handler_call(conn, tmp_path, monkeypatch):
    ln, clock, sent = _measured_lane(conn, tmp_path, monkeypatch)
    ln.handle(_buy(recv_monotonic=clock.t))
    first_receipt = _receipt(conn)
    pending_id = conn.execute('SELECT id FROM memecoin_pending_entries').fetchone()[0]
    lane.m.release_pending(conn, pending_id, 'fixture_complete')
    clock.t += 10
    ln.handle(_buy(recv_monotonic=clock.t))
    second_receipt = _receipt(conn)
    assert sent == [1, 1]
    assert second_receipt['pretrace_phase_ms'] == pytest.approx(first_receipt['pretrace_phase_ms'])
    assert second_receipt['pretrace_total_ms'] == pytest.approx(29)
    assert second_receipt['pretrace_cpu_ms'] == pytest.approx(7.25)


def test_direct_traced_call_keeps_missing_handler_anchors_unknown(conn, tmp_path, monkeypatch):
    ln, clock, _sent = _measured_lane(conn, tmp_path, monkeypatch)
    ln._queue_timing = {}
    monkeypatch.setattr(ln, '_reserve_and_execute', lambda *args: {'status': 'filled'})
    buy = _buy(recv_monotonic=clock.t)
    facts = dict(their_usd=300., mint=buy.mint, price_usd=4.5e-6, mcap_usd=4500.)
    result = ln._traced(buy, _snap(clock.t), facts, clock.perf_counter())
    assert result['status'] == 'filled'
    receipt = _receipt(conn)
    assert receipt['pretrace_total_ms'] is None
    assert receipt['pretrace_cpu_ms'] is None
    assert receipt.get('pretrace_phase_ms') is None
