"""A $175 native buying buffer must stay usable after fees and account deposits."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_gas_topup as topup
from sniper.settings import memecoin as cfg


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    for key, value in {
        "MEMECOIN_GAS_ENTRY_SOL": .002, "MEMECOIN_GAS_EXIT_SOL": .0004,
        "MEMECOIN_GAS_RETRY_FACTOR": 2,
        "MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD": 5.0,
        "MEMECOIN_SOL_EXEC_BUFFER_CAP_USD": 175.0,
        "MEMECOIN_SOLANA_SLOT_LANE_ENABLED": "1",
        "MEMECOIN_SOLANA_SLOT_LANE_MODE": "live",
        "MEMECOIN_REAL_ENABLED": True,
    }.items():
        monkeypatch.setattr(cfg, key, value, raising=False)
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


def refill(c, tmp_path, *, sol=1.75, fresh_sol=None, quote_fn=None, daily_cap_usd=260.0, **kw):
    return topup.buffer_topup_once(
        c, sol_balance=sol, usdc_balance=400.0, sol_usd=100.0,
        cap_usd=175.0, topup_usd=130.0, daily_cap_usd=daily_cap_usd,
        min_usdc_remaining_usd=175.0, now_iso="2026-10-05T08:00:00+00:00",
        keypair=object(), pubkey="owner", rpc_url="rpc", lock_dir=str(tmp_path),
        sol_balance_fn=lambda: sol if fresh_sol is None else fresh_sol,
        usdc_balance_fn=lambda: 400.0,
        quote_fn=quote_fn or (lambda *_a: {"outAmount": "69000000"}),
        build_fn=lambda *_a: "tx", sign_fn=lambda *_a: ("signed", "sig"),
        send_fn=lambda *_a: "sig", confirm_fn=lambda *_a: "unknown", **kw)


def test_refill_funds_full_clip_plus_reserves_and_avoids_dust(ledger, tmp_path):
    result = refill(ledger, tmp_path)
    assert result["state"] == "unknown"
    assert result["spent_usd"] == 6.90
    assert m.pending_funding_totals(ledger, "solana") == (6.90, 0)


@pytest.mark.parametrize("daily,state", [(0.0, "unknown"), (260.0, "daily_cap")])
def test_daily_limit_can_be_disabled_without_resetting_refill_history(ledger, tmp_path, daily, state):
    m.record_gas_topup(ledger, now_iso="2026-10-05T07:00:00+00:00", day="2026-10-05",
                      usdc_spent=2000.0, sol_received=None, sol_before=0.1,
                      tx_sig="prior-refill", status="submitted", purpose="buffer")
    m.record_gas_topup(ledger, now_iso="2026-10-05T07:00:01+00:00", day="2026-10-05",
                      usdc_spent=0.0, sol_received=None, sol_before=0.1,
                      tx_sig="prior-refill", status="confirmed", purpose="buffer")
    assert m.gas_topup_spent_today(ledger, "2026-10-05", purpose="buffer") == 2000
    result = refill(ledger, tmp_path, daily_cap_usd=daily)
    assert result["state"] == state
    assert result["spent_usd"] == (6.90 if daily == 0 else 0.0)
    if daily == 0:
        assert refill(ledger, tmp_path, daily_cap_usd=0)["state"] == "unresolved_topup"


@pytest.mark.parametrize("daily", [-1.0, float("nan"), float("inf")])
def test_invalid_daily_limit_never_submits(ledger, tmp_path, daily):
    result = refill(ledger, tmp_path, daily_cap_usd=daily)
    assert result["state"] in {"invalid_policy", "balance_or_price_unavailable"}
    assert m.unresolved_gas_topup(ledger, purpose="buffer") is None


def test_fees_within_refill_headroom_need_no_quote(ledger, tmp_path):
    def forbidden(*_a):
        pytest.fail("a still-full native buffer must not request another quote")
    result = refill(ledger, tmp_path, sol=1.80, quote_fn=forbidden)
    assert result["state"] == "buffer_cap"
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 0)


@pytest.mark.parametrize("daily", [0.0, 260.0])
def test_pending_native_buy_is_not_refilled_before_its_balance_settles(ledger, tmp_path, daily):
    m.reserve_pending(ledger, mint="coin", source_user_id="wallet", strategy="real",
                      reserved_at="2026-10-05T07:59:59+00:00", network_id="solana",
                      notional=100, funding_asset="sol", funding_amount_atomic=1_000_000_000,
                      funding_sol_usd=100, funding_overhead_atomic=10_000_000)
    result = refill(ledger, tmp_path, sol=1.0, daily_cap_usd=daily)
    assert result["state"] == "pending_native_entry"
    assert m.pending_funding_totals(ledger, "solana") == (0.0, 1_010_000_000)


def test_fresh_balance_above_target_cancels_prepared_refill(ledger, tmp_path):
    result = refill(ledger, tmp_path, fresh_sol=2.0)
    assert result["state"] == "balance_changed"
    assert m.unresolved_gas_topup(ledger, purpose="buffer") is None


def test_native_reservation_arriving_during_quote_cancels_refill(ledger, tmp_path):
    def quote(*_a):
        m.reserve_pending(ledger, mint="coin", source_user_id="wallet", strategy="real",
                          reserved_at="2026-10-05T07:59:59+00:00", network_id="solana",
                          notional=100, funding_asset="sol", funding_amount_atomic=1_000_000_000,
                          funding_sol_usd=100, funding_overhead_atomic=10_000_000)
        return {"outAmount": "69000000"}
    result = refill(ledger, tmp_path, quote_fn=quote)
    assert result["state"] == "balance_changed"
    assert m.unresolved_gas_topup(ledger, purpose="buffer") is None


def test_unknown_reservation_read_never_becomes_an_empty_book(ledger, tmp_path, monkeypatch):
    def broken(*_a, **_kw):
        raise sqlite3.OperationalError("unavailable")
    monkeypatch.setattr(m, "pending_funding_totals", broken)
    result = refill(ledger, tmp_path)
    assert result["state"] == "buffer_state_unavailable"
    assert m.unresolved_gas_topup(ledger, purpose="buffer") is None


@pytest.mark.parametrize("balance,price", [(None,100), (1.75,None), (1.75,0), (1.75,float('nan'))])
def test_unknown_balance_or_price_never_reports_a_zero_buffer(ledger, balance, price):
    with pytest.raises(ValueError):
        topup.sol_buffer_allocation(ledger, sol_balance=balance, sol_usd=price, cap_usd=175)


def test_accounting_separates_fast_funds_without_changing_manual_sizing(ledger):
    from test_memecoin_real_deploy_routes import _client
    cl, c = _client({"acct_stats_fn": lambda _m: {}, "acct_balances_fn": lambda: {
        "solana": {"cash_usd": 423.0, "native": 1.85, "native_usd": 185.0}}})
    try:
        sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
        assert sol["fast_lane_funds_usd"] == 175
        assert sol["native_reserve_usd"] == pytest.approx(1.891784)
        assert sol["fast_lane_headroom_usd"] == 5
        assert sol["fast_lane_tradable_usd"] == 598
        assert sol["sizing_equity"] == 423
        assert sol["equity"] == 608
        assert sol["gas_required"] == pytest.approx(.01891784)
    finally:
        c.close()


def test_accounting_unknown_native_keeps_fast_funds_unknown(ledger):
    from test_memecoin_real_deploy_routes import _client
    cl, c = _client({"acct_stats_fn": lambda _m: {}, "acct_balances_fn": lambda: {
        "solana": {"cash_usd": 423.0, "native": None, "native_usd": None}}})
    try:
        sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
        assert sol["fast_lane_funds_usd"] is None
        assert sol["fast_lane_tradable_usd"] is None
    finally:
        c.close()


def test_zero_native_balance_is_zero_funds_and_low_reserve(ledger):
    from test_memecoin_real_deploy_routes import _client
    cl, c = _client({"acct_stats_fn": lambda _m: {}, "acct_balances_fn": lambda: {
        "solana": {"cash_usd": 423, "native": 0, "native_usd": 0,
                   "native_price_usd": 100}}})
    try:
        sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
        assert sol["fast_lane_funds_usd"] == 0
        assert sol["native_reserve_usd"] == 0
        assert sol["gas_ok"] is False
        assert sol["fast_lane_tradable_usd"] == 423
    finally:
        c.close()


def test_disabled_lane_does_not_report_sol_as_tradable(ledger, monkeypatch):
    from test_memecoin_real_deploy_routes import _client
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "shadow")
    cl, c = _client({"acct_stats_fn": lambda _m: {}, "acct_balances_fn": lambda: {
        "solana": {"cash_usd": 423, "native": 1.85, "native_usd": 185}}})
    try:
        sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
        assert sol["fast_lane_enabled"] is False
        assert sol["fast_lane_funds_usd"] == 0
        assert sol["fast_lane_tradable_usd"] == 423
    finally:
        c.close()


@pytest.mark.parametrize("enabled,mode,real,active", [
    ("on", "live", True, False), ("1", "LIVE", True, True),
    ("1", "live", False, False),
])
def test_accounting_uses_the_runner_lane_configuration(ledger, monkeypatch, enabled, mode, real, active):
    from test_memecoin_real_deploy_routes import _client
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_ENABLED", enabled)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", mode)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", real)
    cl, c = _client({"acct_stats_fn": lambda _m: {}, "acct_balances_fn": lambda: {
        "solana": {"cash_usd": 423, "native": 1.85, "native_usd": 185}}})
    try:
        sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
        assert sol["fast_lane_enabled"] is active
        assert sol["fast_lane_funds_usd"] == (175 if active else 0)
    finally:
        c.close()


def test_dashboard_shows_fast_funds_separately_from_reserves():
    from test_memecoin_cockpit_money_ui import _acct_panel
    panel = _acct_panel({"by_network": {"solana": {
        "cash": 423, "gas": 1.85, "gas_usd": 185, "native_reserve_usd": 1.891784,
        "fast_lane_funds_usd": 175, "fast_lane_headroom_usd": 5,
        "fast_lane_tradable_usd": 598, "fast_lane_enabled": True,
        "sizing_equity": 423, "deployed": 0, "reserved": 0, "open_n": 0,
        "unrealized": 0, "equity": 608, "clip_next": 175}},
        "totals": {}, "slots": {"used": 0, "max": 3}, "allowlist_by_chain": {}})
    assert "fast-lane SOL" in panel
    assert "$175.00" in panel
    assert "$598.00" in panel
    assert "fees / accounts" in panel
    assert "native gas <b>$185.00" not in panel
