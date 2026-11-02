import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_gas_topup as G


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_reclaim_keeps_the_target_after_its_own_gas_cost():
    """A reclaim that ignores its gas cost drops the wallet below the target."""
    plan = G.plan_gas_reclaim(
        native_balance_wei=5_000_000,
        target_wei=2_000_000,
        gas_price_wei=10,
    )

    # native - target - the reclaim's own wrap gas (WRAP_GAS_LIMIT x gas price)
    assert plan == {"act": True, "state": "reclaim",
                    "amount_wei": 5_000_000 - 2_000_000 - G.WRAP_GAS_LIMIT * 10}


def test_runner_reclaims_surplus_as_weth_without_charging_the_unwrap_cap(tmp_path):
    """A native-input route must not strand trade cash as gas after its order ends."""
    conn = _conn()
    calls = {"signed": [], "sent": []}
    context = {
        "chain": "4663",
        "address": "0xabc",
        "gas_topup": {
            "weth_token": "0xWETH",
            "rpc_url": "https://rpc",
            "native_balance_raw_fn": lambda: 5_000_000_000_000_000,
            "weth_balance_raw_fn": lambda: 50_000_000_000_000_000,
            "nonce_fn": lambda: 12,
            "gas_price_fn": lambda: 1_000_000_000,
            "sign_fn": lambda tx: (calls["signed"].append(tx), ("0xsigned", "0xhash"))[1],
            "send_fn": lambda raw: (calls["sent"].append(raw), "0xhash")[1],
            "confirm_once_fn": lambda _tx_hash: "confirmed",
        },
    }

    out = G.run_evm_gas_topup_pass(
        conn, context=context, now_iso="2026-08-31T12:00:00+00:00",
        low_water_wei=1_000_000_000_000_000,
        target_wei=2_000_000_000_000_000,
        min_weth_reserve_wei=10_000_000_000_000_000,
        daily_cap_wei=4_000_000_000_000_000,
        lock_dir=str(tmp_path),
    )

    assert out["state"] == "reclaim" and out["status"] == "confirmed"
    assert calls["sent"] == ["0xsigned"]
    assert calls["signed"][0]["data"] == "0xd0e30db0"
    assert calls["signed"][0]["value"] == (5_000_000_000_000_000 - 2_000_000_000_000_000
                                           - G.WRAP_GAS_LIMIT * 1_000_000_000)
    row = conn.execute(
        "SELECT direction, status FROM memecoin_evm_gas_topups"
    ).fetchone()
    assert tuple(row) == ("wrap", "confirmed")
    assert m.evm_gas_topup_spent_today(conn, "4663", "2026-08-31") == 0
