"""Tests for the signal copy-quote capture.

The invariants here are the ones that make the captured number trustworthy: exact quantities,
horizons anchored to the entry quote, the funded-hook delay staying visible, failures recorded
as terminal rows rather than gaps, and no signing seam anywhere in the import graph.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.services import memecoin_signal_copy_quotes as cq

T0 = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def mdb(tmp_path):
    return cq.open_measurement_db(tmp_path / "copy.db")


@pytest.fixture()
def ro():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE memecoin_events(
            fomo_event_id TEXT, source_user_id TEXT, network_id TEXT, mint TEXT, side TEXT,
            is_first_buy INTEGER, created_at TEXT, received_at TEXT, real_decision_at TEXT,
            their_market_cap REAL);
        CREATE TABLE IF NOT EXISTS memecoin_wallets(source_user_id TEXT, handle TEXT);
        """
    )
    return c


def add_event(c, eid, mint, *, uid="uid-1", net="solana", first=1, decided="2026-08-11T12:00:01+00:00",
              created="2026-08-11T12:00:00+00:00", received="2026-08-11T12:00:00.500000+00:00"):
    c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,side,"
              "is_first_buy,created_at,received_at,real_decision_at) "
              "VALUES (?,?,?,?,'buy',?,?,?,?)",
              (eid, uid, net, mint, first, created, received, decided))


def _row(eid="e1", leg="entry", horizon=0, anchor=None):
    return {"origin_event_id": eid, "source_user_id": "uid-1", "network_id": "solana",
            "mint": "MintAAA", "leg": leg, "horizon_s": horizon, "due_at": cq.iso(T0),
            "anchor_at": cq.iso(anchor) if anchor else None}


def test_undecided_events_are_never_quoted(ro):
    """Capture must trail the funded decision, never race it."""
    add_event(ro, "e1", "MintA", decided=None)
    add_event(ro, "e2", "MintB", decided="2026-08-11T12:00:01+00:00")
    got = cq.due_episodes(ro, "uid-1", "solana", "2026-01-01")
    assert [e["fomo_event_id"] for e in got] == ["e2"]


def test_add_on_buys_and_other_chains_are_excluded(ro):
    add_event(ro, "e1", "MintA", first=0)
    add_event(ro, "e2", "MintB", net="4663")
    add_event(ro, "e3", "MintC")
    assert [e["fomo_event_id"] for e in cq.due_episodes(ro, "uid-1", "solana", "2026-01-01")] \
        == ["e3"]


def test_one_episode_per_mint(ro):
    """A re-buy on a coin already captured is not a new episode."""
    add_event(ro, "e1", "MintA", created="2026-08-11T12:00:00+00:00")
    add_event(ro, "e2", "MintA", created="2026-08-11T12:05:00+00:00")
    assert len(cq.due_episodes(ro, "uid-1", "solana", "2026-01-01")) == 1


def test_scheduling_is_idempotent(mdb, ro):
    add_event(ro, "e1", "MintA")
    ep = cq.due_episodes(ro, "uid-1", "solana", "2026-01-01")[0]
    cq.schedule_entry(mdb, ep, now=T0)
    cq.schedule_entry(mdb, ep, now=T0)
    assert mdb.execute("SELECT COUNT(*) FROM copy_quote_schedule").fetchone()[0] == 1


def test_exit_legs_are_anchored_to_the_entry_quote_not_the_source_event(mdb):
    """Horizons run from entry-quote completion. Anchoring to the source event would fold a slow
    funded hook into the horizon and silently mislabel every point on the curve."""
    anchor = T0 + timedelta(seconds=7)          # hook + quote took 7s
    cq.schedule_exits(mdb, _row(), anchor)
    rows = {r["horizon_s"]: r for r in mdb.execute(
        "SELECT horizon_s, due_at, anchor_at FROM copy_quote_schedule WHERE leg='exit'")}
    assert set(rows) == set(cq.EXIT_LEGS_S)
    assert cq.parse_iso(rows[5]["due_at"]) == anchor + timedelta(seconds=5)
    assert cq.parse_iso(rows[300]["due_at"]) == anchor + timedelta(seconds=300)
    assert cq.parse_iso(rows[5]["anchor_at"]) == anchor


