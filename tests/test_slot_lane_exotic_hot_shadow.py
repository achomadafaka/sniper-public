"""Hot-quote warm legs, SHADOW ONLY (Alpha 2026-10-07, option (a) of the late-landing root cause).

A live exotic buy waits for its USDC -> quote Jupiter leg (p50 199 / p90 341 ms over 48 sent legs since
10-02) before the decision. Warm-after-use keeps (venue, quote, exact clip) warm 60 s; today's two
WLDTE73P failures had a same-quote leg 97 s and 114 s before, on another venue, at another clip
($167.09 vs $163.45), so it missed both. The proposal: one leg per HOT quote mint, venue-free
(maxAccounts=20 fits every builder), kept 600 s, served snapped DOWN to the buy's clip within 10%.
It changes spend, so it runs in shadow first: every sent live leg is judged against the last sent
leg of its quote, logged with the snapped clip and the warm-mint count (the Jupiter load). It never
calls Jupiter and never changes a buy.
"""
import logging

import pytest

from sniper.execution import raydium_launchlab_buy as LL
from test_slot_lane_cpmm import PEPE_EV, _exotic_builder
from test_slot_lane_exotic_live import _buy, _live_worker, exotic_cpmm  # noqa: F401 (fixture)

Q, Q2 = "WLDTE73Pquote1111111111111111111111111111111", "OtherQuote111111111111111111111111111111111"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t


def test_the_first_leg_of_a_quote_is_a_miss_and_is_remembered():
    clock = Clock()
    hot = LL.HotQuoteShadow(clock=clock)
    v = hot.observe("pump_amm", Q, 167_090_000)
    assert v["would_hit"] is False and v["prev_clip"] is None and v["prev_age_s"] is None
    assert v["warm_mints"] == 1 and hot.stats == {"checked": 1, "would_hit": 0}


def test_a_smaller_prior_clip_within_ten_percent_on_any_venue_snaps_down():
    clock = Clock()
    hot = LL.HotQuoteShadow(clock=clock)
    hot.observe("pump_amm", Q, 150_000_000)
    clock.t += 114.0
    v = hot.observe("raydium_cpmm", Q, 163_450_000)                  # 150.00 >= 0.9 x 163.45
    assert v["would_hit"] is True and v["snapped_atomic"] == 150_000_000
    assert v["prev_venue"] == "pump_amm" and v["prev_age_s"] == pytest.approx(114.0)


@pytest.mark.parametrize("prev, clip, age", [
    (167_090_000, 163_450_000, 97.0),      # today's pair: the prior clip is LARGER, never spent up
    (140_000_000, 163_450_000, 30.0),      # more than 10% under
    (160_000_000, 163_450_000, 601.0),     # past the 600 s window
])
def test_a_larger_far_smaller_or_old_prior_is_a_miss(prev, clip, age):
    clock = Clock()
    hot = LL.HotQuoteShadow(clock=clock)
    hot.observe("pump_amm", Q, prev)
    clock.t += age
    v = hot.observe("raydium_cpmm", Q, clip)
    assert v["would_hit"] is False and v["snapped_atomic"] is None


def test_the_warm_mint_count_is_the_quotes_seen_inside_the_window():
    clock = Clock()
    hot = LL.HotQuoteShadow(clock=clock)
    hot.observe("pump_amm", Q, 10_000_000)
    clock.t += 300.0
    assert hot.observe("pump_amm", Q2, 10_000_000)["warm_mints"] == 2
    clock.t += 301.0
    assert hot.observe("pump_amm", Q2, 10_000_000)["warm_mints"] == 1      # Q expired at 600 s


def test_a_sent_live_leg_is_judged_and_logged_after_the_requeue(monkeypatch, caplog):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    order = []
    w.requeue_fn = lambda buy: order.append("requeue")
    real = w.hot_shadow.observe
    w.hot_shadow.observe = lambda *a: order.append("shadow") or real(*a)
    with caplog.at_level(logging.INFO):
        assert w.process(_buy())["status"] == "requeued"
        assert w.process(_buy())["status"] == "requeued"
    assert order == ["requeue", "shadow", "requeue", "shadow"], "measured after the hand-back, never before"
    assert len(funded) == 4, "the shadow never serves a leg"
    lines = [r.getMessage() for r in caplog.records if "hot quote shadow" in r.getMessage()]
    assert len(lines) == 2 and "would_hit=1" in lines[1] and "snapped=10000000" in lines[1]
    assert w.stats["hot_shadow_would_hit"] == 1


def test_a_refused_live_leg_is_not_remembered(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10,
                                                admission=lambda buy, q: "mcap $1 over ceiling $0")
    w.process(_buy())
    assert refused and w.hot_shadow.stats["checked"] == 0
