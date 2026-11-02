"""The paper entry must be the funded entry's own quote, not a second one taken seconds later.

MANLETS (2026-09-23, same origin, both sides) is the case:

    funded  their 8.91387e-05  quote 9.14524e-05 (route)  entry 9.15237e-05  21:08:48.949
    paper   their 8.91387e-05  quote 7.12800e-05 (MID)    entry 1.02274e-04  21:08:51.687

Two separate defects, and neither is a slippage model — `executable_entry_fill` already returns the
exact route quote and nothing is added on top.

1. THE PAPER ENTRY IS 11.75% ABOVE THE FUNDED ENTRY, TAKEN 2.738 s LATER. The fan-out runs after
   the funded hook and buys its own quote. Size cannot explain it and points the other way: paper
   bought $100 against funded's $175, and a smaller clip prices BETTER on a thin pool.
2. `quote_price` MEANT TWO THINGS. Both sides compute `fill/quote - 1`, but funded puts the route
   quote there while paper put the DexScreener MID — 20% below the source's own price here. So the
   same column held a route quote on one side and a mid on the other, and the two were compared.

The mid is not lost: it is already in `memecoin_entry_snapshots.features_json["price"]`, which is
where it belongs.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct


ARMS = ["funded_clone"]
THEIR = 8.91387e-05
FUNDED_QUOTE = 9.14524e-05
LATE_QUOTE = 1.02274e-04          # what a quote 2.7 s later cost on MANLETS
MID = 7.12800e-05                 # the DexScreener mid at that instant


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(eid="origin-1"):
    return CopyEvent("u1", "alice", "MANLETSMINT", "MANLETS", "buy", THEIR, 1e6, 250.0, True,
                     eid, "t", "t", 0.3, network_id="solana")


def _run(conn, ev, funded_quote=None):
    return ct.copy_event_to_arms(
        conn, ev, 100.0, "2026-09-23T21:08:51.687000+00:00", arms=ARMS, fill_fn=ct.honest_fill,
        stats_fn=lambda *a, **k: {"price": MID, "liquidity": 21974.46},
        quote_fn=lambda mint, network_id, size_usd: (LATE_QUOTE, "route_quote"),
        funded_quote=funded_quote)


def _run_sized(conn, ev, funded_clip=None):
    return ct.copy_event_to_arms(
        conn, ev, 100.0, "2026-09-23T21:08:51.687000+00:00", arms=ARMS, fill_fn=ct.honest_fill,
        stats_fn=lambda *a, **k: {"price": MID, "liquidity": 21974.46},
        quote_fn=lambda mint, network_id, size_usd: (LATE_QUOTE, "route_quote"),
        funded_clip=funded_clip)


def _position(conn):
    return conn.execute("SELECT * FROM memecoin_positions ORDER BY id DESC LIMIT 1").fetchone()


def test_the_funded_quote_is_used_when_the_funded_path_bought_one():
    """The whole point: the arm takes the funded instant's price, not its own later one.

    ⚠️ THE REACH VALUE IS THE PAIR OF PRICES. They must DIFFER — give the late quote the funded
    price and the test passes whether or not the reuse happened.
    """
    conn = _conn()
    assert FUNDED_QUOTE != LATE_QUOTE, "the fixture cannot detect reuse if both prices agree"
    _run(conn, _ev(), funded_quote={"price": FUNDED_QUOTE,
                                    "observed_at": "2026-09-23T21:08:48.900000+00:00"})
    pos = _position(conn)
    assert pos["entry_price"] == pytest.approx(FUNDED_QUOTE)
    assert pos["entry_fill_source"] == "funded_quote_reuse"
    assert pos["quote_observed_at"] == "2026-09-23T21:08:48.900000+00:00"
    assert conn.execute("SELECT quote_observed_at FROM memecoin_fills WHERE kind='entry'").fetchone()[0] \
        == pos["quote_observed_at"]


def test_without_a_funded_quote_the_arm_buys_exactly_one_of_its_own():
    conn = _conn()
    calls = []

    def quote(mint, network_id, size_usd):
        calls.append(mint)
        return LATE_QUOTE, "route_quote"

    ct.copy_event_to_arms(conn, _ev(), 100.0, "2026-09-23T21:08:51.687000+00:00", arms=ARMS,
                          fill_fn=ct.honest_fill,
                          stats_fn=lambda *a, **k: {"price": MID, "liquidity": 21974.46},
                          quote_fn=quote, funded_quote=None)
    pos = _position(conn)
    assert pos["entry_price"] == pytest.approx(LATE_QUOTE)
    assert pos["entry_fill_source"] == "paper_quote"
    assert pos["quote_observed_at"] is None
    assert len(calls) == 1, "never a second quote for the same event"


def test_own_quote_keeps_observation_time_and_route_binding(monkeypatch):
    from sniper.services import memecoin_quotes

    calls = []
    def quote_entry(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            unit_price=str(LATE_QUOTE), observed_at="2026-09-23T21:08:51.600000+00:00",
            quote_provider="jupiter", route_binding={"venue": "jupiter", "amm_keys": ["pool-1"]},
            quote_fingerprint="quote-fingerprint-1",
        )

    monkeypatch.setattr(memecoin_quotes, "quote_entry", quote_entry)
    conn = _conn()
    result = ct.copy_event_to_arms(
        conn, _ev(), 100.0, "2026-09-23T21:08:51.687000+00:00", arms=ARMS,
        fill_fn=ct.honest_fill, stats_fn=lambda *a, **k: {"price": MID},
    )
    assert result == {"funded_clone": "opened"}
    assert len(calls) == 1
    pos = _position(conn)
    snap = conn.execute("SELECT features_json FROM memecoin_entry_snapshots").fetchone()
    assert pos["quote_observed_at"] == "2026-09-23T21:08:51.600000+00:00"
    assert conn.execute("SELECT quote_observed_at FROM memecoin_fills WHERE kind='entry'").fetchone()[0] \
        == pos["quote_observed_at"]
    assert json.loads(snap["features_json"])["paper_quote"] == {
        "observed_at": "2026-09-23T21:08:51.600000+00:00",
        "provider": "jupiter",
        "route_binding": {"venue": "jupiter", "amm_keys": ["pool-1"]},
        "fingerprint": "quote-fingerprint-1",
    }
    assert pos["quote_route_target"] is None


def test_quote_price_is_the_route_quote_and_never_the_mid():
    """The column meant two things; now it means one, and it is the one funded means."""
    conn = _conn()
    _run(conn, _ev(), funded_quote={"price": FUNDED_QUOTE,
                                    "observed_at": "2026-09-23T21:08:48.900000+00:00"})
    pos = _position(conn)
    assert pos["quote_price"] == pytest.approx(FUNDED_QUOTE)
    assert pos["quote_price"] != pytest.approx(MID)


def test_paper_reports_no_execution_slippage_because_it_cannot_measure_any():
    """Absent, not zero. Paper never executes, so it has no fill to compare against its quote.

    Writing 0.0 would claim a measurement of zero; the honest value is unknown.
    """
    conn = _conn()
    _run(conn, _ev(), funded_quote={"price": FUNDED_QUOTE,
                                    "observed_at": "2026-09-23T21:08:48.900000+00:00"})
    pos = _position(conn)
    assert pos["execution_slippage_pct"] is None


def test_source_drift_is_the_number_that_compares_to_funded():
    """With the funded quote reused, paper's drift equals funded's by construction: +2.60%."""
    conn = _conn()
    _run(conn, _ev(), funded_quote={"price": FUNDED_QUOTE,
                                    "observed_at": "2026-09-23T21:08:48.900000+00:00"})
    pos = _position(conn)
    assert pos["source_drift_pct"] == pytest.approx((FUNDED_QUOTE / THEIR - 1.0) * 100.0)
    assert pos["source_drift_pct"] == pytest.approx(2.5957, abs=1e-3)


def test_the_arms_own_quote_shows_the_timing_gap_it_still_carries():
    """Without reuse the drift is +14.73% against funded's +2.60% — the 2.7 s, measured."""
    conn = _conn()
    _run(conn, _ev(), funded_quote=None)
    pos = _position(conn)
    assert pos["source_drift_pct"] == pytest.approx((LATE_QUOTE / THEIR - 1.0) * 100.0)
    assert pos["source_drift_pct"] == pytest.approx(14.7358, abs=1e-3)