def test_the_fast_window_is_dense_through_ninety_seconds(mdb):
    assert cq.FAST_LEGS_S[0] == 5 and cq.FAST_LEGS_S[-1] == 90
    assert all(b - a == 5 for a, b in zip(cq.FAST_LEGS_S, cq.FAST_LEGS_S[1:]))
    assert cq.COMMON_TAIL_S == (120, 300, 900, 1800, 3600, 21600)
    # PUMP20_POST60 baselines at 60s, so 12 of these legs are inside its blind spot
    assert sum(1 for h in cq.EXIT_LEGS_S if h < 60) == 11


def test_exit_quotes_use_the_exact_entry_quantity(mdb):
    """A round number would measure a different trade than the one we could have made."""
    cq.record(mdb, _row(), status="ok", started=T0, quoted=T0,
              quote={"input_asset": "USDC", "input_amount_atomic": "50000000",
                     "output_asset": "MintAAA", "output_amount_atomic": "123456789012345",
                     "route": "Raydium", "quote_id": "1"})
    assert cq.expected_tokens(mdb, "e1") == 123456789012345


def test_atomic_amounts_survive_as_text(mdb):
    """High-decimal tokens exceed SQLite's signed 64-bit INTEGER; a silently overflowed amount is
    worse than a missing one."""
    huge = "9" * 30
    cq.record(mdb, _row(), status="ok", started=T0, quoted=T0,
              quote={"input_asset": "USDC", "input_amount_atomic": "50000000",
                     "output_asset": "MintAAA", "output_amount_atomic": huge,
                     "route": None, "quote_id": None})
    stored = mdb.execute("SELECT output_amount_atomic FROM copy_quote_result").fetchone()[0]
    assert stored == huge
    assert cq.expected_tokens(mdb, "e1") == int(huge)


def test_a_failed_quote_is_a_terminal_row_not_a_gap(mdb):
    """A missing row and a failed quote are different facts and must stay distinguishable."""
    cq.record(mdb, _row(leg="exit", horizon=5, anchor=T0), status="failed", started=T0,
              quoted=T0 + timedelta(milliseconds=800), failure="provider_timeout")
    r = mdb.execute("SELECT * FROM copy_quote_result").fetchone()
    assert r["status"] == "failed" and r["failure_reason"] == "provider_timeout"
    assert r["output_amount_atomic"] is None
    sched = mdb.execute("SELECT status, attempts FROM copy_quote_schedule").fetchone()
    assert sched is None or sched["status"] == "failed"


def test_a_failed_entry_yields_no_quantity_so_exits_cannot_invent_one(mdb):
    cq.record(mdb, _row(), status="failed", started=T0, quoted=T0, failure="entry_quote_unavailable")
    assert cq.expected_tokens(mdb, "e1") is None


def test_the_funded_hook_delay_stays_visible_on_every_result(mdb):
    """The entry quote is taken AFTER the funded hook, so the hook's latency is inside it. Both
    delays are stored so a headline number can never hide the hook's contribution."""
    ep = {"received_at": "2026-08-11T12:00:00+00:00",
          "real_decision_at": "2026-08-11T12:00:02+00:00"}
    cq.record(mdb, _row(), status="ok", started=T0, quoted=T0 + timedelta(seconds=3),
              quote={"input_asset": "USDC", "input_amount_atomic": "1", "output_asset": "M",
                     "output_amount_atomic": "2", "route": None, "quote_id": None},
              ep=ep, anchor=T0)
    r = mdb.execute("SELECT * FROM copy_quote_result").fetchone()
    assert r["delay_source_to_quote_ms"] == pytest.approx(3000.0)
    assert r["delay_decision_to_quote_ms"] == pytest.approx(1000.0)
    assert r["quote_duration_ms"] == pytest.approx(3000.0)
    assert r["actual_elapsed_s"] == pytest.approx(3.0), "the REAL horizon, not the target"


def test_normalise_rejects_a_quote_without_an_output_amount():
    assert cq.normalise_quote({}, input_asset="A", output_asset="B") is None
    assert cq.normalise_quote({"outAmount": ""}, input_asset="A", output_asset="B") is None
    assert cq.normalise_quote(None, input_asset="A", output_asset="B") is None
    ok = cq.normalise_quote(
        {"inAmount": "50000000", "outAmount": "77", "contextSlot": 9,
         "routePlan": [{"swapInfo": {"label": "Raydium"}}, {"swapInfo": {"label": "Orca"}}]},
        input_asset="USDC", output_asset="M")
    assert ok["output_amount_atomic"] == "77" and ok["route"] == "Raydium>Orca"


