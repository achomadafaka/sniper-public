import sqlite3
import importlib

from sniper.persistence import db_memecoin as m


class _CaptureProxy:
    def __getattr__(self, name):
        return getattr(importlib.import_module("sniper.services.memecoin_capture_v2"), name)


capture = _CaptureProxy()


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _stats(network, mint, *, pair="pair-a", price=1.0, liquidity=1000.0):
    chain = {"solana": "solana", "8453": "base", "1": "ethereum"}[network]
    return {
        "price": price, "liquidity": liquidity, "market_cap": 1_000_000,
        "volume_24h": 50_000, "chain": chain, "base_address": mint,
        "pair_address": pair, "dex_id": "dex",
    }


def test_register_cohort_merges_contexts_and_keeps_chains_separate():
    conn = _conn()
    capture.register_capture_cohort(
        conn, "8453", "0xabc", "signal", "2026-07-11T00:00:00+00:00",
        "2026-07-12T00:00:00+00:00", 100,
    )
    capture.register_capture_cohort(
        conn, "8453", "0xabc", "position", "2026-07-11T00:01:00+00:00",
        "2026-07-13T00:00:00+00:00", 400,
    )
    capture.register_capture_cohort(
        conn, "1", "0xabc", "control", "2026-07-11T00:01:00+00:00",
        "2026-07-12T00:00:00+00:00", 250,
    )
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 ORDER BY network_id",
    )]
    assert len(rows) == 2
    base = next(r for r in rows if r["network_id"] == "8453")
    assert base["contexts"] == "position,signal"
    assert base["priority"] == 400
    assert base["expires_at"] == "2026-07-13T00:00:00+00:00"


def test_sweep_budget_prioritizes_and_counts_skipped_denominator():
    conn = _conn()
    now = "2026-07-11T01:00:00+00:00"
    for network, mint, context, priority in [
        ("solana", "signal", "signal", 100),
        ("solana", "control", "control", 250),
        ("solana", "position", "position", 400),
    ]:
        capture.register_capture_cohort(
            conn, network, mint, context, now, "2026-07-12T00:00:00+00:00", priority,
        )

    seen = []

    def stats(keys):
        seen.extend(keys)
        return {(network, mint): _stats(network, mint) for network, mint in keys}

    result = capture.capture_v2_sweep(
        conn, now, stats, max_mints=2, sweep_id="sweep-1", period_s=30,
    )
    assert seen == [("solana", "position"), ("solana", "control")]
    assert result.requested == 2 and result.budget_skipped == 1 and result.captured == 2
    skipped = conn.execute(
        "SELECT skipped_sweep_count FROM memecoin_capture_cohorts_v2 WHERE mint='signal'",
    ).fetchone()[0]
    assert skipped == 1
    sweep = conn.execute("SELECT * FROM memecoin_quote_sweeps_v2 WHERE sweep_id='sweep-1'").fetchone()
    assert sweep["eligible_count"] == 3 and sweep["budget_skipped_count"] == 1


def test_separate_sweeps_and_same_address_on_two_chains_persist():
    conn = _conn()
    for network in ("8453", "1"):
        capture.register_capture_cohort(
            conn, network, "0xabc", "signal", "2026-07-11T00:00:00+00:00",
            "2026-07-12T00:00:00+00:00", 100,
        )

    def stats(keys):
        return {(network, mint): _stats(network, mint, price=1.0 if network == "8453" else 2.0)
                for network, mint in keys}

    capture.capture_v2_sweep(
        conn, "2026-07-11T01:00:00+00:00", stats, 10, sweep_id="s1", period_s=30,
    )
    capture.capture_v2_sweep(
        conn, "2026-07-11T01:00:31+00:00", stats, 10, sweep_id="s2", period_s=30,
    )
    rows = [dict(r) for r in conn.execute(
        "SELECT sweep_id, network_id, price FROM memecoin_quote_observations_v2 ORDER BY sweep_id, network_id",
    )]
    assert len(rows) == 4
    assert {(r["network_id"], r["price"]) for r in rows} == {("8453", 1.0), ("1", 2.0)}


