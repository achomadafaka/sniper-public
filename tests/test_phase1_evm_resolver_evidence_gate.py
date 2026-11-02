"""Codex audit #10 (35948d32..d7ef0f2c): scripts/research/phase1_evm_resolver.py:325 (main's
per-event aggregation).

Before the fix, ANY buyer_cls that was not "contract_*" -- including "read_failed" (eth_getCode
itself could not answer) -- was added to `per_trader_candidates` unconditionally, and the receipt
read's quote-asset-spend result was never checked at all before counting a candidate toward the
>=CONFIRM_MIN_COINS confirm bar. A candidate whose identity evidence never actually resolved could
still supply an "agreeing coin". Every recorded event also got note="ok" regardless, hiding which
ones actually had complete evidence.

The cross-trader collision check (`addr_to_traders`/`shared`) was built only from `proposals` --
addresses that had ALREADY individually cleared one trader's own confirm bar. A shared-executor
address used by trader A for 3 coins and trader B for only 1 was invisible as a collision, because
B's single candidate never reached `proposals` to be counted.

These two extracted, pure functions carry the fix; `main()` just calls them.
"""
import scripts.research.phase1_evm_resolver as R
import sqlite3


def test_read_failed_buyer_class_is_not_eligible():
    note, eligible = R.classify_evidence("read_failed", True)
    assert eligible is False
    assert note == "buyer_class_unread"


def test_an_unread_receipt_is_unknown_not_a_confirmed_spend():
    note, eligible = R.classify_evidence("eoa", None)
    assert eligible is False
    assert note == "receipt_unread"


def test_a_clean_receipt_with_no_quote_spend_is_not_eligible():
    note, eligible = R.classify_evidence("eip7702_delegated_eoa", False)
    assert eligible is False
    assert note == "quote_spend_not_confirmed"


def test_an_accepted_class_with_a_confirmed_spend_is_ok():
    for cls in ("eoa", "eip7702_delegated_eoa"):
        note, eligible = R.classify_evidence(cls, True)
        assert eligible is True
        assert note == "ok"


def test_the_collision_check_catches_a_trader_who_never_cleared_the_bar_alone():
    """Trader A has 3 agreeing coins on ADDR (clears CONFIRM_MIN_COINS=3 alone); trader B has only
    1 agreeing coin on the SAME address (never clears the bar by itself). ADDR is genuinely shared
    infrastructure and must be flagged, even though only A's side would ever reach `proposals`."""
    per_trader_candidates = {
        "trader_a": {"0xaddr": {"mint1", "mint2", "mint3"}},
        "trader_b": {"0xaddr": {"mint4"}},
    }
    shared = R.collisions_from(per_trader_candidates)
    assert "0xaddr" in shared, (
        "an address used by two traders must be flagged shared even when one trader's own "
        "count never reaches the confirm bar")


def test_an_address_used_by_only_one_trader_is_never_flagged():
    per_trader_candidates = {
        "trader_a": {"0xaddr": {"mint1", "mint2", "mint3"}},
    }
    assert R.collisions_from(per_trader_candidates) == set()


def test_main_persists_shared_address_rejection(tmp_path, monkeypatch):
    """The rejection must reach the database when a second trader has only one match."""
    path = tmp_path / "resolver.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE memecoin_evm_wallet_addresses(source_user_id TEXT, handle TEXT, address TEXT);
        CREATE TABLE memecoin_events(source_user_id TEXT, mint TEXT, created_at TEXT,
                                    side TEXT, network_id TEXT);
        INSERT INTO memecoin_evm_wallet_addresses VALUES('a','alpha','old-a'),('b','beta','old-b');
    """)
    at = "2026-09-16T12:00:00+00:00"
    conn.executemany("INSERT INTO memecoin_events VALUES(?,?,?,'buy','4663')",
                     [(uid, mint, at) for uid, mint in
                      (("a", "mint1"), ("a", "mint2"), ("a", "mint3"), ("b", "mint4"))])
    conn.commit()
    monkeypatch.setattr(R, "DB", path)
    monkeypatch.setattr(R.sys, "argv", ["phase1_evm_resolver.py"])
    monkeypatch.setattr(R.time, "sleep", lambda _: None)

    def rpc(method, params):
        if method == "eth_blockNumber":
            return "0x1000", None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(R._epoch(at))}, None
        if method == "eth_getTransactionReceipt":
            return {"from": "shared"}, None
        raise AssertionError(method)

    monkeypatch.setattr(R, "_rpc", rpc)
    monkeypatch.setattr(R, "get_logs_windowed", lambda *args: [{}])
    monkeypatch.setattr(R, "candidate_buyer", lambda *args: ("tx", "shared"))
    monkeypatch.setattr(R, "classify_code", lambda *args: "eoa")
    monkeypatch.setattr(R, "quote_spent_by", lambda *args: True)

    R.main()

    assert conn.execute("SELECT source_user_id,status FROM phase1_evm_resolver_proposals").fetchall() == [
        ("a", "rejected_shared_address")]
    rejection = conn.execute("SELECT rule,detail FROM phase1_evm_resolver_rejects").fetchone()
    assert rejection == ("cross_trader_shared_address", "also proposed for source_user_id(s): ['b']")
    assert conn.execute("SELECT count(*) FROM phase1_evm_resolver_events").fetchone()[0] == 4
    conn.close()