def test_the_helper_reads_the_quote_the_funded_path_already_persisted():
    """One indexed lookup, not a second Jupiter call. Absent is the common case, not an error."""
    conn = _conn()
    assert m.funded_quote_for_origin(conn, "origin-1") is None, "no attempt row yet"
    assert m.funded_quote_for_origin(conn, None) is None
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, network_id, mint,"
        " decision_started_at, source_timestamp_trust, clock_domain, quote_price,"
        " quote_observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("a1", "origin-1", "solana", "MANLETSMINT", "2026-09-23T21:08:48.000000+00:00",
         "claimed_untrusted", "observed_real_utc", FUNDED_QUOTE,
         "2026-09-23T21:08:48.900000+00:00"))
    conn.commit()
    got = m.funded_quote_for_origin(conn, "origin-1")
    assert got["price"] == pytest.approx(FUNDED_QUOTE)
    assert got["observed_at"] == "2026-09-23T21:08:48.900000+00:00"


def test_a_refused_attempt_carries_no_quote_so_the_arm_falls_back():
    """223 of 229 origins in the reference window are refusals that never reached a quote."""
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, network_id, mint,"
        " decision_started_at, source_timestamp_trust, clock_domain) VALUES (?,?,?,?,?,?,?)",
        ("a2", "origin-2", "solana", "MANLETSMINT", "2026-09-23T21:08:48.000000+00:00",
         "claimed_untrusted", "observed_real_utc"))
    conn.commit()
    assert m.funded_quote_for_origin(conn, "origin-2") is None


