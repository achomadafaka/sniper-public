"""Offline shared-ledger lifecycle. Route/provider certification is a separate gate."""

import sqlite3

from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.execution.evm_exit_executor import execute_exit_evm
from sniper.persistence import db_memecoin as db


def test_ink_entry_close_restart_has_one_inventory_lifecycle(tmp_path):
    path = tmp_path / "funded.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    mint = "0x" + "b" * 40
    pending = db.reserve_pending(conn, mint=mint, source_user_id="source", strategy="alpha_b",
                                 notional=10.0, reserved_at="2026-09-29T09:00:00Z",
                                 network_id="57073")
    entry = execute_entry_evm(
        conn, pending_id=pending, mint=mint, network_id="57073", usd_amount=10.0,
        strategy="alpha_b", source_user_id="source", now_iso="2026-09-29T09:00:01Z",
        quote_fn=lambda *_a, **_k: {"sellAmount": "5000000000000000",
            "transaction": {"to": "0xrouter", "data": "0xdead"}, "issues": {}},
        approve_fn=lambda *_a: True,
        sign_fn=lambda _tx: ("0xentry", "0x" + "a" * 64, 1),
        send_fn=lambda _raw: True, confirm_fn=lambda _h: "confirmed",
        usdc_fn=lambda: 100.0, token_fn=lambda _mint: 0.0,
        receipt_token_fn=lambda _h, _mint: (5.0, 18),
        receipt_spent_fn=lambda _h: 0.005,
        sell_decimals=18, sell_token_usd=2000.0,
        require_receipt_amounts=True)
    assert entry["status"] == "filled"

    position = db.get_position(conn, entry["position_id"])
    close = execute_exit_evm(
        conn, position, "manual", "2026-09-29T09:01:00Z",
        quote_fn=lambda *_a: {"sellAmount": str(5 * 10**18),
            "buyAmount": str(6 * 10**15), "minBuyAmount": str(5 * 10**15),
            "transaction": {"to": "0xrouter"}, "issues": {}},
        approve_fn=lambda *_a: True,
        sign_fn=lambda _tx: ("0xexit", "0x" + "c" * 64, 2),
        send_fn=lambda _raw: True, confirm_fn=lambda _h: "confirmed",
        usdc_fn=lambda: 100.0, token_balance_fn=lambda _mint: 5.0,
        receipt_proceeds_fn=lambda _h: 12.0, require_receipt_proceeds=True,
        quote_asset_usd=2000.0, quote_asset_decimals=18, slippage_ladder=[3])
    assert close["status"] == "closed"
    conn.close()

    with sqlite3.connect(path) as restarted:
        restarted.row_factory = sqlite3.Row
        assert db.get_position(restarted, entry["position_id"])["is_open"] == 0
        assert db.open_qty(restarted, entry["position_id"]) == 0.0
        assert restarted.execute(
            "SELECT count(*) FROM memecoin_fills WHERE position_id=?",
            (entry["position_id"],)).fetchone()[0] == 2
        assert db.pending_row(restarted, pending)["status"] == "confirmed"
