"""Quote-asset residue counts in position PnL (operator order 2026-10-07 ~15:30Z, via Alpha).

Zphinx (71502): the native pump exit sized its ZEC -> USDC tail to the curve leg's MINIMUM, so
3,254,365 atomic ZEC stayed in the wallet; the entry leg left 184,497 more. The operator sold the
ZEC through Other Holdings for $45.42, and none of it reached the position: booked +$4.61, true
~+$49.99. These tests pin:
  - every exit leaves a residue row (or a known zero) read from its own receipt, off the exit path;
  - a holding sell's proceeds are credited pro rata by atomic amount to the residue it consumed,
    append-only, into realized_pnl, once; the foreign share is never booked;
  - unknown never becomes zero: an unread or unreadable exit before a sell holds that sell's credit;
  - MEMECOIN_QUOTE_RESIDUE_CREDITS off = both passes inert; a credit can be reverted.
"""
import contextlib
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_quote_residue as Q
from sniper.settings import memecoin as cfg

OWNER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
COIN = "6BamsvP4ZMrBStjJJfri8XEfS3iu5wacpHc1hWX1NijF"
ZEC = "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
LINK = "LinkhB3afbBKb2EQQu7s7umdZceV3wcvAUJhQAfQ23L"
ENTRY_SIG, EXIT_SIG, SELL_SIG = "3Zp4eWC1", "5rApMRHr", "2AkdaLJP"
OPENED, CLOSED = "2026-10-07T14:59:08.796876+00:00", "2026-10-07T14:59:36.150575+00:00"
SOLD_AT, SELL_BLOCK_TIME = "2026-10-07T15:09:51.978483+00:00", 1791385791   # 15:09:51Z
NOW = "2026-10-07T16:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_pages(monkeypatch):
    from sniper.services import memecoin_alerts as A
    monkeypatch.setattr(A, "send_alert", lambda *a, **k: (False, 0.0))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS_SINCE", "2026-09-09T00:00:00+00:00",
                        raising=False)
    monkeypatch.setattr(Q, "_SELL_RECEIPTS", {})
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _bal(mint, amount, decimals, index, owner=OWNER):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def _keys(n=8):
    return [{"pubkey": OWNER, "signer": True}] + [{"pubkey": "acct%d" % i, "signer": False}
                                                  for i in range(1, n)]


def _transfer(source, destination, amount):
    return {"parsed": {"type": "transferChecked", "info": {
        "source": source, "destination": destination, "tokenAmount": {"amount": str(amount)}}}}


def zphinx_exit_receipt():
    """The real 71502 close: coin all sold, ZEC curve out 16,271,829, tail in 13,017,464 (0.80)."""
    return {"slot": 454256732, "blockTime": 1791385176, "transaction": {
        "signatures": [EXIT_SIG], "message": {"accountKeys": _keys(), "instructions": []}},
        "meta": {"err": None,
                 "preTokenBalances": [_bal(COIN, 7340884560782, 6, 1), _bal(ZEC, 187710, 8, 2),
                                      _bal(USDC, 333848019, 6, 3)],
                 "postTokenBalances": [_bal(COIN, 0, 6, 1), _bal(ZEC, 3442075, 8, 2),
                                       _bal(USDC, 505374786, 6, 3)],
                 "innerInstructions": [{"index": 2, "instructions": [
                     _transfer("curve_vault", "acct2", 16271829),
                     _transfer("acct2", "tail_pool", 13017464)]}]}}


def plain_exit_receipt(sig="plainSig"):
    """A coin -> USDC exit that leaves nothing: wSOL wrap/unwrap nets zero."""
    return {"slot": 454256000, "blockTime": 1791385000, "transaction": {
        "signatures": [sig], "message": {"accountKeys": _keys(), "instructions": []}},
        "meta": {"err": None,
                 "preTokenBalances": [_bal(COIN, 5000, 6, 1), _bal(USDC, 100, 6, 3), _bal(WSOL, 0, 9, 4)],
                 "postTokenBalances": [_bal(COIN, 0, 6, 1), _bal(USDC, 9_000_100, 6, 3),
                                       _bal(WSOL, 0, 9, 4)],
                 "innerInstructions": []}}


