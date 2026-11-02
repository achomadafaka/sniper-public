"""The EVM gas pass on chains other than 4663 (2026-09-30, operator via Alpha; BINF 6fc71d90).

A flap Portal exit on BSC pays NATIVE BNB (the Portal unwraps the pool's WBNB) while funded entries
spend WBNB, and the gas pass that wraps native above its target back into the quote token ran on
4663 only, hard-coded in memecoin_runner_loop. 67604 BINF and 67623 BINANCESTOCKS left 0.3107 BNB
native against 0.000008 WBNB, and BINF 6fc71d90 refused "below min-trade floor ... WBNB balance
$0.01". Every test here enters through the runner loop's own `run_evm_gas_topup_pass` with the real
pass underneath; only the chain seams and the lock directory are faked.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

from sniper.execution.exec_lock import wallet_exec_lock
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_runner_loop as loop  # noqa: E402

E = 10 ** 18
GP = 100_000_000                     # 0.1 gwei, BSC tonight
NOW = "2026-09-30T22:10:00+00:00"
ADDR = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TARGET = int((0.00255 + 0.00414) * E)   # book need + 3 round trips (the wrap script's 0.00669)
LOW = int(0.00255 * E)


@pytest.fixture
def cfg(monkeypatch):
    """Tonight's BSC gas settings (.env, as in test_memecoin_wrap_excess_gas_56): entry 0.00005,
    approval 0.00002, exit 0.0006 BNB, retry factor 2, 2 concurrent on 56, flap curve on."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "gas_entry_native_for", lambda chain: 0.00005)
    monkeypatch.setattr(cfg, "gas_approval_native_for", lambda chain: 0.00002)
    monkeypatch.setattr(cfg, "gas_exit_native_for", lambda chain: 0.0006)
    monkeypatch.setattr(cfg, "gas_reserve_min_native_for", lambda chain: 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2.0)
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda chain, **kw: 2)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_PORTAL_GAS", 1_500_000)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GASPRICE_BUFFER", 1.5)
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663,56")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_HOLD_S", 3.0)
    return cfg


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ctx(chain, calls, *, native, weth=8_000_000_000_000, gas_price=GP, confirm=None):
    def confirm_once(tx_hash):
        calls["confirm_lock_free"].append(_lock_free(calls["lock_dir"], chain))
        return (confirm or (lambda: "confirmed"))()
    return {"chain": chain, "address": ADDR, "gas_topup": {
        "weth_token": WBNB, "rpc_url": "offline://%s" % chain,
        "native_balance_raw_fn": lambda: native,
        "weth_balance_raw_fn": lambda: weth,
        "nonce_fn": lambda: 41,
        "gas_price_fn": lambda: gas_price,
        "sign_fn": lambda tx: (calls["signed"].append((chain, tx)), ("0xraw", "0xh" + chain))[1],
        "send_fn": lambda raw: "0xh" + chain,
        "confirm_once_fn": confirm_once,
    }}


def _lock_free(lock_dir, chain):
    with wallet_exec_lock("evm-%s-%s" % (chain, ADDR), lock_dir=lock_dir, blocking=False) as held:
        return held


def _run(conn, contexts, tmp_path, alert_state=None):
    """The runner loop's own pass; the only change is the test's lock directory."""
    return loop.run_evm_gas_topup_pass(
        conn, contexts=contexts, now_iso=NOW, gas_alert_state=alert_state,
        topup_fn=lambda c, **kw: G.run_evm_gas_topup_pass(c, lock_dir=str(tmp_path), **kw))


def _calls(tmp_path):
    return {"signed": [], "confirm_lock_free": [], "lock_dir": str(tmp_path)}


