"""runner_health: the watchdog's wedge/auth-death detector. The old watchdog was pgrep-only,
so a runner that was alive-but-wedged (loop stuck) or permanently auth-dead (401) counted as
'up' forever. runner_health reads the heartbeat (age + auth_ok) so the watchdog can restart it."""
from sniper.services import memecoin_runner as run


def test_healthy_when_fresh_and_auth_ok():
    hb = {"ts": 1000.0, "auth_ok": True}
    r = run.runner_health(hb, now_epoch=1010.0, max_age_s=120)
    assert r["healthy"] is True and r["reason"] == "ok" and abs(r["age_s"] - 10.0) < 1e-6


def test_stale_heartbeat_is_unhealthy():
    hb = {"ts": 1000.0, "auth_ok": True}
    r = run.runner_health(hb, now_epoch=1000.0 + 300, max_age_s=120)   # 300s > 120s -> wedged
    assert r["healthy"] is False and r["reason"] == "stale"


def test_auth_dead_is_unhealthy_even_when_fresh():
    hb = {"ts": 1000.0, "auth_ok": False}
    r = run.runner_health(hb, now_epoch=1005.0, max_age_s=120)
    assert r["healthy"] is False and r["reason"] == "auth_dead"


def test_missing_heartbeat_is_unhealthy():
    r = run.runner_health(None, now_epoch=1000.0, max_age_s=120)
    assert r["healthy"] is False and r["reason"] == "no_heartbeat"


# --- the funded runner's INFO lines were discarded by the root log level (2026-09-02) -----------
#
# Nothing configures a logging level, so the root logger sits at WARNING and every logger.info on
# the funded path was formatted and thrown away. Seven of the eleven explain why a position did NOT
# exit -- tp riding, stop unconfirmed, rails blind, mirror holding, a rail rejected by the
# executable quote. The operator hand-closed 44 of 55 positions and Gamma found 4 of 7 stuck bags
# closed by hand; every one of those had a machine reason that never printed.
#
# LEVEL ONLY. Not one message string moved: _RETRY_TRANSIENT matches reason strings exactly, and
# eleven edits is eleven chances to "improve" something a matcher depends on.

import logging
import pathlib
import re

from sniper.services import memecoin_real_runner as RR


def _runner_source():
    return pathlib.Path(RR.__file__).read_text()


def test_no_logger_info_survives_on_the_funded_runner():
    """A funded line logged at INFO is a line that does not exist."""
    src = re.sub(r"#.*", "", _runner_source())          # ignore prose that names the call
    assert "logger.info(" not in src, (
        "an INFO call on the funded runner is discarded by the root WARNING level")


def test_the_seven_hold_explanations_are_all_present_and_at_warning():
    """These are the lines that answer 'why did nothing exit last night'."""
    src = _runner_source()
    for frag in ("real tp unconfirmed", "real %s unconfirmed (%.0fs/%.0fs elapsed)",
                 "real %s unconfirmed (%d/%d sweeps)", "real %s rejected by executable quote",
                 "real rails blind-but-idle", "real mirror: HOLD", "mirror_disabled by operator"):
        i = src.find(frag)
        assert i > 0, f"{frag!r} vanished"
        head = src.rfind("logger.", 0, i)
        assert src[head:head + 15].startswith("logger.warning"), f"{frag!r} is not at WARNING"


def test_blind_but_idle_logs_BOTH_edges_and_the_exit_carries_the_duration():
    """The only per-sweep line in the family. Entry-only logging would trade a volume problem for
    'cannot tell how long it lasted', which is the same defect wearing different clothes."""
    src = _runner_source()
    assert "_BLIND_IDLE_SINCE" in src
    assert "blind-but-idle CLEARED" in src, "the exit edge must exist"
    clear = src[src.find("def _blind_idle_clear"):][:700]
    assert "%.1fs" in clear and "time.monotonic()" in clear, "the exit edge must carry elapsed"


def test_the_exit_edge_fires_once_and_reports_elapsed(caplog, monkeypatch):
    RR._BLIND_IDLE_SINCE.clear()
    monkeypatch.setattr(RR.time, "monotonic", lambda: 1000.0)
    RR._BLIND_IDLE_SINCE[7] = 940.0                      # blind for 60s
    with caplog.at_level(logging.WARNING, logger=RR.__name__):
        RR._blind_idle_clear({"id": 7, "mint": "0xabc"})
    assert "60.0s" in caplog.text and "0xabc" in caplog.text
    assert 'blind window ended' in caplog.text and 'back within reach' not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=RR.__name__):
        RR._blind_idle_clear({"id": 7, "mint": "0xabc"})  # already cleared
    assert caplog.text == "", "the exit edge must not repeat"


def test_a_position_that_closes_while_blind_cannot_leak_forever():
    RR._BLIND_IDLE_SINCE.clear()
    RR._BLIND_IDLE_SINCE[1] = 0.0                        # ancient
    RR._BLIND_IDLE_SINCE[2] = 9_000.0                    # recent
    RR._blind_idle_prune(9_100.0)
    assert 1 not in RR._BLIND_IDLE_SINCE and 2 in RR._BLIND_IDLE_SINCE


