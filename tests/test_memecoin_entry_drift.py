from __future__ import annotations

import math
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.execution.entry_attribution import bind_quote_metadata, buy_drift
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.execution.solana_wallet import TokenBalanceSnapshot
from sniper.execution.solana_swap_executor import execute_entry
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services.memecoin_entry_trace import EntryAttemptTrace
from sniper.services.memecoin_real_leg import real_entry
from sniper.services.memecoin_real_runner import _finish_trace_for_result
from sniper.settings import memecoin as cfg


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _reverse_quote(_mint, amount_atomic):
    """A sell-route stub, shared by fixtures with different clips and decimals.

    It only ever had to prove a route EXISTS, so it returned `outAmount: "1"` -- one USDC atomic
    back for the whole bag. The entry round-trip cap reads this same quote now, and one atomic is
    a 100% loss, so every fixture using it would abort. The rate below is deliberately generous:
    these tests are about drift and fill accounting, and the stub must stay out of the way of a
    gate none of them are exercising.
    """
    # The fixtures span 10 to 500,000,000 token atomic against $10-$12 clips, so no single
    # rate is neutral for all of them. A $100 floor is unambiguously "not a bad round trip"
    # at every one of those clips.
    out = str(max(int(amount_atomic) * 100, 100_000_000))
    return {
        "inAmount": str(amount_atomic),
        "outAmount": out,
        "otherAmountThreshold": out,
    }


def test_buy_drift_signs_are_explicit_and_each_metric_is_independent():
    assert buy_drift(100.0, 110.0, 121.0) == {
        "source_drift_pct": pytest.approx(10.0),
        "execution_slippage_pct": pytest.approx(10.0),
        "total_copy_drift_pct": pytest.approx(21.0),
    }
    assert buy_drift(100.0, 90.0, 81.0) == {
        "source_drift_pct": pytest.approx(-10.0),
        "execution_slippage_pct": pytest.approx(-10.0),
        "total_copy_drift_pct": pytest.approx(-19.0),
    }
    assert buy_drift(100.0, None, 121.0) == {
        "source_drift_pct": None,
        "execution_slippage_pct": None,
        "total_copy_drift_pct": pytest.approx(21.0),
    }
    for bad in (0.0, -1.0, math.nan, math.inf):
        assert buy_drift(bad, 1.0, 1.0)["source_drift_pct"] is None


def test_quote_id_hashes_only_canonical_public_execution_fields():
    base = {
        "sellAmount": "10000000",
        "buyAmount": "500000000",
        "transaction": {"to": "0xRouter", "data": "0xsecret-calldata"},
        "taker": "0xwallet",
    }
    a = bind_quote_metadata(
        dict(base), network_id="8453", mint="0xABC",
        quote_observed_at="2026-07-15T12:00:00+00:00",
        sell_decimals=6, buy_decimals=8, sell_token_usd=1.0,
    )
    b = bind_quote_metadata(
        {**base, "transaction": {"to": "0xrouter", "data": "0xdifferent"},
         "taker": "0xother"},
        network_id="8453", mint="0xabc",
        quote_observed_at="2026-07-15T12:00:00+00:00",
        sell_decimals=6, buy_decimals=8, sell_token_usd=1.0,
    )
    assert a["_sniper_quote_id"] == b["_sniper_quote_id"]
    assert a["_sniper_quote_id"].startswith("q1:")
    assert a["_sniper_quote_price"] == pytest.approx(2.0)
    changed = bind_quote_metadata(
        {**base, "buyAmount": "500000001"}, network_id="8453", mint="0xabc",
        quote_observed_at="2026-07-15T12:00:00+00:00",
        sell_decimals=6, buy_decimals=8, sell_token_usd=1.0,
    )
    assert changed["_sniper_quote_id"] != a["_sniper_quote_id"]


