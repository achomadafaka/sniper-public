"""The paper arms copy every chain. MEMECOIN_PAPER_ARM_CHAINS_<ARM> scopes one arm to a chain set.

Gamma measured BSC and Base paper rows as 95% unpriceable, and every one of those events buys a
Jupiter route quote before it is refused. The gate must therefore sit ABOVE the quote, which is the
same reason the "NO ARMS, NO WORK" early return exists in `copy_event_to_arms`.

An arm with no knob allows every chain, so an unset environment must be byte-identical to the old
behaviour. That is the property these tests pin hardest: the knob ships inert.
"""
import sqlite3

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.settings import memecoin as cfg


ARMS = ["funded_clone", "profile_matched_v1"]


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(network_id, side="buy", eid="e1"):
    return CopyEvent("u1", "alice", "MINT", "PEPE", side, 0.005, 1e6, 100.0, True,
                     eid, "t", "t", 0.3, network_id=network_id)


class _Quote:
    """Stands in for the executable fill. Records every call so a test can prove it never ran."""

    def __init__(self):
        self.calls = []

    def __call__(self, mint, network_id, size_usd):
        self.calls.append((mint, network_id, size_usd))
        return 0.005, "jupiter"


def _run(conn, ev, arms=ARMS, quote=None):
    return ct.copy_event_to_arms(conn, ev, 100.0, "t", arms=arms,
                                 fill_fn=lambda *a, **k: (0.005, "jupiter"),
                                 quote_fn=quote)


def test_an_unset_knob_scopes_nothing(monkeypatch):
    """The ship-inert property. No environment, no branch, no change."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS", {})
    out = _run(_conn(), _ev("56"))
    assert set(out) == set(ARMS)
    assert "skipped_chain_scope" not in set(out.values())


def test_an_out_of_scope_chain_skips_before_it_buys_a_quote(monkeypatch):
    """The whole point: a BSC event must not spend a Jupiter route quote to be refused."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS",
                        {a: frozenset({"solana", "4663"}) for a in ARMS})
    quote = _Quote()
    out = _run(_conn(), _ev("56"), quote=quote)
    assert out == {a: "skipped_chain_scope" for a in ARMS}
    assert quote.calls == [], "an out-of-scope chain bought a quote before refusing"


@pytest.mark.parametrize("chain", ["solana", "4663"])
def test_an_in_scope_chain_still_trades(chain, monkeypatch):
    """A scope that refused everything would look identical to a working one in the skip test."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS",
                        {a: frozenset({"solana", "4663"}) for a in ARMS})
    out = _run(_conn(), _ev(chain))
    assert "skipped_chain_scope" not in set(out.values())


def test_one_scoped_arm_does_not_refuse_the_others(monkeypatch):
    """The knob is PER ARM. A narrow arm must not take an unscoped sibling down with it, and the
    sibling still needs its quote — so the early return may not swallow the event."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS",
                        {"funded_clone": frozenset({"solana"})})
    out = _run(_conn(), _ev("56"))
    assert out["funded_clone"] == "skipped_chain_scope"
    assert out["profile_matched_v1"] != "skipped_chain_scope"


def test_a_sell_is_never_scoped(monkeypatch):
    """A position this lane already opened must stay closable, whatever the scope says now."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS", {})
    c = _conn()
    _run(c, _ev("56", eid="open-1"))
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS",
                        {a: frozenset({"solana"}) for a in ARMS})
    out = _run(c, _ev("56", side="sell", eid="close-1"))
    assert "skipped_chain_scope" not in set(out.values())


def test_the_helper_allows_every_chain_without_a_knob(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_CHAINS", {"funded_clone": frozenset({"solana"})})
    assert cfg.paper_arm_allows_chain("funded_clone", "solana") is True
    assert cfg.paper_arm_allows_chain("funded_clone", "56") is False
    assert cfg.paper_arm_allows_chain("profile_matched_v1", "56") is True
    assert cfg.paper_arm_allows_chain("FUNDED_CLONE", "56") is False, "arm match is case-folded"
