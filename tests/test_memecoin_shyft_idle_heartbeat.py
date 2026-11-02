"""Idle publication cannot increase funded work cadence or revive stale proof."""
import copy
import json

import pytest

from scripts import memecoin_runner_loop as loop
from scripts.memecoin_runtime_verify import shyft_runtime_graph_errors
from sniper.services import memecoin_runner as runner
from test_memecoin_shyft_runtime_gate import graph


class Clock:
    def __init__(self, now=100.0):
        self.now = now
        self.waits = []

    def monotonic(self):
        return self.now

    def wait(self, duration):
        self.waits.append(duration)
        self.now += duration
        return False


def idle(clock, publish, *, provider='shyft', funded_only=True, wait=None, rest=5.0):
    return loop.wait_for_idle_heartbeat(rest, provider=provider,
        funded_only=funded_only, wait_fn=wait or clock.wait,
        heartbeat_fn=publish, clock=clock.monotonic)


def test_idle_publication_keeps_the_original_five_second_work_deadline():
    # Resetting the work deadline after every publication would delay the next work cycle.
    clock, publications = Clock(), []
    def publish():
        publications.append(clock.now)
        clock.now += .4  # Publication time consumes the idle budget.
    assert idle(clock, publish) is False
    assert publications == [101.0, 102.0, 103.0, 104.0]
    assert clock.now == 105.0
    assert all(0 < wait <= 1 for wait in clock.waits)


@pytest.mark.parametrize('provider,funded_only', [
    ('legacy', True), ('shyft', False), ('legacy', False),
])
def test_other_profiles_keep_the_existing_single_wait(provider, funded_only):
    clock = Clock()
    def forbidden():
        pytest.fail('heartbeat-only wake must stay scoped to Shyft funded-only')
    idle(clock, forbidden, provider=provider, funded_only=funded_only)
    assert clock.waits == [5.0]
    assert clock.now == 105.0


def test_j7_arrival_returns_before_any_idle_publication():
    # Replacing early-return with another slice would hold an already queued source event.
    clock, publications = Clock(), []
    def relay_wait(duration):
        assert duration == 1.0
        clock.now += .25
        return True
    assert idle(clock, lambda: publications.append(clock.now), wait=relay_wait) is True
    assert clock.now == 100.25
    assert publications == []


def test_publication_fault_does_not_reset_or_extend_the_work_deadline():
    clock, attempts = Clock(), []
    def broken():
        attempts.append(clock.now)
        raise OSError('local publication unavailable')
    assert idle(clock, broken) is False
    assert attempts == [101.0, 102.0, 103.0, 104.0]
    assert clock.now == 105.0


@pytest.mark.parametrize('rest', [0, .25, 1.0])
def test_no_extra_publication_at_the_next_work_deadline(rest):
    clock, publications = Clock(), []
    idle(clock, lambda: publications.append(clock.now), rest=rest)
    assert publications == []
    assert clock.now == 100.0 + rest


def fixture(tmp_path, monkeypatch):
    env, beat, rh = graph()
    path = tmp_path / 'heartbeat.json'
    beat.update(started_at='this-process', events=7, opened=2, bracket_ts=95.0,
                dip_lane={'pending': 3}, address_coverage={'armed': 33})
    path.write_text(json.dumps(beat))
    # Real receivers stamp their own clocks. Publishing must only serialize these values.
    lane = copy.deepcopy(beat['slot_lane'])
    feeds = lane.pop('signal_feeds')
    for feed in feeds.values():
        feed['head_slot_mono'] = 104.9
        feed['head_slot'] = 43
    preparation = lane.pop('preparation')
    lane['signal'] = dict(signal_feeds=feeds,
                          preparation=preparation | {'pending': {}})
    lane['execution_observer']['head_monotonic'] = 104.9
    lane['execution_observer']['head_slot'] = 43
    rh.update(at=105.0)
    rh['stream']['head_monotonic'] = 104.9
    rh['stream']['head_slot'] = 43
    monkeypatch.setattr(runner.time, 'time', lambda: 105.0)
    monkeypatch.setattr(runner.time, 'monotonic', lambda: 105.0)
    kwargs = dict(path=str(path), started_at='this-process', slot_lane_status=lane,
                  tpu_status=copy.deepcopy(beat['tpu']),
                  rpc_pool_status=copy.deepcopy(beat['solana_rpc_pool']),
                  fastlane_status=copy.deepcopy(beat['fastlane']))
    return env, beat, rh, path, kwargs


