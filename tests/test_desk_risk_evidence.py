"""risk_evidence v2: measured facts only; coverage must be PROVEN by the caller (never faked from the window end), unknown is never zero or inactivity,
malformed rows and unprovable exclusions of our own fills make the affected figures None."""
import math

import pytest

from sniper.services import memecoin_desk_risk as R

T = 10_000.0
COV = {"from": T - 1000.0, "through": T + 10.0}


def sw(t, px, *, buy=True, sol=1.0, actor="a", sig=None, quote="unset"):
    return {"received_at": t, "is_buy": 1 if buy else 0, "quote_atomic": int(sol * 1e9) if quote == "unset" else quote, "actor": actor,
            "virtual_quote": int(px * 1e15), "virtual_token": int(1e18), "signature": sig if sig is not None else "s%.2f" % t}


def px_of(row):
    return (row["virtual_quote"] / 1e9) / (row["virtual_token"] / 1e6)


def tape(seconds=400, step=1.0, base=1e-9):
    return [sw(T - seconds + i * step, base * (1 + 0.01 * (i % 5)), buy=i % 3 != 0, actor="s%d" % (i % 4)) for i in range(int(seconds / step))]


def ev(rows, **kw):
    kw.setdefault("coverage", COV)
    kw.setdefault("ours_complete", True)
    return R.risk_evidence(rows, as_of=kw.pop("as_of", T + 2.0), opened_ts=kw.pop("opened_ts", T - 100), entry_price=1e-9, sb=kw.pop("sb", 0), **kw)


def test_without_proven_coverage_every_window_fact_is_unknown_and_no_end_is_invented():
    e = ev(tape(), coverage=None)
    assert e["known_through"] is None and e["last_closed_at"] is None and e["stale_s"] is None and "coverage" in e["unknown"]
    for name in ("last_price", "high_30s", "buy_sol_30s", "sell_sol_30s", "swaps_60s", "n5", "high_since_fill", "held_s", "new_high_60s", "distinct_sellers_30s"):
        assert e[name] is None and name in e["unknown"], name
    assert e["sb"] == 0 and e["entry_price"] == 1e-9
    assert ev(tape(), coverage={"from": T, "through": float("nan")})["known_through"] is None


def test_end_is_the_earlier_of_the_elapsed_clock_and_the_proven_coverage_and_late_coverage_leaves_windows_unproven():
    e = ev(tape(), as_of=T + 2.0)
    assert e["known_through"] == math.floor(T + 2.0 - R.SB_LATE_S) == T and e["stale_s"] == pytest.approx(2.0)
    behind = ev(tape(), as_of=T + 30.0, coverage={"from": T - 1000.0, "through": T - 20.0})                    # the capture is only proven through T - 20
    assert behind["known_through"] == T - 20 and behind["stale_s"] == pytest.approx(50.0)                         # freshness exposed, never advertised as 'now'
    late_start = ev(tape(), coverage={"from": T - 20.0, "through": T + 10.0})                                      # coverage begins 20 s ago
    assert late_start["buy_sol_30s"] is None and "buy_sol_30s" in late_start["unknown"] and late_start["swaps_60s"] is None
    assert late_start["high_30s"] is None and late_start["n5"] is None


def test_windows_flows_sellers_highs_and_activity_are_measured_from_the_proven_tape():
    rows = tape()
    e = ev(rows)
    w30 = [r for r in rows if T - 30 <= r["received_at"] < T]
    assert e["sell_sol_30s"] == pytest.approx(sum(1.0 for r in w30 if not r["is_buy"])) and e["buy_sol_30s"] == pytest.approx(sum(1.0 for r in w30 if r["is_buy"]))
    assert e["distinct_sellers_30s"] == len({r["actor"] for r in w30 if not r["is_buy"]}) and 0 < e["sell_share_30s"] < 1
    assert e["buy_sol_prev_30s"] == pytest.approx(sum(1.0 for r in rows if T - 60 <= r["received_at"] < T - 30 and r["is_buy"]))
    assert e["swaps_60s"] == 60 and e["swaps_median_60s_prior_300s"] == 60 and e["swaps_60s_vs_median"] == pytest.approx(1.0)
    assert e["high_30s"] == e["max_price_30s"] and e["low_120s"] <= e["low_60s"] <= e["low_30s"] and e["n5"] is not None and e["n5"] > 0
    assert e["last_price"] == pytest.approx(px_of(rows[-1])) and e["last_swap_age_s"] == pytest.approx(T - rows[-1]["received_at"])