def sell_receipt(consumed=3442075, block_time=SELL_BLOCK_TIME, *, sig=SELL_SIG):
    return {"slot": 454258300, "blockTime": block_time, "transaction": {
        "signatures": [sig], "message": {"accountKeys": _keys(), "instructions": []}},
        "meta": {"err": None, "preTokenBalances": [_bal(ZEC, consumed, 8, 2), _bal(USDC, 0, 6, 3)],
                 "postTokenBalances": [_bal(ZEC, 0, 8, 2), _bal(USDC, 45423891, 6, 3)],
                 "innerInstructions": []}}


def _position(conn, *, pid=71502, mint=COIN, realized=4.606769999999955, is_open=0,
              attempt="e375d2fdd4ed4f6bbdf81f85756a11c7", closed_at=CLOSED):
    conn.execute(
        "INSERT INTO memecoin_positions (id, source_user_id, mint, ticker, entry_price, entry_notional,"
        " is_open, opened_at, closed_at, realized_pnl, execution_mode, network_id, strategy, attempt_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?, 'real', 'solana', 'alpha_b', ?)",
        (pid, "u1", mint, "Zphinx", 2.27e-05, 166.919997, is_open, OPENED,
         None if is_open else closed_at, None if is_open else realized, attempt))
    conn.commit()


def _fill(conn, *, pid=71502, kind="bracket_exit", sig=EXIT_SIG, at=CLOSED):
    conn.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, fill_source,"
                 " created_at, tx_sig, network_id) VALUES (?,?,?,?,?,?,?,?, 'solana')",
                 (pid, kind, 1.0, 1.0, 171.526767, "chain", at, sig))
    conn.commit()


def _entry_residue(conn, atomic=184497, attempt="e375d2fdd4ed4f6bbdf81f85756a11c7", at=OPENED,
                   mint=ZEC):
    assert Q.record_fill_residue(conn, quote_mint=mint, residue_atomic=atomic, quote_usd=1319.62,
                                 decimals=8, venue="pump_curve", attempt_id=attempt,
                                 entry_sig=ENTRY_SIG, recorded_at=at)


def _sold(conn, *, intent="f97d916d", mint=ZEC, balance=3442075, usd=45.423891, sig=SELL_SIG,
          resolved_at=SOLD_AT):
    conn.execute("INSERT INTO memecoin_holding_sell_intents (intent_id, network_id, mint, requested_at,"
                 " expires_at, state, claimed_at, resolved_at, sig, usd_received, balance_raw, allow_small)"
                 " VALUES (?, 'solana', ?, ?, ?, 'sold', ?, ?, ?, ?, ?, 1)",
                 (intent, mint, "2026-10-07T15:09:47+00:00", "2026-10-07T16:09:47+00:00", resolved_at,
                  resolved_at, sig, usd, str(balance)))
    conn.commit()


def _receipts(mapping):
    calls = []

    def fn(sig):
        calls.append(sig)
        return mapping.get(sig)
    fn.calls = calls
    return fn


