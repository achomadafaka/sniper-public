"""Offline guard checks: no worker, services or provider calls."""
import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "desk_pumpswap_canary_guard", ROOT / "scripts/research/desk_pumpswap_canary_guard.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


def beat(ts=100.0, p90=100.0, **change):
    b = {"ts": ts, "auth_ok": True,
         "slot_lane": {"mode": "live", "connected": True, "startup_gate": {"state": "ready", "errors": []}},
         "exit_sweep_timing": {"fastlane_poll": {"n": 20, "count": 20, "p50_ms": 90.0, "p90_ms": p90}}}
    for k, v in change.items():
        if k == "lane":
            b["slot_lane"].update(v)
        elif k == "poll":
            b["exit_sweep_timing"]["fastlane_poll"].update(v)
        else:
            b[k] = v
    return b


def test_a_fresh_ready_heartbeat_gives_its_fastlane_p90():
    assert G.sample(beat(), now=101.0) == (None, 100.0, 100.0)


@pytest.mark.parametrize("b, now, reason", [
    (beat(), 105.01, "heartbeat_stale_or_future"), (beat(), 99.0, "heartbeat_stale_or_future"),
    (beat(auth_ok=False), 101, "auth_not_ok"), (beat(auth_ok=None), 101, "auth_not_ok"),
    (beat(lane={"mode": "shadow"}), 101, "slot_lane_not_ready"),
    (beat(lane={"connected": False}), 101, "slot_lane_not_ready"),
    (beat(lane={"startup_gate": {"state": "blocked"}}), 101, "slot_lane_not_ready"),
    (beat(exit_sweep_timing={}), 101, "fastlane_timing_missing"),
    (beat(poll={"count": 0}), 101, "fastlane_timing_missing"),
    (beat(poll={"p90_ms": None}), 101, "fastlane_timing_missing"),
    (beat(poll={"p90_ms": float("nan")}), 101, "fastlane_timing_missing"),
    ({}, 101, "heartbeat_unreadable"), (beat(ts=True), 101, "heartbeat_unreadable"),
])
def test_each_unready_input_names_its_abort(b, now, reason):
    assert G.sample(b, now=now)[0] == reason


def test_the_heartbeat_age_bound_is_inclusive_at_five_seconds():
    assert G.sample(beat(), now=105.0)[0] is None


def test_the_verdict_warms_then_aborts_above_baseline_plus_thirty():
    assert G.verdict(100.0, [500.0] * 9)["reason"] == "warming"
    assert G.verdict(100.0, [130.0] * 10)["ok"] is True                  # exactly +30 allowed
    bad = G.verdict(100.0, [130.01] * 10)
    assert not bad["ok"] and bad["reason"] == "fastlane_p90_regression" and bad["limit_ms"] == 130.0


def test_q90_is_type_7():
    assert G.q90([float(i) for i in range(11)]) == 9.0 and G.q90([5.0]) == 5.0


def attached(tmp_path, *, script, emissions_at=40, baseline_s=30, duration=10, wait_s=1000):
    """`script(now, emissions_on)` returns the runner heartbeat; the universe heartbeat reports emissions from
    `emissions_at` (None = never). The clock advances 1 s per sleep."""
    hb, uhb = tmp_path / "hb.json", tmp_path / "universe.heartbeat"
    now, pages = [100.0], []

    def write():
        on = emissions_at is not None and now[0] >= 100.0 + emissions_at
        uhb.write_text(json.dumps({"ts": now[0], "pumpswap_emissions": on}))
        hb.write_text(json.dumps(script(now[0], on)))

    def sleep(_delay):
        now[0] += 1.0
        write()

    write()
    log = tmp_path / "guard.jsonl"
    status = G.run(heartbeat=hb, universe_heartbeat=uhb, log=log, duration=duration, baseline_s=baseline_s,
                   wait_s=wait_s, clock=lambda: now[0], monotonic=lambda: now[0], sleep=sleep,
                   alert=lambda kind, text: pages.append((kind, text)))
    return status, pages, [json.loads(line) for line in log.read_text().splitlines()]


def test_a_healthy_restart_passes_with_a_baseline_frozen_at_emissions_on(tmp_path):
    status, pages, rows = attached(tmp_path, script=lambda now, on: beat(ts=now, p90=100.0))
    frozen = [r for r in rows if r.get("reason") == "frozen_at_emissions_on"][0]
    assert status == 0 and rows[-1]["reason"] == "complete" and frozen["n"] >= 30 and frozen["baseline_p90_ms"] == 100.0
    assert pages and "PASSED" in pages[-1][1]


