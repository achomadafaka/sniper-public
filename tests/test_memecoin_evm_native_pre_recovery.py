import sqlite3

from sniper.execution import evm_real_seams as seams
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_confirmed_native_pre_transaction_binds_and_confirms_the_rewrap():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at="2026-08-31T21:17:43Z", network_id="4663",
    )
    m.set_pending_broadcast(c, pid, "0xpre", 7, "2026-08-31T21:17:43Z")
    m.mark_pending_recovery_status(c, pid, "native_pre_transaction_ambiguous")
    pending = m.pending_row(c, pid)
    owner = "0x1111111111111111111111111111111111111111"
    weth = "0x2222222222222222222222222222222222222222"

    def send(raw):
        row = m.pending_row(c, pid)
        assert raw == "0x01"
        assert row["cleanup_tx_sig"] == "0xcleanup"
        assert row["cleanup_amount_atomic"] == "16"
        return "0xcleanup"

    result = seams.recover_confirmed_native_pre_transaction(
        c, pending, address=owner, quote_token=weth,
        transaction_fn=lambda _sig: {
            "hash": "0xpre", "from": owner, "to": weth, "value": "0x0",
            "input": "0x2e1a7d4d" + format(16, "064x"),
        },
        sign_fn=lambda tx: (
            ("0x01", "0xcleanup", 8)
            if tx == {"to": weth, "data": "0xd0e30db0", "value": 16,
                      "gas": seams.WETH_CALL_GAS_LIMIT}
            else None
        ),
        send_fn=send, confirm_fn=lambda _sig: "confirmed",
        now_iso="2026-08-31T21:18:00Z",
    )

    assert result == {"status": "cleanup_confirmed"}
    row = m.pending_row(c, pid)
    assert row["cleanup_tx_sig"] == "0xcleanup"
    assert row["cleanup_status"] == "confirmed"


def test_orphaned_native_pre_transaction_still_rewraps_after_chain_proof():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at="2026-08-31T21:17:43Z", network_id="4663",
    )
    m.set_pending_broadcast(c, pid, "0xpre", 7, "2026-08-31T21:17:43Z")
    m.mark_pending_orphaned(c, pid)
    owner = "0x1111111111111111111111111111111111111111"
    weth = "0x2222222222222222222222222222222222222222"

    result = seams.recover_confirmed_native_pre_transaction(
        c, m.pending_row(c, pid), address=owner, quote_token=weth,
        transaction_fn=lambda _sig: {
            "hash": "0xpre", "from": owner, "to": weth, "value": "0x0",
            "input": "0x2e1a7d4d" + format(16, "064x"),
        },
        sign_fn=lambda _tx: ("0x01", "0xcleanup", 8),
        send_fn=lambda _raw: "0xcleanup", confirm_fn=lambda _sig: "confirmed",
        now_iso="2026-08-31T21:18:00Z",
    )

    assert result == {"status": "cleanup_confirmed"}


def _endpoint_http(answers, *, errors=()):
    """Fake `http` with the real contract: http("POST", url, body=...) -> (status, json, detail).

    Answers differ PER ENDPOINT, the way a pool with a lagging member does.
    """
    reads = []

    def http(_method, url, body=None, **_kw):
        reads.append(url)
        if url in errors:
            raise OSError("endpoint refused the read")
        return 200, {"jsonrpc": "2.0", "id": 1, "result": answers.get(url)}, ""

    http.reads = reads
    return http


def test_confirm_reads_a_secondary_when_the_primary_endpoint_cannot_answer():
    # The four `native pre-transaction ambiguous` entries of 2026-09-04 were all false: the
    # unwraps confirmed on chain while the single polled endpoint errored or lagged.
    primary, backup = "https://primary.invalid", "https://backup.invalid"
    http = _endpoint_http({backup: {"status": "0x1"}}, errors=(primary,))
    state = seams.confirm_receipt_across_pool(
        "0xpre", primary, http=http, attempts=4, poll_s=0,
        candidates=[primary, backup], sleep=lambda _s: None,
    )
    assert state == "confirmed"
    assert http.reads[0] == primary        # the configured primary still gets the first read


def test_confirm_stays_unknown_when_no_endpoint_ever_sees_the_receipt():
    # Fail-closed is unchanged: a clean null everywhere admits nothing.
    primary, backup = "https://primary.invalid", "https://backup.invalid"
    http = _endpoint_http({primary: None, backup: None})
    assert seams.confirm_receipt_across_pool(
        "0xpre", primary, http=http, attempts=4, poll_s=0,
        candidates=[primary, backup], sleep=lambda _s: None,
    ) == "unknown"


def test_a_null_from_a_lagging_node_does_not_end_the_wait_early():
    # `not_found` from a node 4 blocks behind means "keep waiting", never proof of absence.
    calls = {"n": 0}

    def http(_method, url, body=None, **_kw):
        calls["n"] += 1
        mined = calls["n"] >= 4            # the receipt only appears on a later poll
        return 200, {"jsonrpc": "2.0", "id": 1,
                     "result": {"status": "0x1"} if mined else None}, ""

    assert seams.confirm_receipt_across_pool(
        "0xpre", "https://primary.invalid", http=http, attempts=8, poll_s=0,
        candidates=["https://primary.invalid", "https://backup.invalid"],
        sleep=lambda _s: None,
    ) == "confirmed"


def test_a_fast_null_does_not_beat_a_slower_real_receipt_in_the_hedged_read():
    """The deeper half of the same defect: `eth_getTransactionReceipt` is a hedged, first-answer-
    wins read, so a lagging member's fast `null` used to settle it while the member holding the
    receipt was still in flight."""
    import time as _t
    from sniper.execution import evm_wallet as w
    from sniper.execution import evm_rpc_pool as pool

    pool.reset_for_tests()
    lagging, current = "https://lagging.invalid", "https://current.invalid"
    pool.mark_identity(current, True)

    def http(_method, url, body=None, **_kw):
        if url == lagging:
            return 200, {"jsonrpc": "2.0", "id": 1, "result": None}, ""
        _t.sleep(0.05)                      # the node that HAS the receipt answers slower
        return 200, {"jsonrpc": "2.0", "id": 1, "result": {"status": "0x1"}}, ""

    answer = w._rpc_hedged_read(
        lagging, "{}", http, pool, lambda _c: True, [lagging, current],
        method="eth_getTransactionReceipt",
    )
    assert answer["result"] == {"status": "0x1"}
    pool.reset_for_tests()


def test_the_hedged_read_still_returns_a_null_nobody_overturns():
    # Fail-closed: when every member honestly has no receipt, that IS the answer.
    from sniper.execution import evm_wallet as w
    from sniper.execution import evm_rpc_pool as pool

    pool.reset_for_tests()
    a, b = "https://a.invalid", "https://b.invalid"
    pool.mark_identity(b, True)
    answer = w._rpc_hedged_read(
        a, "{}", lambda _m, _u, body=None, **_k: (200, {"jsonrpc": "2.0", "id": 1,
                                                        "result": None}, ""),
        pool, lambda _c: True, [a, b], method="eth_getTransactionReceipt",
    )
    assert answer is not None and answer["result"] is None
    pool.reset_for_tests()
