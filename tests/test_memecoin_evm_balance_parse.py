"""An empty `balanceOf` result is UNREADABLE, never zero and never a foreign exception type.

Live case: position 50693 (LARP, 0x43fb2502…) logged `invalid literal for int() with base 16: '0x'`
on every reconcile pass. The reconciler already caught it and recorded the position as `unreadable`,
so nothing was stuck — but `erc20_balance_checked` promises RuntimeError for a failed read, and a
caller catching the documented type would have seen a bare ValueError instead.

Unknown is not zero, and unknown is not a crash of a different colour.
"""
import pytest

from sniper.execution import evm_wallet as W

TOKEN = "0x43fb250234419a47544091ca1b41dcf1cce23b00"
OWNER = "0x" + "11" * 20


def _rpc(result):
    """An `_eth_call` seam that returns one fixed result."""
    return lambda token, data, rpc_url, http=None: result


@pytest.mark.parametrize("empty", ["0x", "0X", "", None, "   "])
def test_an_empty_result_is_unreadable_not_zero(empty):
    with pytest.raises(RuntimeError) as exc:
        W._balance_word(empty, TOKEN)
    assert "no data" in str(exc.value)


@pytest.mark.parametrize("bad", ["garbage", "0xzz", "0x 1", []])
def test_a_malformed_result_raises_the_documented_type(bad):
    with pytest.raises(RuntimeError):
        W._balance_word(bad, TOKEN)


@pytest.mark.parametrize("raw,want", [("0x0", 0), ("0x1a", 26), ("0xde0b6b3a7640000", 10 ** 18)])
def test_a_real_balance_still_parses(raw, want):
    assert W._balance_word(raw, TOKEN) == want


def test_a_genuine_zero_balance_is_still_zero():
    """`0x0` is a wallet that holds none of the token. That is an ANSWER and must not raise."""
    assert W._balance_word("0x0" + "0" * 63, TOKEN) == 0


def test_checked_balance_raises_runtime_error_on_an_empty_result(monkeypatch):
    """The whole point of the `_checked` variant: a caller can tell a failed read from an empty
    wallet. It must not leak a ValueError to a caller catching RuntimeError."""
    monkeypatch.setattr(W, "_eth_call", _rpc("0x"))

    with pytest.raises(RuntimeError):
        W.erc20_balance_checked(OWNER, TOKEN, "https://rpc", decimals=18)


def test_the_fail_closed_readers_keep_their_contracts(monkeypatch):
    """`erc20_balance` and `erc20_balance_raw` document a fail-closed 0 for the sell-sizing callers,
    which bail on a non-positive balance and retry. That is unchanged."""
    monkeypatch.setattr(W, "_eth_call", _rpc("0x"))

    assert W.erc20_balance(OWNER, TOKEN, "https://rpc", decimals=18) == 0.0
    assert W.erc20_balance_raw(OWNER, TOKEN, "https://rpc") == 0


def test_the_reconciler_records_unreadable_rather_than_drift(monkeypatch):
    """END TO END on the live shape. A position whose balance cannot be read must come back as
    `unreadable`, never as drift — a failed read is not evidence that the bag is gone."""
    import sqlite3

    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_reconcile as R

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(conn, source_user_id="u", source_handle="h", mint=TOKEN, ticker="LARP",
                          entry_price=1.0, entry_notional=75.22, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="t",
                          execution_mode="real", network_id="4663")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=75.22,
                  fill_source="chain", fraction=1.0, created_at="t", tx_sig="E")

    def reader(_pos):
        return W._balance_word("0x", TOKEN)          # the live failure

    alerts = []
    findings = R.reconcile_real_positions(conn, balance_fn=reader, alert_fn=lambda *a: alerts.append(a))

    assert [f["status"] for f in findings] == ["unreadable"]
    assert findings[0]["actual"] is None
    assert alerts == [], "a failed read must never page as drift"


def test_raw_checked_balance_keeps_unreadable_distinct_from_zero(monkeypatch):
    monkeypatch.setattr(W, "_eth_call", _rpc("0x"))
    with pytest.raises(RuntimeError):
        W.erc20_balance_raw_checked(OWNER, TOKEN, "https://rpc")
    monkeypatch.setattr(W, "_eth_call", _rpc("0x0"))
    assert W.erc20_balance_raw_checked(OWNER, TOKEN, "https://rpc") == 0