# --- size: the paper clip must be the funded clip when funded spent one -------------------------
#
# Paper ran flat at MEMECOIN_TRADE_SIZE_USD while funded re-sizes by bankroll inside the entry path.
# Measured over 206 funded positions in 7 days: solana median $175.00, 4663 $39.81, 56 $68.30,
# against paper's $100 everywhere. Same signal, different money, so dollar PnL was never comparable.


def test_the_funded_clip_is_used_when_the_funded_entry_spent_one():
    conn = _conn()
    _run_sized(conn, _ev(), funded_clip={"usd": 175.0})
    pos = _position(conn)
    assert pos["entry_notional"] == pytest.approx(175.0)


def test_without_a_funded_clip_the_arm_keeps_the_configured_base():
    """A refusal never sized, and refusals are most of this study."""
    conn = _conn()
    _run_sized(conn, _ev(), funded_clip=None)
    pos = _position(conn)
    assert pos["entry_notional"] == pytest.approx(100.0)


def test_the_size_provenance_is_recorded_so_the_two_are_never_averaged():
    import json
    conn = _conn()
    _run_sized(conn, _ev("origin-clip"), funded_clip={"usd": 175.0})
    row = conn.execute("SELECT features_json FROM memecoin_entry_snapshots "
                       "WHERE upstream_event_id=?", ("origin-clip",)).fetchone()
    facts = json.loads(row["features_json"])
    assert facts["paper_size_source"] == "funded_clip"
    assert facts["paper_size_usd"] == pytest.approx(175.0)

    conn2 = _conn()
    _run_sized(conn2, _ev("origin-base"), funded_clip=None)
    row2 = conn2.execute("SELECT features_json FROM memecoin_entry_snapshots "
                         "WHERE upstream_event_id=?", ("origin-base",)).fetchone()
    assert json.loads(row2["features_json"])["paper_size_source"] == "base_size"


def test_funded_clone_does_not_scale_by_the_copied_traders_size(monkeypatch):
    """The funded runner is handed a flat base and re-sizes by bankroll; it never reads their size.

    ⚠️ CONVICTION SIZING IS OFF IN PRODUCTION (`MEMECOIN_CONVICTION_SIZING` is False), so dropping it
    for this arm changes nothing today and the honest claim is a CONTRACT, not a fix. The knob is
    forced on here precisely so the test can tell the two branches apart — with it off, both return
    the base and the assertion passes without exercising anything.

    ⚠️ THE OTHER REACH VALUE IS `their_size_usd`: far from the pivot, or the scaled and unscaled
    numbers coincide even with the knob on.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CONVICTION_SIZING", True)
    assert ct.arm_base_size(["funded_clone"], 50_000.0, 100.0) == 100.0
    scaled = ct.arm_base_size(["control"], 50_000.0, 100.0)
    assert scaled != 100.0, "the other arms keep the behaviour they were measured with"
