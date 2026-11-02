"""A CONFIRMED swap must never be dropped because a balance read failed.

WAAI (0xccacb3a8, attempt 08b483c1, 2026-09-04 20:58): receipt status 0x1, 1,724,021 tokens really
in the wallet, 0.061189 ETH really spent -- and NO position row. An unmanaged bag with no stop, no
take-profit, and a reservation still holding the slot, found only because the operator asked. The
EVM-RPC-DEGRADED alert fired the same second.

The receipt fallback was gated on `spent > 0`, so it only rescued a HALF-failed measurement. RPC
degradation fails both balance reads together, so `spent <= 0` too and the fallback was skipped.
"""
from sniper.execution.evm_real_seams import erc20_sent_in_receipt

_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ME = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
TOK = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _rpc_returning(receipt):
    def _rpc(url, method, params, http=None):
        return {"result": receipt}
    return _rpc


def _receipt(logs, status="0x1", h="0xabc"):
    return {"status": status, "transactionHash": h, "logs": logs}


def _log(token, frm, to, amount):
    return {"address": token,
            "topics": [_TRANSFER, "0x" + frm[2:].rjust(64, "0"), "0x" + to[2:].rjust(64, "0")],
            "data": "0x" + format(amount, "064x")}


def test_a_spend_is_read_from_the_receipt(monkeypatch):
    import sniper.execution.evm_real_seams as S
    logs = [_log(TOK, ME, "0x" + "11" * 20, 61189025000000000)]
    monkeypatch.setattr(S, "_rpc", _rpc_returning(_receipt(logs)))
    assert erc20_sent_in_receipt("0xabc", TOK, ME, "http://rpc") == 61189025000000000


def test_a_receive_is_not_mistaken_for_a_spend(monkeypatch):
    """It matches topics[1] (from), not topics[2] (to) -- the mirror must not double-count."""
    import sniper.execution.evm_real_seams as S
    logs = [_log(TOK, "0x" + "11" * 20, ME, 5)]      # sent TO us, not FROM us
    monkeypatch.setattr(S, "_rpc", _rpc_returning(_receipt(logs)))
    assert erc20_sent_in_receipt("0xabc", TOK, ME, "http://rpc") is None


def test_a_reverted_receipt_yields_nothing(monkeypatch):
    import sniper.execution.evm_real_seams as S
    logs = [_log(TOK, ME, "0x" + "11" * 20, 5)]
    monkeypatch.setattr(S, "_rpc", _rpc_returning(_receipt(logs, status="0x0")))
    assert erc20_sent_in_receipt("0xabc", TOK, ME, "http://rpc") is None


def test_a_mismatched_hash_yields_nothing(monkeypatch):
    """Never attribute another transaction's transfers to this one."""
    import sniper.execution.evm_real_seams as S
    logs = [_log(TOK, ME, "0x" + "11" * 20, 5)]
    monkeypatch.setattr(S, "_rpc", _rpc_returning(_receipt(logs, h="0xdifferent")))
    assert erc20_sent_in_receipt("0xabc", TOK, ME, "http://rpc") is None


def test_the_receipt_fallback_no_longer_requires_a_good_spend_read():
    """The guard that caused WAAI: it read `spent > 0 and got <= 0`."""
    import pathlib, re
    src = pathlib.Path("sniper/execution/evm_swap_executor.py").read_text()
    body = re.sub(r"#.*", "", src)
    assert "if spent > 0 and got <= 0 and receipt_token_fn is not None:" not in body
    assert "if got <= 0 and receipt_token_fn is not None:" in body
