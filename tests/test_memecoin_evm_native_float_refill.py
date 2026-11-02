"""A native-ETH float that the gas pass keeps topped up (2026-09-28, Option B, inert until set).

The float skip (evm_native_float, spec 2026-09-27) spends native only when native >= clip + a
3-swap gas reserve + MEMECOIN_EVM_GAS_TOPUP_LOW. The gas pass refilled native only below that SAME
LOW, and reclaimed everything above TARGET, so a float served one or two entries and then sat
stranded between LOW and one clip. MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_NATIVE splits the two
jobs: on a float chain the gas pass refills below it, while float_covers keeps LOW as its floor.

Unset, or outside LOW < refill < TARGET, or on a chain that is not a float chain, the gas pass gets
today's LOW byte for byte: a misconfiguration can never switch the gas top-up off.
"""
import sqlite3
import threading
import time

import pytest

import scripts.memecoin_runner_loop as runner_loop
from sniper.execution.exec_lock import wallet_exec_lock
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G
from sniper.settings import memecoin as cfg

E = 10 ** 18
LOW, TARGET = 2 * E // 1000, 145 * E // 1000
REFILL = 735 * E // 10000


def _bounds(monkeypatch, *, refill, float_chains, target=TARGET):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_LOW_WEI", LOW)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", target)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_NATIVE_FLOAT_REFILL_BELOW_WEI", refill, raising=False)
    if float_chains is None:
        monkeypatch.delenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_EVM_NATIVE_FLOAT_CHAINS", float_chains)
    seen = {}

    def topup(conn, **kwargs):
        seen.update(kwargs)
        return {"state": "above_low_water"}
    runner_loop.run_evm_gas_topup_pass(object(), contexts={"4663": {"chain": "4663"}},
                                       now_iso="2026-09-28T07:00:00+00:00", topup_fn=topup)
    return seen


def test_unset_passes_todays_bounds_byte_for_byte(monkeypatch):
    seen = _bounds(monkeypatch, refill=0, float_chains="4663")
    assert seen["low_water_wei"] == LOW and seen["target_wei"] == TARGET
    assert sorted(seen) == ["context", "daily_cap_wei", "low_water_wei", "min_weth_reserve_wei",
                            "now_iso", "target_wei"]


def test_a_float_chain_refills_below_the_refill_mark(monkeypatch):
    assert _bounds(monkeypatch, refill=REFILL, float_chains="4663")["low_water_wei"] == REFILL


@pytest.mark.parametrize("float_chains", [None, "8453", ""])
def test_a_chain_that_is_not_a_float_chain_keeps_low(monkeypatch, float_chains):
    assert _bounds(monkeypatch, refill=REFILL, float_chains=float_chains)["low_water_wei"] == LOW


@pytest.mark.parametrize("refill", [LOW, LOW - 1, TARGET, TARGET + 1])
def test_a_refill_mark_outside_low_and_target_is_ignored(monkeypatch, refill):
    """At or above TARGET the planner would refuse every top-up (target_not_above_low_water) and
    starve exits of gas; at or below LOW it changes nothing."""
    assert _bounds(monkeypatch, refill=refill, float_chains="4663")["low_water_wei"] == LOW


def test_the_float_floor_stays_low():
    """float_covers keeps LOW as the native it must leave behind, whatever the refill mark."""
    import inspect
    from sniper.execution import evm_real_seams
    src = inspect.getsource(evm_real_seams)
    probe = src[src.index("def native_float_probe"):src.index("def cleanup_native")]
    assert "low_wei=cfg.MEMECOIN_EVM_GAS_TOPUP_LOW_WEI" in probe


@pytest.mark.parametrize("native_eth,act,amount_eth", [
    (0.081, False, 0),          # after one $170 use: still covers another, no refill
    (0.017, True, 0.128),       # after two: refill to TARGET
    (0.0734, True, 0.0716),     # just under the mark
])
def test_the_refill_cycle(native_eth, act, amount_eth):
    plan = G.plan_gas_topup(native_balance_wei=int(native_eth * E), weth_balance_wei=5 * E,
                            low_water_wei=REFILL, target_wei=TARGET, min_weth_reserve_wei=E // 100,
                            daily_cap_wei=0, spent_today_wei=0)
    assert plan["act"] is act
    if act:
        assert abs(plan["amount_wei"] - int(amount_eth * E)) < E // 10000


# --- what a refill costs an entry: the short lock ---------------------------------------------

