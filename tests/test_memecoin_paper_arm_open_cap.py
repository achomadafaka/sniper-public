"""A paper arm may not accumulate open coins without a bound.

On 2026-09-05 the operator added `real_shadow` as a measurement arm and removed it within the hour,
recorded in `.env` beside `MEMECOIN_ARMS`: "its paper positions are marked through the same
quoter/Jupiter lanes as funded reads; operator prefers no contention with money-deciding calls."
The arm that answers the refusal-family question has to survive that objection, so it carries the
bound the old arms never had.

THE COST IS PER OPEN COIN, NOT PER POSITION AND NOT PER ARM. `bracket_pass` builds ONE batched
price map keyed by `coin_identity` over every open position, so a second position on a coin already
open costs nothing, and `copy_event_to_arms` buys one entry quote per EVENT however many arms are
running. Measured over 2026-09-16..2026-09-23T12:49Z, the window where the two arms were alive:
distinct open coins p50 26, p90 36, p99 40, peak 46; open position rows p50 57, p90 80, peak 102.

So the cap is on distinct open COINS and the check sits ABOVE the quote, for the same reason the
chain scope and the sampler do: an event this lane will not open must not buy a route quote first.
"""
import sqlite3

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.settings import memecoin as cfg


# `real_shadow` and not a new name: it is the arm the operator created for this exact measurement on
# 2026-09-05 (funded exit rails, every roster signal) and removed an hour later for the contention
# these caps answer. Reusing it needs no policy registration; `resolve_paper_arm_policy` already
# freezes the funded rails per episode, and `build_paper_cohort` mints a fresh cohort whenever those
# knobs change, which they have since it last ran.
ARMS = ["real_shadow"]


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(mint, network_id="solana", eid=None):
    return CopyEvent("u1", "alice", mint, "PEPE", "buy", 0.005, 1e6, 100.0, True,
                     eid or ("e-" + mint), "t", "t", 0.3, network_id=network_id)


def _open_paper(conn, mint, network_id="solana"):
    """One open paper position on this coin, which is what the cap counts."""
    m.open_position(conn, source_user_id="u1", source_handle="alice", mint=mint, ticker="PEPE",
                    entry_price=0.005, entry_notional=100.0, entry_fill_source="test",
                    entry_lag_s=0.0, their_size_usd=100.0,
                    opened_at="2026-09-23T00:00:00+00:00",
                    strategy=ARMS[0], network_id=network_id, execution_mode="paper")


class _Recorder:
    def __init__(self, value):
        self.calls = []
        self.value = value

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.value


def _run(conn, ev, quote=None, stats=None):
    """Drive the arm through the real quote branch, so a test can prove the quote never ran."""
    return ct.copy_event_to_arms(conn, ev, 100.0, "t", arms=ARMS, fill_fn=ct.honest_fill,
                                 stats_fn=stats or _Recorder({"price": 0.005}),
                                 quote_fn=quote or _Recorder((0.005, "jupiter")))


def test_an_unset_cap_bounds_nothing(monkeypatch):
    """Ships inert: with no cap configured the arm behaves exactly as it does today."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", 0)
    conn = _conn()
    for i in range(5):
        _open_paper(conn, "M%d" % i)
    quote = _Recorder((0.005, "jupiter"))
    out = _run(conn, _ev("NEW"), quote=quote)
    assert "skipped_open_coin_cap" not in set(out.values())
    assert quote.calls, "an uncapped arm still prices the event"


def test_a_new_coin_is_refused_once_the_open_coin_cap_is_reached(monkeypatch):
    """The reach value is the COIN COUNT: three coins open against a cap of three.

    A cap above the open count, or a mint already in the open set, both leave the defect
    unreachable and the test would pass against an arm with no cap at all.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", 0)
    conn = _conn()
    for i in range(3):
        _open_paper(conn, "M%d" % i)
    quote = _Recorder((0.005, "jupiter"))
    out = _run(conn, _ev("NEW"), quote=quote)
    assert out == {ARMS[0]: "skipped_open_coin_cap"}
    assert quote.calls == [], "the cap must be checked ABOVE the quote, never after paying for one"


def test_a_coin_already_open_is_never_capped(monkeypatch):
    """Another wallet on an open coin adds no coin to the batched price map.

    Capping it would censor the second wallet's eligible origin.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", 3)
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", 0)
    conn = _conn()
    for i in range(3):
        _open_paper(conn, "M%d" % i)
    quote = _Recorder((0.005, "jupiter"))
    ev = _ev("M1")
    ev.source_user_id = "u2"
    out = _run(conn, ev, quote=quote)
    assert "skipped_open_coin_cap" not in set(out.values())
    assert quote.calls, "an already-open coin is priced as usual"


def test_the_blind_chain_sub_cap_counts_only_the_chains_that_cost_a_provider_call(monkeypatch):
    """Solana is served by the batched DexScreener call; a blind-chain coin enters the
    GeckoTerminal pass instead, which is one HTTP per ~30 coins plus a per-mint rotation of 8.
    So the sub-cap counts non-Solana coins only, and Solana coins must not consume it."""
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", 2)
    conn = _conn()
    for i in range(5):
        _open_paper(conn, "S%d" % i, network_id="solana")
    for i in range(2):
        _open_paper(conn, "B%d" % i, network_id="4663")
    quote = _Recorder((0.005, "jupiter"))
    blind = _run(conn, _ev("NEWBLIND", network_id="4663"), quote=quote)
    assert blind == {ARMS[0]: "skipped_open_blind_cap"}
    assert quote.calls == []
    solana = _run(conn, _ev("NEWSOL", network_id="solana"), quote=quote)
    assert "skipped_open_blind_cap" not in set(solana.values())
    assert quote.calls, "five open Solana coins do not spend the blind budget"


def test_a_capped_event_is_recorded_as_censored_rather_than_dropped(monkeypatch):
    """A cap that binds is a measured quantity, not a silence.

    Every share this arm produces is conditional on what the cap refused, so the refusal has to
    land in the same durable channel as the sampler's and the chain scope's.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_COINS", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_ARM_MAX_OPEN_BLIND_COINS", 0)
    conn = _conn()
    _open_paper(conn, "M0")
    _run(conn, _ev("NEW", eid="origin-42"))
    row = conn.execute("SELECT action FROM memecoin_entry_snapshots "
                       "WHERE upstream_event_id=?", ("origin-42",)).fetchone()
    assert row is not None, "a capped origin must leave a record"
    assert row["action"] == "skipped_open_coin_cap"
