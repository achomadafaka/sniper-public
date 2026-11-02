import math
from sniper.algos.polymarket_crypto5m import regime_vol as RV


def _calm_closes(now, n=200, step=1e-4, base=60000.0):
    # tiny alternating 1s returns ~1e-4 (BTC's typical 1s stdev) -> calm
    closes, px = {}, base
    for i in range(n):
        px *= math.exp(step if i % 2 == 0 else -step)
        closes[now - n + 1 + i] = px
    return closes


def test_calm_window_has_low_divergence_no_jump():
    now = 1_000_000
    out = RV.regime_vol(_calm_closes(now), now)
    assert out is not None
    assert out["jump_flag"] is False
    assert out["ticks_since_jump"] is None
    assert out["divergence"] < 1.5          # fast ~= slow when calm


def test_jump_excluded_from_slow_included_in_fast():
    now = 1_000_000
    closes = _calm_closes(now)
    base_slow = RV.regime_vol(closes, now)["vol_slow"]
    # inject a single 4sigma+ up jump at the latest second
    closes[now] = closes[now - 1] * math.exp(0.004)
    out = RV.regime_vol(closes, now)
    assert out["jump_flag"] is True
    assert out["ticks_since_jump"] == 0
    assert out["vol_fast"] > out["vol_slow"]                     # fast spikes (jump included)
    assert abs(out["vol_slow"] - base_slow) / base_slow < 0.25   # slow ~unchanged (jump excluded)
    assert out["divergence"] > 2.0                              # the regime signal


def test_ticks_since_jump_counts_up_after_the_jump():
    now = 1_000_000
    closes = _calm_closes(now, n=205)
    # a clean single jump: LEVEL-SHIFT every close from (now-5) onward up by exp(0.004). That makes exactly
    # one jump return at sec now-5 and calm returns after it (overwriting one close would also create a
    # revert jump the next second).
    for s in list(closes):
        if s >= now - 5:
            closes[s] *= math.exp(0.004)
    out = RV.regime_vol(closes, now)
    assert out["ticks_since_jump"] == 5
    assert out["jump_flag"] is False                            # latest tick is not the jump


def _flat_with_recent_jump_closes(now, n=200, base=60000.0):
    # Faithful to live BTC 1s klines: a near-flat SUB-TICK path (returns ~1e-7 — the price is unchanged or
    # one tick in most 1s buckets) with a single ~1e-4 jump at the latest second. The MAD filter flags the
    # jump, so the jump-EXCLUDED stdev collapses to the ~1e-7 noise floor while vol_fast (jump-included)
    # still catches it -> divergence in the hundreds and z_ours = ln/(vol_slow*sqrt t) explodes to ~-640.
    closes, px = {}, base
    for i in range(n):
        px *= math.exp(1e-7 if i % 2 == 0 else -1e-7)
        closes[now - n + 1 + i] = px
    closes[now] = closes[now - 1] * math.exp(1e-4)
    return closes


def test_zero_dominated_window_floors_vol_slow():
    # Regression for the live brain-v2 bug (2026-06-26): without a floor, vol_slow collapsed to ~1e-7, so
    # z_ours blew up to ~-640, divergence to ~774, the blend weight pinned at ~0 and z_v2 degenerated to the
    # market price on EVERY tick (calm or sharp). The floor matches the live vol (live_desk.VOL_FLOOR_PER_S).
    now = 1_000_000
    bug = RV.regime_vol(_flat_with_recent_jump_closes(now), now, vol_floor=0.0)   # unfloored -> the bug
    assert bug["vol_slow"] < 1e-4             # the pathological collapse
    assert bug["divergence"] > 50.0           # huge -> would pin the weight at ~0
    fixed = RV.regime_vol(_flat_with_recent_jump_closes(now), now)                # default floor 1e-4
    assert fixed["vol_slow"] == 1e-4          # floored, not the ~1e-7 raw stdev
    assert fixed["divergence"] < 50.0         # sane now -> the diffusion can actually contribute


def test_degenerate_returns_none():
    assert RV.regime_vol({}, 1000) is None
    assert RV.regime_vol({999: 60000.0}, 1000) is None          # too few points


def test_sharp_label_relative_and_absolute():
    # dead-calm window with a tiny relative-only spike -> NOT sharp (absolute floor protects)
    calm = [1e-5, -1e-5, 1e-5, 8e-5, -1e-5]      # max 8e-5 > 4x median but < abs_floor 5e-4
    assert RV.sharp_window_label(calm, settlement=1) == "calm"
    # clears BOTH thresholds, big move is UP, settles UP -> continue
    up = [1e-4, -1e-4, 1e-4, 9e-4, -1e-4]
    assert RV.sharp_window_label(up, settlement=1) == "sharp_continue"
    # same sharp UP move but settles DOWN -> revert
    assert RV.sharp_window_label(up, settlement=0) == "sharp_revert"
    # sharp DOWN move that settles UP -> revert
    dn = [1e-4, -9e-4, 1e-4, -1e-4]
    assert RV.sharp_window_label(dn, settlement=1) == "sharp_revert"


def test_sharp_label_empty_is_calm():
    assert RV.sharp_window_label([], settlement=1) == "calm"
