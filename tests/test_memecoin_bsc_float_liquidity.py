"""Maintenance must leave quote cash when the configured BSC float exceeds liquid capital."""
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G
from sniper.settings import memecoin as cfg
import scripts.memecoin_runner_loop as loop

E = 10 ** 18
NOW = "2026-10-04T12:00:00+00:00"
BASE = dict(low_water_wei=2_550_000_000_000_000, target_wei=6_690_000_000_000_000,
            min_weth_reserve_wei=0, daily_cap_wei=0, reclaim_floor_gas=0,
            hold_under_reservation=True, hold_until_receipt_s=0)
FLOAT = dict(BASE, low_water_wei=70_000_000_000_000_000,
             target_wei=102_550_000_000_000_000)
NATIVE = 102_444_649_750_000_000
WBNB = 9_911_698_794_477


@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def context(ledger, *, native=None, weth=None, receipt="confirmed"):
    signed = []

    def send(_raw):
        row = ledger.execute("SELECT tx_hash, status FROM memecoin_evm_gas_topups").fetchone()
        assert dict(row) == {"tx_hash": "0xhash", "status": "submitted"}
        return "0xhash"

    ctx = {"chain": "56", "address": "0xabc", "gas_topup": {
        "weth_token": "0xWBNB", "rpc_url": "https://no-network",
        "native_balance_raw_fn": native or (lambda: NATIVE),
        "weth_balance_raw_fn": weth or (lambda: WBNB),
        "nonce_fn": lambda: 7, "gas_price_fn": lambda: 50_000_000,
        "sign_fn": lambda tx: (signed.append(tx), ("0xsigned", "0xhash"))[1],
        "send_fn": send, "confirm_once_fn": lambda _hash: receipt}}
    return ctx, signed


def run(ledger, ctx, tmp_path):
    return G.run_evm_gas_topup_pass(ledger, context=ctx, now_iso=NOW,
        lock_dir=str(tmp_path), balance_bound_base=BASE, **FLOAT)


def test_runner_reclaims_current_stranded_native_to_tradeable_cash(ledger, tmp_path, monkeypatch):
    """Missing BSC wiring leaves the current wallet at $0.008 cash indefinitely."""
    monkeypatch.setattr(G, "gas_pass_bounds", lambda _chain: dict(BASE))
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_WEI", 100_000_000_000_000_000)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_WEI", 70_000_000_000_000_000)
    ctx, signed = context(ledger)
    out = loop._run_chain_gas_pass(ledger, "56", contexts={"56": ctx}, now_iso=NOW,
        gas_alert_state=None,
        topup_fn=lambda conn, **kw: G.run_evm_gas_topup_pass(conn, lock_dir=str(tmp_path), **kw))
    assert out["state"] == "reclaim" and out["status"] == "confirmed"
    assert signed[0]["value"] == 47_864_869_025_602_762
    assert signed[0]["data"] == "0xd0e30db0" and signed[0]["chainId"] == 56
    row = ledger.execute("SELECT direction, native_amount_atomic, status FROM memecoin_evm_gas_topups").fetchone()
    assert dict(row) == {"direction": "wrap", "native_amount_atomic": "47864869025602762",
                         "status": "confirmed"}


def test_runner_refills_near_full_float_before_native_entry_loses_to_gas(ledger, tmp_path, monkeypatch):
    from sniper.execution.evm_native_float import float_covers
    monkeypatch.setattr(G, "gas_pass_bounds", lambda _chain: dict(BASE))
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_WEI", 100_000_000_000_000_000)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_WEI", 70_000_000_000_000_000)
    native, weth, clip = 102_524_383_450_000_000, 266_601_315_298_265_464, 99_974_355_167_433_762
    assert not float_covers(native, clip, 400_000, 50_000_000, BASE["low_water_wei"])
    ctx, signed = context(ledger, native=lambda: native, weth=lambda: weth)
    out = loop._run_chain_gas_pass(ledger, "56", contexts={"56": ctx}, now_iso=NOW,
        gas_alert_state=None,
        topup_fn=lambda conn, **kw: G.run_evm_gas_topup_pass(conn, lock_dir=str(tmp_path), **kw))
    assert out["state"] == "topup" and out["status"] == "confirmed"
    assert signed[0]["data"] == G.encode_withdraw(106_690_000_000_000_000 - native)
    assert signed[0]["value"] == 0
    kept = native + int(signed[0]["data"][10:], 16) - G.WETH_CALL_GAS_LIMIT * 50_000_000
    assert float_covers(kept, clip, 400_000, 50_000_000, BASE["low_water_wei"])
    assert cfg.MEMECOIN_BSC_FAST_LANE_FLOAT_WEI == 100_000_000_000_000_000


