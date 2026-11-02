"""Offline review witnesses for risk_evidence v2; root owns execution."""

import pytest

from sniper.services import memecoin_desk_risk as R


T = 10_000.0


def swap(t, price=1e-9, *, sig=None, buy=1, actor="other", quote=1_000_000_000):
    return {
        "received_at": t,
        "is_buy": buy,
        "quote_atomic": quote,
        "actor": actor,
        "virtual_quote": int(price * 1e15),
        "virtual_token": 10**18,
        "signature": "s:%s" % t if sig is None else sig,
    }


def evidence(rows, **kwargs):
    return R.risk_evidence(
        rows,
        as_of=T + 2,
        opened_ts=kwargs.pop("opened_ts", T - 150),
        entry_price=1e-9,
        sb=0,
        coverage=kwargs.pop("coverage", {"from": T - 1_000, "through": T + 10}),
        ours_complete=kwargs.pop("ours_complete", True),
        **kwargs,
    )


def moving_tape():
    return [swap(T - 400 + i, 1e-9 * (1 + (i % 5) / 100)) for i in range(400)]


@pytest.mark.parametrize("case,expected", [("never_higher", 150), ("recent_high", 2),
                                           ("missing_anchor", None), ("capture_gap", None)])
def test_stagnation_age_distinguishes_no_new_high_from_unknown(case, expected):
    rows = [swap(T - 151), swap(T - 149, .9e-9), swap(T - 2, .8e-9)]
    kwargs = {}
    if case == "recent_high":
        rows[-1] = swap(T - 2, 1.1e-9)
    elif case == "missing_anchor":
        rows = rows[1:]
    elif case == "capture_gap":
        kwargs["coverage"] = {"from": T - 140, "through": T}
    ev = evidence(rows, **kwargs)
    assert ev["secs_since_new_high"] == expected
    assert ("secs_since_new_high" in ev["unknown"]) is (expected is None)


def test_noise_requires_provable_non_own_signatures_for_every_included_row():
    rows = moving_tape()
    assert evidence(rows)["n5"] > 0
    rows[-1]["signature"] = None
    assert evidence(rows)["n5"] is None


@pytest.mark.parametrize("missing_signature", [False, True])
def test_last_price_requires_provable_non_own_identity(missing_signature):
    row = swap(T - 1)
    if missing_signature:
        row["signature"] = None
    e = evidence([row], ours_complete=missing_signature)
    assert e["last_price"] is None


def test_maker_bar_cannot_keep_our_own_only_maker_after_own_fill_exclusion():
    row = swap(T - 1, sig="ours", actor="us")
    e = evidence(
        [row],
        ours_sigs=frozenset({"ours"}),
        bars=[{"t": T - 1, "n_makers": 1}],
    )
    assert e["swaps_60s"] == 0
    assert e["max_makers_per_1s_30s"] in (None, 0)


def test_newest_unpriceable_swap_does_not_leave_an_older_price_known():
    rows = [swap(T - 2), swap(T - 1)]
    rows[-1]["virtual_quote"] = None
    e = evidence(rows)
    assert e["swaps_60s"] == 2
    assert e["last_price"] is None


def test_since_fill_anchor_cannot_skip_a_newer_unpriceable_pre_fill_state():
    opened = T - 150
    rows = [swap(opened - 2, 2e-9), swap(opened - 1), swap(T - 1)]
    rows[1]["virtual_quote"] = None
    e = evidence(rows, opened_ts=opened)
    assert e["anchor_price"] is None
    assert e["high_since_fill"] is None
    assert e["new_high_120s"] is None


@pytest.mark.parametrize("direction", [None, 2, -1, "0", "sell", float("nan")])
def test_unknown_direction_is_not_a_known_sell_but_remains_activity(direction):
    e = evidence([swap(T - 1, buy=direction)])
    assert e["swaps_60s"] == 1
    assert e["buy_sol_30s"] is None
    assert e["sell_sol_30s"] is None
    assert e["distinct_sellers_30s"] is None