def test_claim_due_returns_only_arrived_legs(mdb):
    cq.schedule_exits(mdb, _row(), T0, legs=(5, 900))
    assert [r["horizon_s"] for r in cq.claim_due(mdb, now=T0 + timedelta(seconds=10))] == [5]
    assert len(cq.claim_due(mdb, now=T0 + timedelta(seconds=901))) == 2


def _imported_names(path):
    """Every name the module actually imports, from the AST — so a docstring that WARNS about a
    forbidden symbol does not read as a use of it."""
    import ast
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(f"{node.module}.{a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
    return out


def test_the_capture_module_imports_no_execution_seam():
    """The capture module is pure logic: it must not reach the execution layer at all."""
    from pathlib import Path
    names = _imported_names(
        Path(__file__).resolve().parents[1] / "sniper/services/memecoin_signal_copy_quotes.py")
    assert not [n for n in names if "execution" in n], names


def test_the_worker_imports_quote_seams_only_never_a_signer():
    """The quote seams sit in the same module as `jupiter_swap_tx` and the signers. This process
    has no business constructing a transaction, so the boundary is asserted, not assumed."""
    from pathlib import Path
    names = _imported_names(
        Path(__file__).resolve().parents[1] / "scripts/memecoin_copy_quote_worker.py")
    seams = {n for n in names if n.startswith("sniper.execution")}
    assert seams == {
        "sniper.execution.solana_real_seams.jupiter_entry_quote",
        "sniper.execution.solana_real_seams._typed_jupiter_exit_quote",
        "sniper.execution.solana_real_seams.USDC_MINT",
        "sniper.execution.solana_real_seams._http",
        # Added 2026-08-27. Not a quote seam and not a signer: it declares WHICH Jupiter budget
        # this process spends. Without it the worker quoted on the funded key by default, which is
        # the exit brain's budget. The exact-set assertion is kept rather than loosened, so the
        # next name that appears here still has to be justified in writing.
        "sniper.execution.solana_real_seams.declare_measurement_process",
    }, seams
    for forbidden in ("jupiter_swap_tx", "_sign", "keypair", "send", "submit", "broadcast"):
        assert not [n for n in seams if forbidden in n], f"{forbidden!r} reached the worker"


def test_the_worker_reads_the_funded_ledger_read_only():
    from pathlib import Path
    names = _imported_names(
        Path(__file__).resolve().parents[1] / "scripts/memecoin_copy_quote_worker.py")
    assert "sniper.persistence.db_core.get_readonly_connection" in names
    assert "sniper.persistence.db_core.get_connection" not in names


def test_cohort_members_must_name_their_chain():
    """A wallet is armed, screened and captured PER CHAIN. A chainless member would pool them."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "w", Path(__file__).resolve().parents[1] / "scripts/memecoin_copy_quote_worker.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    assert w.parse_cohort("frankdegods:solana") == [("frankdegods", "solana")]
    assert w.parse_cohort(" a:solana , b:robin ") == [("a", "solana"), ("b", "robin")]
    with pytest.raises(SystemExit):
        w.parse_cohort("frankdegods")          # bare handle
    with pytest.raises(SystemExit):
        w.parse_cohort("")                     # empty cohort


def test_a_missing_cohort_wallet_is_fatal_not_skipped():
    """A silently shrunk cohort looks identical to a wallet that stopped trading."""
    import importlib.util, sqlite3
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "w2", Path(__file__).resolve().parents[1] / "scripts/memecoin_copy_quote_worker.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    ro = sqlite3.connect(":memory:")
    ro.execute("CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT)")
    ro.execute("INSERT INTO memecoin_wallets VALUES ('uid-1','present')")
    assert w.resolve_cohort(ro, "present:solana") == [("uid-1", "solana", "present")]
    with pytest.raises(SystemExit):
        w.resolve_cohort(ro, "present:solana,absent:solana")


def test_entry_quote_failures_are_typed_like_exit_failures():
    """An episode with no entry quote has no true entry and every exit leg is refused for it, so
    the REASON decides whether the coin was unquotable or our provider was throttled — opposite
    conclusions that an untyped 'entry_quote_unavailable' string could not tell apart."""
    assert cq.classify_quote_failure({"exception": TimeoutError()}) == "provider_timeout"
    assert cq.classify_quote_failure({"exception": OSError()}) == "rpc_transport"
    assert cq.classify_quote_failure({"response": (429, {})}) == "rate_limit"
    assert cq.classify_quote_failure({"response": (503, {})}) == "rpc_transport"
    assert cq.classify_quote_failure({"response": (200, {"error": "no route found"})}) == "no_route"
    assert cq.classify_quote_failure(
        {"response": (200, {"error": "not enough liquidity"})}) == "insufficient_liquidity"
    assert cq.classify_quote_failure({}) == "malformed_response"


def test_every_typed_failure_is_a_real_QuoteFailure_member():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    known = {e.value for e in QuoteFailure}
    for observed in ({"exception": TimeoutError()}, {"exception": OSError()},
                     {"response": (429, {})}, {"response": (503, {})},
                     {"response": (200, {"error": "no route"})}, {}):
        assert cq.classify_quote_failure(observed) in known


def test_the_common_grid_is_identical_for_every_wallet():
    """Cross-wallet comparison happens on the common grid. A per-wallet grid would make the
    sampling schedule a confounder — a wallet could 'win' by being measured nearer its own peak."""
    for key in cq.TAIL_EXTENSIONS_S:
        legs = set(cq.legs_for(key))
        assert set(cq.EXIT_LEGS_S) <= legs, f"{key} lost a common leg"
    # extensions only ever ADD, and only after the common grid ends
    for key, ext in cq.TAIL_EXTENSIONS_S.items():
        assert all(h > max(cq.EXIT_LEGS_S) for h in ext), f"{key} extension reaches into the grid"


def test_the_tail_reaches_six_hours_for_every_wallet():
    """The label is "did an exact sell of the acquired quantity reach 1.30x the executable entry
    cost". A 300s ceiling made that outcome structurally unresolvable, so the whole study read
    NOT ESTIMABLE no matter how many episodes accrued. Six hours is where the label lives."""
    assert cq.legs_for("anyone:solana")[-1] == 21600
    assert 21600 in cq.EXIT_LEGS_S


def test_no_wallet_gets_a_different_schedule():
    """Per-wallet tails made the sampling schedule a confounder in cross-wallet comparison."""
    assert cq.TAIL_EXTENSIONS_S == {}
    for who in ("frankdegods:solana", "CJdotsol:solana", "someone_new:robin"):
        assert cq.legs_for(who) == cq.EXIT_LEGS_S


def test_legs_are_deduplicated_and_ordered():
    cq.TAIL_EXTENSIONS_S["dup:test"] = (300, 300, 900)     # 300 already in the common grid
    try:
        legs = cq.legs_for("dup:test")
        assert len(legs) == len(set(legs)), "a repeated leg would double-schedule"
        assert list(legs) == sorted(legs)
    finally:
        cq.TAIL_EXTENSIONS_S.pop("dup:test")


def test_a_burst_of_episodes_is_refused_at_the_active_cap(mdb):
    """The cap fails toward LESS capture. Without it a signal burst becomes an unbounded fan-out."""
    for i in range(cq.MAX_ACTIVE_EPISODES):
        cq.schedule_entry(mdb, {"fomo_event_id": f"e{i}", "source_user_id": "uid-1",
                                "network_id": "solana", "mint": f"m{i}"}, now=T0)
    assert cq.active_episode_count(mdb) == cq.MAX_ACTIVE_EPISODES
    assert "max_active_episodes" in cq.admission_block(mdb, T0)


def test_the_daily_quote_budget_counts_failures_too(mdb):
    """A failed call cost the provider exactly what a successful one did."""
    for i in range(3):
        cq.record(mdb, _row(eid=f"e{i}", leg="exit", horizon=5, anchor=T0),
                  status="failed", started=T0, quoted=T0, failure="rate_limit")
    assert cq.quotes_today(mdb, T0) == 3
    assert cq.admission_block(mdb, T0, max_daily=3).startswith("daily_quote_budget")
    assert cq.admission_block(mdb, T0, max_daily=99) is None


def test_admission_gates_new_episodes_only_never_a_leg_in_flight(mdb):
    """An episode under way must be allowed to finish: a curve missing its middle is worse than
    an episode that never started. So the cap is checked before an ENTRY, never before an exit."""
    import inspect
    src = inspect.getsource(cq.admission_block)
    assert "new episode" in src.lower() or "NEW episode" in src
    # the schedule/claim path has no budget check at all
    assert "admission_block" not in inspect.getsource(cq.claim_due)
    assert "admission_block" not in inspect.getsource(cq.schedule_exits)


def test_prior_buyers_uses_RECEIPT_time_not_trade_time(ro):
    """Events arrive late (p99 lag 403s). A buy CREATED before our reference can still be
    unreceived when we decide, so counting by created_at credits knowledge we did not have.
    Correcting this moved the retrospective cell from 10 mints to 6."""
    # created early, received LATE - must not count at a 12:00 decision
    ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,side,"
               "is_first_buy,created_at,received_at,real_decision_at) VALUES (?,?,?,?,'buy',1,?,?,?)",
               ("late", "uid-late", "solana", "MintA", "2026-08-11T11:00:00+00:00",
                "2026-08-11T12:05:00+00:00", "x"))
    # created and received early - counts
    ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,side,"
               "is_first_buy,created_at,received_at,real_decision_at) VALUES (?,?,?,?,'buy',1,?,?,?)",
               ("ontime", "uid-ontime", "solana", "MintA", "2026-08-11T11:30:00+00:00",
                "2026-08-11T11:30:02+00:00", "x"))
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T12:00:00+00:00", "solana") == 1


def test_prior_buyers_is_scoped_to_the_chain(ro):
    """The same mint string exists on more than one chain. Chains are never pooled."""
    for net in ("solana", "4663"):
        ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,side,"
                   "is_first_buy,created_at,received_at,real_decision_at) "
                   "VALUES (?,?,?,?,'buy',1,?,?,?)",
                   (f"e-{net}", f"w-{net}", net, "MintA", "2026-08-11T11:00:00+00:00",
                    "2026-08-11T11:00:00+00:00", "x"))
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T12:00:00+00:00", "solana") == 1
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T12:00:00+00:00", "4663") == 1


def test_prior_buyers_counts_only_wallets_in_STRICTLY_BEFORE(ro):
    """Target leakage killed a prior signal on this system (+1,453bps -> +392 once the leak was
    removed). Buyers arriving AFTER the reference moment are the coin's own outcome."""
    for i, when in enumerate(("2026-08-11T11:59:00+00:00", "2026-08-11T11:59:30+00:00",
                              "2026-08-11T12:00:30+00:00")):
        ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,"
                   "side,is_first_buy,created_at,received_at,real_decision_at) "
                   "VALUES (?,?,?,?,'buy',1,?,?,?)",
                   (f"e{i}", f"uid-{i}", "solana", "MintA", when, when, when))
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T12:00:00+00:00", "solana") == 2
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T11:00:00+00:00", "solana") == 0