def test_unknown_quote_flow_is_unknown_not_zero_and_an_unpriceable_swap_is_activity_not_silence():
    rows = tape()
    rows[-5]["quote_atomic"] = None                                                                              # a swap with an unknown amount inside the last 30 s
    e = ev(rows)
    assert e["buy_sol_30s"] is None and e["sell_sol_30s"] is None and e["sell_share_30s"] is None and e["distinct_sellers_30s"] is None
    assert e["high_30s"] is not None and e["swaps_60s"] == 60                                                    # prices and counts are unaffected
    rows2 = tape()
    rows2[-5]["virtual_quote"] = 0                                                                               # unpriceable: no reserves
    e2 = ev(rows2)
    assert e2["swaps_60s"] == 60 and e2["swaps_median_60s_prior_300s"] == 60                                     # still counted as activity
    assert e2["high_30s"] is None and "high_30s" in e2["unknown"] and e2["last_price"] is not None               # the window's extremes are unknown, an older window is fine
    assert e2["high_120s"] is None and e2["sell_sol_30s"] is not None                                          # the 120 s extremes hold the bad row; flows (quote known) do not care


def test_malformed_rows_are_rejected_and_make_their_windows_unknown():
    rows = tape()
    rows[-3]["quote_atomic"] = -5
    rows[-10]["virtual_token"] = -1
    nan_row = dict(rows[-4], received_at=float("nan"))
    e = ev(rows + [nan_row])
    assert e["rejected_rows"] == 3 and e["buy_sol_30s"] is None and e["high_30s"] is None and e["swaps_60s"] is None
    assert ev(tape())["rejected_rows"] == 0


def test_our_own_fills_are_excluded_only_when_provable_else_the_figures_are_unknown():
    rows = tape()
    ours = {rows[-1]["signature"], rows[-2]["signature"]}
    base, ex = ev(rows), ev(rows, ours_sigs=frozenset(ours))
    assert ex["swaps_60s"] == base["swaps_60s"] - 2 and ex["last_swap_at"] < base["last_swap_at"]
    unproven = ev(rows, ours_sigs=frozenset(ours), ours_complete=False)                                           # the caller cannot assert the list is complete
    for name in ("buy_sol_30s", "sell_sol_30s", "distinct_sellers_30s", "swaps_60s", "high_30s", "max_makers_per_1s_30s"):
        assert unproven[name] is None, name
    nosig = tape()
    nosig[-5]["signature"] = None                                                                                  # a row whose signature is unknown cannot be proven not ours
    e = ev(nosig)
    assert e["buy_sol_30s"] is None and e["swaps_60s"] is None and e["high_30s"] is None


def test_since_fill_needs_capture_from_the_fill_an_anchor_price_and_a_clean_history():
    rows = tape()
    e = ev(rows, opened_ts=T - 100)
    assert e["anchor_price"] is not None and e["high_since_fill"] >= e["anchor_price"] and e["low_since_fill"] <= e["anchor_price"]
    assert e["new_high_60s"] is not None and e["new_high_120s"] is None and e["held_s"] == pytest.approx(100.0)         # held 100 s: a 120 s claim is unproven
    late = ev(rows, opened_ts=T - 100, coverage={"from": T - 50.0, "through": T + 10.0})                           # coverage starts AFTER the fill: no since-fill history
    for name in ("anchor_price", "high_since_fill", "low_since_fill", "secs_since_new_high", "new_high_60s"):
        assert late[name] is None and name in late["unknown"], name
    young = ev(rows, opened_ts=T - 20)
    assert young["high_30s_post_fill"] is None and young["new_high_60s"] is None
    held = ev(rows, opened_ts=T - 200)
    assert held["high_30s_post_fill"] == held["high_30s"] and held["high_120s_post_fill"] == held["high_120s"] and held["new_high_120s"] is not None
    flat = [sw(T - 300 + i, 1e-9) for i in range(299)]
    f = ev(flat, opened_ts=T - 150)
    assert f["secs_since_new_high"] == f["held_s"] and f["new_high_60s"] is False and f["new_high_120s"] is False  # nothing exceeded the anchor


def test_aggregate_maker_bars_cannot_prove_our_exclusion_and_sb_is_a_bool():
    rows = tape()
    bars = [{"t": T - 40 + i, "n_makers": 3} for i in range(10)] + [{"t": T - 10 + i, "n_makers": 7 if i == 3 else 1} for i in range(8)]
    m = ev(rows, bars=bars)
    assert m["max_makers_per_1s_30s"] is None and m["max_makers_per_1s_prev_30s"] is None
    assert ev(rows, bars=bars, ours_complete=False)["max_makers_per_1s_30s"] is None and ev(rows)["max_makers_per_1s_30s"] is None
    assert ev(rows, sb=1)["sb"] == 1 and ev(rows, sb=None)["sb"] == 0
    assert not any(k in m for k in ("pnl", "pnl_exact", "action", "reason", "threshold"))
