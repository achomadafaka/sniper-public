"""Runtime policy proof survives normal and idle heartbeat serialization without raw callbacks."""
import json

import base58
import pytest

from sniper.services import memecoin_entry_opportunity as policy
from sniper.services import memecoin_runner as runner
from sniper.settings import memecoin as cfg

UID = 'pump:' + base58.b58encode(bytes([21]) * 32).decode()


@pytest.mark.parametrize('idle', [False, True])
def test_effective_movement_mode_survives_heartbeat_serialization(tmp_path, idle):
    path = tmp_path / 'heartbeat.json'
    status = dict(mode='live', copy_market_movement_only=True,
                  copy_market_unknown_policy='proceed', private_callback=lambda: None)
    assert runner.write_heartbeat({}, auth_ok=True, path=path, slot_lane_status=status,
                                  started_at='this-process')
    if idle:
        assert runner.refresh_idle_heartbeat(path=path, slot_lane_status=status,
            started_at='this-process', tpu_status={}, rpc_pool_status={}, fastlane_status={})
    actual = json.loads(path.read_text())['slot_lane']
    assert actual['copy_market_movement_only'] is True
    assert actual['copy_market_unknown_policy'] == 'proceed'
    assert 'private_callback' not in actual


def snapshot(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON',
        json.dumps({UID: dict(max_slot_delta=1, allow_later_buys=True)}))
    return dict(policy.snapshot(), head_callback_ready=True)


def test_effective_entry_policy_is_published_with_only_fixed_fields(tmp_path, monkeypatch):
    path = tmp_path / 'heartbeat.json'
    expected = snapshot(monkeypatch)
    raw = dict(expected, callback=lambda: None, url='https://hidden.invalid/?key=hidden', token=object())
    assert runner.write_heartbeat({}, auth_ok=True, path=path,
        slot_lane_status=dict(mode='live', entry_opportunity=raw))
    assert json.loads(path.read_text())['slot_lane']['entry_opportunity'] == expected
    assert 'hidden' not in path.read_text() and '"callback":' not in path.read_text()


def test_bad_policy_fields_stay_unknown_and_cannot_break_the_heartbeat(tmp_path):
    path = tmp_path / 'heartbeat.json'
    raw = dict(enabled='hidden', first_buy_only=object(), default_max_slot_delta=True,
        qualified_exception_ids=[UID, 'https://hidden.invalid/?key=hidden', object()],
        head_callback_ready=lambda: True, head_max_age_ms=-1,
        deadline_basis='https://hidden.invalid', landing_deadline_guaranteed={'key':'hidden'})
    assert runner.write_heartbeat({}, auth_ok=True, path=path,
        slot_lane_status=dict(entry_opportunity=raw))
    actual = json.loads(path.read_text())['slot_lane']['entry_opportunity']
    assert actual == dict(enabled=None, first_buy_only=None, default_max_slot_delta=None,
        qualified_exception_ids=[UID], head_callback_ready=None, head_max_age_ms=None,
        deadline_basis=None, landing_deadline_guaranteed=None)
    assert 'hidden' not in path.read_text()


@pytest.mark.parametrize('raw', [None, object(), 'hidden'])
def test_absent_or_unreadable_policy_does_not_invent_readiness(tmp_path, raw):
    path = tmp_path / 'heartbeat.json'
    assert runner.write_heartbeat({}, auth_ok=True, path=path,
        slot_lane_status=dict(entry_opportunity=raw))
    assert 'entry_opportunity' not in json.loads(path.read_text())['slot_lane']


def test_idle_heartbeat_reuses_current_policy_and_drops_absent_cached_policy(tmp_path, monkeypatch):
    path = tmp_path / 'heartbeat.json'
    original = snapshot(monkeypatch)
    assert runner.write_heartbeat(dict(events=7, opened=2), auth_ok=True, path=path,
        started_at='this-process', bracket_ts=95.,
        slot_lane_status=dict(entry_opportunity=original))
    current = dict(original, enabled=False, head_callback_ready=False, qualified_exception_ids=[])
    kwargs = dict(started_at='this-process', slot_lane_status=dict(entry_opportunity=current),
        tpu_status={}, rpc_pool_status={}, fastlane_status={}, path=path)
    assert runner.refresh_idle_heartbeat(**kwargs)
    beat = json.loads(path.read_text())
    assert beat['slot_lane']['entry_opportunity'] == current
    assert (beat['events'], beat['opened'], beat['bracket_ts']) == (7, 2, 95.)
    assert runner.refresh_idle_heartbeat(**dict(kwargs, slot_lane_status={}))
    assert 'entry_opportunity' not in json.loads(path.read_text())['slot_lane']
