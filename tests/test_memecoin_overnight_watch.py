"""Runner-health alarms for the overnight watch.

The watch was DB-only and therefore blind to the failure that prompted these tests: LI.FI came up
unauthenticated on 2026-09-01 and stayed that way for ~26h while producing zero refusal rows. A
disabled route removes an option; it does not reject a signal, so nothing lands in
memecoin_entry_attempts and a refusal-shaped monitor sees a quiet, healthy lane.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "memecoin_overnight_watch", ROOT / "scripts" / "memecoin_overnight_watch.py")
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

NOW = 1_788_344_000.0
STARTED_OLD = "2026-09-02T09:00:00+00:00"      # comfortably past the grace window


def _hb(tmp_path, payload, *, mtime=NOW - 5):
    path = tmp_path / "memecoin_runner.heartbeat"
    path.write_text(json.dumps(payload))
    import os
    os.utime(path, (mtime, mtime))
    return path


def _healthy(**lifi):
    base = {"mode": "authenticated_funded", "authenticated": True,
            "entry_admitted": True, "disable_reasons": []}
    base.update(lifi)
    return {"auth_ok": True, "started_at": STARTED_OLD, "lifi": base}


def test_healthy_runner_raises_no_alarm(tmp_path):
    assert watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW) == []


def test_lifi_disabled_after_grace_alarms(tmp_path):
    """The exact 2026-09-01 state: authenticated False, reasons ['unauthenticated']."""
    payload = _healthy(authenticated=False, entry_admitted=False,
                       disable_reasons=["unauthenticated"])
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert len(alarms) == 1
    assert "LI.FI ROUTE DISABLED" in alarms[0]
    assert "unauthenticated" in alarms[0]


def test_lifi_disabled_within_grace_is_silent(tmp_path):
    """The probe runs after the first heartbeat, so a fresh process is briefly unauthenticated."""
    payload = _healthy(authenticated=False, disable_reasons=["unauthenticated"])
    payload["started_at"] = "2026-09-02T10:12:20+00:00"        # ~13s before NOW
    assert watch.runner_health(_hb(tmp_path, payload), now_s=NOW) == []


def test_unknown_uptime_does_not_grace(tmp_path):
    """An unparseable started_at must fail loud, never silently suppress the alarm."""
    payload = _healthy(authenticated=False, disable_reasons=["unauthenticated"])
    payload["started_at"] = "not-a-timestamp"
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert len(alarms) == 1 and "runner up unknown" in alarms[0]


def test_disabled_mode_is_not_alarmed(tmp_path):
    """LI.FI deliberately switched off during a deploy is not a fault."""
    payload = _healthy(mode="disabled", authenticated=False,
                       disable_reasons=["configuration_invalid"])
    assert watch.runner_health(_hb(tmp_path, payload), now_s=NOW) == []


def test_stale_heartbeat_uses_mtime_not_the_ts_field(tmp_path):
    """A timestamp the runner writes cannot report that the runner stopped writing."""
    payload = _healthy()
    payload["ts"] = NOW                                        # says "fresh"; mtime says otherwise
    alarms = watch.runner_health(_hb(tmp_path, payload, mtime=NOW - 900), now_s=NOW)
    assert len(alarms) == 1 and "STALE" in alarms[0]


def test_stale_heartbeat_suppresses_content_alarms(tmp_path):
    """A stale file describes the past; do not also page about what it happens to contain."""
    payload = _healthy(authenticated=False, disable_reasons=["unauthenticated"])
    payload["auth_ok"] = False
    alarms = watch.runner_health(_hb(tmp_path, payload, mtime=NOW - 900), now_s=NOW)
    assert len(alarms) == 1 and "STALE" in alarms[0]


def test_auth_ok_false_alarms(tmp_path):
    payload = _healthy()
    payload["auth_ok"] = False
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert any("auth_ok=False" in a for a in alarms)


def test_missing_and_unreadable_heartbeat_fail_loud(tmp_path):
    missing = watch.runner_health(tmp_path / "nope.heartbeat", now_s=NOW)
    assert len(missing) == 1 and "ABSENT" in missing[0]

    bad = tmp_path / "memecoin_runner.heartbeat"
    bad.write_text("{not json")
    import os
    os.utime(bad, (NOW - 5, NOW - 5))
    broken = watch.runner_health(bad, now_s=NOW)
    assert len(broken) == 1 and "UNREADABLE" in broken[0]


@pytest.mark.parametrize("reasons", [[], None])
def test_empty_reasons_never_alarm(tmp_path, reasons):
    payload = _healthy(disable_reasons=reasons)
    assert watch.runner_health(_hb(tmp_path, payload), now_s=NOW) == []


# --- signal-feed silence (2026-09-02) ---------------------------------------------------------
# `auth_ok` caught the Privy 401 and paged seven times. It cannot catch a feed that authenticates
# and then delivers nothing — a publicnode websocket accepted a subscription and delivered zero
# payloads on 2026-08-07. A subscription reply is not delivery.

def _ws(**kw):
    base = {"status": "active", "last_payload_at": "2026-09-02T13:24:50+00:00",
            "reconnect_count": 3}
    base.update(kw)
    return base


def test_a_delivering_feed_is_silent(tmp_path):
    payload = _healthy(); payload["fomo_activity_ws"] = _ws()
    assert watch.runner_health(_hb(tmp_path, payload), now_s=NOW) == []


def test_a_connected_feed_that_never_delivered_alarms(tmp_path):
    """The exact 2026-09-02 shape: reconnecting forever, last_payload_at never set."""
    payload = _healthy()
    payload["fomo_activity_ws"] = _ws(status="connecting", last_payload_at=None,
                                      reconnect_count=117)
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert len(alarms) == 1
    assert "SIGNAL FEED SILENT" in alarms[0] and "117" in alarms[0]


def test_a_stalled_feed_alarms_even_while_it_looks_active(tmp_path):
    """status='active' is the handshake's opinion; the payload clock is the observation."""
    payload = _healthy()
    payload["fomo_activity_ws"] = _ws(last_payload_at="2026-09-02T08:00:00+00:00")
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert len(alarms) == 1 and "SIGNAL FEED STALE" in alarms[0]


