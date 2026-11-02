"""Chain-1 entry tip floor (operator GO 2026-10-09 via Alpha, after Beta's Protect read).

Two of five chain-1 entries since 10-06 expired because copier buys tipping 0.5-5.8 gwei were
ordered ahead of ours (0.062 / 0.273) and moved the pool past our signed minimum before a builder
placed us. MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_<chain> (default off) raises ONLY the entry swap's tip
to at least the floor. The existing cap still holds: maxFee never past the per-tx gas reserve over
the gas limit, and never below today's price; the tip never above maxFee. Exit fees have their
own floor, disabled in this fixture to verify the entry knob's scope independently.
"""
import json

import pytest
import rlp
from eth_account import Account

from sniper.execution import chain1_protect as c1
from sniper.execution import evm_real_seams as S
from sniper.settings import memecoin as cfg

GWEI = 10 ** 9
KEY = "0x" + "22" * 32
SWAP = {"to": "0x" + "bb" * 20, "data": "0x" + "ab" * 40, "value": 10 ** 16, "gas": 600_000}


def _history(base_next_gwei, median_tips_gwei):
    bases = [hex(int(0.1 * GWEI))] * len(median_tips_gwei) + [hex(int(base_next_gwei * GWEI))]
    return {"baseFeePerGas": bases, "reward": [[hex(int(t * GWEI))] for t in median_tips_gwei]}


@pytest.fixture
def headroom(monkeypatch):
    monkeypatch.setenv("MEMECOIN_CHAIN1_FEE_HEADROOM", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "1")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", "0.05")
    monkeypatch.setenv("MEMECOIN_EVM_TIP_CAP_GWEI_1", "2")
    monkeypatch.delenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "0")
    monkeypatch.delenv("MEMECOIN_EVM_APPROVAL_PIPELINE", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN", {"1": 0.001})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN", {"1": 0.001})
    h = c1.FeeHeadroom(lambda: _history(0.18, [0.05, 0.06, 0.27, 0.07, 0.1]))
    h.refresh()                                       # next base 0.18 gwei, p75 tip 0.1 gwei
    monkeypatch.setattr(c1, "_HEADROOM", {"1": h})
    monkeypatch.setattr(c1, "start_refresher", lambda *a, **k: None)
    return monkeypatch


def _fields(raw_hex):
    raw = bytes.fromhex(raw_hex[2:])
    assert raw[0] == 2, "not a type-2 envelope"
    f = rlp.decode(raw[1:])
    ii = lambda b: int.from_bytes(b, "big")
    return {"tip": ii(f[2]), "max_fee": ii(f[3]), "gas": ii(f[4])}


def _sign(gas, gas_price, **kw):
    raw, _h = S.sign_tx({"to": "0x" + "11" * 20, "data": "0x", "value": 0, "gas": gas,
                         "gasPrice": gas_price, "nonce": 3, "chainId": 1}, Account.from_key(KEY), **kw)
    return raw


# --- the knob -------------------------------------------------------------------------------------

