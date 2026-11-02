#!/usr/bin/env python3
"""PumpSwap emissions canary, ATTACH mode only (Alpha 2026-10-22, Beta; reworked 2026-10-23 on Gamma's review: the old
mode launched a full second universe worker, two writers of the curve tape and heartbeat. This launches nothing).

The funded criterion is the runner's own heartbeat, exit_sweep_timing.fastlane_poll.p90_ms, the
fast lane's per-tick time (it runs every funded exit rail). One sample per NEW heartbeat (distinct
ts). Before launch the guard samples for `baseline_s` (default 30 min) and freezes the baseline as
the 90th percentile of those samples; it refuses to launch without enough fresh, ready samples.
During the canary it FAILS (logged + paged; the operator turns emissions off) when:
  - the 90th percentile of the canary's samples so far exceeds the frozen baseline + 30 ms;
  - auth_ok is not true;
  - the heartbeat is more than 5 s old (or in the future);
  - the slot lane is not ready (mode live, connected, startup_gate ready);
  - fastlane_poll timing is missing or malformed (missing input is never read as healthy).
It reads only the local heartbeat file and never calls a provider.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

MAX_HEARTBEAT_AGE_S = 5.0
MARGIN_MS = 30.0
MIN_BASELINE_SAMPLES = 30
MIN_CANARY_SAMPLES = 10


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def q90(values):
    """Linear-interpolated 90th percentile (numpy type 7)."""
    xs = sorted(values)
    h = (len(xs) - 1) * 0.9
    i = math.floor(h)
    return xs[-1] if i + 1 >= len(xs) else xs[i] + (h - i) * (xs[i + 1] - xs[i])


def sample(heartbeat, *, now):
    """(reason, p90_ms, ts): reason None = a fresh, authenticated, ready heartbeat with a timing."""
    if not isinstance(heartbeat, dict) or not number(heartbeat.get("ts")) or not number(now):
        return "heartbeat_unreadable", None, None
    ts = heartbeat["ts"]
    if not 0 <= now - ts <= MAX_HEARTBEAT_AGE_S:
        return "heartbeat_stale_or_future", None, ts
    if heartbeat.get("auth_ok") is not True:
        return "auth_not_ok", None, ts
    lane = heartbeat.get("slot_lane")
    gate = lane.get("startup_gate") if isinstance(lane, dict) else None
    if (not isinstance(lane, dict) or lane.get("mode") != "live" or lane.get("connected") is not True
            or not isinstance(gate, dict) or gate.get("state") != "ready"):
        return "slot_lane_not_ready", None, ts
    timing = heartbeat.get("exit_sweep_timing")
    poll = timing.get("fastlane_poll") if isinstance(timing, dict) else None
    if (not isinstance(poll, dict) or type(poll.get("count")) is not int or poll["count"] <= 0
            or not number(poll.get("p90_ms")) or poll["p90_ms"] < 0):
        return "fastlane_timing_missing", None, ts
    return None, float(poll["p90_ms"]), ts


def verdict(baseline_ms, samples):
    """The canary statistic against the frozen baseline. Fewer than MIN_CANARY_SAMPLES = warming."""
    if len(samples) < MIN_CANARY_SAMPLES:
        return {"ok": True, "reason": "warming", "n": len(samples), "baseline_p90_ms": baseline_ms}
    current = q90(samples)
    limit = baseline_ms + MARGIN_MS
    ok = current <= limit
    return {"ok": ok, "reason": "ok" if ok else "fastlane_p90_regression", "n": len(samples),
            "baseline_p90_ms": baseline_ms, "canary_p90_ms": current, "limit_ms": limit}


def emissions_on(universe_heartbeat):
    """True / False from the universe worker heartbeat's pumpswap_emissions, None when unreadable."""
    try:
        value = json.loads(Path(universe_heartbeat).read_text()).get("pumpswap_emissions")
    except (OSError, ValueError, AttributeError):
        return None
    return value if type(value) is bool else None


def page(text, alert=None):
    """Page the operator; a failed page is logged by the caller's record, never raised."""
    try:
        if alert is None:
            from sniper.services.memecoin_alerts import send_alert as alert
        alert("desk_pumpswap_canary", text)
        return True
    except Exception:          # noqa: BLE001 - the verdict is in the log either way
        return False


