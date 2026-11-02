"""Live candidate feed — tested with injected market/trade providers (no network).

Proves: buyers collected across markets (openers only — SELL ignored); outcome-neutral
(no PnL/size filter); appearance floor; deterministic hash order + limit; drops into the
batch as Candidates.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.becker_feed import Candidate
from sniper.analysis.thesis_cards.live_feed import live_candidate_feed


def _t(wallet, side="BUY", size=1.0):
    return {"proxyWallet": wallet, "side": side, "size": size}


def test_collects_buyers_across_markets_openers_only():
    trades = {
        "m1": [_t("0xA"), _t("0xB"), _t("0xC", side="SELL")],   # C is a seller -> ignored
        "m2": [_t("0xA"), _t("0xB")],
        "m3": [_t("0xA")],
    }
    cands = live_candidate_feed(lambda: ["m1", "m2", "m3"], lambda c: trades[c],
                                min_appearances=1, limit=100)
    opened = {c.wallet: c.opened for c in cands}
    assert opened == {"0xa": 3, "0xb": 2}                       # A in 3 markets, B in 2; C none (seller)


def test_appearance_floor():
    trades = {"m1": [_t("0xA"), _t("0xB")], "m2": [_t("0xA")]}
    cands = live_candidate_feed(lambda: ["m1", "m2"], lambda c: trades[c],
                                min_appearances=2, limit=100)
    assert [c.wallet for c in cands] == ["0xa"]                 # only A appears in >=2 markets


def test_outcome_neutral_size_ignored():
    # a tiny buy and a huge buy count the same — no value/PnL weighting
    trades = {"m1": [_t("0xA", size=0.01)], "m2": [_t("0xA", size=1e6)]}
    cands = live_candidate_feed(lambda: ["m1", "m2"], lambda c: trades[c], min_appearances=1)
    assert cands == [Candidate("0xa", 2)]


def test_limit_and_deterministic_order():
    trades = {f"m{i}": [_t(f"0x{i:02d}")] for i in range(10)}
    a = live_candidate_feed(lambda: list(trades), lambda c: trades[c], min_appearances=1, limit=3)
    b = live_candidate_feed(lambda: list(trades), lambda c: trades[c], min_appearances=1, limit=3)
    assert a == b and len(a) == 3                               # deterministic hash order + limit


def test_handles_empty_or_none_trades():
    cands = live_candidate_feed(lambda: ["m1", "m2"], lambda c: None, min_appearances=1)
    assert cands == []