def test_slot_lane_heartbeat_carries_effective_entry_policy_and_resume_readiness(tmp_path):
    import json
    path=tmp_path/'heartbeat.json'
    policy={'fast_lane_only':True,'max_copy_premium_pct':35.,'receipt_resume_ready':True,'j7_verified_backup':True}
    run.write_heartbeat({},path=path,auth_ok=True,slot_lane_status={**policy,'mode':'live','secret':'hidden'})
    actual=json.loads(path.read_text())['slot_lane']
    assert all(actual.get(k)==v for k,v in policy.items())
    assert 'secret' not in actual


def test_heartbeat_copies_account_and_migration_ack_without_keys_or_addresses(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    stream = dict(endpoint='grpc.ams.shyft.to', commitment='confirmed', connected=True,
        subscription_acknowledged=True, connection_generation=3, watch_generation=4,
        requested_account_count=2, requested_accounts=['private-address'],
        acknowledged_accounts=['private-address'], coverage='partial', token='hidden')
    feed = dict(migration_filter_requested=True, migration_filter_acknowledged=True,
        migration_filter_generation='migrations:c3:r2', migration_frames=0, migration_pools=0)
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'account_stream': stream, 'signal': {'signal_feeds': {'shyft:ams': feed}}})
    actual = json.loads(path.read_text())['slot_lane']
    assert actual['account_stream']['requested_account_count'] == 2
    assert actual['account_stream']['acknowledged_account_count'] == 1
    assert actual['account_stream']['subscription_acknowledged'] is True
    assert actual['signal_feeds']['shyft:ams']['migration_filter_generation'] == 'migrations:c3:r2'
    assert actual['signal_feeds']['shyft:ams']['migration_filter_acknowledged'] is True
    assert 'hidden' not in path.read_text() and 'private-address' not in path.read_text()


def test_slot_heartbeat_reports_effective_nozomi_scope_without_credentials(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    for ready, scope in ((True, 'all'), (False, 'listed')):
        run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
            'nozomi_ab_ready': ready, 'nozomi_wallet_scope': scope,
            'api_key': 'secret', 'endpoint': 'https://secret'})
        actual = json.loads(path.read_text())['slot_lane']
        assert actual['nozomi_ab_ready'] is ready
        assert actual['nozomi_wallet_scope'] == scope
        assert 'secret' not in path.read_text()


def test_damm_heartbeat_preserves_hydration_unknown_zero_and_failed_scan(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    fields = {'route_rows': None, 'route_scan_ok': False, 'route_loaded': 0,
              'route_queued': 3, 'route_read_failed': 2, 'route_scan_failed': 1,
              'route_dropped': 0, 'route_refused': 2, 'route_suppressed': 7}
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'damm_prewarm': {}, 'damm_v2_shadow': {},
        'damm_v2_pools': {**fields, 'secret': 'hidden', 'pool': 'untrusted-address'}})
    actual = json.loads(path.read_text())['slot_lane']
    assert {k: actual['damm_prewarm'].get(k) for k in fields} == fields
    assert {k: actual['damm_v2_shadow'].get('pools_' + k) for k in fields} == fields
    assert 'hidden' not in json.dumps(actual)
    assert 'untrusted-address' not in json.dumps(actual)


def test_slot_heartbeat_exposes_queue_and_metadata_counters_without_arbitrary_fields(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    timing = dict(handled=4, queue_expired=1, already_old=0, wallet_busy=0, unknown=2,
        sample='lane_handle_calls_including_retries', window=3, secret='hidden',
        queue_ms=dict(p50=0.0, p90=None, max=1.0, secret='hidden'))
    metadata = dict(capacity=64, offered=3, overflow=0, decoded=0, errors=1,
        roster_refused=0, last_error='ValueError', secret='hidden',
        metadata_refused={'token_instruction_unsupported:26': 2})
    failure = dict(kind='permanent', reason='wrong_program_owner', url='hidden')
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'queue_timing': timing, 'signal': {'metadata_worker': metadata},
        'damm_prewarm': {}, 'damm_v2_pools': {'route_last_failure': failure}})
    actual = json.loads(path.read_text())['slot_lane']
    assert actual['queue_timing']['queue_ms'] == dict(p50=0.0, p90=None, max=1.0)
    assert actual['queue_timing']['handled'] == 4
    assert actual['metadata_worker']['decoded'] == 0
    assert actual['metadata_worker']['last_error'] == 'ValueError'
    assert actual['metadata_worker']['metadata_refused'] == {'token_instruction_unsupported:26': 2}
    assert actual['damm_prewarm']['route_last_failure'] == dict(kind='permanent', reason='wrong_program_owner')
    assert 'hidden' not in json.dumps(actual)