def test_prior_buyers_counts_wallets_not_buys(ro):
    for i, when in enumerate(("2026-08-11T11:00:00+00:00", "2026-08-11T11:30:00+00:00")):
        ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,"
                   "side,is_first_buy,created_at,received_at,real_decision_at) "
                   "VALUES (?,?,?,?,'buy',1,?,?,?)",
                   (f"e{i}", "uid-SAME", "solana", "MintA", when, when, when))
    assert cq.prior_buyer_count(ro, "MintA", "2026-08-11T12:00:00+00:00", "solana") == 1


def _coin_event(c, eid, mint, mcap, uid="uid-x", when="2026-08-11T12:00:00+00:00"):
    c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,side,"
              "is_first_buy,created_at,received_at,real_decision_at,their_market_cap) "
              "VALUES (?,?,?,?,'buy',1,?,?,?,?)",
              (eid, uid, "solana", mint, when, when, when, mcap))


def test_coin_filter_bounds_market_cap_and_consensus(ro):
    # 5 prior buyers on MintGood, 1 on MintLonely, 20 on MintCrowded
    for tag, n in (("Good", 5), ("Lonely", 1), ("Crowded", 20)):
        for i in range(n):
            ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,"
                       "mint,side,is_first_buy,created_at,received_at,real_decision_at) "
                       "VALUES (?,?,?,?,'buy',1,?,?,?)",
                       (f"prior-{tag}-{i}", f"w{i}", "solana", f"Mint{tag}",
                        "2026-08-11T11:00:00+00:00", "2026-08-11T11:00:00+00:00", "x"))
    _coin_event(ro, "sig-good", "MintGood", 500_000)
    _coin_event(ro, "sig-lonely", "MintLonely", 500_000)
    _coin_event(ro, "sig-crowded", "MintCrowded", 500_000)
    _coin_event(ro, "sig-big", "MintBig", 9_000_000)
    got = {e["mint"] for e in cq.coin_filter_episodes(ro, "solana", "2026-01-01")}
    assert got == {"MintGood"}, got


