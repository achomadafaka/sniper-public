"""A J7 buy is credited only by a Transfer of ITS coin to ITS trader (2026-10-08 review SF02).

The report joined a socket receipt to a J7 buy by transaction plus receiver topic, never the token
contract, and the funded-watcher column took any detection whose signature began 4663:<tx>:. A
router transaction that moves coin A and coin B to the same wallet (or one coin to two watched
wallets) then showed a delivery the socket never made for the coin that was bought. Now the socket
cell needs l.address = the J7 mint and the Transfer to the J7 trader, and a removed (reorged) log
never counts; the funded cell needs the exact canonical signature 4663:<tx>:<mint> and the same
wallet. Addresses compare lowercased.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import evm_4663_ws_shadow as sh  # noqa: E402

TX = "0x" + "77" * 32
TRADER, OTHER = "0x" + "1a" * 20, "0x" + "2b" * 20
COIN_A, COIN_B = "0x" + "aa" * 20, "0x" + "bb" * 20
J7_AT = 1051.0


def _topic(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def _db(logs, j7_mint=COIN_B, j7_trader=TRADER):
    conn = sqlite3.connect(":memory:")
    conn.executescript(sh.SCHEMA)
    conn.execute("INSERT INTO runs(run_id, started_at, stopped_at) VALUES ('t', 0, 9999)")
    conn.execute("INSERT INTO events(run_id, socket, at, kind, epoch) VALUES ('t', 'drpc', 900, 'up', 1)")
    for idx, (coin, to, at, removed) in enumerate(logs):
        conn.execute("INSERT INTO logs(tx,log_index,block,sub,address,topic1,topic2,removed,epoch,first_at) "
                     "VALUES (?,?,10,'transfers',?,?,?,?,1,?)",
                     (TX, idx, coin, _topic("0x" + "00" * 20), _topic(to), int(removed), at))
        conn.execute("INSERT INTO receipts VALUES (?,?,'drpc',?,1)", (TX, idx, at))
    conn.execute("INSERT INTO j7 VALUES ('e1', ?, ?, ?, ?, 1)", (TX, j7_trader, j7_mint, J7_AT))
    return conn


def _line(conn, ledger=None):
    out = sh.report(conn, ["drpc"], ledger=ledger)
    (line,) = [l for l in out if l.startswith("  " + TX[:12])]
    return line


def test_a_transfer_of_another_coin_never_credits_the_bought_coin():
    line = _line(_db([(COIN_A, TRADER, 1050.0, False)]))
    assert "drpc MISSED" in line and "-1.0s" not in line, line


def test_the_bought_coin_to_another_wallet_never_credits_the_trader():
    line = _line(_db([(COIN_B, OTHER, 1050.0, False)]))
    assert "drpc MISSED" in line, line


def test_a_multi_coin_router_buy_takes_the_bought_coins_own_transfer():
    line = _line(_db([(COIN_A, TRADER, 1050.0, False), (COIN_B, TRADER, 1052.0, False)]))
    assert "drpc +1.0s" in line, line


def test_two_buyers_in_one_transaction_each_get_their_own_transfer():
    conn = _db([(COIN_B, TRADER, 1052.0, False), (COIN_B, OTHER, 1049.0, False)], j7_trader=OTHER)
    assert "drpc -2.0s" in _line(conn)


def test_addresses_match_whatever_their_case():
    conn = _db([(COIN_B, TRADER, 1052.0, False)], j7_mint=COIN_B.upper().replace("0X", "0x"),
               j7_trader=TRADER.upper().replace("0X", "0x"))
    assert "drpc +1.0s" in _line(conn)


def test_several_logs_of_the_bought_coin_take_the_earliest():
    assert "drpc +0.5s" in _line(_db([(COIN_B, TRADER, 1053.0, False), (COIN_B, TRADER, 1051.5, False)]))


def test_a_removed_log_is_not_a_delivery():
    line = _line(_db([(COIN_B, TRADER, 1050.0, True)]))
    assert "drpc MISSED" in line, line


def _write_log(conn, at, *, removed=False, block=10, block_hash="0x" + "cd" * 32,
               coin=COIN_B, recipient=TRADER, epoch=1):
    from test_evm_4663_ws_shadow import Pacer, PM, INIT
    shadow = sh.Shadow(db_path=":memory:", sockets={}, roster=sh.Roster(lambda: [TRADER]),
                       pool_manager=PM, init_topic=INIT, pacer=Pacer(), run_id="t")
    raw = dict(transactionHash=TX, logIndex="0x0", blockNumber=hex(block), blockHash=block_hash,
               address=coin, topics=["0x" + "00" * 32, _topic(OTHER), _topic(recipient)], removed=removed)
    shadow.write(conn, ("log", "drpc", at, "transfers", epoch, sh.parse_log(raw)))


def test_live_then_removed_updates_delivery_without_changing_first_arrival():
    conn = _db([])
    _write_log(conn, 1052)
    _write_log(conn, 1053, removed=True)
    assert conn.execute("SELECT removed, first_at FROM logs").fetchone() == (1, 1052)
    assert conn.execute("SELECT at FROM receipts").fetchone() == (1052,)
    assert "drpc MISSED" in _line(conn)


def test_reincluded_log_uses_its_new_block_arrival_and_ignores_old_removal():
    conn = _db([])
    _write_log(conn, 1052)
    _write_log(conn, 1053, removed=True)
    new_hash = "0x" + "ef" * 32
    _write_log(conn, 1060, block=10, block_hash=new_hash)
    _write_log(conn, 1061, removed=True)
    assert conn.execute("SELECT removed, block_hash, first_at FROM logs").fetchone() == (0, new_hash, 1052)
    assert conn.execute("SELECT at FROM receipts").fetchone() == (1060,)
    assert "drpc +9.0s" in _line(conn)


def test_old_live_delivery_cannot_undo_its_removal():
    conn = _db([])
    _write_log(conn, 1052)
    _write_log(conn, 1053, removed=True)
    _write_log(conn, 1054)
    assert "drpc MISSED" in _line(conn)
    assert "logs seen 0" in "\n".join(sh.report(conn, ["drpc"]))


def test_delayed_old_live_cannot_overwrite_reincluded_block():
    conn = _db([])
    _write_log(conn, 1052)
    _write_log(conn, 1053, removed=True)
    new_hash = "0x" + "ef" * 32
    _write_log(conn, 1060, block_hash=new_hash)
    _write_log(conn, 1061)
    _write_log(conn, 1062, removed=True)
    assert conn.execute("SELECT removed,block_hash FROM logs").fetchone() == (0, new_hash)
    assert conn.execute("SELECT at FROM receipts").fetchone() == (1060,)
    assert "drpc +9.0s" in _line(conn)


def test_reinclusion_retires_old_identity_even_when_its_removal_was_missed():
    conn = _db([])
    _write_log(conn, 1052)
    new_hash = "0x" + "ef" * 32
    _write_log(conn, 1060, block_hash=new_hash)
    _write_log(conn, 1061)
    assert conn.execute("SELECT removed,block_hash FROM logs").fetchone() == (0, new_hash)
    assert "drpc +9.0s" in _line(conn)


def test_reincluded_transaction_uses_its_current_log_content():
    conn = _db([])
    _write_log(conn, 1052)
    _write_log(conn, 1053, removed=True)
    _write_log(conn, 1060, block_hash="0x" + "ef" * 32, coin=COIN_A, recipient=OTHER)
    assert conn.execute("SELECT address,topic2 FROM logs").fetchone() == (COIN_A, _topic(OTHER))
    assert "drpc MISSED" in _line(conn)


def test_duplicate_notification_keeps_first_arrival_roster_epoch():
    conn = _db([])
    conn.execute("INSERT INTO events(run_id,socket,at,kind,epoch) VALUES ('t','peer',900,'up',1)")
    _write_log(conn, 1052)
    _write_log(conn, 1053, epoch=2)
    assert conn.execute("SELECT epoch FROM logs").fetchone() == (1,)
    assert "MISSED while up 1" in "\n".join(sh.report(conn, ["peer"]))


def test_existing_shadow_schema_gains_block_identity_without_losing_rows():
    conn = sqlite3.connect(":memory:")
    conn.executescript(sh.SCHEMA.replace(", block_hash TEXT", "", 1))
    conn.execute("INSERT INTO logs(tx,log_index,block,removed,first_at) VALUES (?,0,10,1,1052)", (TX,))
    sh.ensure_schema(conn)
    assert conn.execute("SELECT tx,removed,first_at,block_hash FROM logs").fetchone() == (TX, 1, 1052, None)


def test_the_printed_row_names_the_coin_it_judged():
    assert COIN_B[:10] in _line(_db([(COIN_B, TRADER, 1052.0, False)]))


def _ledger(*rows):
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE onchain_wallet_detections (signature TEXT, address TEXT, mint TEXT, "
                   "network_id TEXT, detected_at TEXT)")
    for signature, address, mint in rows:
        ledger.execute("INSERT INTO onchain_wallet_detections VALUES (?,?,?,'4663',"
                       "'1970-01-01T00:17:32+00:00')", (signature, address, mint))   # epoch 1052
    return ledger


def test_a_funded_detection_of_another_coin_in_the_same_tx_is_not_this_buys():
    ledger = _ledger(("4663:%s:%s" % (TX, COIN_A), TRADER, COIN_A))
    assert "funded_watcher none" in _line(_db([]), ledger)


def test_a_funded_detection_of_this_coin_for_another_wallet_is_not_this_buys():
    ledger = _ledger(("4663:%s:%s" % (TX, COIN_B), OTHER, COIN_B))
    assert "funded_watcher none" in _line(_db([]), ledger)


def test_the_exact_funded_detection_is_credited():
    ledger = _ledger(("4663:%s:%s" % (TX, COIN_B), TRADER, COIN_B))
    assert "funded_watcher +1.0s" in _line(_db([]), ledger)
