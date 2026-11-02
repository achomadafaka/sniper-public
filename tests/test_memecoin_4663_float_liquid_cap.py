"""4663's native float takes at most half the liquid capital (2026-10-15, Alpha (B),
MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS).

The legacy 4663 gas pass refilled native to 0.08 ETH below 0.075. On a ~$212 book that parked
0.0800 ETH ($191) as native beside 0.00914 WETH ($21.79), and the gate sizes 4663 on WETH alone,
so the clip was $21.65. 58% of 4663 fills over 14 days spent WETH directly (in-swap unwrap, relay,
kyber, v2), so counting native as spendable would only move the failure. The pass now takes the
56/8453 bound: the float target is the gas need plus half of native + WETH.

Pins (Alpha): the first pass on today's balances wraps ~0.031 ETH and leaves native >= the clip plus
the book keep, so native routes still skip the unwrap; an unreadable balance does nothing; the knob
off is today's pass byte for byte."""
import sqlite3

import pytest

import scripts.memecoin_runner_loop as loop
from sniper.execution.evm_native_float import float_covers
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G
from sniper.settings import memecoin as cfg

E = 10 ** 18
NOW = "2026-10-15T16:00:00+00:00"
# The live wallet at 2026-10-15 ~15:50Z (research dRPC read).
NATIVE = 79_999_007_669_954_000
WETH = 9_140_983_403_907_862
LOW, TARGET, REFILL, WETH_RESERVE = 2 * E // 1000, 8 * E // 100, 75 * E // 1000, 5 * E // 1000
# gas_pass_bounds("4663") with the live gas knobs: book need 0.00379, + 3 round trips 0.00414.
BASE = dict(low_water_wei=3_790_000_000_000_000, target_wei=7_930_000_000_000_000,
            min_weth_reserve_wei=0, daily_cap_wei=0, reclaim_floor_gas=0,
            hold_under_reservation=True, hold_until_receipt_s=0.0)
GAS_PRICE = 10_000_000                  # 0.01 gwei


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS", "4663")
    monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", "4663")
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_LOW_WEI", LOW)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", TARGET)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_WEI", REFILL, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI", WETH_RESERVE)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_DAILY_CAP_WEI", 0)
    monkeypatch.setattr(G, "gas_pass_bounds", lambda chain, cfg=None: dict(BASE))


def _kwargs(monkeypatch):
    seen = {}

    def topup(conn, **kwargs):
        seen.update(kwargs)
        return {"state": "above_low_water"}
    loop.run_evm_gas_topup_pass(object(), contexts={"4663": {"chain": "4663"}}, now_iso=NOW,
                                topup_fn=topup)
    return seen


def test_the_knob_adds_only_the_half_liquid_bound(monkeypatch):
    on = _kwargs(monkeypatch)
    monkeypatch.setenv("MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS", "")
    off = _kwargs(monkeypatch)
    assert on.pop("balance_bound_base") == BASE
    assert on.pop("hold_under_reservation") is True
    assert on.pop("hold_until_receipt_s") == cfg.MEMECOIN_EVM_GAS_TOPUP_HOLD_S
    assert on == off and sorted(off) == ["context", "daily_cap_wei", "low_water_wei",
                                         "min_weth_reserve_wei", "now_iso", "target_wei"]
    assert off["low_water_wei"] == REFILL and off["target_wei"] == TARGET


@pytest.mark.parametrize("raw", ["", "56", "4663,x"])
def test_another_chain_or_a_malformed_knob_is_off(monkeypatch, raw):
    monkeypatch.setenv("MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS", raw)
    seen = _kwargs(monkeypatch)
    assert not {"balance_bound_base", "hold_under_reservation", "hold_until_receipt_s"} & set(seen)


# --- the real pass on the live balances ---------------------------------------------------------

@pytest.fixture
def ledger():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def _context(*, native=lambda: NATIVE, weth=lambda: WETH):
    signed = []
    ctx = {"chain": "4663", "address": "0xabc", "gas_topup": {
        "weth_token": "0xWETH", "rpc_url": "https://no-network",
        "native_balance_raw_fn": native, "weth_balance_raw_fn": weth,
        "nonce_fn": lambda: 7, "gas_price_fn": lambda: GAS_PRICE,
        "sign_fn": lambda tx: (signed.append(tx), ("0xsigned", "0xhash"))[1],
        "send_fn": lambda _raw: "0xhash", "confirm_once_fn": lambda _h: "confirmed"}}
    return ctx, signed


