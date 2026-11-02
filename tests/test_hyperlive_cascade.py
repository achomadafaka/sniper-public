import importlib.util
import os
import random

from sniper.hyperlive import cascade as C
from sniper.hyperlive import persistence as P


# ---------------------------------------------------------------------------
# Task 2 — detect_cascades
# ---------------------------------------------------------------------------

def _flat_series(asset, n, *, start_ts=0, bucket_ms=10_000, mid=100.0, oi=1e9, n_tr=10, notl=1e5):
    """A calm baseline: tiny ±2bps oscillation, steady OI, steady volume (so σ>0)."""
    out = []
    for i in range(n):
        m = mid * (1 + (0.0002 if i % 2 else -0.0002))
        out.append({"asset": asset, "ts_ms": start_ts + i * bucket_ms,
                    "mid": m, "oi": oi, "n_trades": n_tr, "notional": notl})
    return out


def _step_cascade(asset, n, at, move_frac, *, oi_drop=0.02, spike=200, base_oi=1e9):
    """Calm series with a near-instant step move at index `at` that HOLDS afterward (a real
    cascade is captured within ~1 bucket, not a slow 60s ramp), plus an OI drop + trade spike
    around the event. move_frac<0 = down-cascade."""
    s = _flat_series(asset, n)
    drop_mid = s[at - 1]["mid"] * (1 + move_frac)
    for i in range(at, n):
        s[i]["mid"] = drop_mid * (1 + (0.0002 if i % 2 else -0.0002))   # hold at new level
        s[i]["oi"] = base_oi * (1 - oi_drop)                            # OI stays depressed
    for i in range(at, min(at + 3, n)):
        s[i]["n_trades"] = spike                                        # trade-count spike
        s[i]["notional"] = 6e5
    return s


def test_magnitude_bucket_and_asset_class():
    assert C.magnitude_bucket(45) == "30-60"
    assert C.magnitude_bucket(90) == "60-120"
    assert C.magnitude_bucket(200) == "120-250"
    assert C.magnitude_bucket(900) == "250+"
    assert C.asset_class("BTC") == "major" and C.asset_class("ETH") == "major"
    assert C.asset_class("WIF") == "alt"


def test_planted_cascade_fires_one_event_correct_direction():
    s = _step_cascade("BTC", 250, at=200, move_frac=-0.015)   # -150bps step-and-hold, OI -2%, spike
    evs = C.detect_cascades(s)
    assert len(evs) == 1                                       # cooldown collapses the held window to one event
    e = evs[0]
    assert e["asset"] == "BTC" and e["direction"] == -1
    assert e["ts_ms"] == s[200]["ts_ms"]                      # fires the first bucket the window spans the step
    assert e["ret_w"] < -100 and e["oi_delta_w"] < 0
    assert e["detector_version"] == C.DETECTOR_VERSION
    assert e["event_id"] == f"BTC:{s[200]['ts_ms']}:{C.DETECTOR_VERSION}"


def test_slow_drift_fires_none():
    s = _flat_series("ETH", 300)
    for i in range(300):                               # +200bps over the WHOLE series = slow, sub-k drift
        s[i]["mid"] = 100.0 * (1 + 0.00007 * i)
    assert C.detect_cascades(s) == []


def test_min_move_floor_suppresses_sub_floor_move():
    # σ>0 (~4bps), a -20bps step clears k·σ (12bps) but is under the 30bps absolute floor -> no event.
    s = _step_cascade("SOL", 250, at=200, move_frac=-0.002)   # -20bps
    assert C.detect_cascades(s) == []


def test_cooldown_suppresses_second_event_in_window():
    # Two down-steps 100s apart (< 300s cooldown). The first fires; the second is suppressed.
    s = _step_cascade("BTC", 300, at=200, move_frac=-0.015)
    drop2 = s[209]["mid"] * (1 - 0.015)
    for i in range(210, 300):
        s[i]["mid"] = drop2 * (1 + (0.0002 if i % 2 else -0.0002))
        s[i]["oi"] = 1e9 * (1 - 0.04)
    for i in range(210, 213):
        s[i]["n_trades"] = 200
    evs = C.detect_cascades(s)
    assert len(evs) == 1 and evs[0]["ts_ms"] == s[200]["ts_ms"]