def _realized(conn, pid=71502):
    return conn.execute("SELECT realized_pnl FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0]


INVALID_RECEIPTS = [
    (("transaction", "signatures"), ["foreignSig"]),
    (("transaction", "message", "accountKeys"), [{"pubkey": "foreign", "signer": True}]),
    (("transaction", "message", "accountKeys"), [{"pubkey": OWNER, "signer": False}]),
    (("transaction", "message", "accountKeys"), []),
    (("slot",), None),
    (("slot",), 0),
    (("slot",), -1),
    (("slot",), True),
    (("slot",), "454258300"),
    (("meta",), {}),
    (("meta", "err"), {"InstructionError": [1, "failed"]}),
]


def _replace(tx, path, value):
    node = tx
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return tx


@pytest.mark.parametrize("path,value", INVALID_RECEIPTS)
def test_an_unbound_exit_receipt_never_records_residue(conn, path, value):
    _position(conn)
    _fill(conn)
    bad = _replace(zphinx_exit_receipt(), path, value)
    assert Q.read_exit_residue_once(conn, receipt_fn=lambda sig: bad, owner=OWNER,
                                    now_iso=NOW) == [(EXIT_SIG, "pending", 0)]
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue").fetchone()[0] == 0
    assert _realized(conn) == pytest.approx(4.60677)
    assert Q.read_exit_residue_once(conn, receipt_fn=lambda sig: zphinx_exit_receipt(), owner=OWNER,
                                    now_iso=NOW) == [(EXIT_SIG, "read", 1)]


@pytest.mark.parametrize("path,value", INVALID_RECEIPTS + [
    (("blockTime",), None), (("blockTime",), -1), (("blockTime",), True),
])
def test_an_unbound_sell_receipt_cannot_credit_or_poison_the_cache(conn, path, value):
    _position(conn)
    _entry_residue(conn)
    _sold(conn)
    bad = _replace(sell_receipt(), path, value)
    [out] = Q.credit_sells_once(conn, receipt_fn=lambda sig: bad, owner=OWNER, now_iso=NOW)
    assert out["state"] == "sell_receipt_unreadable"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0
    assert _realized(conn) == pytest.approx(4.60677)
    [out] = Q.credit_sells_once(conn, receipt_fn=lambda sig: sell_receipt(), owner=OWNER, now_iso=NOW)
    assert out["state"] == "credited"
    assert _realized(conn) > 4.60677


# ---- pure ------------------------------------------------------------------------------------------

def test_an_exit_receipt_yields_every_other_mint_it_left_priced_by_its_own_tail():
    out = Q.exit_residue_from_receipt(zphinx_exit_receipt(), OWNER, COIN)
    assert out["negative"] == []
    [row] = out["rows"]
    assert (row["mint"], row["atomic"], row["decimals"]) == (ZEC, 3254365, 8)
    # tail rate = 171.526767 USDC / 0.13017464 ZEC = $1,317.67/ZEC -> 0.03254365 ZEC = $42.88
    assert row["usd"] == pytest.approx(42.8817, abs=1e-3)


def test_coin_usdc_and_wsol_are_never_residue_and_a_clean_exit_is_an_empty_list():
    assert Q.exit_residue_from_receipt(plain_exit_receipt(), OWNER, COIN) == {"rows": [], "negative": []}


def test_an_exit_that_spent_a_prior_balance_is_flagged_never_recorded():
    rec = zphinx_exit_receipt()
    rec["meta"]["postTokenBalances"][1] = _bal(ZEC, 100000, 8, 2)        # below the 187,710 before
    out = Q.exit_residue_from_receipt(rec, OWNER, COIN)
    assert out["rows"] == [] and out["negative"] == [ZEC]


def test_a_failed_or_unparseable_receipt_is_unknown_not_zero():
    rec = zphinx_exit_receipt()
    rec["meta"]["err"] = {"InstructionError": [2, {"Custom": 6003}]}
    assert Q.exit_residue_from_receipt(rec, OWNER, COIN) is None
    assert Q.exit_residue_from_receipt(None, OWNER, COIN) is None
    assert Q.exit_residue_from_receipt({"meta": {"err": None}}, OWNER, COIN) is None


def test_a_sell_is_split_pro_rata_by_atomic_and_the_foreign_share_is_not_booked():
    plan = Q.plan_sell_credits([{"residue_id": 9, "position_id": 71502, "atomic": 184497},
                                {"residue_id": 10, "position_id": 71502, "atomic": 3254365}],
                               consumed_atomic=3442075, usd_received=45.423891)
    assert [round(c["usd"], 6) for c in plan["credits"]] == [2.434744, 42.946746]
    assert plan["foreign_atomic"] == 3213 and round(plan["foreign_usd"], 6) == 0.042401
    assert plan["short"] is False


def test_a_sell_smaller_than_the_recorded_residue_splits_over_the_residue_and_is_flagged():
    plan = Q.plan_sell_credits([{"residue_id": 1, "position_id": 1, "atomic": 600},
                                {"residue_id": 2, "position_id": 2, "atomic": 400}],
                               consumed_atomic=800, usd_received=8.0)
    assert [c["usd"] for c in plan["credits"]] == [pytest.approx(4.8), pytest.approx(3.2)]
    assert sum(c["usd"] for c in plan["credits"]) == pytest.approx(8.0)
    assert plan["short"] is True and plan["foreign_atomic"] == 0 and plan["foreign_usd"] == 0.0


# ---- the exit read pass -----------------------------------------------------------------------------

def test_the_read_pass_writes_the_exit_residue_bound_to_its_position(conn):
    _position(conn)
    _fill(conn)
    receipts = _receipts({EXIT_SIG: zphinx_exit_receipt()})
    Q.read_exit_residue_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    rows = [dict(r) for r in conn.execute(
        "SELECT position_id, kind, quote_mint, residue_atomic, source_sig, source_at FROM memecoin_quote_residue")]
    assert rows == [{"position_id": 71502, "kind": "exit", "quote_mint": ZEC, "residue_atomic": "3254365",
                     "source_sig": EXIT_SIG, "source_at": CLOSED}]
    Q.read_exit_residue_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    assert receipts.calls == [EXIT_SIG]                                   # read once, never again
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue").fetchone()[0] == 1


def test_an_exit_with_nothing_left_is_a_known_zero(conn):
    _position(conn)
    _fill(conn, sig="plainSig")
    Q.read_exit_residue_once(conn, receipt_fn=_receipts({"plainSig": plain_exit_receipt()}), owner=OWNER,
                             now_iso=NOW)
    assert conn.execute("SELECT state FROM memecoin_quote_residue_reads WHERE sig='plainSig'").fetchone()[0] == "read"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue").fetchone()[0] == 0


def test_an_unreadable_exit_receipt_stays_unknown_and_gives_up_after_three_tries(conn):
    _position(conn)
    _fill(conn)
    none = _receipts({})
    for _ in range(5):
        Q.read_exit_residue_once(conn, receipt_fn=none, owner=OWNER, now_iso=NOW)
    assert len(none.calls) == 3
    assert conn.execute("SELECT state, tries FROM memecoin_quote_residue_reads").fetchone()[:] == ("unreadable", 3)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue").fetchone()[0] == 0


def test_the_read_pass_skips_entries_other_chains_and_exits_before_the_cutoff(conn, monkeypatch):
    _position(conn)
    _fill(conn, kind="entry", sig=ENTRY_SIG, at=OPENED)
    _fill(conn, sig="oldSig", at="2026-09-01T00:00:00+00:00")
    _position(conn, pid=2, mint="0xcoin", attempt="evm-a")
    conn.execute("UPDATE memecoin_positions SET network_id='4663' WHERE id=2")
    _fill(conn, pid=2, sig="0xhash")
    receipts = _receipts({})
    Q.read_exit_residue_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    assert receipts.calls == []


# ---- the credit pass --------------------------------------------------------------------------------

def _zphinx_ledger(conn):
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    Q.read_exit_residue_once(conn, receipt_fn=_receipts({EXIT_SIG: zphinx_exit_receipt()}), owner=OWNER,
                             now_iso=NOW)
    _sold(conn)


def test_a_holding_sell_is_credited_once_into_the_owner_realized_pnl(conn):
    _zphinx_ledger(conn)
    receipts = _receipts({SELL_SIG: sell_receipt()})
    Q.credit_sells_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    assert _realized(conn) == pytest.approx(4.60677 + 2.434744 + 42.946746, abs=1e-6)     # 49.988
    credits = [dict(r) for r in conn.execute(
        "SELECT position_id, intent_id, residue_atomic, consumed_atomic, foreign_atomic, round(usd, 6) AS usd,"
        " realized_before IS NOT NULL AS b FROM memecoin_quote_residue_credits ORDER BY residue_atomic")]
    assert [(c["residue_atomic"], c["usd"], c["foreign_atomic"]) for c in credits] == [
        ("184497", 2.434744, "3213"), ("3254365", 42.946746, "3213")]
    assert {c["intent_id"] for c in credits} == {"f97d916d"} and all(c["b"] for c in credits)
    assert {r[0] for r in conn.execute("SELECT swept_by FROM memecoin_quote_residue")} == {"f97d916d"}
    Q.credit_sells_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)          # idempotent
    assert _realized(conn) == pytest.approx(49.98826, abs=1e-5)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 2