@pytest.mark.parametrize("direction,buy,sell", [(0, 0, 1), (1, 1, 0), (False, 0, 1), (True, 1, 0)])
def test_sql_and_boolean_directions_remain_known(direction, buy, sell):
    e = evidence([swap(T - 1, buy=direction)])
    assert e["swaps_60s"] == 1
    assert e["buy_sol_30s"] == buy
    assert e["sell_sol_30s"] == sell


def test_a_known_zero_quote_is_activity_and_zero_flow():
    e = evidence([swap(T - 1, quote=0)])
    assert e["swaps_60s"] == 1
    assert e["buy_sol_30s"] == 0
    assert e["sell_sol_30s"] == 0


@pytest.mark.parametrize("stamp", [None, float("nan"), float("inf"), -1, True, "bad", 10**1000])
def test_unlocatable_event_makes_window_facts_unknown_not_inactivity(stamp):
    e = evidence([swap(stamp)])
    assert e["rejected_rows"] == 1
    for field in ("swaps_60s", "buy_sol_30s", "sell_sol_30s", "last_price", "n5", "anchor_price"):
        assert e[field] is None, field


@pytest.mark.parametrize("coverage", [
    {"from": T, "through": T - 1},
    {"from": -1, "through": T},
    {"from": T - 1, "through": float("nan")},
    {"from": T - 1, "through": 10**1000},
])
def test_malformed_coverage_cannot_advertise_a_known_end(coverage):
    e = evidence([], coverage=coverage)
    assert e["known_through"] is None
    assert e["swaps_60s"] is None


@pytest.mark.parametrize("signature", ["", 0, True, [], {}])
def test_malformed_signature_is_unknown_instead_of_proof_or_an_exception(signature):
    row = swap(T - 1)
    row["signature"] = signature
    e = evidence([row])
    assert e["swaps_60s"] is None
    assert e["last_price"] is None
    assert e["sell_sol_30s"] is None


@pytest.mark.parametrize("actor", [None, "", " ", 0, [], {}])
def test_a_seller_without_a_nonempty_actor_keeps_seller_count_unknown(actor):
    e = evidence([swap(T - 1, buy=0, actor=actor)])
    assert e["swaps_60s"] == 1
    assert e["sell_sol_30s"] == 1
    assert e["distinct_sellers_30s"] is None


def test_window_edges_are_left_inclusive_and_right_exclusive():
    rows = [swap(T - 30, buy=0), swap(T, quote=-1)]
    e = evidence(rows, coverage={"from": T - 60, "through": T})
    assert e["known_through"] == T
    assert e["sell_sol_30s"] == 1
    assert e["swaps_60s"] == 1
    assert e["rejected_rows"] == 0


def test_noise_requires_coverage_of_whole_closed_five_second_buckets():
    e = R.risk_evidence(
        moving_tape(), as_of=T + 4, opened_ts=T - 150, entry_price=1e-9, sb=0,
        coverage={"from": T - 298, "through": T + 2}, ours_complete=True,
    )
    # end=10002, but the 60 closed noise buckets span [9700,10000).
    assert e["known_through"] == T + 2
    assert e["n5"] is None


def test_explicit_ps_sol_price_needs_no_synthetic_curve_reserves():
    row = swap(T - 1)
    del row["virtual_quote"]
    del row["virtual_token"]
    row["price_sol"] = 2e-9
    e = evidence([row])
    assert e["last_price"] == pytest.approx(2e-9)
    assert e["high_30s"] == pytest.approx(2e-9)
    assert e["buy_sol_30s"] == 1


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf"), True, "2e-9", 10**1000])
def test_unknown_explicit_ps_price_cannot_fall_back_to_reserves_on_latest_state(price):
    row = swap(T - 1)
    row["price_sol"] = price
    e = evidence([row])
    assert e["last_price"] is None
    assert e["high_30s"] is None
    assert e["swaps_60s"] == 1
    assert e["buy_sol_30s"] == 1