@pytest.mark.parametrize(("network_id", "sell_amount", "sell_decimals", "sell_usd", "expected"), [
    ("8453", "10000000", 6, 1.0, 2.0),
    ("56", "10000000000000000000", 18, 1.0, 2.0),
    ("4663", "1000000000000000000", 18, 3000.0, 600.0),
])
def test_evm_quote_price_uses_chain_quote_decimals_and_existing_usd_snapshot(
    network_id, sell_amount, sell_decimals, sell_usd, expected,
):
    quote = bind_quote_metadata(
        {"sellAmount": sell_amount, "buyAmount": "500000000",
         "transaction": {"to": "0xrouter"}},
        network_id=network_id, mint="0xtoken",
        quote_observed_at="2026-07-15T12:00:00+00:00",
        sell_decimals=sell_decimals, buy_decimals=8, sell_token_usd=sell_usd,
    )
    assert quote["_sniper_quote_price"] == pytest.approx(expected)


def test_executor_uses_pending_exact_price_not_latest_trader_event():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=12.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="solana",
        origin_event_id="exact-event", attempt_id="attempt-1", signal_source="global_feed",
        source_price=0.02, source_market_cap=1_000_000.0,
        observed_at="2026-07-15T11:59:59+00:00", clock_domain="observed_real_utc",
    )
    m.insert_event(conn, {
        "fomo_event_id": "newer", "source_user_id": "trader", "source_handle": "h",
        "mint": "MINT", "ticker": "M", "side": "buy", "their_price": 9.0,
        "their_market_cap": 9_000_000.0, "their_size_usd": 1.0, "is_first_buy": False,
        "created_at": "2026-07-15T12:00:01+00:00", "received_at": "2026-07-15T12:00:01+00:00",
        "lag_s": 0.0, "signal_source": "global_feed", "network_id": "solana",
        "received_at_normalized": "2026-07-15T12:00:01+00:00",
        "clock_domain": "observed_real_utc",
    })
    usdc = iter((100.0, 88.0))
    token = iter(((0.0, 6), (500.0, 6)))
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=12.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:02+00:00",
        quote_fn=lambda _mint, _usd: {
            "inAmount": "10000000", "outAmount": "500000000",
            "otherAmountThreshold": "470000000",
            "priceImpactPct": "0.01", "routePlan": [{"swapInfo": {"ammKey": "AMM1"}}],
        },
        reverse_quote_fn=_reverse_quote,
        sign_fn=lambda _quote: (b"signed", "sig"), send_fn=lambda _raw: True,
        confirm_fn=lambda _sig: "confirmed", usdc_fn=lambda: next(usdc),
        token_fn=lambda _mint: next(token),
    )
    assert result["status"] == "filled"
    assert result["fill_id"] > 0
    position = m.get_position(conn, result["position_id"])
    assert position["origin_event_id"] == "exact-event"
    assert position["attempt_id"] == "attempt-1"
    assert position["their_price"] == pytest.approx(0.02)
    assert position["quote_price"] == pytest.approx(0.02)
    assert position["fill_gap_pct"] == pytest.approx(20.0)
    assert position["total_copy_drift_pct"] == pytest.approx(20.0)
    assert position["price_attribution"] == "exact_event"
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE id=?", (result["fill_id"],)).fetchone())
    assert (fill["attempt_id"], fill["origin_event_id"], fill["network_id"],
            fill["signal_source"]) == ("attempt-1", "exact-event", "solana", "global_feed")
    assert fill["execution_slippage_pct"] == pytest.approx(20.0)


def test_evm_executor_persists_exact_origin_quote_and_fill_drift():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="0xtoken", source_user_id="trader", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="8453",
        origin_event_id="evm-event", attempt_id="evm-attempt", signal_source="follow_activity",
        source_price=1.8, source_market_cap=2_000_000.0,
    )
    usdc = iter((100.0, 90.0))
    token = iter(((0.0, 8), (5.0, 8)))
    result = execute_entry_evm(
        conn, pending_id=pending_id, mint="0xtoken", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader", now_iso="2026-07-15T12:00:01+00:00",
        quote_fn=lambda _mint, _usd: {
            "sellAmount": "10000000", "buyAmount": "500000000",
            "transaction": {"to": "0xRouter", "data": "0xdead"},
            "issues": {"allowance": None},
        },
        approve_fn=lambda *_args: True, sign_fn=lambda _tx: ("0xraw", "0xhash", 7),
        send_fn=lambda _raw: "0xhash", confirm_fn=lambda _hash: "confirmed",
        usdc_fn=lambda: next(usdc), token_fn=lambda _mint: next(token),
        sell_decimals=6, sell_token_usd=1.0,
    )
    assert result["status"] == "filled"
    position = m.get_position(conn, result["position_id"])
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE id=?", (result["fill_id"],)).fetchone())
    assert position["origin_event_id"] == fill["origin_event_id"] == "evm-event"
    assert position["attempt_id"] == fill["attempt_id"] == "evm-attempt"
    assert position["quote_price"] == fill["quote_price"] == pytest.approx(2.0)
    assert fill["source_drift_pct"] == pytest.approx((2.0 / 1.8 - 1.0) * 100.0)
    assert fill["execution_slippage_pct"] == pytest.approx(0.0)
    assert fill["total_copy_drift_pct"] == pytest.approx((2.0 / 1.8 - 1.0) * 100.0)