def test_binf_the_native_a_flap_exit_paid_goes_back_into_wbnb(cfg, tmp_path, monkeypatch):
    from sniper.services import memecoin_real_runner as R
    alerts = []
    monkeypatch.setattr(R, "check_evm_gas_alert",
                        lambda c, result, st, **kw: alerts.append(result.get("chain_id")))
    conn, calls = _conn(), _calls(tmp_path)
    native = 310_711_000_000_000_000                        # 0.310711 BNB at 22:0xZ
    out = _run(conn, {"56": _ctx("56", calls, native=native)}, tmp_path, alert_state={})
    r56 = out["chains"]["56"]
    assert out["chains"]["4663"]["state"] == "context_unavailable"
    assert (r56["state"], r56["status"]) == ("reclaim", "confirmed")
    [(chain, tx)] = calls["signed"]
    assert (chain, tx["to"], tx["data"], tx["chainId"]) == ("56", WBNB, "0xd0e30db0", 56)
    assert tx["value"] == native - TARGET - 150_000 * GP        # keeps the gas target, and its own gas
    row = conn.execute("SELECT chain_id, direction, status FROM memecoin_evm_gas_topups").fetchone()
    assert tuple(row) == ("56", "wrap", "confirmed")
    assert calls["confirm_lock_free"] == [False]               # the receipt was read under the lock
    assert r56["lock_held_ms"] >= 0
    assert alerts == ["56"]                                     # the 56 GAS-LOW page sees its own pass


