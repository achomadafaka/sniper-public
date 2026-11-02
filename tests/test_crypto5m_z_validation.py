import importlib.util
import math
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("zval", os.path.join(ROOT, "scripts", "crypto5m_z_validation.py"))
ZV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ZV)


def test_phi_monotone():
    assert ZV.phi(-5) < 0.01 and abs(ZV.phi(0) - 0.5) < 1e-9 and ZV.phi(5) > 0.99


def test_window_brier_delta_uses_sharp_ticks_only():
    y = 1
    rows = [
        {"divergence": 5.0, "jump_flag": 0, "z": -1.0, "z_v2": 1.5, "settlement": y},  # sharp: z bad, z_v2 good
        {"divergence": 0.5, "jump_flag": 0, "z": 3.0, "z_v2": 0.0, "settlement": y},   # calm: ignored by gate
    ]
    d = ZV.window_brier_delta(rows)              # mean over SHARP ticks of Brier(z) - Brier(z_v2)
    assert d is not None and d > 0              # only the sharp tick counts, where z_v2 is better


def test_window_brier_delta_none_without_sharp_ticks():
    rows = [{"divergence": 0.4, "jump_flag": 0, "z": 0.1, "z_v2": 0.1, "settlement": 1}]
    assert ZV.window_brier_delta(rows) is None


def test_chronological_split_no_leakage():
    windows = {100: [], 200: [], 300: [], 400: [], 500: []}
    train, holdout = ZV.chronological_split(windows, train_frac=0.6)
    assert max(train) < min(holdout)            # every holdout window strictly later than every train window


def _seed_db(path, rows):
    import sniper.algos.polymarket_crypto5m.z_shadow_log as ZL
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    ZL.ensure_schema(c)
    for r in rows:
        ZL.record_tick(c, r)
    for ws in {r["window_start_ts"] for r in rows}:
        first = next(x for x in rows if x["window_start_ts"] == ws)
        ZL.resolve_window(c, ws, settlement=first["_settlement"], sharp_label=first["_label"], now_ts=ws + 305)
    return c


def test_gate_accruing_below_milestone(tmp_path):
    p = str(tmp_path / "z.sqlite")
    rows = [{"engine_version": "v", "window_start_ts": 1000 + i, "tick_ts": 1000 + i, "condition_id": "c",
             "btc_now": 1, "btc_start": 1, "seconds_left": 100, "vol_slow": 1e-4, "vol_fast": 5e-4,
             "jump_flag": 1, "divergence": 5.0, "z": 0.0, "z_ours": 0.0, "z_market": 0.0, "market_mid": 0.5,
             "z_v2": 0.0, "weight": 0.5, "_settlement": 1, "_label": "sharp_revert"} for i in range(5)]
    _seed_db(p, rows)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    g = ZV.gate(c, milestone=240)
    assert g["status"] == "accruing" and g["n_sharp_revert_holdout"] < 240


def test_sigmoid_fit_recovers_both_directions():
    # synthetic: high divergence -> market is the truth-aligned estimate (ours wrong) -> w->0;
    # low divergence -> ours right, market wrong -> w->1. fit must recover both.
    train = []
    for i in range(200):
        sharp = i % 2 == 0
        y = 1
        if sharp:
            train.append({"divergence": 6.0, "z_ours": -2.0, "z_market": 2.5, "settlement": y, "jump_flag": 0})
        else:
            train.append({"divergence": 0.5, "z_ours": 2.5, "z_market": -2.0, "settlement": y, "jump_flag": 0})
    a, b = ZV.fit_weight(train)
    w_sharp = ZV.sigmoid(a - b * math.log(6.0))
    w_calm = ZV.sigmoid(a - b * math.log(0.5))
    assert w_sharp < 0.3 and w_calm > 0.7
