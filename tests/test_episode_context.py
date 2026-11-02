"""WO-3 — episodic memory. Deterministic checks on the pure episode_context primitive."""
from sniper.analysis.episode_context import episode_context


def _swap(block_no, ts, price, base=100, kind="v2_swap"):
    """One swap in a block; quote_side=1 so price = |amount1|/|amount0| = quote/base."""
    return {"kind": kind, "block_no": block_no, "block_ts": ts,
            "amount0": str(base), "amount1": str(int(round(price * base)))}


def test_shakeout_and_drawdown_and_age():
    # price path 10, 8(low), 12, 6(dip below 8, recovers to 15) -> low 8 broke & recovered = 1 shakeout
    swaps = [_swap(1, 100, 10), _swap(2, 110, 8), _swap(3, 120, 12),
             _swap(4, 130, 6), _swap(5, 140, 15)]
    ctx = episode_context(swaps, quote_side=1, confirm=1)
    assert ctx["prior_shakeouts"] == 1
    assert ctx["confirmed_swing_lows"] == 2               # lows at 8 and 6
    assert abs(ctx["prior_peak_drawdown"] - 0.5) < 1e-9   # 12 -> 6
    assert ctx["structure_age_s"] == 40                   # 140 - 100
    assert ctx["n_price_bars"] == 5
    assert ctx["lp_removes"] is None                      # no burn/sync ticks


def test_lp_removes_counted_when_present():
    swaps = [_swap(1, 100, 10), _swap(2, 110, 8, kind="sol_swap"),
             {"kind": "sol_mint", "block_no": 3, "block_ts": 120, "amount0": "0", "amount1": "0"},
             {"kind": "sol_burn", "block_no": 4, "block_ts": 130, "amount0": "0", "amount1": "0"}]
    ctx = episode_context(swaps, quote_side=1, confirm=1)
    assert ctx["lp_removes"] == 1                         # one burn tick counted
    assert ctx["lp_adds"] == 1


def test_context_is_causal_at_decision_time():
    swaps = [_swap(1, 100, 10), _swap(2, 110, 8), _swap(3, 120, 12),
             {"kind": "sol_burn", "block_no": 4, "block_ts": 200,
              "amount0": "0", "amount1": "0"}]
    ctx = episode_context(swaps, quote_side=1, confirm=1, as_of_ts=120)
    assert ctx["structure_age_s"] == 20
    assert ctx["n_price_bars"] == 3
    assert ctx["lp_removes"] is None


def test_no_quote_side_is_honest_nulls():
    ctx = episode_context([{"kind": "v2_swap"}], quote_side=None)
    assert ctx["prior_shakeouts"] is None and ctx["n_price_bars"] == 0


def test_empty_tape():
    ctx = episode_context([], quote_side=1)
    assert ctx["prior_shakeouts"] == 0 and ctx["structure_age_s"] is None