def test_real_serializer_refreshes_stream_proof_and_preserves_main_diagnostics(tmp_path, monkeypatch):
    # Only publishing fresh raw streams can bridge the old five-second heartbeat interval.
    env, original, rh, path, kwargs = fixture(tmp_path, monkeypatch)
    assert shyft_runtime_graph_errors(original, rh, env, now=105, monotonic=105, rh_now=105)
    assert runner.refresh_idle_heartbeat(**kwargs) is True
    beat = json.loads(path.read_text())
    assert beat['ts'] == 105.0
    for key in ('started_at', 'events', 'opened', 'auth_ok', 'bracket_ts',
                'dip_lane', 'address_coverage'):
        assert beat[key] == original[key]
    assert beat['slot_lane']['execution_observer']['head_monotonic'] == 104.9
    assert shyft_runtime_graph_errors(beat, rh, env,
        now=105.75, monotonic=105.75, rh_now=105.75) == []
    # No timer/thread publishes while this main thread is blocked in real work.
    assert shyft_runtime_graph_errors(beat, rh, env, now=108, monotonic=108, rh_now=108)


@pytest.mark.parametrize('mutation', [
    'disconnected', 'missing_ack', 'stale_head', 'missing_observer_clock',
    'source_disconnected', 'missing_raw_slot_lane', 'missing_raw_tpu',
    'missing_raw_rpc', 'missing_raw_fastlane',
])
def test_idle_publication_cannot_refresh_absent_stale_or_disconnected_evidence(tmp_path, monkeypatch, mutation):
    env, _, rh, path, kwargs = fixture(tmp_path, monkeypatch)
    raw = kwargs['slot_lane_status']['signal']['signal_feeds']['shyft:grpc.ams.shyft.to']
    if mutation == 'disconnected': raw['connected'] = False
    elif mutation == 'missing_ack': raw['acknowledged_owners'] = []
    elif mutation == 'stale_head': raw['head_slot_mono'] = 100.0
    elif mutation == 'missing_observer_clock': kwargs['slot_lane_status']['execution_observer']['head_monotonic'] = None
    elif mutation == 'source_disconnected': kwargs['fastlane_status']['source_sell_mirror']['account_socket']['connected'] = False
    elif mutation == 'missing_raw_slot_lane': kwargs['slot_lane_status'] = None
    elif mutation == 'missing_raw_tpu': kwargs['tpu_status'] = None
    elif mutation == 'missing_raw_rpc': kwargs['rpc_pool_status'] = None
    elif mutation == 'missing_raw_fastlane': kwargs['fastlane_status'] = None
    assert runner.refresh_idle_heartbeat(**kwargs) is True
    beat = json.loads(path.read_text())
    assert shyft_runtime_graph_errors(beat, rh, env, now=105, monotonic=105, rh_now=105)


@pytest.mark.parametrize('startup', [None, '', 'previous-process'])
def test_wrong_or_unknown_process_heartbeat_is_not_republished(tmp_path, monkeypatch, startup):
    _, beat, _, path, kwargs = fixture(tmp_path, monkeypatch)
    beat['started_at'] = startup
    path.write_text(json.dumps(beat))
    before = path.read_bytes()
    assert runner.refresh_idle_heartbeat(**kwargs) is False
    assert path.read_bytes() == before


def test_atomic_write_failure_retains_the_original_heartbeat(tmp_path, monkeypatch):
    _, _, _, path, kwargs = fixture(tmp_path, monkeypatch)
    before = path.read_bytes()
    from sniper.util import atomic
    def broken(*args, **kwargs):
        raise OSError('local disk unavailable')
    monkeypatch.setattr(atomic, 'atomic_write_json', broken)
    assert runner.refresh_idle_heartbeat(**kwargs) is False
    assert path.read_bytes() == before