def test_pure_cap_reserves_gas_then_splits_usable_liquidity_without_mutating_inputs():
    bounded = G.balance_bound_float(FLOAT, base_bounds=BASE,
                                    native_balance_wei=NATIVE, weth_balance_wei=WBNB)
    assert bounded["target_wei"] == 54_572_280_724_397_238
    assert bounded["low_water_wei"] == 36_241_423_272_069_619
    assert NATIVE + WBNB - bounded["target_wei"] == 47_882_280_724_397_239
    assert FLOAT["target_wei"] == 102_550_000_000_000_000
    assert bounded["reclaim_floor_gas"] == FLOAT["reclaim_floor_gas"]


def test_a_large_bankroll_keeps_the_configured_float():
    assert G.balance_bound_float(FLOAT, base_bounds=BASE, native_balance_wei=E,
                                 weth_balance_wei=E) == FLOAT


@pytest.mark.parametrize("native,weth", [(0, 0), (1, 0), (0, 6_000_000_000_000_000)])
def test_insufficient_total_liquidity_keeps_the_base_gas_reserve(native, weth):
    bounded = G.balance_bound_float(FLOAT, base_bounds=BASE,
                                    native_balance_wei=native, weth_balance_wei=weth)
    assert bounded["target_wei"] == BASE["target_wei"]
    assert bounded["low_water_wei"] == BASE["low_water_wei"]


@pytest.mark.parametrize("bad", [None, True, -1, "-1", "bad", 1.5, float("nan")])
@pytest.mark.parametrize("field", ["native_balance_wei", "weth_balance_wei"])
def test_unknown_or_malformed_atomic_balance_abstains(bad, field):
    balances = dict(native_balance_wei=NATIVE, weth_balance_wei=WBNB)
    balances[field] = bad
    assert G.balance_bound_float(FLOAT, base_bounds=BASE, **balances) is None


def test_wbnb_growth_under_the_lock_cancels_the_outside_wrap(ledger, tmp_path):
    reads = iter([WBNB, 200_000_000_000_000_000])
    ctx, signed = context(ledger, weth=lambda: next(reads))
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "at_or_under_target" and signed == []
    assert not m.has_unresolved_evm_gas_topup(ledger, "56")


def test_wbnb_loss_under_the_lock_redecides_unwrap_as_wrap(ledger, tmp_path):
    reads = iter([95_000_000_000_000_000, 2_000_000_000_000_000])
    ctx, signed = context(ledger, native=lambda: 40_000_000_000_000_000,
                           weth=lambda: next(reads))
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "reclaim" and out["status"] == "confirmed"
    assert signed[0]["data"] == "0xd0e30db0"
    assert signed[0]["value"] == 15_647_500_000_000_000


def test_small_native_move_recomputes_the_amount_under_the_lock(ledger, tmp_path):
    reads = iter([NATIVE] * 3 + [101_944_649_750_000_000])
    ctx, signed = context(ledger, native=lambda: next(reads))
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "reclaim"
    assert signed[0]["value"] == 47_614_869_025_602_762


def test_unknown_wbnb_under_the_lock_cannot_authorize_a_wrap(ledger, tmp_path):
    reads = iter([WBNB, None])
    ctx, signed = context(ledger, weth=lambda: next(reads))
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "liquid_balance_unreadable" and signed == []