def test_coin_filter_ignores_wallet_identity(ro):
    """This cohort tests a COIN property. Mixing a wallet screen in would make the two
    inseparable in the results."""
    import inspect
    src = inspect.getsource(cq.coin_filter_episodes)
    assert "source_user_id = ?" not in src and "curated" not in src


def test_cohort_provenance_is_stored_not_inferred(mdb):
    """Wallet and coin-filter episodes answer different questions and must never be pooled."""
    cq.schedule_entry(mdb, {"fomo_event_id": "e1", "source_user_id": "u", "network_id": "solana",
                            "mint": "M1"}, now=T0, cohort="wallet")
    cq.schedule_entry(mdb, {"fomo_event_id": "e2", "source_user_id": "u", "network_id": "solana",
                            "mint": "M2", "prior_buyers": 6, "their_market_cap": 420_000.0},
                      now=T0, cohort="coin_filter")
    rows = {r["origin_event_id"]: r for r in mdb.execute(
        "SELECT origin_event_id, cohort, prior_buyers, mcap_at_signal FROM copy_quote_schedule")}
    assert rows["e1"]["cohort"] == "wallet"
    assert rows["e2"]["cohort"] == "coin_filter"
    assert rows["e2"]["prior_buyers"] == 6
    assert rows["e2"]["mcap_at_signal"] == 420_000.0


