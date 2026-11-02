"""The rent sweep must return locked SOL without ever closing an account the trader still needs."""
from __future__ import annotations

import sqlite3

import pytest

from sniper.services import memecoin_rent_sweep as rs

OWNER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
RENT = 2039280


def _conn(open_mints=(), reserved_mints=()):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_positions (execution_mode TEXT, is_open INT, mint TEXT)")
    c.execute("CREATE TABLE memecoin_pending_entries (status TEXT, mint TEXT)")
    for mint in open_mints:
        c.execute("INSERT INTO memecoin_positions VALUES ('real', 1, ?)", (mint,))
    for mint in reserved_mints:
        c.execute("INSERT INTO memecoin_pending_entries VALUES ('reserved', ?)", (mint,))
    return c


def _accounts_response(rows):
    return {"result": {"value": [
        {"pubkey": pk,
         "account": {"lamports": RENT,
                     "data": {"parsed": {"info": {"mint": mint,
                                                  "tokenAmount": {"amount": str(amount)}}}}}}
        for pk, mint, amount in rows]}}


def _caller(rows, *, balance=50_000_000):
    def call(_url, method, _params, **_kw):
        if method == "getBalance":
            return {"result": {"value": balance}}
        if method == "getTokenAccountsByOwner":
            return _accounts_response(rows)
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": "11111111111111111111111111111111"}}}
        if method == "sendTransaction":
            return {"result": "SIG"}
        raise AssertionError("unexpected rpc method %s" % method)
    return call


# ---------------------------------------------------------------- selection

def test_only_zero_balance_accounts_are_swept():
    rows = [("acc1", "mintA", 0), ("acc2", "mintB", 12345), ("acc3", "mintC", 0)]
    got = rs.empty_token_accounts("https://rpc", OWNER, call_fn=_caller(rows))
    assert [pk for pk, _m, _l in got] == ["acc1", "acc3"]


def test_an_unparseable_account_is_left_alone_never_assumed_empty():
    def call(_url, _method, _params, **_kw):
        return {"result": {"value": [{"pubkey": "bad", "account": {"lamports": RENT,
                                                                   "data": {"parsed": {}}}}]}}
    assert rs.empty_token_accounts("https://rpc", OWNER, call_fn=call) == []


def test_a_mint_with_an_open_position_is_protected():
    conn = _conn(open_mints=["mintA"])
    accounts = [("acc1", "mintA", RENT), ("acc2", "mintB", RENT)]
    assert [pk for pk, _m, _l in rs.sweepable(accounts, rs.protected_mints(conn))] == ["acc2"]


def test_a_mint_with_an_unresolved_reservation_is_protected():
    """THE LIVE-MONEY CASE. A reservation that has not filled can name a mint whose account is
    still EMPTY, so the zero-balance test alone would happily close it mid-entry."""
    conn = _conn(reserved_mints=["mintA"])
    accounts = [("acc1", "mintA", RENT), ("acc2", "mintB", RENT)]
    assert [pk for pk, _m, _l in rs.sweepable(accounts, rs.protected_mints(conn))] == ["acc2"]


def test_an_unreadable_roster_sweeps_NOTHING_rather_than_everything():
    """None means unknown. An empty set would read as 'nothing is protected'."""
    class Broken:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("no such table")
    assert rs.protected_mints(Broken()) is None
    assert rs.sweepable([("acc1", "mintA", RENT)], None) == []


def test_an_account_with_an_unknown_mint_is_kept():
    assert rs.sweepable([("acc1", None, RENT)], set()) == []


# ---------------------------------------------------------------- rpc choice

def test_the_sweep_walks_past_a_rate_limited_endpoint():
    seen = []

    def call(url, method, params, **kw):
        seen.append(url)
        if url == "https://spent":
            raise OSError("HTTP Error 429: Too Many Requests")
        return _caller([])(url, method, params, **kw)

    url, balance = rs.pick_live_rpc(["https://spent", "https://good"], OWNER, call_fn=call)
    assert url == "https://good" and balance == 50_000_000
    assert seen == ["https://spent", "https://good"]


def test_no_endpoint_answering_is_refusal_not_a_zero_sweep():
    def call(*_a, **_k):
        raise OSError("down")
    tally = rs.rent_sweep_once(_conn(), rpc_candidates=["https://a"], keypair=None,
                               owner_text=OWNER, call_fn=call)
    assert tally["state"] == "no_rpc" and tally["closed"] == 0


# ---------------------------------------------------------------- pass behaviour

def test_below_the_minimum_no_signature_is_spent():
    rows = [("acc1", "mintA", 0), ("acc2", "mintB", 0)]
    tally = rs.rent_sweep_once(_conn(), rpc_candidates=["https://rpc"], keypair=None,
                               owner_text=OWNER, min_accounts=8, call_fn=_caller(rows))
    assert tally["state"] == "below_min" and tally["closed"] == 0