def test_the_floor_is_off_unless_a_positive_finite_number_is_set(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", raising=False)
    assert cfg.evm_entry_tip_floor_wei_for("1") == 0
    for raw in ("abc", "nan", "inf", "-1", " ", "0"):
        monkeypatch.setenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", raw)
        assert cfg.evm_entry_tip_floor_wei_for("1") == 0, raw
    monkeypatch.setenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", "1.0")
    assert cfg.evm_entry_tip_floor_wei_for("1") == GWEI
    assert cfg.evm_entry_tip_floor_wei_for("56") == 0


# --- the signer -----------------------------------------------------------------------------------

def test_a_floor_raises_the_tip_and_the_max_fee_follows_it_under_the_reserve(headroom):
    got = _fields(_sign(600_000, int(0.45 * GWEI), tip_floor_wei=GWEI))
    assert got["tip"] == GWEI                                    # over today's p75 of 0.1
    assert got["max_fee"] == 2 * int(0.18 * GWEI) + GWEI         # 1.36 gwei, under 0.001 / 600k
    assert got["gas"] * got["max_fee"] <= int(0.001 * 1e18)


def test_the_gas_reserve_cap_clips_a_heavy_routes_floor_and_the_tip_never_passes_max_fee(headroom):
    got = _fields(_sign(1_500_000, int(0.45 * GWEI), tip_floor_wei=GWEI))
    reserve = int(0.001 * 1e18)
    assert got["max_fee"] == reserve // 1_500_000                 # 0.667 gwei
    assert got["tip"] == got["max_fee"] and got["gas"] * got["max_fee"] <= reserve


def test_a_floor_under_todays_tip_changes_nothing(headroom):
    assert _sign(600_000, int(0.45 * GWEI), tip_floor_wei=int(0.01 * GWEI)) == \
        _sign(600_000, int(0.45 * GWEI))


def test_stale_fee_data_clips_the_floor_to_todays_price(headroom):
    c1._HEADROOM["1"]._state = (c1._HEADROOM["1"]._state[0] - 31, *c1._HEADROOM["1"]._state[1:])
    got = _fields(_sign(600_000, int(0.45 * GWEI), tip_floor_wei=GWEI))
    assert got["max_fee"] == int(0.45 * GWEI) and got["tip"] == int(0.45 * GWEI)


def test_a_legacy_chain_ignores_the_floor():
    acct = Account.from_key(KEY)
    tx = {"to": "0x" + "11" * 20, "data": "0x", "value": 0, "gas": 21000, "gasPrice": GWEI,
          "nonce": 1, "chainId": 56}
    assert S.sign_tx(dict(tx), acct, tip_floor_wei=5 * GWEI) == S.sign_tx(dict(tx), acct)


# --- the production executor: only the entry swap gets the floor ----------------------------------

def _rpc():
    def http(_method, _url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": hex(7)}, None
        if meth == "eth_gasPrice":
            return 200, {"result": hex(int(0.3 * GWEI))}, None
        if meth == "eth_getBalance":
            return 200, {"result": hex(10 ** 18)}, None
        if meth == "eth_feeHistory":
            return 200, {"result": _history(0.18, [0.05, 0.06, 0.27, 0.07, 0.1])}, None
        return 200, {"result": "0x0"}, None
    return http


def _entry_and_exit_raw(monkeypatch):
    """Raw hex from the entry's own signer (the sign_fn the executor hands execute_entry_evm) and from
    the exit, approval and plain `_sign` paths, on one executor."""
    from sniper.execution import evm_swap_executor as executor
    from sniper.persistence import db_memecoin as m
    import sqlite3
    monkeypatch.setattr(S.m, "pending_row", lambda *a: {"strategy": "fixture", "source_user_id": "u"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    entry = []

    def execute(*a, sign_fn, **kw):
        entry.append(sign_fn(dict(SWAP))[0])
        # The plain path signs its WETH unwrap pre-transaction with the same sign_fn.
        entry.append(sign_fn({"to": "0x" + "dd" * 20, "data": "0x2e1a7d4d" + "%064x" % 10 ** 16,
                              "value": 0, "gas": 150_000})[0])
        return {"status": "aborted", "reason": "fixture"}
    monkeypatch.setattr(executor, "execute_entry_evm", execute)
    ex = S.build_real_evm_executor(acct=Account.from_key(KEY), chain_id="1", rpc_url="https://rpc",
                                   http=_rpc(), price_fn=lambda: 2500.0, usdc_fn=lambda: 300.0,
                                   token_fn=lambda _m: 0.0,
                                   venue_quote_fn=lambda *a, **k: {
                                       "sellAmount": "1", "buyAmount": "1", "transaction": {"to": "0x" + "cc" * 20}})
    c1._HEADROOM["1"] = c1.FeeHeadroom(lambda: _history(0.18, [0.05, 0.06, 0.27, 0.07, 0.1]))
    c1._HEADROOM["1"].refresh()
    ex["execute_fn"](None, 1, "0x" + "aa" * 20, 25, "now")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (id, mint, network_id, is_open, execution_mode, "
              "entry_price, opened_at) VALUES (1, ?, '1', 1, 'real', 1.0, '2026-10-09T00:00:00+00:00')",
              ("0x" + "aa" * 20,))
    c.commit()
    exit_raw = ex["_make_exit_sign_fn"](c, 1, "close_claim")(dict(SWAP))[0]
    plain = ex["_sign"](dict(SWAP))[0]
    _entry_and_exit_raw.unwrap = entry[1]
    return entry[0], exit_raw, plain, ex


def test_only_the_entry_swap_signs_the_floor(headroom):
    headroom.setenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", "1.0")
    entry, exit_raw, plain, _ex = _entry_and_exit_raw(headroom)
    assert _fields(entry)["tip"] == GWEI
    assert _fields(_entry_and_exit_raw.unwrap)["tip"] == int(0.1 * GWEI), "the unwrap keeps today's tip"
    assert _fields(exit_raw)["tip"] == int(0.1 * GWEI)          # today's headroom tip
    assert _fields(plain)["tip"] == int(0.1 * GWEI)


def test_with_the_knob_off_the_entry_signs_todays_bytes(headroom):
    entry, _exit, plain, _ex = _entry_and_exit_raw(headroom)
    assert _fields(entry)["tip"] == int(0.1 * GWEI)
    assert entry == plain                                         # same tx, nonce and fees as today


def test_the_entry_gas_guard_prices_the_floor(headroom):
    _entry, _exit, _plain, ex = _entry_and_exit_raw(headroom)
    today = ex["_expected_gas_price"]()
    assert today == int(0.3 * GWEI) + int(0.1 * GWEI)              # raw + today's tip
    headroom.setenv("MEMECOIN_EVM_ENTRY_TIP_FLOOR_GWEI_1", "1.0")
    assert ex["_expected_gas_price"]() == int(0.3 * GWEI) + GWEI
