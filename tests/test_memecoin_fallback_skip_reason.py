"""A fast-lane-only refusal names the slot lane's own decision for the same source transaction
(Alpha, 2026-10-06 night: jpegkite HCHL2NsA read native_fast_lane_unavailable while the slot lane had
refused it at the source-buy floor). Log and trace only: the refusal reason itself is unchanged."""
from types import SimpleNamespace
import collections

import pytest

from sniper.services import memecoin_slot_skips as skips
from sniper.services import memecoin_real_runner as rr

SIG = "2j8xNazj1Zq1AZcMxHHHajpCzPMWHcuzMuxVJUGqTNJWhcnBk7pRQ3VPJYMgvYCaPSoaLExC1QyRBsxpuCcax6gw"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(skips, "_LAST", collections.OrderedDict())


def test_the_latest_lane_decision_is_kept_and_a_later_hint_cannot_erase_it():
    skips.note(SIG, "rejected", "source buy $1499 (0.384%) under mcap floor 0.800% ($3125)")
    skips.note(SIG, "pair_expired")
    assert skips.decision_for(SIG) == ("rejected", "source buy $1499 (0.384%) under mcap floor 0.800% ($3125)")
    assert skips.decision_for("other") is None


def test_the_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(skips, "LAST_MAX", 8)
    for i in range(20):
        skips.note("s%d" % i, "rejected", "r")
    assert len(skips._LAST) <= 8 and skips.decision_for("s19") == ("rejected", "r")


@pytest.mark.parametrize("origin", ["onchain:" + SIG, "swap:raybot:%s:wallet:mint" % SIG])
def test_a_fast_lane_refusal_carries_the_slot_lanes_reason(origin):
    skips.note(SIG, "rejected", "source buy $1499 (0.384%) under mcap floor 0.800% ($3125)")
    marks = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: marks.append((name, kw)))
    res = rr._fast_lane_refusal(SimpleNamespace(fomo_event_id=origin), "solana", trace)
    assert res["reason"] == "native_fast_lane_unavailable"           # identifier unchanged
    assert res["slot_lane_skip"] == "rejected: source buy $1499 (0.384%) under mcap floor 0.800% ($3125)"
    assert marks == [("slot_lane_skip", {"outcome": "rejected",
                                         "reason": "source buy $1499 (0.384%) under mcap floor 0.800% ($3125)"})]


def test_no_lane_decision_says_nothing():
    res = rr._fast_lane_refusal(SimpleNamespace(fomo_event_id="onchain:" + SIG), "solana", None)
    assert res == {"status": "rejected", "chain": "solana", "reason": "native_fast_lane_unavailable"}
