"""74149: reproduce unsigned refusals, then recover without trusting the owner index."""
import json

import pytest
from solders.keypair import Keypair

from sniper.execution import solana_exit_executor as E, solana_wallet as W
from sniper.persistence import db_memecoin as M
import test_memecoin_exit_executor as T

MINT = "7UGNXBZ2Wr1uyHVXkg2CBTS87VSHrTEFgTfNAgPypump"
ATOMS = 1542769777573


@pytest.mark.parametrize("confirmation", ["confirmed", "timeout"])
def test_two_unsigned_trail_refusals_then_exact_account_proof_recovers(monkeypatch, confirmation):
    monkeypatch.setattr(E, "_overlap_reads_on", lambda: True)
    c, pos = T._c(entry_notional=25.)
    c.execute("UPDATE memecoin_positions SET mint=?,entry_price=? WHERE id=?",
              (MINT, 25 / (ATOMS / 1e6), pos["id"]))
    c.execute("UPDATE memecoin_fills SET qty=? WHERE position_id=?", (ATOMS / 1e6, pos["id"]))
    c.commit()
    pos = M.get_position(c, pos["id"])
    owner = str(Keypair().pubkey())
    use_direct = False
    direct_present = False
    reads, quotes, sends = [], [], []
    sold = False

    def http(method, url, headers=None, body=None):
        request = json.loads(body)
        reads.append(request["method"])
        if request["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"context": {"slot": 100}, "value": []}}, None
        assert request["method"] == "getMultipleAccounts"
        account = {"owner": W._TOKEN_2022_PROGRAM, "data": {"parsed": {"type": "account", "info": {
            "owner": owner, "mint": MINT, "state": "initialized",
            "tokenAmount": {"amount": str(ATOMS), "decimals": 6}}}}}
        return 200, {"result": {"context": {"slot": 120},
                                "value": [None, account if direct_present else None]}}, None

    def snapshot(mint):
        return W.token_balance_snapshot(owner, mint, "rpc", http=http, ata_fallback=use_direct)

    def quote(mint, quantity, slip, *, atomic=None):
        assert mint == MINT and atomic == ATOMS and quantity == ATOMS / 1e6
        quotes.append(atomic)
        return {"inAmount": str(atomic), "outAmount": "18731839", "otherAmountThreshold": "16858655"}

    def confirm(sig):
        nonlocal sold
        sold = confirmation == "confirmed"
        return confirmation

    def run():
        return E.execute_exit(c, pos, "real_trail", "2026-10-23T15:18:14+00:00",
            quote_fn=quote, sign_fn=lambda q: (b"tx", "SIG"), send_fn=sends.append,
            confirm_fn=confirm, usdc_fn=lambda: 18.731839 if sold else 0.,
            token_balance_fn=lambda mint: pytest.fail("snapshot must stay authoritative"),
            token_snapshot_fn=snapshot, slippage_ladder=[10.], sleep_fn=lambda t: None)

    for _ in range(2):
        assert run()["status"] == "balance_unreadable"
        assert M.get_position(c, pos["id"])["close_claim_id"] is None
    assert not quotes and not sends
    use_direct = True
    assert run()["status"] == "balance_unreadable"
    assert not quotes and not sends  # a failed direct proof never licenses a sell
    direct_present = True
    result = run()
    assert len(quotes) == len(sends) == 1
    assert reads.count("getMultipleAccounts") == 3  # absent proof, sizing, independent pre-sign proof
    exits = c.execute("SELECT count(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                      (pos["id"],)).fetchone()[0]
    row = M.get_position(c, pos["id"])
    if confirmation == "confirmed":
        assert result["status"] == "closed" and not row["is_open"] and exits == 1
    else:
        assert result["status"] == "unknown" and row["is_open"] and row["close_claim_id"] and exits == 0
        assert run()["status"] == "already_closing" and len(sends) == 1