def test_a_sell_waits_while_an_exit_before_it_is_unread(conn):
    _position(conn)
    _entry_residue(conn)
    _fill(conn)                                    # the exit is not read yet: its residue is unknown
    _sold(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    assert _realized(conn) == pytest.approx(4.60677)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0


def test_an_unreadable_exit_pages_once_and_never_holds_later_sells(conn, monkeypatch):
    """Gamma LOW / Alpha 2026-10-07: one bad receipt must not stop the feature silently. A read that
    gives up pages once, naming the signature; sells after it proceed (its residue, if any, falls
    to the foreign share and is never booked)."""
    from sniper.services import memecoin_alerts as A
    pages = []
    monkeypatch.setattr(A, "send_alert", lambda kind, text, **kw: pages.append((kind, text, kw.get("source_id"))))
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    for _ in range(5):
        Q.read_exit_residue_once(conn, receipt_fn=_receipts({}), owner=OWNER, now_iso=NOW)
    assert len(pages) == 1 and EXIT_SIG in pages[0][1] and pages[0][2] == "quote-residue-unreadable:" + EXIT_SIG
    _sold(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    assert _realized(conn) == pytest.approx(4.60677 + 2.434744, abs=1e-6)     # entry share; the exit's is unknown


def test_a_pending_exit_read_still_holds_the_sell(conn):
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    Q.read_exit_residue_once(conn, receipt_fn=_receipts({}), owner=OWNER, now_iso=NOW)     # 1 of 3 tries
    _sold(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0


def test_residue_that_landed_after_the_sell_is_not_its_owner(conn):
    _zphinx_ledger(conn)
    _position(conn, pid=2, mint="LaterCoin", attempt="later-a", closed_at="2026-10-07T15:30:00+00:00")
    _entry_residue(conn, atomic=500, attempt="later-a", at="2026-10-07T15:20:00+00:00")
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    assert _realized(conn, 2) == pytest.approx(4.60677)                  # untouched
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits WHERE position_id=2"
                        ).fetchone()[0] == 0


def test_an_unpriced_owner_waits_and_is_credited_once_it_is_priced(conn):
    _zphinx_ledger(conn)
    conn.execute("UPDATE memecoin_positions SET realized_pnl=NULL WHERE id=71502")
    conn.commit()
    receipts = _receipts({SELL_SIG: sell_receipt()})
    Q.credit_sells_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    assert _realized(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0
    conn.execute("UPDATE memecoin_positions SET realized_pnl=4.60677 WHERE id=71502")
    conn.commit()
    Q.credit_sells_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW)
    assert _realized(conn) == pytest.approx(49.98826, abs=1e-5)


def test_a_sell_whose_proceeds_were_not_read_is_not_credited(conn):
    _zphinx_ledger(conn)
    conn.execute("UPDATE memecoin_holding_sell_intents SET usd_received=NULL")
    conn.commit()
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 0


def test_a_credit_can_flip_a_losing_coin_and_that_is_deliberate(conn):
    """66021 (LinkhB3a mirror exit): booked -$10.82; its residue sold for $16.23 of a $23.31 sell."""
    _position(conn, pid=66021, mint="LinkCoin", realized=-10.815588, attempt="link-a",
              closed_at="2026-09-27T13:35:00+00:00")
    conn.execute("INSERT INTO memecoin_quote_residue (network_id, quote_mint, residue_atomic, residue_usd,"
                 " recorded_at, position_id, kind, source_sig, source_at) VALUES"
                 " ('solana', ?, '1146335626', 16.257, ?, 66021, 'exit', 'wig6M1Ep', ?)",
                 (LINK, NOW, "2026-09-27T13:35:00+00:00"))
    conn.execute("INSERT INTO memecoin_quote_residue_reads (sig, position_id, fill_kind, state, tries, read_at)"
                 " VALUES ('wig6M1Ep', 66021, 'mirror_exit', 'read', 1, ?)", (NOW,))
    conn.commit()
    _sold(conn, intent="0fd53319", mint=LINK, balance=1646892730, usd=23.310073, sig="5XwqHbG4",
          resolved_at="2026-09-27T13:39:00+00:00")
    rec = sell_receipt(consumed=1646892730, block_time=1790516340, sig="5XwqHbG4")  # 13:39:00Z
    rec["meta"]["preTokenBalances"][0]["mint"] = rec["meta"]["postTokenBalances"][0]["mint"] = LINK
    assert m.coin_identity("solana", "LinkCoin") in m.losing_real_coin_ids(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({"5XwqHbG4": rec}), owner=OWNER, now_iso=NOW)
    assert _realized(conn, 66021) == pytest.approx(-10.815588 + 16.225202, abs=1e-5)
    assert m.coin_identity("solana", "LinkCoin") not in m.losing_real_coin_ids(conn)


def test_both_passes_are_inert_with_the_flag_off(conn, monkeypatch):
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    _sold(conn)
    monkeypatch.setattr(cfg, "MEMECOIN_QUOTE_RESIDUE_CREDITS", False, raising=False)
    receipts = _receipts({EXIT_SIG: zphinx_exit_receipt(), SELL_SIG: sell_receipt()})
    assert Q.read_exit_residue_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW) == []
    assert Q.credit_sells_once(conn, receipt_fn=receipts, owner=OWNER, now_iso=NOW) == []
    assert receipts.calls == [] and _realized(conn) == pytest.approx(4.60677)


def test_a_credit_is_reverted_append_only(conn):
    _zphinx_ledger(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    ids = [r[0] for r in conn.execute("SELECT id FROM memecoin_quote_residue_credits")]
    for cid in ids:
        assert m.revert_quote_residue_credit(conn, cid, now_iso=NOW, reason="test rollback")
        assert not m.revert_quote_residue_credit(conn, cid, now_iso=NOW, reason="twice")
    assert _realized(conn) == pytest.approx(4.60677, abs=1e-6)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits WHERE reverted_at IS NOT NULL"
                        ).fetchone()[0] == 2


def test_the_card_shows_pending_and_credited_residue_apart(conn):
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    Q.read_exit_residue_once(conn, receipt_fn=_receipts({EXIT_SIG: zphinx_exit_receipt()}), owner=OWNER,
                             now_iso=NOW)
    before = m.quote_residue_by_position(conn, [71502])[71502]
    assert before["pending_usd"] == pytest.approx(2.4289 + 42.8817, abs=1e-2) and before["credited_usd"] == 0.0
    _sold(conn)
    Q.credit_sells_once(conn, receipt_fn=_receipts({SELL_SIG: sell_receipt()}), owner=OWNER, now_iso=NOW)
    after = m.quote_residue_by_position(conn, [71502])[71502]
    assert after["pending_usd"] == 0.0 and after["credited_usd"] == pytest.approx(45.38149, abs=1e-5)


def test_the_exit_booking_counts_usdc_only_never_the_quote_residue():
    """The credit is safe only because the exit's own booking never valued the residue."""
    from sniper.execution.solana_swap_executor import receipt_exit_proceeds
    rec = zphinx_exit_receipt()
    assert receipt_exit_proceeds(rec, signature=EXIT_SIG, owner=OWNER) == pytest.approx(171.526767)


def test_the_same_credit_applied_twice_books_once(conn):
    """The ledger guard itself (UNIQUE residue_id + the done check), not just the pass's filter."""
    _position(conn)
    _entry_residue(conn)
    rid = conn.execute("SELECT id FROM memecoin_quote_residue").fetchone()[0]
    credit = [{"residue_id": rid, "position_id": 71502, "atomic": 184497, "usd": 2.434744}]
    kw = dict(intent_id="f97d916d", mint=ZEC, credits=credit, consumed_atomic=3442075, foreign_atomic=3213,
              foreign_usd=0.042401, short=False, now_iso=NOW)
    assert m.apply_quote_residue_credits(conn, **kw) == 1
    assert m.apply_quote_residue_credits(conn, **kw) == 0
    assert m.apply_quote_residue_credits(conn, **{**kw, "intent_id": "other"}) == 0
    assert _realized(conn) == pytest.approx(4.60677 + 2.434744, abs=1e-6)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 1


def test_the_closed_ledger_and_the_position_card_carry_pending_and_credited_residue(monkeypatch, tmp_path):
    from flask import Flask
    from sniper.routes import api_memecoin as api
    from _web_csrf import csrf_client
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    _position(c)
    _entry_residue(c)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    cl = csrf_client(app)
    row = cl.get("/api/memecoin/real/closed").get_json()["closed"][0]
    assert row["quote_residue"] == {"pending_usd": pytest.approx(2.4346, abs=1e-3), "credited_usd": 0.0}
    detail = cl.get("/api/memecoin/real/positions/71502").get_json()["position"]
    assert detail["quote_residue"]["pending_usd"] == pytest.approx(2.4346, abs=1e-3)
    _position(c, pid=2, mint="OtherCoin", attempt="other-a")
    rows = {r["id"]: r for r in cl.get("/api/memecoin/real/closed").get_json()["closed"]}
    assert rows[2]["quote_residue"] is None                              # no residue: nothing to show


def _backfill_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_quote_residue_backfill.py"
    spec = importlib.util.spec_from_file_location("memecoin_quote_residue_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_backfill_dry_run_books_on_a_copy_and_leaves_the_ledger_untouched(conn, capsys):
    bf = _backfill_module()
    _position(conn)
    _entry_residue(conn)
    _fill(conn)
    _sold(conn)
    receipts = _receipts({EXIT_SIG: zphinx_exit_receipt(), SELL_SIG: sell_receipt()})
    copy = bf.copy_ledger(conn)
    before = {r[0]: r[1] for r in copy.execute("SELECT id, realized_pnl FROM memecoin_positions")}
    sells = bf.run(copy, receipts, OWNER, NOW)
    bf.report(copy, sells, before)
    assert _realized(copy) == pytest.approx(49.98826, abs=1e-5)
    assert _realized(conn) == pytest.approx(4.60677)                    # the ledger never moved
    for table in ("memecoin_quote_residue_credits", "memecoin_quote_residue_reads"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    out = capsys.readouterr().out
    assert "71502" in out and "TOTAL credit 45.3815 over 1 positions" in out
    assert "foreign (never booked): $0.0424 over 1 sells" in out


def test_a_later_sell_never_claims_residue_an_earlier_sell_of_the_mint_consumed(conn):
    """Each holding sell takes the WHOLE balance, so residue before a sold sell is that sell's even
    while it cannot be credited yet (here its receipt is unreadable)."""
    _zphinx_ledger(conn)                                                    # 71502 residue, sell f97d916d
    _position(conn, pid=2, mint="LaterCoin", attempt="later-a", closed_at="2026-10-07T15:30:00+00:00")
    _entry_residue(conn, atomic=500, attempt="later-a", at="2026-10-07T15:20:00+00:00")
    _sold(conn, intent="second", balance=500, usd=0.65, sig="secondSig", resolved_at="2026-10-07T15:40:00+00:00")
    rec = sell_receipt(consumed=500, block_time=1791387600)                 # 2026-10-07T15:40:00Z
    rec["transaction"]["signatures"] = ["secondSig"]
    Q.credit_sells_once(conn, receipt_fn=_receipts({"secondSig": rec}), owner=OWNER, now_iso=NOW)
    assert _realized(conn) == pytest.approx(4.60677)                      # the first sell's owners wait
    assert _realized(conn, 2) == pytest.approx(4.60677 + 0.65)              # the second sold only its own
    assert {r[0] for r in conn.execute("SELECT intent_id FROM memecoin_quote_residue_credits")} == {"second"}


def test_a_residue_can_be_credited_reverted_and_credited_again_twice(conn):
    """Gamma LOW: the second revert of a re-credited residue raised UNIQUE and rolled back."""
    _position(conn)
    _entry_residue(conn)
    rid = conn.execute("SELECT id FROM memecoin_quote_residue").fetchone()[0]
    kw = dict(intent_id="f97d916d", mint=ZEC, credits=[{"residue_id": rid, "position_id": 71502, "atomic": 184497,
              "usd": 2.5}], consumed_atomic=184497, foreign_atomic=0, foreign_usd=0.0, short=False, now_iso=NOW)
    for _ in range(2):
        assert m.apply_quote_residue_credits(conn, **kw) == 1
        cid = conn.execute("SELECT id FROM memecoin_quote_residue_credits WHERE reverted_at IS NULL").fetchone()[0]
        assert m.revert_quote_residue_credit(conn, cid, now_iso=NOW, reason="test")
    assert _realized(conn) == pytest.approx(4.60677, abs=1e-9)
    assert conn.execute("SELECT COUNT(*), COUNT(DISTINCT residue_id) FROM memecoin_quote_residue_credits").fetchone()[:] == (2, 1)
    assert m.quote_residue_by_position(conn, [71502])[71502]["credited_usd"] == 0.0


def test_a_residue_above_int64_splits_exactly():
    """CAST(... AS INTEGER) saturates at 2**63 (Gamma nit): amounts stay Python ints end to end."""
    big = 2 ** 64 + 7
    plan = Q.plan_sell_credits([{"residue_id": 1, "position_id": 1, "atomic": str(big)}],
                               consumed_atomic=str(big), usd_received=3.0)
    assert plan["credits"][0]["usd"] == pytest.approx(3.0) and plan["foreign_atomic"] == 0


def test_the_owner_query_keeps_huge_amounts_exact(conn):
    _position(conn)
    _entry_residue(conn, atomic=2 ** 64 + 7)
    [owner] = m.quote_residue_owners(conn, ZEC, before_iso=NOW, intent_id="x")
    assert int(owner["atomic"]) == 2 ** 64 + 7


OLD_CREDITS_DDL = """CREATE TABLE memecoin_quote_residue_credits(
        id INTEGER PRIMARY KEY AUTOINCREMENT, residue_id INTEGER NOT NULL UNIQUE,
        position_id INTEGER NOT NULL, intent_id TEXT NOT NULL, quote_mint TEXT NOT NULL,
        residue_atomic TEXT NOT NULL, consumed_atomic TEXT NOT NULL, foreign_atomic TEXT NOT NULL,
        foreign_usd REAL, usd REAL NOT NULL, short INTEGER NOT NULL DEFAULT 0,
        realized_before REAL, realized_after REAL, credited_at TEXT NOT NULL,
        reverted_at TEXT, revert_reason TEXT)"""


def _old_ledger(tmp_path, rows=()):
    """A ledger the 39c3fd840 release created (column UNIQUE residue_id), deployed before fea99acd1."""
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute(OLD_CREDITS_DDL)
    c.execute("CREATE INDEX idx_memecoin_quote_residue_credits_position ON memecoin_quote_residue_credits(position_id)")
    for r in rows:
        c.execute("INSERT INTO memecoin_quote_residue_credits(residue_id, position_id, intent_id, quote_mint,"
                  " residue_atomic, consumed_atomic, foreign_atomic, usd, credited_at, reverted_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)", r)
    c.commit()
    return c


def _credits_ddl(c):
    return c.execute("SELECT sql FROM sqlite_master WHERE name='memecoin_quote_residue_credits'").fetchone()[0]


def test_an_empty_old_credits_table_is_rebuilt_with_the_live_only_unique(tmp_path):
    c = _old_ledger(tmp_path)
    m.ensure_memecoin_tables(c)
    c.commit()
    assert "UNIQUE" not in _credits_ddl(c).split("residue_id", 1)[1].split(",", 1)[0]
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                    "AND tbl_name='memecoin_quote_residue_credits'")} == {
        "idx_memecoin_quote_residue_credits_live", "idx_memecoin_quote_residue_credits_position"}
    _position(c)
    _entry_residue(c)
    rid = c.execute("SELECT id FROM memecoin_quote_residue").fetchone()[0]
    kw = dict(intent_id="i", mint=ZEC, credits=[{"residue_id": rid, "position_id": 71502, "atomic": 1, "usd": 1.0}],
              consumed_atomic=1, foreign_atomic=0, foreign_usd=0.0, short=False, now_iso=NOW)
    for _ in range(2):
        assert m.apply_quote_residue_credits(c, **kw) == 1
        cid = c.execute("SELECT id FROM memecoin_quote_residue_credits WHERE reverted_at IS NULL").fetchone()[0]
        assert m.revert_quote_residue_credit(c, cid, now_iso=NOW, reason="t")
    m.ensure_memecoin_tables(c)                                # idempotent on the new DDL
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM memecoin_quote_residue_credits").fetchone()[0] == 2


def test_an_old_credits_table_with_rows_is_copied_and_swapped_keeping_every_row(tmp_path):
    rows = [(5, 71502, "a", ZEC, "10", "10", "0", 1.5, NOW, None),
            (-6, 71502, "b", ZEC, "20", "20", "0", 2.5, NOW, NOW)]          # an old-style reverted row
    c = _old_ledger(tmp_path, rows)
    m.ensure_memecoin_tables(c)
    c.commit()
    got = [tuple(r) for r in c.execute("SELECT residue_id, intent_id, usd, reverted_at FROM "
                                       "memecoin_quote_residue_credits ORDER BY id")]
    assert got == [(5, "a", 1.5, None), (-6, "b", 2.5, NOW)]
    assert "UNIQUE" not in _credits_ddl(c).split("residue_id", 1)[1].split(",", 1)[0]
    idx = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' "
                                   "AND tbl_name='memecoin_quote_residue_credits'")}
    assert {"idx_memecoin_quote_residue_credits_live", "idx_memecoin_quote_residue_credits_position"} <= idx
    c.execute("INSERT INTO memecoin_quote_residue_credits(residue_id, position_id, intent_id, quote_mint,"
              " residue_atomic, consumed_atomic, foreign_atomic, usd, credited_at) VALUES(7,1,'c','m','1','1','0',1,?)", (NOW,))
    with pytest.raises(sqlite3.IntegrityError):              # one live credit per residue still holds
        c.execute("INSERT INTO memecoin_quote_residue_credits(residue_id, position_id, intent_id, quote_mint,"
                  " residue_atomic, consumed_atomic, foreign_atomic, usd, credited_at) VALUES(7,1,'d','m','1','1','0',1,?)", (NOW,))
