"""The operator's de-risk backfill: pos 31099's unbooked partial, MOONDENG 36517's stuck intent and
Gamma's LOW-2 (an unpriced partial with qty NULL). Dry run by default; one receipt read per tx."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]
OWNER = "Owner1111111111111111111111111111111111111"
MINT = "CrJPSvj625TnPdWS42aG5ybMcHeFvnNqq5AExVespump"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIG = "43zJwhPXoWg1CZXCR2vY64gDQtC1JHoayFeYNGkRXvNHtH7Xwp2XePm3coMB4AgEdBwi9wSby8g947jaw7XfvcUU"


def _script():
    spec = importlib.util.spec_from_file_location("bf", ROOT / "scripts/memecoin_backfill_derisk.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ledger(path, *, final_qty=147273.532045, realized=79.61457, is_open=0, intent_status="confirmed",
            network="solana"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    cur = conn.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, entry_notional, "
        "is_open, opened_at, closed_at, execution_mode, network_id, strategy, realized_pnl) "
        "VALUES ('w1', ?, 'Momota', 0.00025462823821326825, 50.0, ?, '2026-08-07T18:20:01+00:00', "
        "?, 'real', ?, 'alpha_b', ?)",
        (MINT, is_open, None if is_open else "2026-08-11T09:23:14+00:00", network,
         None if is_open else realized))
    pid = cur.lastrowid
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
                 "fraction, created_at, tx_sig) VALUES(?,?,?,?,?,?,?,?,?)",
                 (pid, "entry", 0.00025462823821326825, 196364.709393, 50.0, "chain", 1.0,
                  "2026-08-07T18:20:01+00:00", "entrysig"))
    if not is_open:
        conn.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, "
                     "fill_source, fraction, created_at, tx_sig) VALUES(?,?,?,?,?,?,?,?,?)",
                     (pid, "bracket_exit", 129.61457 / final_qty, final_qty, 129.61457, "chain", 1.0,
                      "2026-08-11T09:23:14+00:00", "closesig"))
    conn.execute("INSERT INTO memecoin_exit_intents(claim_id, position_id, action, intent_json, "
                 "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                 ("b7f2079f-%d" % pid, pid, "derisk", json.dumps({
                     "action": "derisk", "tx_identity": SIG, "fraction": 0.25,
                     "balance_snapshot": 196364.709393, "sell_amount_atomic": 49091177348}),
                  intent_status, "2026-08-07T19:34:31+00:00", "2026-08-07T19:34:32+00:00"))
    conn.commit()
    conn.close()
    return pid


def _tx(*, sold_atomic=49091177348, usdc_in=31_250_000, err=None, sig=SIG):
    return {
        "blockTime": 1786131271,
        "transaction": {"signatures": [sig], "message": {"accountKeys": [
            {"pubkey": OWNER, "signer": True, "writable": True}]}},
        "meta": {"err": err,
                 "preTokenBalances": [
                     {"mint": MINT, "owner": OWNER,
                      "uiTokenAmount": {"amount": str(196364709393), "decimals": 6}},
                     {"mint": USDC, "owner": OWNER,
                      "uiTokenAmount": {"amount": "1000000", "decimals": 6}}],
                 "postTokenBalances": [
                     {"mint": MINT, "owner": OWNER,
                      "uiTokenAmount": {"amount": str(196364709393 - sold_atomic), "decimals": 6}},
                     {"mint": USDC, "owner": OWNER,
                      "uiTokenAmount": {"amount": str(1000000 + usdc_in), "decimals": 6}}]}}


def _run(path, argv, tx, reads=None):
    def fetch(url, sig):
        if reads is not None:
            reads.append((url, sig))
        return tx
    return _script().main(argv + ["--db", str(path)], fetch=fetch, owner=OWNER,
                          research_url="https://research.example/", now="2026-10-07T12:00:00+00:00")


def _pos(path, pid):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return (dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()),
                [dict(r) for r in conn.execute(
                    "SELECT kind, price, qty, notional, fill_source, tx_sig FROM memecoin_fills "
                    "WHERE position_id=? ORDER BY id", (pid,))],
                [dict(r) for r in conn.execute(
                    "SELECT claim_id, status FROM memecoin_exit_intents WHERE position_id=?", (pid,))])
    finally:
        conn.close()


def test_the_receipt_gives_the_tokens_sold_and_the_usdc_received():
    out = _script().receipt_outcome(_tx(), sig=SIG, owner=OWNER, mint=MINT)
    assert out["state"] == "landed"
    assert out["sold_qty"] == pytest.approx(49091.177348) and out["proceeds"] == pytest.approx(31.25)
    assert out["at"].startswith("2026-08-07")


def test_a_failed_or_missing_transaction_is_never_a_sale():
    bf = _script()
    assert bf.receipt_outcome(_tx(err={"InstructionError": [3, "x"]}), sig=SIG, owner=OWNER,
                              mint=MINT)["state"] == "failed"
    assert bf.receipt_outcome(None, sig=SIG, owner=OWNER, mint=MINT)["state"] == "not_found"
    assert bf.receipt_outcome(_tx(sold_atomic=0), sig=SIG, owner=OWNER,
                              mint=MINT)["state"] == "unreadable"


def test_a_dry_run_prints_the_receipt_and_realized_before_and_after_and_writes_nothing(
        tmp_path, capsys):
    path = tmp_path / "ledger.db"
    pid = _ledger(path)
    reads = []
    assert _run(path, ["--position", str(pid)], _tx(), reads) == 0
    out = capsys.readouterr().out
    assert "LANDED" in out and "sold 49091.177348 tokens for 31.250000 USDC" in out, out
    assert "realized 79.61457 -> 110.86457" in out and "DRY RUN" in out, out
    assert reads == [("https://research.example/", SIG)]                 # one read per tx
    _pos_row, fills, _intents = _pos(path, pid)
    assert [f["kind"] for f in fills] == ["entry", "bracket_exit"]


def test_confirm_books_the_partial_prices_it_and_adds_it_to_realized_once(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    pid = _ledger(path)
    assert _run(path, ["--position", str(pid), "--confirm"], _tx()) == 0
    pos, fills, _intents = _pos(path, pid)
    assert pos["realized_pnl"] == pytest.approx(79.61457 + 31.25)
    derisk = [f for f in fills if f["kind"] == "derisk"]
    assert len(derisk) == 1 and derisk[0]["tx_sig"] == SIG
    assert derisk[0]["qty"] == pytest.approx(49091.177348)
    assert derisk[0]["notional"] == pytest.approx(31.25)
    assert derisk[0]["fill_source"] == "chain_backfill_receipt"
    assert m.derisk_proceeds(sqlite3.connect(path), pid) == pytest.approx(31.25)
    capsys.readouterr()
    assert _run(path, ["--position", str(pid), "--confirm"], _tx()) == 0       # a second run
    assert "no de-risk intent without a fill" in capsys.readouterr().out
    assert _pos(path, pid)[0]["realized_pnl"] == pytest.approx(79.61457 + 31.25)


def test_a_not_found_partial_with_equal_quantities_stays_unresolved(tmp_path, capsys):
    """Review C08 (2026-10-08): MOONDENG 36517's close sold exactly the entry balance, which this
    used to take as proof the stuck partial never ran. final = entry - partial + inflow, so equality
    proves only partial == inflow (a transfer in, a distribution, a positive rebase), never
    partial == 0. A null receipt is the node having no record, not chain failure: the intent stays
    unresolved, and the equality is printed as a diagnostic only."""
    path = tmp_path / "ledger.db"
    pid = _ledger(path, final_qty=196364.709393, realized=79.61457,
                  intent_status="submitted_unknown")
    assert _run(path, ["--position", str(pid), "--confirm"], None) == 0
    out = capsys.readouterr().out
    assert "NOT FOUND" in out and "not proof" in out
    pos, fills, intents = _pos(path, pid)
    assert intents[0]["status"] == "submitted_unknown" and pos["realized_pnl"] == pytest.approx(79.61457)
    assert [f["kind"] for f in fills] == ["entry", "bracket_exit"]


def test_a_not_found_partial_without_ledger_proof_changes_nothing(tmp_path):
    path = tmp_path / "ledger.db"
    pid = _ledger(path, intent_status="submitted_unknown")                  # close sold 75%
    assert _run(path, ["--position", str(pid), "--confirm"], None) == 0
    assert _pos(path, pid)[2][0]["status"] == "submitted_unknown"


def test_a_failed_transaction_fails_its_intent(tmp_path):
    path = tmp_path / "ledger.db"
    pid = _ledger(path, intent_status="submitted_unknown")
    assert _run(path, ["--position", str(pid), "--confirm"], _tx(err={"x": 1})) == 0
    assert _pos(path, pid)[2][0]["status"] == "failed"


@pytest.mark.parametrize("receipt_sig", ["foreignSig", "", None])
def test_an_unrelated_failed_receipt_cannot_fail_the_requested_intent(tmp_path, capsys, receipt_sig):
    path = tmp_path / "ledger.db"
    pid = _ledger(path, intent_status="submitted_unknown")
    before = _pos(path, pid)
    assert _run(path, ["--position", str(pid), "--confirm"],
                _tx(err={"x": 1}, sig=receipt_sig)) == 0
    assert "UNREADABLE" in capsys.readouterr().out
    assert _pos(path, pid) == before


def test_low2_a_quantity_less_unpriced_partial_gets_its_receipt_qty_and_price(tmp_path):
    path = tmp_path / "ledger.db"
    pid = _ledger(path)
    conn = sqlite3.connect(path)
    fill_id = conn.execute(
        "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, fraction, "
        "created_at, tx_sig) VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, "derisk", None, None, None, "chain_confirmed_unpriced", 0.25,
         "2026-08-07T19:34:31+00:00", SIG)).lastrowid
    conn.execute("UPDATE memecoin_positions SET realized_pnl=NULL WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    assert _run(path, ["--fill", str(fill_id), "--confirm"], _tx()) == 0
    pos, fills, _intents = _pos(path, pid)
    derisk = [f for f in fills if f["kind"] == "derisk"][0]
    assert derisk["qty"] == pytest.approx(49091.177348) and derisk["notional"] == pytest.approx(31.25)
    # realized was unknown (the partial unpriced): now computed from every priced leg
    assert pos["realized_pnl"] == pytest.approx(129.61457 + 31.25 - 50.0)


def test_no_read_while_a_funded_solana_position_is_open(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    _ledger(path)
    open_pid = _ledger(path, is_open=1)
    reads = []
    assert _run(path, ["--position", str(open_pid)], _tx(), reads) == 2
    assert reads == [] and "REFUSED" in capsys.readouterr().out


def test_a_non_solana_position_is_refused(tmp_path, capsys):
    path = tmp_path / "ledger.db"
    pid = _ledger(path, network="4663")
    reads = []
    assert _run(path, ["--position", str(pid)], _tx(), reads) == 0
    assert reads == [] and "REFUSED (not a funded Solana position)" in capsys.readouterr().out