def test_heartbeat_observer_and_warmer_allowlisted_scalars(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    for state, reason, generation in (
            ('disabled', None, None), ('unknown', 'observer_start_failed', 0),
            ('observing', None, 2), ('stopped', None, 3)):
        run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
            'execution_observer': dict(state=state, reason=reason, generation=generation,
                journal=object(), url='https://hidden', key='hidden', subscription=42),
            'exotic_warm': dict(http_calls=4, hits=0, misses=None, seeded=2, noted=0,
                refreshed=1, refresh_failed=1, rate_limited=2, expired=0, evicted=0,
                busy=0, last_error='TimeoutError', funded_429_skips=3, renewed=5, cap=3,
                payload=object(), url='https://hidden')})
        actual = json.loads(path.read_text())['slot_lane']
        assert actual['execution_observer'] == dict(state=state, reason=reason, generation=generation)
        assert actual['exotic_warm'] == dict(http_calls=4, hits=0, misses=None, seeded=2,
            noted=0, refreshed=1, refresh_failed=1, rate_limited=2, expired=0, evicted=0,
            busy=0, last_error='TimeoutError', funded_429_skips=3, renewed=5, cap=3)
        assert 'hidden' not in path.read_text()
    # Objects inside named fields must not crash JSON or reveal their repr.
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'execution_observer': dict(state={'url': 'hidden'}, reason=object(), generation=[]),
        'exotic_warm': dict(http_calls=object(), last_error={'payload': 'hidden'})})
    actual = json.loads(path.read_text())['slot_lane']
    assert actual['execution_observer'] == dict(state=None, reason=None, generation=None)
    assert actual['exotic_warm']['http_calls'] is None
    assert actual['exotic_warm']['last_error'] is None
    assert 'hidden' not in path.read_text()
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'execution_observer': dict(state='https://hidden', reason='key=hidden', generation=-1),
        'exotic_warm': dict(http_calls=True, last_error='https://hidden')})
    actual = json.loads(path.read_text())['slot_lane']
    assert actual['execution_observer'] == dict(state=None, reason=None, generation=None)
    assert actual['exotic_warm']['http_calls'] is None
    assert actual['exotic_warm']['last_error'] is None
    assert 'hidden' not in path.read_text()


def test_heartbeat_keeps_missing_observer_and_warmer_absent(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={})
    actual = json.loads(path.read_text())['slot_lane']
    assert 'execution_observer' not in actual and 'exotic_warm' not in actual


def test_heartbeat_preserves_staged_startup_errors_without_provider_payloads(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={
        'mode': 'staged', 'startup_gate': {
            'state': 'staged', 'errors': ('shyft_watcher_wrong_release',
                'custody_identity_unverified', 'watcher_check:ValueError',
                'https://private.invalid/?key=hidden'), 'token': 'hidden'}})
    actual = json.loads(path.read_text())['slot_lane']['startup_gate']
    assert actual == {'state': 'staged', 'errors': [
        'shyft_watcher_wrong_release', 'custody_identity_unverified', 'watcher_check:ValueError']}
    assert 'hidden' not in json.dumps(actual)


def test_source_partial_heartbeat_publishes_worker_health_without_error_payloads(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    status = dict(alive=False, healthy=False, last_pass_age_s=None, busy=False,
        busy_age_s=0.0, stalled=True, error='https://hidden/?key=secret',
        wake=dict(bound=False, datagrams=0, bind_failures=1, last_error='private-secret',
                  journal=object()), journal=object())
    run.write_heartbeat({}, path=path, auth_ok=True,
                        slot_lane_status={'source_partials': status})
    actual = json.loads(path.read_text())['slot_lane']['source_partials']
    assert actual == dict(alive=False, healthy=False, last_pass_age_s=None, busy=False,
        busy_age_s=0.0, stalled=True, error='worker_error',
        wake=dict(bound=False, datagrams=0, bind_failures=1, last_error='wake_error'))
    assert 'hidden' not in path.read_text() and 'secret' not in path.read_text()
    assert status['error'] == 'https://hidden/?key=secret'


def test_source_partial_heartbeat_preserves_missing_unknown_and_rejects_bad_types(tmp_path):
    import json
    path = tmp_path / 'heartbeat.json'
    for status in ({}, {'source_partials': None}, {'source_partials': 'private-secret'}):
        run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status=status)
        actual = json.loads(path.read_text())['slot_lane']
        if 'source_partials' not in status:
            assert 'source_partials' not in actual
        else:
            assert actual['source_partials'] is None
    for age in (-1, float('nan'), float('inf'), True, 'private-secret', object()):
        run.write_heartbeat({}, path=path, auth_ok=True, slot_lane_status={'source_partials':
            dict(alive=0, healthy='private-secret', busy=None, stalled=[],
                 last_pass_age_s=age, busy_age_s=age, error=object(),
                 wake=dict(bound=1, datagrams=True, bind_failures=-1, last_error=object()))})
        actual = json.loads(path.read_text())['slot_lane']['source_partials']
        assert actual == dict(alive=None, healthy=None, busy=None, stalled=None,
            last_pass_age_s=None, busy_age_s=None, error=None,
            wake=dict(bound=None, datagrams=None, bind_failures=None, last_error=None))
