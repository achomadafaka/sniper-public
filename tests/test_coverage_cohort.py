"""The research cohort's job is to remove selection, so its guards are about what it refuses to drop.

Three errors from the analysis that motivated it, each now a test:
  - a missing-mcap bucket was read as a small-coin finding,
  - 1,854 events on 8 mints were treated as a large sample,
  - a mid-tape move was quoted as if it were profit.
"""
import json

import pytest

from sniper.research import coverage_cohort as C


def _conn():
    conn = C.connect(":memory:")
    C.ensure_tables(conn)
    return conn


def _event(conn, eid, mint="MINT", at="2026-08-20T12:00:00+00:00", chain="solana", **kw):
    kw.setdefault("side", "buy")
    return C.record_event(conn, event_id=eid, observed_at=at, chain_id=chain, mint=mint, **kw)


# --- missing mcap is typed, never inferred -------------------------------------------------------

def test_a_present_mcap_is_taken_from_the_feed():
    assert C.classify_mcap(250_000.0) == (250_000.0, "feed", None)


@pytest.mark.parametrize("raw,reason", [(None, "no_supply"), (0, "no_supply")])
def test_an_absent_mcap_records_why(raw, reason):
    value, source, missing = C.classify_mcap(raw)
    assert value is None and source is None
    assert missing in C.MCAP_MISSING_REASONS and missing == reason


def test_a_derived_mcap_is_labelled_derived_not_feed():
    """The feed value and a supply*price derivation disagree often enough to need splitting."""
    value, source, missing = C.classify_mcap(None, supply=1_000_000.0, price=0.25)
    assert value == pytest.approx(250_000.0)
    assert source == "derived" and missing is None


def test_missing_mcap_is_never_stored_as_a_number():
    """THE BUG THIS PREVENTS. A missing-mcap bucket was read as '<$25k' and reported a 28% hit
    rate. A missing value must never sort into a size band."""
    conn = _conn()
    _event(conn, "e1", feed_market_cap=None)
    row = conn.execute("SELECT mcap_value, mcap_missing_reason FROM cohort_events").fetchone()
    assert row["mcap_value"] is None
    assert row["mcap_missing_reason"] is not None


# --- episodes: a burst is one market event -------------------------------------------------------

def test_buys_close_together_share_an_episode():
    conn = _conn()
    _event(conn, "e1", at="2026-08-20T12:00:00+00:00")
    _event(conn, "e2", at="2026-08-20T12:05:00+00:00")
    rows = conn.execute("SELECT episode_id, episode_seq FROM cohort_events ORDER BY observed_at").fetchall()
    assert rows[0]["episode_id"] == rows[1]["episode_id"]
    assert [r["episode_seq"] for r in rows] == [0, 1]


def test_a_quiet_gap_starts_a_new_episode():
    conn = _conn()
    _event(conn, "e1", at="2026-08-20T12:00:00+00:00")
    _event(conn, "e2", at="2026-08-20T12:20:00+00:00")     # > 900s gap
    ids = [r["episode_id"] for r in conn.execute(
        "SELECT episode_id FROM cohort_events ORDER BY observed_at")]
    assert ids[0] != ids[1]


def test_episodes_do_not_span_mints_or_chains():
    conn = _conn()
    _event(conn, "e1", mint="A")
    _event(conn, "e2", mint="B")
    _event(conn, "e3", mint="A", chain="4663")
    ids = {r["event_id"]: r["episode_id"] for r in conn.execute(
        "SELECT event_id, episode_id FROM cohort_events")}
    assert len({ids["e1"], ids["e2"], ids["e3"]}) == 3


# --- typed failures are results ------------------------------------------------------------------

def test_a_failed_quote_is_censored_not_dropped():
    """A coin nobody can sell is the most important row in the table."""
    conn = _conn()
    _event(conn, "e1")
    C.record_quote(conn, event_id="e1", horizon_s=60, side="sell", chain_id="solana",
                   requested_at="2026-08-20T12:01:00+00:00", failure_class="NO_ROUTE")
    row = conn.execute("SELECT failure_class, censored FROM cohort_quotes").fetchone()
    assert row["failure_class"] == "NO_ROUTE" and row["censored"] == 1


def test_a_successful_quote_is_not_censored():
    conn = _conn()
    _event(conn, "e1")
    C.record_quote(conn, event_id="e1", horizon_s=0, side="buy", chain_id="solana",
                   requested_at="t", unit_price_usd=0.001)
    assert conn.execute("SELECT censored FROM cohort_quotes").fetchone()["censored"] == 0


# --- executable, not mid-tape --------------------------------------------------------------------