def test_a_regression_after_emissions_on_fails_and_pages_the_stop_action(tmp_path):
    status, pages, rows = attached(tmp_path, script=lambda now, on: beat(ts=now, p90=200.0 if on else 100.0))
    assert status == 2 and [r for r in rows if r.get("reason") == "fastlane_p90_regression"]
    assert "MEMECOIN_DESK_PUMPSWAP_EMISSIONS=0" in pages[-1][1]


def test_the_baseline_uses_only_the_last_window_before_the_restart(tmp_path):
    # early samples are slow (old), the last 30 s are fast: the baseline is the last window only
    status, _pages, rows = attached(tmp_path, emissions_at=80, baseline_s=30,
                                    script=lambda now, on: beat(ts=now, p90=500.0 if now < 145 else 100.0))
    frozen = [r for r in rows if r.get("reason") == "frozen_at_emissions_on"][0]
    assert frozen["baseline_p90_ms"] == 100.0 and status == 0


def test_no_restart_seen_gives_up_without_a_verdict(tmp_path):
    status, pages, rows = attached(tmp_path, emissions_at=None, wait_s=50, script=lambda now, on: beat(ts=now))
    assert status == 3 and rows[-1]["reason"] == "no_restart_seen" and pages == []


def test_too_few_baseline_samples_fails(tmp_path):
    status, _p, rows = attached(tmp_path, emissions_at=5, script=lambda now, on: beat(ts=now))
    assert status == 2 and [r for r in rows if r.get("reason") == "baseline_too_few_samples"]


@pytest.mark.parametrize("bad,reason", [({"auth_ok": False}, "auth_not_ok"), ({"lane": {"connected": False}}, "slot_lane_not_ready")])
def test_an_unready_runner_after_the_restart_fails(tmp_path, bad, reason):
    status, _p, rows = attached(tmp_path, script=lambda now, on: beat(ts=now, **(bad if on else {})))
    assert status == 2 and [r for r in rows if r.get("reason") == reason]


def test_emissions_turned_off_mid_canary_fails(tmp_path):
    def script(now, on):
        return beat(ts=now)
    # emissions on at 40, then the universe heartbeat says off again
    status, _p, rows = attached(tmp_path, script=script, emissions_at=40, duration=30)
    assert status == 0                                    # control: stays on, passes
    hb = tmp_path / "b"
    hb.mkdir()
    real = G.emissions_on
    calls = {"n": 0}

    def flip(path):
        calls["n"] += 1
        v = real(path)
        return False if v and calls["n"] > 45 else v
    G.emissions_on = flip
    try:
        status, _p, rows = attached(hb, script=script, emissions_at=40, duration=30)
    finally:
        G.emissions_on = real
    assert status == 2 and rows[-2]["reason"] == "emissions_turned_off"


def test_it_launches_nothing():
    src = (ROOT / "scripts/research/desk_pumpswap_canary_guard.py").read_text()
    assert "subprocess" not in src and "Popen" not in src



def test_an_unready_sample_while_waiting_only_resets_the_baseline(tmp_path):
    # a runner redeploy at t=+10..+12 s (auth not ok), emissions on at +60: no fail, baseline from the clean samples after
    status, pages, rows = attached(tmp_path, emissions_at=60, baseline_s=40,
                                   script=lambda now, on: beat(ts=now, **({"auth_ok": False} if 110 <= now < 113 else {})))
    assert status == 0 and not [p for p in pages if "FAILED" in p[1]]
    assert [r for r in rows if r.get("reason") == "buffer_reset" and r.get("cause") == "auth_not_ok"]


def test_a_redeploy_right_before_the_flip_leaves_too_few_samples_and_fails(tmp_path):
    status, _p, rows = attached(tmp_path, emissions_at=60, baseline_s=30,
                                script=lambda now, on: beat(ts=now, **({"auth_ok": False} if 140 <= now < 155 else {})))
    assert status == 2 and [r for r in rows if r.get("reason") == "baseline_too_few_samples"]


def test_an_unready_sample_after_the_flip_still_fails(tmp_path):
    status, pages, rows = attached(tmp_path, script=lambda now, on: beat(ts=now, **({"auth_ok": False} if on else {})))
    assert status == 2 and [r for r in rows if r.get("reason") == "auth_not_ok" and r.get("phase") == "canary"]