def test_coin_filter_waives_the_decision_gate_for_off_roster_wallets(ro):
    """The feed is GLOBAL - 6,446 wallets appear in events, only 221 are on the roster, and
    real_decision_at is set exclusively for roster wallets. Requiring it unconditionally would
    restrict a COIN cohort to a 221-wallet sample, which is the wallet selection this cohort
    exists to remove. Off-roster wallets never reach the funded hook, so there is nothing to
    race and nothing to trail."""
    ro.execute("INSERT INTO memecoin_wallets VALUES ('uid-roster','onroster')")
    for tag in ("Rost", "Off"):
        for i in range(5):
            ro.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,"
                       "side,is_first_buy,created_at,received_at,real_decision_at) "
                       "VALUES (?,?,?,?,'buy',1,?,?,?)",
                       (f"p-{tag}-{i}", f"w{i}", "solana", f"Mint{tag}",
                        "2026-08-11T11:00:00+00:00", "2026-08-11T11:00:00+00:00", "x"))
    # roster wallet WITHOUT a decision -> excluded; off-roster WITHOUT one -> included
    _coin_event(ro, "sig-roster-undecided", "MintRost", 500_000, uid="uid-roster")
    ro.execute("UPDATE memecoin_events SET real_decision_at=NULL "
               "WHERE fomo_event_id='sig-roster-undecided'")
    _coin_event(ro, "sig-offroster", "MintOff", 500_000, uid="uid-stranger")
    got = {e["mint"] for e in cq.coin_filter_episodes(ro, "solana", "2026-01-01")}
    assert got == {"MintOff"}, got
    # once the roster wallet IS decided it qualifies too
    ro.execute("UPDATE memecoin_events SET real_decision_at='2026-08-11T12:00:01+00:00' "
               "WHERE fomo_event_id='sig-roster-undecided'")
    got = {e["mint"] for e in cq.coin_filter_episodes(ro, "solana", "2026-01-01")}
    assert got == {"MintOff", "MintRost"}, got


def test_exit_legs_inherit_cohort_provenance_from_the_entry(mdb):
    """Exit legs written without provenance carried cohort=NULL, so a coin-filter episode's 20
    legs pooled with the wallet cohort's — the one thing the cohort column exists to prevent.
    The earlier provenance test only checked ENTRY rows and passed while this was broken."""
    cq.schedule_entry(mdb, {"fomo_event_id": "e1", "source_user_id": "u", "network_id": "solana",
                            "mint": "M1", "prior_buyers": 6, "their_market_cap": 420_000.0},
                      now=T0, cohort="coin_filter")
    cq.schedule_exits(mdb, _row(), T0)
    rows = list(mdb.execute("SELECT leg, cohort, prior_buyers, mcap_at_signal, spec_version "
                            "FROM copy_quote_schedule WHERE origin_event_id='e1'"))
    assert len(rows) == 1 + len(cq.EXIT_LEGS_S)
    assert {r["cohort"] for r in rows} == {"coin_filter"}, "an exit leg lost its cohort"
    assert {r["prior_buyers"] for r in rows} == {6}
    assert {r["spec_version"] for r in rows} == {cq.SPEC_VERSION}