def test_a_fresh_runner_with_no_payload_yet_is_graced(tmp_path):
    payload = _healthy()
    payload["started_at"] = "2026-09-02T10:12:20+00:00"        # ~13s before NOW
    payload["fomo_activity_ws"] = _ws(status="connecting", last_payload_at=None)
    assert watch.runner_health(_hb(tmp_path, payload), now_s=NOW) == []


def test_an_unparseable_payload_clock_fails_loud(tmp_path):
    payload = _healthy()
    payload["fomo_activity_ws"] = _ws(last_payload_at="not-a-timestamp")
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert len(alarms) == 1 and "unparseable" in alarms[0]


def test_no_ws_block_at_all_does_not_alarm(tmp_path):
    """Absent telemetry is not evidence of a dead feed; do not invent an alarm from nothing."""
    assert watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW) == []


# --- half-completed deploy (2026-09-02) --------------------------------------------------------
# The deploy stops the runner EARLY and switches `current` LATE. Killed in between, the watchdog
# relaunches from `current` -- still the OLD release -- so the runner comes back healthy on the old
# code and every other check here passes. A half-deploy is otherwise indistinguishable from a
# successful one. Measured: a 2-minute client timeout cut a funded deploy exactly there at 14:56Z.

def _intent(tmp_path, sha="3d6e8eaf368b", *, age_s=0.0):
    p = tmp_path / ".memecoin_deploy_intent"
    p.write_text(sha + "\n")
    import os
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


def test_no_intent_file_is_the_normal_state(tmp_path):
    assert watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW) == []


def test_a_deploy_in_flight_right_now_is_silent(tmp_path):
    """A real deploy holds the intent for a couple of minutes; that is not a fault."""
    _intent(tmp_path, age_s=60)
    assert watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW) == []


def test_an_intent_that_outlived_its_deploy_alarms(tmp_path):
    _intent(tmp_path, age_s=1800)
    alarms = watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW)
    assert len(alarms) == 1
    assert "DEPLOY INCOMPLETE" in alarms[0] and "3d6e8eaf368b" in alarms[0]


def test_the_alarm_fires_even_though_the_runner_is_perfectly_healthy(tmp_path):
    """The whole point: every other signal says fine, because the old release IS fine."""
    _intent(tmp_path, age_s=1800)
    payload = _healthy(); payload["fomo_activity_ws"] = _ws()
    alarms = watch.runner_health(_hb(tmp_path, payload), now_s=NOW)
    assert [a for a in alarms if "DEPLOY INCOMPLETE" in a]


def test_a_stale_heartbeat_does_not_suppress_the_deploy_alarm(tmp_path):
    """A killed deploy can ALSO leave the runner down. Both facts must reach the operator."""
    _intent(tmp_path, age_s=1800)
    alarms = watch.runner_health(_hb(tmp_path, _healthy(), mtime=NOW - 900), now_s=NOW)
    assert any("DEPLOY INCOMPLETE" in a for a in alarms)
    assert any("STALE" in a for a in alarms)


def test_an_unreadable_intent_fails_loud(tmp_path):
    p = tmp_path / ".memecoin_deploy_intent"
    p.mkdir()                                   # a directory: stat succeeds, read_text does not
    import os
    os.utime(p, (NOW - 1800, NOW - 1800))
    alarms = watch.runner_health(_hb(tmp_path, _healthy()), now_s=NOW)
    assert any("DEPLOY INCOMPLETE" in a and "unreadable" in a for a in alarms)