def run(*, heartbeat, universe_heartbeat, log, duration=1800, baseline_s=1800, wait_s=6 * 3600, clock=time.time,
        monotonic=time.monotonic, sleep=time.sleep, alert=None):
    """ATTACH mode (Gamma/Alpha 2026-10-23): launches nothing. Samples the live runner heartbeat while the universe
    worker runs with emissions OFF, keeping the last `baseline_s` of samples; when its heartbeat first reports
    pumpswap_emissions true (the restart that turns emissions on), freezes the baseline from those samples and watches
    the canary for `duration`. A failure is logged and paged; the stop action is the operator's: set
    MEMECOIN_DESK_PUMPSWAP_EMISSIONS=0 and restart the universe worker. Exit 0 pass, 2 fail, 3 no restart seen."""
    if not number(duration) or not 0 < duration <= 1800:
        raise ValueError("duration must be in (0,1800] seconds")
    if not number(baseline_s) or not 0 < baseline_s <= 3600:
        raise ValueError("baseline_s must be in (0,3600] seconds")
    with Path(log).open("a") as output:
        def record(result):
            output.write(json.dumps({"ts": clock(), **result}, allow_nan=False) + "\n")
            output.flush()

        def fail(result):
            record({"ok": False, **result})
            paged = page("desk PumpSwap emissions canary FAILED (%s): set MEMECOIN_DESK_PUMPSWAP_EMISSIONS=0 and restart "
                         "the universe worker. %s" % (result.get("reason"), json.dumps(result, sort_keys=True)), alert)
            record({"ok": False, "phase": "page", "paged": paged})
            return 2

        def read():
            try:
                beat = json.loads(Path(heartbeat).read_text())
            except (OSError, ValueError):
                return "heartbeat_unreadable", None, None
            return sample(beat, now=clock())

        try:
            # 1. Baseline while emissions are OFF: the last baseline_s of fresh, ready samples. An unready reading
            #    here refuses (the baseline is never built from a degraded runner) and starts nothing.
            seen, base = set(), []            # base: (monotonic, p90)
            give_up = monotonic() + wait_s
            while True:
                state = emissions_on(universe_heartbeat)
                if state is True:
                    break
                if monotonic() >= give_up:
                    record({"ok": False, "phase": "baseline", "reason": "no_restart_seen", "waited_s": wait_s})
                    return 3
                why, p90, ts = read()
                if why is not None:
                    # WAITING, NOT JUDGING (Gamma on e1f83dad4): the runner is redeployed several times a day, so an
                    # unready sample before emissions turn on only discards the baseline buffer; the freeze below
                    # still demands MIN_BASELINE_SAMPLES clean samples in the last baseline_s.
                    if base:
                        record({"ok": True, "phase": "baseline", "reason": "buffer_reset", "cause": why, "dropped": len(base)})
                    base = []
                    sleep(0.5)
                    continue
                if ts not in seen:
                    seen.add(ts)
                    base.append((monotonic(), p90))
                base = [(t, v) for t, v in base if monotonic() - t <= baseline_s]
                sleep(0.5)
            values = [v for _t, v in base]
            if len(values) < MIN_BASELINE_SAMPLES:
                return fail({"phase": "baseline", "reason": "baseline_too_few_samples", "n": len(values)})
            baseline_ms = q90(values)
            record({"ok": True, "phase": "baseline", "reason": "frozen_at_emissions_on", "n": len(values),
                    "baseline_p90_ms": baseline_ms})
            # 2. Canary: emissions ON, same rule; emissions turned off again mid-window ends it as a fail.
            deadline, canary = monotonic() + duration, []
            while True:
                if emissions_on(universe_heartbeat) is False:
                    return fail({"phase": "canary", "reason": "emissions_turned_off"})
                why, p90, ts = read()
                if why is not None:
                    return fail({"phase": "canary", "reason": why})
                if ts not in seen:
                    seen.add(ts)
                    canary.append(p90)
                result = verdict(baseline_ms, canary)
                if not result["ok"]:
                    return fail({"phase": "canary", **result})
                if monotonic() >= deadline:
                    record({"phase": "canary", **result, "reason": "complete", "duration_s": duration})
                    page("desk PumpSwap emissions canary PASSED: %s" % json.dumps(result, sort_keys=True), alert)
                    return 0
                sleep(0.5)
        except KeyboardInterrupt:
            record({"ok": False, "reason": "interrupted"})
            return 130


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--heartbeat", type=Path, required=True, help="data/memecoin_runner.heartbeat")
    parser.add_argument("--universe-heartbeat", type=Path, required=True, help="data/desk/universe.heartbeat")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=1800, help="canary seconds, maximum 1800")
    parser.add_argument("--baseline-s", type=float, default=1800, help="baseline seconds before emissions turn on")
    parser.add_argument("--wait-s", type=float, default=6 * 3600, help="give up when no restart is seen")
    args = vars(parser.parse_args(argv))
    try:
        return run(**args)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "reason": "invalid", "error": str(exc)}))
        return 2

if __name__ == "__main__":
    sys.exit(main())