def test_evm_failed_fill_keeps_independent_source_drift_only():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="0xtoken", source_user_id="trader", strategy="alpha_b", notional=10.0,
        reserved_at="t", network_id="8453", source_price=1.8,
    )
    result = execute_entry_evm(
        conn, pending_id=pending_id, mint="0xtoken", network_id="8453", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader", now_iso="2026-07-15T12:00:01+00:00",
        quote_fn=lambda *_: {
            "sellAmount": "10000000", "buyAmount": "500000000",
            "transaction": {"to": "0xrouter"}, "issues": {"allowance": None},
        },
        approve_fn=lambda *_: True, sign_fn=lambda _: ("0xraw", "0xhash", 7),
        send_fn=lambda _: "0xhash", confirm_fn=lambda _: "failed",
        usdc_fn=lambda: 100.0, token_fn=lambda _: (0.0, 8),
        sell_decimals=6, sell_token_usd=1.0,
    )
    assert result["source_drift_pct"] == pytest.approx((2.0 / 1.8 - 1.0) * 100.0)
    assert result["execution_slippage_pct"] is None
    assert result["total_copy_drift_pct"] is None


def test_legacy_reservation_uses_filtered_fallback_and_labels_it():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="solana",
    )
    for event_id, user, price in (("wanted", "trader", 1.0), ("other", "other", 99.0)):
        m.insert_event(conn, {
            "fomo_event_id": event_id, "source_user_id": user, "source_handle": "h",
            "mint": "MINT", "ticker": "M", "side": "buy", "their_price": price,
            "their_market_cap": 1.0, "their_size_usd": 1.0, "is_first_buy": False,
            "created_at": "2026-07-15T12:00:00+00:00", "received_at": "2026-07-15T12:00:00+00:00",
            "lag_s": 0.0, "signal_source": "global_feed", "network_id": "solana",
            "received_at_normalized": "2026-07-15T12:00:00+00:00",
            "clock_domain": "observed_real_utc",
        })
    balances = iter((10.0, 0.0))
    tokens = iter((0.0, 10.0))
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:01+00:00",
        quote_fn=lambda *_: {
            "inAmount": "10000000", "outAmount": "10",
            "otherAmountThreshold": "9", "priceImpactPct": 0,
        },
        reverse_quote_fn=_reverse_quote,
        sign_fn=lambda _: (b"raw", "sig"), send_fn=lambda _: True,
        confirm_fn=lambda _: "confirmed", usdc_fn=lambda: next(balances),
        token_fn=lambda _: next(tokens),
    )
    position = m.get_position(conn, result["position_id"])
    assert position["their_price"] == 1.0
    assert position["price_attribution"] == "legacy_source_chain"


class _Clock:
    def __init__(self):
        self.value = 100.0

    def perf_counter(self):
        return self.value

    def advance(self, seconds=0.01):
        self.value += seconds