def test_the_default_chain_list_is_todays_pass_byte_for_byte(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS")
    assert cfg.memecoin_evm_gas_topup_chains() == ("4663",)
    seen = []
    out = loop.run_evm_gas_topup_pass(
        _conn(), contexts={"4663": {"chain": "4663"}, "56": {"chain": "56"}}, now_iso=NOW,
        topup_fn=lambda c, **kw: (seen.append(kw), {"state": "above_low_water"})[1])
    assert out == {"state": "above_low_water"}
    assert [sorted(kw) for kw in seen] == [["context", "daily_cap_wei", "low_water_wei",
                                            "min_weth_reserve_wei", "now_iso", "target_wei"]]
    assert seen[0]["context"]["chain"] == "4663"


def test_56_never_takes_the_4663_eth_bounds(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", 145 * 10 ** 15)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_MIN_WETH_RESERVE_WEI", 10 ** 16)
    b = G.gas_pass_bounds("56", cfg)
    assert (b["low_water_wei"], b["target_wei"]) == (LOW, TARGET)
    assert (b["min_weth_reserve_wei"], b["daily_cap_wei"]) == (0, 0)
    assert b["reclaim_floor_gas"] == int((1_500_000 + 100_000 + 150_000) * 1.5)
    monkeypatch.setattr(cfg, "gas_reserve_min_native_for", lambda chain: 0.02)
    b = G.gas_pass_bounds("56", cfg)
    assert (b["low_water_wei"], b["target_wei"]) == (int(0.02 * E), int((0.02 + 0.00414) * E))


@pytest.mark.parametrize("flap, kept", [
    # gas spike to 5 gwei: the flap Portal exit + approve + unwrap, at the signing buffer, is
    # 2,625,000 gas x 5 gwei = 0.013125 BNB, above the 0.00669 target: the floor is what stays
    (True, 2_625_000 * 5 * 10 ** 9),
    (False, TARGET)])
def test_a_gas_spike_never_wraps_away_a_flap_exits_gas(cfg, tmp_path, monkeypatch, flap, kept):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", flap)
    conn, calls = _conn(), _calls(tmp_path)
    native, gp = int(0.02 * E), 5 * 10 ** 9
    _run(conn, {"56": _ctx("56", calls, native=native, gas_price=gp)}, tmp_path)
    [(_chain, tx)] = calls["signed"]
    assert tx["value"] == native - kept - 150_000 * gp


@pytest.mark.parametrize("native", [int(0.3 * E), int(0.001 * E)], ids=["reclaim", "unwrap"])
def test_a_reservation_in_flight_on_56_holds_the_pass_in_both_directions(cfg, tmp_path, native):
    """Alpha's chain-1 rule (beta/eth-mainnet 808b0bc37), now 56's: an in-flight entry is sized and
    proved against the WBNB and native balances; neither moves under it."""
    conn, calls = _conn(), _calls(tmp_path)
    m.reserve_pending(conn, mint="0x" + "77" * 20, source_user_id="u1", strategy="s",
                      notional=90.0, reserved_at=NOW, network_id="56")
    reads = []
    ctx = _ctx("56", calls, native=native, weth=int(0.3 * E))
    ctx["gas_topup"]["native_balance_raw_fn"] = lambda: (reads.append(1), native)[1]
    out = _run(conn, {"56": ctx}, tmp_path)
    assert out["chains"]["56"]["state"] == "reservation_in_flight" and calls["signed"] == []
    assert reads == []                                          # before any read


def test_4663_keeps_reclaiming_beside_a_pending_4663_reservation(cfg, tmp_path, monkeypatch):
    """The guard is 56's (and any new chain's); 4663's pass is unchanged."""
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_CHAINS", "4663")
    conn, calls = _conn(), _calls(tmp_path)
    m.reserve_pending(conn, mint="0x" + "66" * 20, source_user_id="u1", strategy="s",
                      notional=90.0, reserved_at=NOW, network_id="4663")
    native = cfg.MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI + int(0.1 * E)
    out = _run(conn, {"4663": _ctx("4663", calls, native=native)}, tmp_path)
    assert out["state"] == "reclaim" and len(calls["signed"]) == 1
    assert "lock_held_ms" not in out and calls["confirm_lock_free"] == [True]


def test_the_lock_is_held_until_our_receipt_so_the_next_entry_measures_a_settled_wallet(
        cfg, tmp_path):
    conn, calls = _conn(), _calls(tmp_path)
    answers = iter(["pending", "not_found", "confirmed"])
    out = _run(conn, {"56": _ctx("56", calls, native=int(0.3 * E),
                                 confirm=lambda: next(answers))}, tmp_path)
    assert out["chains"]["56"]["status"] == "confirmed"
    assert calls["confirm_lock_free"] == [False, False, False]
    assert _lock_free(str(tmp_path), "56")                       # released afterwards


def test_past_the_hold_budget_the_row_stays_unknown_and_the_lock_is_released(
        cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_HOLD_S", 0.3)
    conn, calls = _conn(), _calls(tmp_path)
    out = _run(conn, {"56": _ctx("56", calls, native=int(0.3 * E),
                                 confirm=lambda: "pending")}, tmp_path)
    r56 = out["chains"]["56"]
    assert r56["status"] == "submitted_unknown" and r56["lock_held_ms"] < 2_000
    assert m.has_unresolved_evm_gas_topup(conn, "56")          # the next pass settles it first
    assert calls["confirm_lock_free"][-1] is True               # the after-release read, unlocked


def test_low_native_on_56_unwraps_wbnb_up_to_its_own_target(cfg, tmp_path):
    conn, calls = _conn(), _calls(tmp_path)
    native = int(0.001 * E)
    out = _run(conn, {"56": _ctx("56", calls, native=native, weth=int(0.3 * E))}, tmp_path)
    [(_chain, tx)] = calls["signed"]
    assert out["chains"]["56"]["state"] == "topup"
    assert tx["data"] == G.encode_withdraw(TARGET - native)


def test_an_unreadable_ledger_holds_the_pass(cfg, tmp_path, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "pending_totals", broken)
    conn, calls = _conn(), _calls(tmp_path)
    out = _run(conn, {"56": _ctx("56", calls, native=int(0.3 * E))}, tmp_path)
    assert out["chains"]["56"]["state"] == "reservation_read_failed" and calls["signed"] == []


def test_a_reservation_that_lands_while_we_read_is_honoured_under_the_lock(cfg, tmp_path):
    conn, calls = _conn(), _calls(tmp_path)
    native = int(0.3 * E)

    def native_then_reserve():
        # an entry reserves between our outside reads and our lock
        if not conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0]:
            m.reserve_pending(conn, mint="0x" + "78" * 20, source_user_id="u1", strategy="s",
                              notional=90.0, reserved_at=NOW, network_id="56")
        return native
    ctx = _ctx("56", calls, native=native)
    ctx["gas_topup"]["weth_balance_raw_fn"] = lambda: (native_then_reserve(), 8_000_000_000_000)[1]
    out = _run(conn, {"56": ctx}, tmp_path)
    assert out["chains"]["56"]["state"] == "reservation_in_flight" and calls["signed"] == []


def test_56_keeps_its_guards_when_the_operator_turns_the_short_lock_off(cfg, tmp_path, monkeypatch):
    """MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK=0 restores 4663's old lock scope; that path has neither the
    reclaim floor nor the receipt hold, so a chain that needs them never takes it."""
    monkeypatch.setenv("MEMECOIN_EVM_GAS_TOPUP_SHORT_LOCK", "0")
    conn, calls = _conn(), _calls(tmp_path)
    answers = iter(["pending", "confirmed"])
    native, gp = int(0.02 * E), 5 * 10 ** 9
    out = _run(conn, {"56": _ctx("56", calls, native=native, gas_price=gp,
                                 confirm=lambda: next(answers))}, tmp_path)
    [(_chain, tx)] = calls["signed"]
    assert tx["value"] == native - 2_625_000 * gp - 150_000 * gp       # the flap exit's floor kept
    assert out["chains"]["56"]["status"] == "confirmed" and calls["confirm_lock_free"] == [False, False]


def test_an_ambiguous_send_is_polled_under_the_lock_too(cfg, tmp_path):
    """No answer from the send is not proof it did not land: the receipt decides, under the lock."""
    conn, calls = _conn(), _calls(tmp_path)
    ctx = _ctx("56", calls, native=int(0.3 * E))
    ctx["gas_topup"]["send_fn"] = lambda raw: None
    out = _run(conn, {"56": ctx}, tmp_path)
    assert (out["chains"]["56"]["status"], calls["confirm_lock_free"]) == ("confirmed", [False])


def test_a_reservation_that_never_settles_pages_once_after_fifteen_minutes(cfg, tmp_path, monkeypatch):
    """Gamma LOW on 33f2d619e: an ambiguous 56 entry leaves a 'reserved' row that holds the pass in
    both directions, and a held result carries no native balance, so GAS-LOW never saw it."""
    from sniper.services import memecoin_alerts as A
    pages = []
    monkeypatch.setattr(A, "alert_evm_gas_pass_held",
                        lambda chain, held_min, state: pages.append((chain, round(held_min), state)))
    conn, calls, alert_state = _conn(), _calls(tmp_path), {}
    pid = m.reserve_pending(conn, mint="0x" + "77" * 20, source_user_id="u1", strategy="s",
                            notional=90.0, reserved_at=NOW, network_id="56")
    ctx = {"56": _ctx("56", calls, native=int(0.3 * E))}

    def at(minute):
        loop.run_evm_gas_topup_pass(
            conn, contexts=ctx, now_iso="2026-09-30T22:%02d:00+00:00" % minute,
            gas_alert_state=alert_state,
            topup_fn=lambda c, **kw: G.run_evm_gas_topup_pass(c, lock_dir=str(tmp_path), **kw))
    for minute in (10, 20, 24):
        at(minute)
    assert pages == []
    at(25)                                       # 15 min since the hold began
    at(40)
    assert pages == [("56", 15, "reservation_in_flight")]
    m.release_pending(conn, pid, "test")
    at(41)                                       # the reservation settled: the pass ran
    assert calls["signed"]
    m.reserve_pending(conn, mint="0x" + "79" * 20, source_user_id="u1", strategy="s",
                      notional=90.0, reserved_at=NOW, network_id="56")
    at(42)
    at(57)
    assert len(pages) == 2                       # a new hold pages again


# ---- the BSC fast lane's native float (Alpha 2026-10-02, option (b)): carried only while live ----

FLOAT = int(0.1 * E)


@pytest.mark.parametrize("mode, kept", [("live", TARGET + FLOAT), ("runner_shadow", TARGET), ("", TARGET)])
def test_the_56_reclaim_keeps_the_lane_float_only_while_the_lane_is_live(cfg, tmp_path, monkeypatch,
                                                                          mode, kept):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", mode)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_WEI", FLOAT)
    conn, calls = _conn(), _calls(tmp_path)
    native = int(0.31 * E)
    _run(conn, {"56": _ctx("56", calls, native=native)}, tmp_path)
    [(_chain, tx)] = calls["signed"]
    assert tx["data"] == "0xd0e30db0" and tx["value"] == native - kept - 150_000 * GP


@pytest.mark.parametrize("refill, signed", [(int(0.08 * E), True), (0, False),
                                            (int(0.2 * E), False)], ids=["on", "off", "above_target"])
def test_the_float_refill_unwraps_wbnb_back_to_the_float_only_when_its_knob_is_set(
        cfg, tmp_path, monkeypatch, refill, signed):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_WEI", FLOAT)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_FLOAT_REFILL_BELOW_WEI", refill)
    conn, calls = _conn(), _calls(tmp_path)
    native = int(0.03 * E)                              # a lane fill spent most of the float
    _run(conn, {"56": _ctx("56", calls, native=native, weth=int(0.3 * E))}, tmp_path)
    if signed:
        [(_chain, tx)] = calls["signed"]
        assert tx["data"] == G.encode_withdraw(TARGET + FLOAT - native)
    else:
        assert calls["signed"] == []                    # above 56's own low-water: nothing moves
