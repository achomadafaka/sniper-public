"""A bound receipt's complete atomic snapshots must be valid before residue is persisted."""
import copy

import pytest

from sniper.services import memecoin_quote_residue as Q
from test_memecoin_quote_residue_credits import (  # noqa: F401 (conn fixture)
    COIN, EXIT_SIG, NOW, OWNER, SELL_SIG, ZEC, _bal, _entry_residue, _fill, _position,
    _realized, _replace, _sold, conn, plain_exit_receipt, sell_receipt, zphinx_exit_receipt,
)


BAD_BALANCES = [
    pytest.param(("transaction", "message", "accountKeys"), "physical-duplicate",
                 id="duplicate-physical-account"),
    pytest.param(("transaction", "message", "accountKeys"), "physical-cross-side",
                 id="changed-physical-account-index-and-mint"),
    pytest.param(("meta", "preTokenBalances"), "duplicate", id="duplicate-pre-index"),
    pytest.param(("meta", "postTokenBalances"), "duplicate", id="duplicate-post-index"),
    pytest.param(("meta", "preTokenBalances"), None, id="missing-pre-snapshot"),
    pytest.param(("meta", "postTokenBalances"), {}, id="malformed-post-snapshot"),
    pytest.param(("meta", "preTokenBalances", 0), None, id="malformed-balance-row"),
    pytest.param(("meta", "preTokenBalances", 0, "accountIndex"), -1, id="negative-index"),
    pytest.param(("meta", "postTokenBalances", 0, "accountIndex"), 8, id="out-of-range-index"),
    pytest.param(("meta", "preTokenBalances", 0, "accountIndex"), True, id="boolean-index"),
    pytest.param(("meta", "preTokenBalances", 0, "accountIndex"), "2", id="string-index"),
    pytest.param(("meta", "preTokenBalances", 0, "mint"), "", id="missing-mint"),
    pytest.param(("meta", "preTokenBalances", 0, "owner"), None, id="missing-owner"),
    pytest.param(("meta", "postTokenBalances", 0, "mint"), "other-mint", id="changed-mint"),
    pytest.param(("meta", "postTokenBalances", 0, "owner"), "foreign", id="changed-owner"),
    pytest.param(("meta", "postTokenBalances", 0, "uiTokenAmount", "decimals"), 7,
                 id="changed-decimals"),
    pytest.param(("meta", "preTokenBalances", 0, "uiTokenAmount", "decimals"), True,
                 id="boolean-decimals"),
    pytest.param(("meta", "preTokenBalances", 0, "uiTokenAmount", "decimals"), 256,
                 id="overflow-decimals"),
    pytest.param(("meta", "preTokenBalances", 0, "uiTokenAmount", "decimals"), "8",
                 id="string-decimals"),
    pytest.param(("meta", "preTokenBalances", 0, "uiTokenAmount"), {}, id="missing-atomic-metadata"),
]
BAD_BALANCES += [
    pytest.param(("meta", "preTokenBalances", 0, "uiTokenAmount", "amount"), amount,
                 id="amount-%s" % name)
    for name, amount in [
        ("none", None), ("empty", ""), ("boolean", True), ("float", 20.5),
        ("fraction", "20.5"), ("whitespace", " 20"), ("signed", "+20"),
        ("negative-int", -1), ("negative-string", "-1"), ("overflow-int", 2 ** 64),
        ("overflow-string", str(2 ** 64)), ("unicode", "٢"), ("hex", "0x14"),
        ("exponent", "1e2"),
    ]
]


def _malformed(tx, path, value):
    if value == "physical-cross-side":
        keys = tx["transaction"]["message"]["accountKeys"]
        keys[4] = copy.deepcopy(keys[2])
        row = next(row for row in tx["meta"]["postTokenBalances"] if row["mint"] == ZEC)
        row.update(accountIndex=4, mint="other-mint")
        row["uiTokenAmount"]["decimals"] = 7
        return tx
    if value == "physical-duplicate":
        keys = tx["transaction"]["message"]["accountKeys"]
        keys[4] = copy.deepcopy(keys[2])
        for field in ("preTokenBalances", "postTokenBalances"):
            row = next(row for row in tx["meta"][field] if row["mint"] == ZEC)
            tx["meta"][field].append({**copy.deepcopy(row), "accountIndex": 4})
        return tx
    if value == "duplicate":
        rows = tx[path[0]][path[1]]
        rows.append(copy.deepcopy(rows[0]))
        return tx
    return _replace(tx, path, value)