def test_real_leg_links_trace_position_fill_and_latency_with_monotonic_stages(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    conn = _conn()
    ev = SimpleNamespace(
        fomo_event_id="event-linked", source_user_id="trader", source_handle="alice",
        signal_source="global_feed", network_id="solana", mint="MINT", ticker="M",
        side="buy", their_price=0.02, their_market_cap=1_000_000.0,
        created_at="2026-07-15T11:59:59+00:00",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc",
    )
    clock = _Clock()
    trace = EntryAttemptTrace.start(conn, ev, "2026-07-15T12:00:00+00:00", clock)
    usdc = iter((100.0, 90.0))
    token = iter(((0.0, 6), (500.0, 6)))

    def executor(c, pending_id, mint, size, now_iso, impact_cap_pct=None, trace=None):
        def step(value):
            clock.advance()
            return value
        return execute_entry(
            c, pending_id=pending_id, mint=mint, usd_amount=size, strategy="alpha_b",
            source_user_id="trader", now_iso=now_iso,
            quote_fn=lambda *_: step({
                "inAmount": "10000000", "outAmount": "500000000",
                "otherAmountThreshold": "470000000",
                "priceImpactPct": 0, "routePlan": [{"swapInfo": {"ammKey": "AMM"}}],
            }),
            reverse_quote_fn=lambda mint, amount: step(_reverse_quote(mint, amount)),
            sign_fn=lambda _: step((b"raw", "sig")), send_fn=lambda _: step(True),
            confirm_fn=lambda _: step("confirmed"), usdc_fn=lambda: next(usdc),
            token_fn=lambda _: next(token), trace=trace,
        )

    result = real_entry(
        conn, ev, day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
        now_iso="2026-07-15T12:00:00+00:00", trade_size=10.0,
        token_safety_fn=lambda _: (True, "ok"), execute_fn=executor,
        kill_path=str(tmp_path / "kill"), clock=clock.perf_counter,
        impact_cap_pct=3.0, trace=trace,
    )
    _finish_trace_for_result(trace, result, identity=SimpleNamespace(network_id="solana"))
    assert result["status"] == "filled"
    position = m.get_position(conn, result["position_id"])
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE id=?", (result["fill_id"],)).fetchone())
    latency = dict(conn.execute("SELECT * FROM memecoin_exec_latency").fetchone())
    attempt = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    for row in (position, fill, latency, attempt):
        assert row["attempt_id"] == trace.attempt_id
    for row in (position, fill, latency, attempt):
        assert row["quote_id"] == result["quote_id"]
    assert attempt["quote_completed_at"] < attempt["signing_completed_at"]
    assert attempt["signing_completed_at"] < attempt["transaction_submitted_at"]
    assert attempt["transaction_submitted_at"] < attempt["chain_confirmation_completed_at"]
    assert attempt["chain_confirmation_completed_at"] <= attempt["balance_delta_completed_at"]
    assert attempt["balance_delta_completed_at"] <= attempt["persistence_completed_at"]
    assert attempt["quote_age_ms"] > 0
    assert attempt["terminal_status"] == "filled"


def test_no_quote_keeps_unreached_trace_stages_null():
    conn = _conn()
    clock = _Clock()
    ev = SimpleNamespace(
        fomo_event_id="event-no-quote", source_user_id="trader", source_handle="alice",
        signal_source="global_feed", network_id="solana", mint="MINT", ticker="M", side="buy",
        their_price=1.0, their_market_cap=1.0, created_at="2026-07-15T11:59:59+00:00",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc",
    )
    trace = EntryAttemptTrace.start(conn, ev, "2026-07-15T12:00:00+00:00", clock)
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id="solana",
        attempt_id=trace.attempt_id, origin_event_id=ev.fomo_event_id, source_price=1.0,
    )
    clock.advance()
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:00+00:00",
        quote_fn=lambda *_: None, sign_fn=lambda _: None, send_fn=lambda _: None,
        reverse_quote_fn=_reverse_quote,
        confirm_fn=lambda _: "unknown", usdc_fn=lambda: 0.0, token_fn=lambda _: 0.0,
        trace=trace,
    )
    trace.finish(result["status"], result["reason"], pending_id=pending_id)
    row = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    assert row["quote_completed_at"] is not None
    assert row["signing_completed_at"] is None
    assert row["transaction_submitted_at"] is None
    assert row["chain_confirmation_completed_at"] is None
    assert row["balance_delta_completed_at"] is None
    assert row["persistence_completed_at"] is None


