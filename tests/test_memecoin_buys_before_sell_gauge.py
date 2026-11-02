"""Buys must be dispatched before the sell gauge writes.

The per-wallet loop is serial and the sell branch performs a DB WRITE (`mark_sell_classified`)
before the buy branch is reached, so a batch leading with large sells puts measurement writes in
front of entry submission on the latency-critical armed fast lane.

CLAUDE.md: "candidate-cohort DB writes run only after the current poll's funded hooks. Never put
probe HTTP or candidate research writes before entry submission."

Context: position 44596 (KIRK) took 16.16s from signal to confirmed order, 11.22s of it before the
funded hook was entered, on a path that is DB-only by design. Three processes hold write handles on
the 7.7 GB funded ledger and SQLite allows one writer at a time.
"""
from sniper.analysis.fomo_account.classify import CASH_TOKENS, classify_swap
from sniper.services.memecoin_follow_activity import _buys_first


# classify_swap keys off the TOKEN ADDRESSES, not the amounts: cash in + token out is a buy,
# token in + cash out is a sell. A fixture built on amount signs classifies as `cash_route`/
# `token_route` for everything and makes these tests pass without exercising the split at all.
_CASH = sorted(CASH_TOKENS)[0]


def _swap(kind, i):
    base = {"id": "%s-%d" % (kind, i), "createdAt": "2026-08-23T00:00:%02d+00:00" % i}
    if kind == "buy":
        base.update(inTokenAddress=_CASH, outTokenAddress="M%d" % i,
                    humanUsdAmountIn=500.0, outHumanAmount=100.0)
    else:
        base.update(inTokenAddress="M%d" % i, outTokenAddress=_CASH,
                    humanUsdAmountOut=500.0, inHumanAmount=100.0)
    return base


def test_the_fixture_actually_produces_buys_and_sells():
    """Guard the guard: if this drifts, every other test here passes vacuously."""
    assert classify_swap(_swap("buy", 1))[0] == "buy"
    assert classify_swap(_swap("sell", 1))[0] == "sell"


def _kinds(swaps):
    return [classify_swap(s)[0] for s in swaps]


def test_every_buy_precedes_every_sell():
    batch = [_swap("sell", 0), _swap("sell", 1), _swap("buy", 2), _swap("sell", 3), _swap("buy", 4)]
    kinds = _kinds(_buys_first(batch))
    assert kinds.index("buy") < min(i for i, k in enumerate(kinds) if k == "sell")


def test_nothing_is_dropped_or_duplicated():
    batch = [_swap("sell", 0), _swap("buy", 1), _swap("sell", 2), _swap("buy", 3)]
    out = _buys_first(batch)
    assert len(out) == len(batch)
    assert sorted(s["id"] for s in out) == sorted(s["id"] for s in batch)


def test_relative_order_is_preserved_within_each_kind():
    batch = [_swap("sell", 0), _swap("buy", 1), _swap("sell", 2), _swap("buy", 3)]
    out = _buys_first(batch)
    assert [s["id"] for s in out if s["id"].startswith("buy")] == ["buy-1", "buy-3"]
    assert [s["id"] for s in out if s["id"].startswith("sell")] == ["sell-0", "sell-2"]


def test_an_all_sell_batch_is_unchanged():
    batch = [_swap("sell", i) for i in range(4)]
    assert [s["id"] for s in _buys_first(batch)] == [s["id"] for s in batch]


def test_an_all_buy_batch_is_unchanged():
    batch = [_swap("buy", i) for i in range(4)]
    assert [s["id"] for s in _buys_first(batch)] == [s["id"] for s in batch]


def test_an_empty_batch_is_safe():
    assert _buys_first([]) == []


def test_a_non_buy_route_sorts_behind_the_buys_and_is_not_lost():
    """A token_route/cash_route swap is not a buy, so it keeps its place behind entry submission."""
    junk = {"id": "junk"}
    assert classify_swap(junk)[0] == "token_route"
    out = _buys_first([junk, _swap("buy", 1)])
    assert [s["id"] for s in out] == ["buy-1", "junk"]