@pytest.mark.parametrize("path,value", BAD_BALANCES)
def test_malformed_sell_atoms_stay_uncredited_and_the_correct_receipt_can_retry(conn, path, value):
    _position(conn, attempt="source", realized=0)
    _entry_residue(conn, atomic=10, attempt="source")
    _sold(conn, balance=20, usd=100)
    bad = _malformed(sell_receipt(consumed=20), path, value)
    [result] = Q.credit_sells_once(conn, receipt_fn=lambda sig: bad, owner=OWNER, now_iso=NOW)
    assert result["state"] == "sell_receipt_unreadable"
    assert _realized(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0
    assert SELL_SIG not in Q._SELL_RECEIPTS
    [result] = Q.credit_sells_once(conn, receipt_fn=lambda sig: sell_receipt(consumed=20),
                                  owner=OWNER, now_iso=NOW)
    assert result["state"] == "credited" and _realized(conn) == 50


@pytest.mark.parametrize("path,value", BAD_BALANCES)
def test_malformed_exit_atoms_stay_unknown_and_the_correct_receipt_can_retry(conn, path, value):
    _position(conn)
    _fill(conn)
    bad = _malformed(zphinx_exit_receipt(), path, value)
    assert Q.read_exit_residue_once(conn, receipt_fn=lambda sig: bad, owner=OWNER,
                                    now_iso=NOW) == [(EXIT_SIG, "pending", 0)]
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue").fetchone()[0] == 0
    assert Q.exit_residue_from_receipt(bad, OWNER, COIN) is None
    assert Q.read_exit_residue_once(conn, receipt_fn=lambda sig: zphinx_exit_receipt(), owner=OWNER,
                                    now_iso=NOW) == [(EXIT_SIG, "read", 1)]


@pytest.mark.parametrize("factory", [sell_receipt, zphinx_exit_receipt])
def test_invalid_foreign_account_atoms_or_same_mint_decimals_are_not_ignored(factory):
    tx = factory()
    for side in ("preTokenBalances", "postTokenBalances"):
        tx["meta"][side].append(_bal(ZEC, 3, 8, 4, owner="foreign"))
    assert Q.exit_residue_from_receipt(tx, OWNER, COIN) is not None
    tx["meta"]["preTokenBalances"][-1]["uiTokenAmount"]["amount"] = 3.5
    assert Q.exit_residue_from_receipt(tx, OWNER, COIN) is None
    tx["meta"]["preTokenBalances"][-1]["uiTokenAmount"]["amount"] = "3"
    for side in ("preTokenBalances", "postTokenBalances"):
        tx["meta"][side][-1]["uiTokenAmount"]["decimals"] = 7
    assert Q.exit_residue_from_receipt(tx, OWNER, COIN) is None


def test_invalid_cached_atoms_are_evicted_so_a_correct_read_can_retry(conn):
    _position(conn, attempt="source", realized=0)
    _entry_residue(conn, atomic=10, attempt="source")
    _sold(conn, balance=20, usd=100)
    Q._SELL_RECEIPTS[SELL_SIG] = _malformed(sell_receipt(consumed=20),
                                          ("meta", "preTokenBalances"), "duplicate")
    [result] = Q.credit_sells_once(conn, receipt_fn=lambda sig: sell_receipt(consumed=20),
                                  owner=OWNER, now_iso=NOW)
    assert result["state"] == "sell_receipt_unreadable" and _realized(conn) == 0
    [result] = Q.credit_sells_once(conn, receipt_fn=lambda sig: sell_receipt(consumed=20),
                                  owner=OWNER, now_iso=NOW)
    assert result["state"] == "credited" and _realized(conn) == 50


def test_distinct_same_mint_accounts_and_new_or_closed_accounts_keep_exact_atoms():
    tx = zphinx_exit_receipt()
    tx["meta"]["preTokenBalances"].append(_bal(ZEC, 10, 8, 4))
    tx["meta"]["postTokenBalances"].append(_bal(ZEC, 30, 8, 4))
    [row] = Q.exit_residue_from_receipt(tx, OWNER, COIN)["rows"]
    assert row["atomic"] == 3254385
    tx["meta"]["preTokenBalances"].pop()
    [row] = Q.exit_residue_from_receipt(tx, OWNER, COIN)["rows"]
    assert row["atomic"] == 3254395
    tx["meta"]["postTokenBalances"].pop()
    tx["meta"]["preTokenBalances"].append(_bal(ZEC, 10, 8, 4))
    [row] = Q.exit_residue_from_receipt(tx, OWNER, COIN)["rows"]
    assert row["atomic"] == 3254355
    assert Q.exit_residue_from_receipt(plain_exit_receipt(), OWNER, COIN) == {
        "rows": [], "negative": []}