@pytest.mark.parametrize("venue", ["solana", "evm"])
def test_unknown_confirmation_keeps_chain_confirmation_and_quote_age_null(venue):
    conn = _conn()
    clock = _Clock()
    network_id = "solana" if venue == "solana" else "8453"
    mint = "MINT" if venue == "solana" else "0xtoken"
    ev = SimpleNamespace(
        fomo_event_id=f"event-{venue}-unknown", source_user_id="trader",
        source_handle="alice", signal_source="global_feed", network_id=network_id,
        mint=mint, ticker="M", side="buy", their_price=1.0, their_market_cap=1.0,
        created_at="2026-07-15T11:59:59+00:00",
        received_at_normalized="2026-07-15T11:59:59.900000+00:00",
        clock_domain="observed_real_utc",
    )
    trace = EntryAttemptTrace.start(conn, ev, "2026-07-15T12:00:00+00:00", clock)
    pending_id = m.reserve_pending(
        conn, mint=mint, source_user_id="trader", strategy="alpha_b", notional=10.0,
        reserved_at="2026-07-15T12:00:00+00:00", network_id=network_id,
        attempt_id=trace.attempt_id, origin_event_id=ev.fomo_event_id, source_price=1.0,
    )
    if venue == "solana":
        result = execute_entry(
            conn, pending_id=pending_id, mint=mint, usd_amount=10.0, strategy="alpha_b",
            source_user_id="trader", now_iso="2026-07-15T12:00:00+00:00",
            quote_fn=lambda *_: {
                "inAmount": "10000000", "outAmount": "10000000", "priceImpactPct": 0,
                "otherAmountThreshold": "9000000",
            },
            reverse_quote_fn=_reverse_quote,
            sign_fn=lambda _: (b"raw", "sig"), send_fn=lambda _: True,
            confirm_fn=lambda _: "unknown", usdc_fn=lambda: 100.0,
            token_fn=lambda _: (0.0, 6), trace=trace,
        )
    else:
        result = execute_entry_evm(
            conn, pending_id=pending_id, mint=mint, network_id=network_id, usd_amount=10.0,
            strategy="alpha_b", source_user_id="trader",
            now_iso="2026-07-15T12:00:00+00:00",
            quote_fn=lambda *_: {
                "sellAmount": "10000000", "buyAmount": "10000000",
                "transaction": {"to": "0xrouter"}, "issues": {"allowance": None},
            },
            approve_fn=lambda *_: True, sign_fn=lambda _: ("0xraw", "0xhash", 7),
            send_fn=lambda _: "0xhash", confirm_fn=lambda _: "unknown",
            usdc_fn=lambda: 100.0, token_fn=lambda _: (0.0, 6), trace=trace,
            sell_decimals=6, sell_token_usd=1.0,
        )
    trace.finish(result["status"], result.get("reason") or result["status"],
                 pending_id=pending_id, quote_age_ms=result.get("quote_age_ms"))
    row = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    assert row["transaction_submitted_at"] is not None
    assert row["chain_confirmation_completed_at"] is None
    assert row["chain_confirmation_ms"] is None
    assert row["quote_age_ms"] is None
    assert m.pending_row(conn, pending_id)["status"] == "reserved"


@pytest.mark.parametrize("atomic_delta", [1, 10, 100])
def test_solana_fill_subtracts_atomic_balances_before_decimal_scaling(atomic_delta):
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="trader", strategy="alpha_b", notional=1.0,
        reserved_at="t", network_id="solana", source_price=1.0,
    )
    base = 9_007_199_254_740_993_000
    snapshots = iter((
        TokenBalanceSnapshot(atomic=base, decimals=6),
        TokenBalanceSnapshot(atomic=base + atomic_delta, decimals=6),
    ))
    usd = iter((100.0, 99.0))
    result = execute_entry(
        conn, pending_id=pending_id, mint="MINT", usd_amount=1.0, strategy="alpha_b",
        source_user_id="trader", now_iso="2026-07-15T12:00:00+00:00",
        quote_fn=lambda *_: {
            "inAmount": "1000000", "outAmount": str(atomic_delta), "priceImpactPct": 0,
            "otherAmountThreshold": str(atomic_delta),
        },
        reverse_quote_fn=_reverse_quote,
        sign_fn=lambda _: (b"raw", "sig"), send_fn=lambda _: True,
        confirm_fn=lambda _: "confirmed", usdc_fn=lambda: next(usd),
        token_fn=lambda _: next(snapshots),
    )
    assert result["status"] == "filled"
    expected_got = atomic_delta / 10**6
    fill = dict(conn.execute("SELECT * FROM memecoin_fills WHERE id=?", (result["fill_id"],)).fetchone())
    assert fill["qty"] == expected_got
    assert fill["price"] == pytest.approx(1.0 / expected_got)