def test_the_round_trip_uses_executable_quotes_on_both_legs():
    conn = _conn()
    _event(conn, "e1")
    C.record_quote(conn, event_id="e1", horizon_s=0, side="buy", chain_id="solana",
                   requested_at="t", unit_price_usd=0.0010)
    C.record_quote(conn, event_id="e1", horizon_s=60, side="sell", chain_id="solana",
                   requested_at="t", unit_price_usd=0.0012)
    net, status = C.executable_round_trip(conn, "e1", 60)
    assert status == "ok" and net == pytest.approx(20.0)


def test_a_censored_leg_makes_the_round_trip_censored_not_zero():
    conn = _conn()
    _event(conn, "e1")
    C.record_quote(conn, event_id="e1", horizon_s=0, side="buy", chain_id="solana",
                   requested_at="t", unit_price_usd=0.001)
    C.record_quote(conn, event_id="e1", horizon_s=60, side="sell", chain_id="solana",
                   requested_at="t", failure_class="INSUFFICIENT_LIQUIDITY")
    assert C.executable_round_trip(conn, "e1", 60) == (None, "censored")


def test_a_missing_leg_is_incomplete_not_a_result():
    conn = _conn()
    _event(conn, "e1")
    assert C.executable_round_trip(conn, "e1", 60)[1] == "incomplete"


# --- arms ----------------------------------------------------------------------------------------

def test_a_ceiling_admits_unknown_mcap_and_says_so():
    """A ceiling that refused unknown mcap would become a data-availability filter, and the coins
    with no mcap are disproportionately the ones the ceiling is about."""
    admitted, reason = C.arm_decision({"mcap_ceiling_usd": 1e6},
                                      {"mcap_value": None, "mcap_missing_reason": "feed_omitted"})
    assert admitted is True and "mcap_unknown" in reason


def test_a_ceiling_refuses_a_coin_above_it():
    admitted, reason = C.arm_decision({"mcap_ceiling_usd": 1e6}, {"mcap_value": 5e6})
    assert admitted is False and "above_ceiling" in reason


def test_the_control_arm_admits_everything():
    for event in ({"mcap_value": 5e9}, {"mcap_value": None, "mcap_missing_reason": "feed_zero"}):
        assert C.arm_decision({"mcap_ceiling_usd": None}, event)[0] is True


def test_refreezing_a_changed_arm_is_refused():
    """The arms are the pre-registration. Redefining one mid-cohort invalidates it."""
    conn = _conn()
    C.freeze_arms(conn)
    C.freeze_arms(conn)                                     # idempotent
    with pytest.raises(ValueError, match="already frozen"):
        C.freeze_arms(conn, {"ceiling_1m": {"mcap_ceiling_usd": 2_000_000.0}})


def test_every_frozen_arm_records_a_decision_for_an_event():
    conn = _conn()
    C.freeze_arms(conn)
    _event(conn, "e1", feed_market_cap=3_000_000.0)
    row = dict(conn.execute("SELECT * FROM cohort_events WHERE event_id='e1'").fetchone())
    C.record_arm_decisions(conn, row)
    got = {r["arm_id"]: r["admitted"] for r in conn.execute(
        "SELECT arm_id, admitted FROM cohort_arm_decisions WHERE event_id='e1'")}
    assert got == {"control": 1, "ceiling_1m": 0, "ceiling_5m": 1}


# --- chain separation and scheduling -------------------------------------------------------------

def test_chains_stay_separate_on_every_row():
    conn = _conn()
    _event(conn, "e1", chain="solana")
    _event(conn, "e2", chain="4663")
    assert {r["chain_id"] for r in conn.execute("SELECT chain_id FROM cohort_events")} == {"solana", "4663"}


def test_due_quotes_only_offers_horizons_that_have_elapsed():
    conn = _conn()
    _event(conn, "e1", at="2026-08-20T12:00:00+00:00")
    due = C.due_quotes(conn, "2026-08-20T12:01:30+00:00")
    assert {d["horizon_s"] for d in due} == {0, 60}
    assert {d["side"] for d in due} == {"buy", "sell"}


def test_due_quotes_does_not_re_offer_a_recorded_quote():
    conn = _conn()
    _event(conn, "e1", at="2026-08-20T12:00:00+00:00")
    C.record_quote(conn, event_id="e1", horizon_s=0, side="buy", chain_id="solana",
                   requested_at="t", unit_price_usd=1.0)
    due = C.due_quotes(conn, "2026-08-20T12:00:30+00:00")
    assert {(d["horizon_s"], d["side"]) for d in due} == {(0, "sell")}


def test_nothing_funded_is_imported():
    import inspect
    src = inspect.getsource(C)
    for banned in ("solana_real_seams", "solana_swap_executor", "solana_exit_executor",
                   "memecoin_real_runner", "memecoin_real_leg", "memecoin_exits", "db_memecoin"):
        assert banned not in src