def test_a_pass_is_bounded_to_one_transaction_worth_of_accounts():
    rows = [(f"acc{i}", f"mint{i}", 0) for i in range(30)]
    tally = rs.rent_sweep_once(
        _conn(), rpc_candidates=["https://rpc"], keypair=None, owner_text=OWNER,
        min_accounts=8, max_accounts=12, execute=False, call_fn=_caller(rows))
    assert tally["found"] == 30
    assert tally["state"] == "report_only"
    assert tally["reclaimed_sol"] == pytest.approx(12 * RENT / 1e9)


def test_protected_accounts_are_counted_and_excluded_from_the_pass():
    rows = [(f"acc{i}", f"mint{i}", 0) for i in range(10)]
    tally = rs.rent_sweep_once(
        _conn(open_mints=["mint0", "mint1"]), rpc_candidates=["https://rpc"], keypair=None,
        owner_text=OWNER, min_accounts=1, execute=False, call_fn=_caller(rows))
    assert tally["found"] == 10 and tally["protected"] == 2
    assert tally["reclaimed_sol"] == pytest.approx(8 * RENT / 1e9)


def test_an_unconfirmed_close_reclaims_nothing_and_stays_retryable(tmp_path, monkeypatch):
    """Unconfirmed is UNKNOWN, not success. Booking the rent on an unconfirmed close would
    overstate the wallet and hide a close that never landed."""
    rows = [(f"acc{i}", f"mint{i}", 0) for i in range(10)]
    monkeypatch.setattr(rs, "close_batch", lambda *a, **k: "unavailable")
    tally = rs.rent_sweep_once(
        _conn(), rpc_candidates=["https://rpc"], keypair=object(), owner_text=OWNER,
        min_accounts=1, call_fn=_caller(rows), lock_dir=str(tmp_path))
    assert tally["state"] == "unavailable"
    assert tally["closed"] == 0
    assert tally["reclaimed_sol"] == 0.0


def test_a_confirmed_close_books_exactly_the_rent_it_closed(tmp_path, monkeypatch):
    rows = [(f"acc{i}", f"mint{i}", 0) for i in range(10)]
    monkeypatch.setattr(rs, "close_batch", lambda *a, **k: "confirmed")
    tally = rs.rent_sweep_once(
        _conn(), rpc_candidates=["https://rpc"], keypair=object(), owner_text=OWNER,
        min_accounts=1, max_accounts=12, call_fn=_caller(rows), lock_dir=str(tmp_path))
    assert tally["closed"] == 10
    assert tally["reclaimed_sol"] == pytest.approx(10 * RENT / 1e9)


def test_a_busy_wallet_skips_the_pass_instead_of_queueing_behind_an_entry(tmp_path):
    """Maintenance must never make a latency-critical entry wait. Holding the lock elsewhere
    makes the sweep yield, not block."""
    import sniper.execution.exec_lock as exec_lock
    rows = [(f"acc{i}", f"mint{i}", 0) for i in range(10)]
    with exec_lock.wallet_exec_lock("sol-%s" % OWNER, lock_dir=str(tmp_path)) as held:
        assert held
        tally = rs.rent_sweep_once(
            _conn(), rpc_candidates=["https://rpc"], keypair=object(), owner_text=OWNER,
            min_accounts=1, call_fn=_caller(rows), confirm_fn=lambda *_a: "confirmed",
            lock_dir=str(tmp_path))
    assert tally["state"] == "wallet_busy" and tally["closed"] == 0


def test_a_mint_reserved_between_the_read_and_the_lock_is_dropped_on_recheck(tmp_path):
    """The window this closes: the protected set is read before the lock, and an entry can reserve
    a mint in between."""
    rows = [("acc1", "mintA", 0), ("acc2", "mintB", 0)]
    conn = _conn()
    calls = {"n": 0}
    real_protected = rs.protected_mints

    def racing(c):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_protected(c)          # first read: nothing reserved
        return {"mintA"}                      # inside the lock: mintA is now reserved

    # Throwaway lock dir. OWNER is the REAL funded pubkey, so a default lock would contend with
    # the live runner's own `sol-<pubkey>` lock — a test must never do that.
    sent = []

    def fake_close(_url, _kp, _owner, batch, **_kw):
        sent.append([mint for _pk, mint, _lam in batch])
        return "confirmed"

    monkeypatch_target = rs.protected_mints
    try:
        rs.protected_mints = racing
        rs_close = rs.close_batch
        rs.close_batch = fake_close
        tally = rs.rent_sweep_once(
            conn, rpc_candidates=["https://rpc"], keypair=object(), owner_text=OWNER,
            min_accounts=1, call_fn=_caller(rows), lock_dir=str(tmp_path))
    finally:
        rs.protected_mints = monkeypatch_target
        rs.close_batch = rs_close
    assert sent == [["mintB"]]                # mintA was dropped by the in-lock recheck
    assert tally["closed"] == 1
