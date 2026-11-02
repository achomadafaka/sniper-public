"""Prepare native-spendable Ethereum cash before entries, without shrinking their gas reserve."""
import sqlite3

import pytest

import scripts.memecoin_runner_loop as loop
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G
from sniper.settings import memecoin as cfg

E = 10 ** 18
NOW = "2026-10-12T21:19:50+00:00"
NATIVE = 23_016_577_499_944_352
WETH = 963_772_526_370_091
GP = 163_454_388
BOUNDS = dict(low_water_wei=26 * E // 1000, target_wei=28 * E // 1000)
CASH = dict(BOUNDS, native_spendable=True, min_weth_reserve_wei=0)


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def chain_settings(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "1")
    for name, value in (("LOW", "0.026"), ("TARGET", "0.028"),
                        ("MIN_WETH_RESERVE", "0"), ("DAILY_CAP", "0.1")):
        monkeypatch.setenv(f"MEMECOIN_EVM_GAS_TOPUP_{name}_NATIVE_1", value)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_HOLD_S", 0)


def context(ledger, *, native=lambda: NATIVE, weth=lambda: WETH,
            gas_price=lambda: GP, receipt="confirmed"):
    calls = {"signed": [], "sent": [], "gas_reads": 0}

    def gas():
        calls["gas_reads"] += 1
        return gas_price()

    def send(raw):
        row = ledger.execute("SELECT tx_hash, status FROM memecoin_evm_gas_topups").fetchone()
        assert dict(row) == {"tx_hash": "0xhash", "status": "submitted"}
        calls["sent"].append(raw)
        return "0xhash"

    ctx = {"chain": "1", "address": "0xabc", "gas_topup": {
        "weth_token": "0xWETH", "rpc_url": "https://no-network",
        "native_balance_raw_fn": native, "weth_balance_raw_fn": weth,
        "nonce_fn": lambda: 14, "gas_price_fn": gas,
        "sign_fn": lambda tx: (calls["signed"].append(tx), ("0xsigned", "0xhash"))[1],
        "send_fn": send, "confirm_once_fn": lambda _hash: receipt}}
    return ctx, calls


def run(ledger, ctx, tmp_path):
    return loop._run_chain_gas_pass(ledger, "1", contexts={"1": ctx}, now_iso=NOW,
        gas_alert_state=None,
        topup_fn=lambda conn, **kw: G.run_evm_gas_topup_pass(conn, lock_dir=str(tmp_path), **kw))


def test_mtf_cash_is_prepared_by_the_runner_and_journaled_before_send(ledger, tmp_path):
    ctx, calls = context(ledger)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "topup" and out["status"] == "confirmed"
    assert out["amount_wei"] == WETH
    tx = calls["signed"][0]
    assert int(tx["data"][10:], 16) == WETH
    assert tx["value"] == 0 and tx["chainId"] == 1 and tx["gasPrice"] == GP
    assert calls["gas_reads"] == 1 and calls["sent"] == ["0xsigned"]
    # Hand-checked conservative maximum fee leaves the exact $50 input plus the same book reserve.
    native_after_max_fee = NATIVE + out["amount_wei"] - tx["gas"] * tx["gasPrice"]
    assert native_after_max_fee == 23_955_831_868_114_443
    assert native_after_max_fee > 19_876_764_062_810_576 + 3_300_000_000_000_000
    row = ledger.execute("SELECT native_amount_atomic, status, direction FROM memecoin_evm_gas_topups").fetchone()
    assert dict(row) == {"native_amount_atomic": str(WETH), "status": "confirmed", "direction": "unwrap"}


def test_native_cash_cap_uses_all_spare_weth_and_preserves_configured_limits():
    bounded = G.balance_bound_float(BOUNDS, base_bounds=CASH,
                                    native_balance_wei=NATIVE, weth_balance_wei=WETH)
    assert bounded == {"target_wei": 23_980_350_026_314_443,
                       "low_water_wei": 23_980_350_026_314_442}
    assert BOUNDS == {"low_water_wei": 26 * E // 1000, "target_wei": 28 * E // 1000}


def test_disabled_native_spendable_retains_the_legacy_full_topup_refusal(
        ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_SPENDABLE_CHAINS", "4663")
    ctx, calls = context(ledger)
    assert run(ledger, ctx, tmp_path)["state"] == "insufficient_weth_for_full_topup"
    assert calls["signed"] == calls["sent"] == []


def test_configured_weth_reserve_is_not_unwrapped(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_1", "0.0005")
    ctx, calls = context(ledger)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "topup" and out["amount_wei"] == 463_772_526_370_091
    assert int(calls["signed"][0]["data"][10:], 16) == 463_772_526_370_091


def test_daily_cap_still_refuses_the_transfer(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_NATIVE_1", "0.0005")
    ctx, calls = context(ledger)
    assert run(ledger, ctx, tmp_path)["state"] == "daily_cap_reached"
    assert calls["signed"] == calls["sent"] == []


def test_fresh_weth_loss_under_lock_sizes_only_the_remaining_cash(ledger, tmp_path):
    reads = iter([WETH, 400_000_000_000_000])
    ctx, calls = context(ledger, weth=lambda: next(reads))
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "topup" and out["amount_wei"] == 400_000_000_000_000
    assert int(calls["signed"][0]["data"][10:], 16) == 400_000_000_000_000


@pytest.mark.parametrize("bad", [None, True, -1, "bad", 1.5, float("nan")])
def test_unknown_or_malformed_weth_under_lock_signs_nothing(ledger, tmp_path, bad):
    reads = iter([WETH, bad])
    ctx, calls = context(ledger, weth=lambda: next(reads))
    assert run(ledger, ctx, tmp_path)["state"] == "liquid_balance_unreadable"
    assert calls["signed"] == calls["sent"] == []


@pytest.mark.parametrize("bad", [None, True, -1, "bad", 1.5, float("nan")])
def test_unknown_or_malformed_native_signs_nothing(ledger, tmp_path, bad):
    ctx, calls = context(ledger, native=lambda: bad)
    assert run(ledger, ctx, tmp_path)["state"] == "liquid_balance_unreadable"
    assert calls["signed"] == calls["sent"] == []


@pytest.mark.parametrize("weth", [2, 24_518_158_200_000])
def test_cash_that_cannot_exceed_the_max_fee_is_not_unwrapped(ledger, tmp_path, weth):
    ctx, calls = context(ledger, weth=lambda: weth)
    assert run(ledger, ctx, tmp_path)["state"] == "topup_not_economic"
    assert calls["signed"] == calls["sent"] == []


@pytest.mark.parametrize("bad", [None, 0, True, -1, "bad", 1.5, float("nan")])
def test_unknown_or_nonpositive_gas_price_signs_nothing(ledger, tmp_path, bad):
    ctx, calls = context(ledger, weth=lambda: E, gas_price=lambda: bad)
    assert run(ledger, ctx, tmp_path)["state"] == "gas_price_unreadable"
    assert calls["signed"] == calls["sent"] == []


def test_native_must_afford_the_max_unwrap_fee(ledger, tmp_path):
    ctx, calls = context(ledger, native=lambda: 100, weth=lambda: E)
    assert run(ledger, ctx, tmp_path)["state"] == "topup_gas_short"
    assert calls["signed"] == calls["sent"] == []


def test_fee_reduced_native_with_no_weth_cannot_start_a_refill_loop(ledger, tmp_path):
    ctx, calls = context(ledger)
    assert run(ledger, ctx, tmp_path)["status"] == "confirmed"
    ctx["gas_topup"]["native_balance_raw_fn"] = lambda: 23_955_831_868_114_443
    ctx["gas_topup"]["weth_balance_raw_fn"] = lambda: 0
    assert run(ledger, ctx, tmp_path)["state"] == "at_or_under_target"
    assert len(calls["signed"]) == len(calls["sent"]) == 1


def test_unknown_receipt_prevents_a_second_native_cash_transfer(ledger, tmp_path):
    ctx, calls = context(ledger, receipt="unknown")
    assert run(ledger, ctx, tmp_path)["status"] == "submitted_unknown"
    assert run(ledger, ctx, tmp_path)["state"] == "unresolved"
    assert len(calls["signed"]) == len(calls["sent"]) == 1


def test_reservation_prevents_cash_reads_and_transfers(ledger, tmp_path):
    m.reserve_pending(ledger, mint="0xcoin", source_user_id="u", strategy="alpha_b",
                      notional=50, reserved_at=NOW, network_id="1")
    def forbidden():
        pytest.fail("maintenance read while an entry reservation is unresolved")
    ctx, calls = context(ledger, native=forbidden, weth=forbidden)
    assert run(ledger, ctx, tmp_path)["state"] == "reservation_in_flight"
    assert calls["signed"] == calls["sent"] == []


@pytest.mark.parametrize("weth", [200_000_000_000_000, 500_000_000_000_000, 2])
def test_open_bag_retains_existing_emergency_weth_reserve_release(
        ledger, tmp_path, monkeypatch, weth):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_1", "0.0005")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_NATIVE_1", "0.000000000000000001")
    ledger.execute("INSERT INTO memecoin_positions "
                   "(mint, network_id, execution_mode, is_open, opened_at) "
                   "VALUES ('0xbag', '1', 'real', 1, ?)", (NOW,))
    ledger.commit()
    ctx, calls = context(ledger, weth=lambda: weth)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "topup" and out["status"] == "confirmed"
    assert out["amount_wei"] == weth
    assert int(calls["signed"][0]["data"][10:], 16) == weth
    assert calls["sent"] == ["0xsigned"]


@pytest.mark.parametrize("weth", [200_000_000_000_000, 500_000_000_000_000])
def test_flat_book_cannot_release_weth_at_or_below_reserve(
        ledger, tmp_path, monkeypatch, weth):
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_1", "0.0005")
    ctx, calls = context(ledger, weth=lambda: weth)
    assert run(ledger, ctx, tmp_path)["state"] == "weth_below_reserve"
    assert calls["signed"] == calls["sent"] == []