def test_an_entry_waits_only_for_the_refills_in_lock_reads_and_send(tmp_path):
    """MEASURED BOUND. The gas pass holds the wallet lock only to re-read native (and WETH) under a
    2 s deadline each, read the nonce, sign and send; the receipt is read after release. An entry
    that arrives inside that window blocks (serialize_executor is blocking), so the wait is the
    in-lock reads plus the send. Here the in-lock native read takes 0.30 s."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    native_reads, inside = [], threading.Event()

    def native():
        native_reads.append(1)
        if len(native_reads) > 3:          # the first three are the agreeing read outside the lock
            inside.set()
            time.sleep(0.30)
        return int(0.017 * E)
    ctx = {"chain": "4663", "address": "0xabc", "gas_topup": {
        "weth_token": "0xWETH", "rpc_url": "https://rpc", "native_balance_raw_fn": native,
        "weth_balance_raw_fn": lambda: 5 * E, "nonce_fn": lambda: 7,
        "gas_price_fn": lambda: 30_000_000, "sign_fn": lambda tx: ("0xraw", "0xhash"),
        "send_fn": lambda raw: "0xhash", "confirm_once_fn": lambda h: "confirmed"}}
    out = {}
    t = threading.Thread(target=lambda: out.update(G.run_evm_gas_topup_pass(
        conn, context=ctx, now_iso="2026-09-28T07:00:00+00:00", low_water_wei=REFILL,
        target_wei=TARGET, min_weth_reserve_wei=E // 100, daily_cap_wei=0,
        lock_dir=str(tmp_path))))
    t.start()
    assert inside.wait(10)
    t0 = time.monotonic()
    with wallet_exec_lock("evm-4663-0xabc", lock_dir=str(tmp_path)):
        waited = time.monotonic() - t0
    t.join(10)
    assert out.get("state") in ("topup", "confirmed", "submitted"), out
    print("entry waited %.3f s behind the gas pass" % waited)
    assert 0.2 <= waited <= 0.30 + 0.5, waited


def test_base_float_uses_explicit_chain_bounds_and_bankroll_cap(monkeypatch):
    monkeypatch.setenv('MEMECOIN_EVM_NATIVE_FLOAT_CHAINS', '8453')
    for name, value in [('LOW', '0.015'), ('TARGET', '0.03'),
                        ('MIN_WETH_RESERVE', '0.005'), ('DAILY_CAP', '0.06')]:
        monkeypatch.setenv(f'MEMECOIN_EVM_GAS_TOPUP_{name}_NATIVE_8453', value)
    seen = {}
    runner_loop._run_chain_gas_pass(object(), '8453', contexts={'8453': {'chain': '8453'}},
        now_iso='2026-10-10T09:00:00+00:00', gas_alert_state=None,
        topup_fn=lambda c, **kw: seen.update(kw) or {'state': 'above_low_water'})
    assert seen['low_water_wei'] == 15 * E // 1000
    assert seen['target_wei'] == 3 * E // 100
    assert seen['min_weth_reserve_wei'] == 5 * E // 1000
    assert seen['daily_cap_wei'] == 6 * E // 100
    assert 'balance_bound_base' in seen


@pytest.mark.parametrize('low', [None, 'broken', 'NaN'])
def test_base_bad_float_bounds_keep_ordinary_gas_maintenance(monkeypatch, low):
    monkeypatch.setenv('MEMECOIN_EVM_NATIVE_FLOAT_CHAINS', '8453')
    for name in ('LOW', 'TARGET', 'MIN_WETH_RESERVE', 'DAILY_CAP'):
        monkeypatch.delenv(f'MEMECOIN_EVM_GAS_TOPUP_{name}_NATIVE_8453', raising=False)
    if low is not None:
        monkeypatch.setenv('MEMECOIN_EVM_GAS_TOPUP_LOW_NATIVE_8453', low)
        monkeypatch.setenv('MEMECOIN_EVM_GAS_TOPUP_TARGET_NATIVE_8453', '0.03')
        monkeypatch.setenv('MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_NATIVE_8453', '0.005')
    seen = {}
    result = runner_loop._run_chain_gas_pass(object(), '8453', contexts={'8453': {'chain': '8453'}},
        now_iso='2026-10-10T09:00:00+00:00', gas_alert_state=None,
        topup_fn=lambda c, **kw: seen.update(kw) or {'state': 'above_low_water'})
    base = G.gas_pass_bounds('8453')
    assert seen['low_water_wei'] == base['low_water_wei']
    assert seen['target_wei'] == base['target_wei']
    assert 'balance_bound_base' not in seen
    assert result['native_float_state'] == 'levels_unset'