def test_oi_missing_skips_event_fail_closed():
    s = _step_cascade("BTC", 250, at=200, move_frac=-0.015)
    for i in range(250):
        s[i]["oi"] = None                              # OI unavailable -> fail-closed
    assert C.detect_cascades(s) == []


# ---------------------------------------------------------------------------
# Task 3 — aligned_moves
# ---------------------------------------------------------------------------

def _obs(asset, ts, mid):
    return {"asset": asset, "ts_ms": ts, "mid": mid, "oi": 1e9, "n_trades": 1, "notional": 1.0}


def test_aligned_moves_continuation_vs_reversion_signs():
    # down-cascade event at t=0 (direction -1); price continues DOWN by 50bps over 60s.
    series = {"BTC": [_obs("BTC", 0, 100.0), _obs("BTC", 60_000, 99.5)]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    cont = C.aligned_moves(ev, series, 60_000, "continuation")
    rev = C.aligned_moves(ev, series, 60_000, "reversion")
    # fwd_ret = -50bps; continuation aligned = dir * fwd = (-1)*(-50) = +50
    assert abs(cont[0]["bps"] - 50.0) < 1e-6
    assert abs(rev[0]["bps"] + 50.0) < 1e-6
    assert cont[0]["asset"] == "BTC" and cont[0]["ts_ms"] == 0


def test_aligned_moves_drops_event_without_forward_obs():
    series = {"BTC": [_obs("BTC", 0, 100.0)]}           # no obs near t+horizon
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    assert C.aligned_moves(ev, series, 60_000, "continuation") == []


# ---------------------------------------------------------------------------
# Task 4 — negative-control battery
# ---------------------------------------------------------------------------

def _ramp(asset, n, slope_bps_per_bucket, start_ts=0, bucket_ms=10_000, mid0=100.0):
    out = []
    for i in range(n):
        mid = mid0 * (1 + slope_bps_per_bucket / 10000.0 * i)
        out.append({"asset": asset, "ts_ms": start_ts + i * bucket_ms,
                    "mid": mid, "oi": 1e9, "n_trades": 1, "notional": 1.0})
    return out


def test_control_flip_negates_aligned():
    series = {"BTC": [_obs("BTC", 0, 100.0), _obs("BTC", 60_000, 99.5)]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    real = C.aligned_moves(ev, series, 60_000, "continuation")[0]["bps"]
    flip = C.control_flip(ev, series, 60_000, "continuation")[0]["bps"]
    assert abs(flip + real) < 1e-6


def test_control_shuffle_uses_other_asset():
    # BTC continues down (real edge); ETH is flat. Shuffle must read ETH's ~0 forward move.
    series = {
        "BTC": [_obs("BTC", 0, 100.0), _obs("BTC", 60_000, 99.0)],
        "ETH": [_obs("ETH", 0, 50.0), _obs("ETH", 60_000, 50.0)],
    }
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    out = C.control_shuffle(ev, series, 60_000, "continuation", rng=random.Random(1))
    assert len(out) == 1 and abs(out[0]["bps"]) < 1e-6      # measured ETH (flat), not BTC


def test_control_shift_reads_a_shifted_window():
    # Real move only in [0,60s]; the shifted window [+30min, +30min+60s] is flat -> ~0.
    series = {"BTC": [_obs("BTC", 0, 100.0), _obs("BTC", 60_000, 99.0),
                      _obs("BTC", 1_800_000, 99.0), _obs("BTC", 1_860_000, 99.0)]}
    ev = [{"asset": "BTC", "ts_ms": 0, "direction": -1}]
    out = C.control_shift(ev, series, 60_000, "continuation", shift_ms=1_800_000)
    assert len(out) == 1 and abs(out[0]["bps"]) < 1e-6
    assert out[0]["ts_ms"] == 0                              # original event time restored


def test_control_matched_picks_nonevent_windows():
    s = _ramp("BTC", 400, 0.0)                         # perfectly flat -> any window ~0
    series = {"BTC": s}
    ev = [{"asset": "BTC", "ts_ms": s[200]["ts_ms"], "direction": -1}]
    out = C.control_matched(ev, series, 60_000, "continuation", rng=random.Random(7))
    assert out and all(abs(r["bps"]) < 1.0 for r in out)


# ---------------------------------------------------------------------------
# Task 5 — exploitability statistics
# ---------------------------------------------------------------------------

def test_clustered_t_positive_for_consistent_edge():
    rows = []
    for a in ("BTC", "ETH", "SOL", "WIF"):
        for h in (0, 1):
            rows.append({"asset": a, "ts_ms": h * 3_600_000, "bps": 30.0})
    assert C.clustered_t(rows) > 2.0


def test_clustered_t_zero_for_noise_around_zero():
    rows = [{"asset": "BTC", "ts_ms": h * 3_600_000, "bps": (10.0 if h % 2 else -10.0)} for h in range(8)]
    assert abs(C.clustered_t(rows)) < 2.0


def test_bh_fdr_keeps_true_screens_nulls():
    pairs = [("real", 0.001)] + [(f"null{i}", 0.6) for i in range(20)]
    survivors = C.bh_fdr(pairs, q=0.10)
    assert "real" in survivors and not any(k.startswith("null") for k in survivors)


def test_thirds_positive_counts_chronological_thirds():
    rows = ([{"asset": "BTC", "ts_ms": i, "bps": 5.0} for i in range(10)] +
            [{"asset": "BTC", "ts_ms": 100 + i, "bps": 5.0} for i in range(10)] +
            [{"asset": "BTC", "ts_ms": 200 + i, "bps": -1.0} for i in range(10)])
    assert C.thirds_positive(rows) == 2


# ---------------------------------------------------------------------------
# Task 6 — canonical gate + planted-edge falsification of the controls
# ---------------------------------------------------------------------------

def test_cell_passes_gate_all_conditions():
    cell = {"key": "k1", "n": 50, "mean_bps": 20.0, "t": 3.0, "thirds_pos": 3, "controls_pass": True}
    assert C.cell_passes_gate(cell, fdr_survivors={"k1"}) is True
    assert C.cell_passes_gate(cell, fdr_survivors=set()) is False                       # FDR screened
    assert C.cell_passes_gate({**cell, "n": 10}, fdr_survivors={"k1"}) is False          # low n
    assert C.cell_passes_gate({**cell, "mean_bps": 8.0}, fdr_survivors={"k1"}) is False  # < fee
    assert C.cell_passes_gate({**cell, "t": 1.0}, fdr_survivors={"k1"}) is False         # weak t
    assert C.cell_passes_gate({**cell, "thirds_pos": 1}, fdr_survivors={"k1"}) is False  # unstable
    assert C.cell_passes_gate({**cell, "controls_pass": False}, fdr_survivors={"k1"}) is False


def _dense_edge_world(n_assets=4, events_per_asset=8, hour_ms=3_600_000, bucket_ms=10_000):
    """A DENSE, time-aligned multi-asset world with a real down-cascade continuation edge.
    Every asset has an obs at every 10s tick (so the cross-asset shuffle, the shifted window, and
    the matched non-event windows all have data — an empty control can't masquerade as a null).
    Each asset cascades at its OWN staggered times (offsets are not HOUR-aligned), so a real edge
    is NOT market-wide drift and the shuffle/shift/matched controls genuinely read ~0."""
    assets = ["BTC", "ETH", "SOL", "WIF"][:n_assets]
    end = events_per_asset * hour_ms + 2_000_000
    ticks = list(range(0, end + 1, bucket_ms))
    series_by_asset, events = {}, []
    for ai, a in enumerate(assets):
        off = ai * 130_000                       # staggered, not a multiple of HOUR or 60s
        dips = {}
        for k in range(events_per_asset):
            t0 = off + k * hour_ms
            move = 0.0040 + 0.0002 * (k % 3)     # 40/42/44 bps -> finite t (no zero-variance sentinel)
            dips[t0 + 60_000] = 100.0 * (1.0 - move)
            events.append({"asset": a, "ts_ms": t0, "direction": -1})
        series_by_asset[a] = [{"asset": a, "ts_ms": t, "mid": dips.get(t, 100.0),
                               "oi": 1e9, "n_trades": 1, "notional": 1.0} for t in ticks]
    return series_by_asset, events


def test_negative_controls_null_on_planted_true_edge():
    """The control battery's certification test: a genuine continuation edge is strong, and EVERY
    control both (a) actually measures data and (b) nulls. The row-floor assertion is the guard
    against the prior bug where empty controls passed `clustered_t([])==0 <= 2` for the WRONG reason."""
    series_by_asset, events = _dense_edge_world()
    rng = random.Random(0)
    floor = int(0.6 * len(events))

    real = C.aligned_moves(events, series_by_asset, 60_000, "continuation")
    assert len(real) == len(events)
    assert C.clustered_t(real) > 2.0 and sum(r["bps"] for r in real) / len(real) > 30

    controls = {
        "flip": C.control_flip(events, series_by_asset, 60_000, "continuation"),
        "shuffle": C.control_shuffle(events, series_by_asset, 60_000, "continuation", rng=rng),
        "matched": C.control_matched(events, series_by_asset, 60_000, "continuation",
                                     rng=rng, exclude_events=events),
        "shift": C.control_shift(events, series_by_asset, 60_000, "continuation", shift_ms=600_000),
    }
    for name, ctrl in controls.items():
        assert len(ctrl) >= floor, f"{name} control too thin ({len(ctrl)}) — must MEASURE, not vacuously pass"
        assert C.clustered_t(ctrl) <= 2.0, f"{name} control failed to null: t={C.clustered_t(ctrl):.2f}"


def test_detect_cascades_tolerates_none_mid():
    """A transient capture gap (None mid) must not crash the detector (it would abort the whole audit)."""
    s = _step_cascade("BTC", 250, at=200, move_frac=-0.015)
    s[150]["mid"] = None                  # gap far from the planted cascade
    evs = C.detect_cascades(s)            # must not raise
    assert isinstance(evs, list) and len(evs) == 1 and evs[0]["ts_ms"] == s[200]["ts_ms"]


# ---------------------------------------------------------------------------
# Task 8 — exploitability audit (run_audit)
# ---------------------------------------------------------------------------

def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "run_cascade_exploit_audit",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "run_cascade_exploit_audit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_run_audit_accruing_when_few_events(tmp_path):
    audit = _load_audit()
    conn = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(conn)
    for i in range(50):                                 # calm -> no events -> accruing
        P.save_cascade_obs(conn, {"BTC": (100.0, 1e9, 5, 1e5)}, ts_ms=i * 10_000)
    res = audit.run_audit(conn, min_n=40, rng=random.Random(0))
    assert res["status"] == "accruing" and res["n_events"] == 0


def test_run_audit_writes_events_and_returns_status(tmp_path):
    audit = _load_audit()
    conn = P.connect(os.path.join(str(tmp_path), "engine.sqlite"))
    P.ensure_schema(conn)
    mid = 100.0
    for i in range(250):                                # one planted down-cascade in calm BTC
        m = mid * (1 + (0.0002 if i % 2 else -0.0002))
        oi, n, notl = 1e9, 10, 1e5
        if 195 <= i <= 200:
            m = 100.0 * (1 - 0.0025 * (i - 194)); oi = 1e9 * (1 - 0.004 * (i - 194)); n = 60
        P.save_cascade_obs(conn, {"BTC": (m, oi, n, notl)}, ts_ms=i * 10_000)
    res = audit.run_audit(conn, min_n=40, rng=random.Random(0))
    assert res["n_events"] >= 1
    assert res["status"] in ("accruing", "no-edge", "edge")
    assert len(P.read_cascade_events(conn)) == res["n_events"]