def test_pair_change_requires_two_consecutive_observations():
    conn = _conn()
    capture.register_capture_cohort(
        conn, "solana", "M", "signal", "2026-07-11T00:00:00+00:00",
        "2026-07-12T00:00:00+00:00", 100,
    )
    sequence = iter(["pair-a", "pair-b", "pair-b"])

    def stats(keys):
        pair = next(sequence)
        return {("solana", "M"): _stats("solana", "M", pair=pair)}

    for i, sweep in enumerate(("s1", "s2", "s3")):
        capture.capture_v2_sweep(
            conn, f"2026-07-11T01:0{i}:00+00:00", stats, 10, sweep_id=sweep, period_s=30,
        )
    qualities = [r[0] for r in conn.execute(
        "SELECT quality FROM memecoin_quote_observations_v2 ORDER BY id",
    )]
    assert qualities == ["ok", "pair_changed", "ok"]
    cohort = conn.execute(
        "SELECT canonical_pair_address, pending_pair_address, pending_pair_count "
        "FROM memecoin_capture_cohorts_v2 WHERE network_id='solana' AND mint='M'",
    ).fetchone()
    assert tuple(cohort) == ("pair-b", None, 0)


def test_ratio_outlier_and_missing_pair_are_retained_but_not_last_good():
    conn = _conn()
    capture.register_capture_cohort(
        conn, "solana", "M", "signal", "2026-07-11T00:00:00+00:00",
        "2026-07-12T00:00:00+00:00", 100,
    )
    outputs = iter([
        {("solana", "M"): _stats("solana", "M", price=1.0)},
        {("solana", "M"): _stats("solana", "M", price=2000.0)},
        {},
    ])
    for i, sweep in enumerate(("s1", "s2", "s3")):
        capture.capture_v2_sweep(
            conn, f"2026-07-11T01:0{i}:00+00:00", lambda _keys: next(outputs), 10,
            sweep_id=sweep, period_s=30,
        )
    rows = [dict(r) for r in conn.execute(
        "SELECT quality, price FROM memecoin_quote_observations_v2 ORDER BY id",
    )]
    assert [r["quality"] for r in rows] == ["ok", "ratio_outlier", "missing_pair"]
    assert rows[-1]["price"] is None
    last_good = conn.execute(
        "SELECT last_good_price FROM memecoin_capture_cohorts_v2 WHERE mint='M'",
    ).fetchone()[0]
    assert last_good == 1.0


def test_capture_pass_registers_open_signal_and_position_cohorts():
    conn = _conn()
    m.create_signal_outcome(
        conn, fomo_event_id="e1", source_user_id="u", handle="h", mint="S",
        ticker="S", buy_ts="2026-07-11T00:00:00+00:00", ref_price=1.0,
        their_size_usd=1000.0, now_iso="2026-07-11T00:00:00+00:00", network_id="solana",
    )
    m.open_position(
        conn, source_user_id="u", source_handle="h", mint="P", ticker="P",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="x", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-07-11T00:00:00+00:00", network_id="solana",
    )

    def stats(keys):
        return {(network, mint): _stats(network, mint) for network, mint in keys}

    result = capture.capture_v2_pass(
        conn, "2026-07-11T01:00:00+00:00", stats_batch_fn=stats, max_mints=10,
        sweep_id="pass-1", period_s=30,
    )
    assert result.requested == 2
    rows = {r["mint"]: r["contexts"] for r in conn.execute(
        "SELECT mint, contexts FROM memecoin_capture_cohorts_v2",
    )}
    assert rows == {"P": "position", "S": "signal"}


def test_capture_pass_registers_v2_only_signal_and_assigns_due_mark():
    conn = _conn()
    capture.create_outcome_v2(
        conn, upstream_event_id="v2", source_user_id="u", handle="h",
        network_id="solana", mint="S", ticker="S",
        signal_observed_at="2026-07-11T00:00:00+00:00", ref_price=1.0,
        horizons=(60,), created_at="2026-07-11T00:00:00+00:00",
    )

    result = capture.capture_v2_pass(
        conn, "2026-07-11T00:01:00+00:00",
        stats_batch_fn=lambda keys: {
            (network, mint): _stats(network, mint, price=1.2) for network, mint in keys
        },
        max_mints=10, sweep_id="v2-pass", period_s=30,
    )

    assert result.requested == 1
    assert conn.execute(
        "SELECT status FROM memecoin_horizon_marks_v2 WHERE target_s=60",
    ).fetchone()[0] == "observed"
    assert conn.execute(
        "SELECT status FROM memecoin_signal_outcomes_v2 WHERE upstream_event_id='v2'",
    ).fetchone()[0] == "complete"