def _pass(ledger, ctx, tmp_path):
    return loop._run_chain_gas_pass(
        ledger, "4663", contexts={"4663": ctx}, now_iso=NOW, gas_alert_state=None,
        topup_fn=lambda conn, **kw: G.run_evm_gas_topup_pass(conn, lock_dir=str(tmp_path), **kw))


def test_the_first_pass_wraps_the_excess_float_and_native_still_covers_a_full_clip(ledger, tmp_path):
    ctx, signed = _context()
    out = _pass(ledger, ctx, tmp_path)
    assert out["state"] == "reclaim" and out["status"] == "confirmed"
    wrapped = int(signed[0]["value"])
    assert signed[0]["data"] == "0xd0e30db0" and 0.030 * E < wrapped < 0.032 * E
    native = NATIVE - wrapped - G.WRAP_GAS_LIMIT * GAS_PRICE
    weth = WETH + wrapped
    # The gate's clip: bankroll equity over 2 slots, clamped to WETH, which binds here (~0.0406).
    clip = min(weth, (weth + native) // 2)
    assert clip == weth and 0.040 * E < clip
    # The float probe's keep for a full 4663 book (3 slots, 2 open) at a hooked-pool swap's gas.
    book_keep = 3_790_000_000_000_000
    assert float_covers(native, clip, 1_000_000, GAS_PRICE, max(LOW, book_keep))


def test_after_the_wrap_the_next_pass_holds_still(ledger, tmp_path):
    ctx, signed = _context()
    _pass(ledger, ctx, tmp_path)
    wrapped = int(signed[0]["value"])
    after = NATIVE - wrapped - G.WRAP_GAS_LIMIT * GAS_PRICE
    ctx2, signed2 = _context(native=lambda: after, weth=lambda: WETH + wrapped)
    out = _pass(ledger, ctx2, tmp_path)
    assert signed2 == [] and out.get("act") is False


@pytest.mark.parametrize("which", ["native", "weth"])
def test_an_unreadable_balance_does_nothing(ledger, tmp_path, which):
    ctx, signed = _context(**{which: lambda: None})
    out = _pass(ledger, ctx, tmp_path)
    assert signed == [] and out.get("act") is not True


def test_the_knob_off_refills_to_the_legacy_target(ledger, tmp_path, monkeypatch):
    """Today's pass: a float below the refill mark goes back to 0.08 ETH, the whole WETH above the
    reserve permitting."""
    monkeypatch.setenv("MEMECOIN_EVM_FLOAT_LIQUID_CAP_CHAINS", "")
    ctx, signed = _context(native=lambda: 36 * E // 1000, weth=lambda: 50 * E // 1000)
    out = _pass(ledger, ctx, tmp_path)
    assert signed and signed[0]["data"].startswith("0x2e1a7d4d")
    assert int(signed[0]["data"][10:], 16) == TARGET - 36 * E // 1000 and out["status"] == "confirmed"


def test_after_a_weth_paid_buy_one_pass_rebalances_then_holds(ledger, tmp_path):
    """Alpha's question: a WETH-paid buy leaves native above the new half-liquid cap. The next pass
    wraps once to the cap (no waiting for an exit), and the one after holds still."""
    native, weth = 48_500_000_000_000_000, 0                      # after the first wrap, then the buy
    ctx, signed = _context(native=lambda: native, weth=lambda: weth)
    assert _pass(ledger, ctx, tmp_path)["state"] == "reclaim"
    wrapped = int(signed[0]["value"])
    native, weth = native - wrapped - G.WRAP_GAS_LIMIT * GAS_PRICE, weth + wrapped
    # half of the liquid capital above the gas need stays native: ~0.0282 native, ~0.0203 WETH
    assert 0.0275 * E < native < 0.0290 * E and 0.0195 * E < weth < 0.0210 * E
    ctx2, signed2 = _context(native=lambda: native, weth=lambda: weth)
    _pass(ledger, ctx2, tmp_path)
    assert signed2 == []


def test_a_live_4663_reservation_holds_the_capped_pass_before_any_read(ledger, tmp_path):
    """Gamma HOLD: the capped pass also wraps native away, so an unsettled 4663 reservation (an
    entry sized and proved against these balances) stops it before a single balance read."""
    m.reserve_pending(ledger, mint="0xcoin", source_user_id=1, strategy="copy", notional=97.0,
                      reserved_at=NOW, network_id="4663")
    assert m.pending_totals(ledger, network="4663")[0] == 1
    reads = []

    def read(name, value):
        return lambda: reads.append(name) or value
    ctx, signed = _context(native=read("native", NATIVE), weth=read("weth", WETH))
    out = _pass(ledger, ctx, tmp_path)
    assert out["state"] == "reservation_in_flight" and reads == [] and signed == []