@pytest.mark.parametrize("bad", [True, -1, "bad", 1.5, float("nan")])
def test_malformed_native_read_signs_nothing(ledger, tmp_path, bad):
    ctx, signed = context(ledger, native=lambda: bad)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "liquid_balance_unreadable" and signed == []


def test_low_native_refills_only_the_balance_bound_float_and_journals_first(ledger, tmp_path):
    ctx, signed = context(ledger, native=lambda: 20_000_000_000_000_000,
                           weth=lambda: 80_000_000_000_000_000)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "topup" and out["status"] == "confirmed"
    assert signed[0]["value"] == 0
    assert int(signed[0]["data"][10:], 16) == 33_345_000_000_000_000
    row = ledger.execute("SELECT direction, native_amount_atomic, status FROM memecoin_evm_gas_topups").fetchone()
    assert dict(row) == {"direction": "unwrap", "native_amount_atomic": "33345000000000000",
                         "status": "confirmed"}


def test_unknown_receipt_blocks_a_second_balance_bound_transfer(ledger, tmp_path):
    ctx, signed = context(ledger, receipt="unknown")
    out = run(ledger, ctx, tmp_path)
    assert out["status"] == "submitted_unknown"
    assert run(ledger, ctx, tmp_path)["state"] == "unresolved"
    assert len(signed) == 1


def test_a_reservation_prevents_balances_or_float_cash_from_moving(ledger, tmp_path):
    m.reserve_pending(ledger, mint="0xcoin", source_user_id="u", strategy="alpha_b",
                      notional=10, reserved_at=NOW, network_id="56")
    def forbidden():
        pytest.fail("maintenance read while a reservation is unresolved")
    ctx, signed = context(ledger, native=forbidden, weth=forbidden)
    assert run(ledger, ctx, tmp_path)["state"] == "reservation_in_flight"
    assert signed == []


def test_gas_price_floor_can_keep_more_native_than_the_liquidity_split(ledger, tmp_path):
    ctx, signed = context(ledger)
    bounds = dict(FLOAT, reclaim_floor_gas=2_000_000_000)
    out = G.run_evm_gas_topup_pass(ledger, context=ctx, now_iso=NOW,
        lock_dir=str(tmp_path), balance_bound_base=BASE, **bounds)
    assert out["state"] == "reclaim" and out["status"] == "confirmed"
    assert signed[0]["value"] == 2_437_149_750_000_000


def test_native_and_wbnb_rechecks_overlap_inside_the_same_wallet_lock(ledger, tmp_path):
    """Serial WBNB after native adds a second provider wait while entries queue for this lock."""
    native_started, weth_started = threading.Event(), threading.Event()
    counts = {"native": 0, "weth": 0}
    overlapped = {}

    def native():
        counts["native"] += 1
        if counts["native"] == 4:
            native_started.set()
            overlapped["native"] = weth_started.wait(0.5)
        return NATIVE

    def weth():
        counts["weth"] += 1
        if counts["weth"] == 2:
            weth_started.set()
            overlapped["weth"] = native_started.wait(0.5)
        return WBNB

    ctx, signed = context(ledger, native=native, weth=weth)
    out = run(ledger, ctx, tmp_path)
    assert out["state"] == "reclaim" and len(signed) == 1
    assert overlapped == {"native": True, "weth": True}


@pytest.mark.parametrize("broken", [0, 1])
def test_parallel_balance_read_preserves_the_other_answer_on_failure(broken):
    def fail():
        raise TimeoutError("read unavailable")
    readers = [lambda: NATIVE, lambda: WBNB]
    readers[broken] = fail
    got = G._read_balances_within(*readers, deadline_s=0.1)
    assert got == ((None, WBNB) if broken == 0 else (NATIVE, None))


def test_late_parallel_balance_is_unknown_and_cannot_change_the_returned_pair():
    release, finished = threading.Event(), threading.Event()
    def native():
        release.wait(1)
        finished.set()
        return NATIVE
    try:
        got = G._read_balances_within(native, lambda: WBNB, deadline_s=0.02)
        assert got == (None, WBNB)
    finally:
        release.set()
    assert finished.wait(1)
    assert got == (None, WBNB)
