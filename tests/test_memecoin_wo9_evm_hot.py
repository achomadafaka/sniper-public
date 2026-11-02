"""WO-9 — EVM hot exit lane. A decoded EVM row (natively transaction-grounded) authorizes a sale
DIRECTLY through the existing supervisor — but only via explicit `transaction_decoded` provenance,
never by inferring "it's an EVM chain". A header-less / mempool / reorged row can wake+prewarm but
never authorize; the pull lane covers the gap.
"""
from sniper.services import memecoin_evm_hot as eh
from sniper.services.memecoin_hot_supervisor import HotSupervisor, row_is_transaction_grounded
from sniper.services import memecoin_tick_stream as ts


def _w(v):
    return format(v & (2 ** 256 - 1), "064x")


def _burn_log(pool="0xpool", removed=False):
    return {"topics": [ts.V3_BURN, "0x" + _w(0), "0x" + _w(50), "0x" + _w(150)],
            "data": "0x" + _w(250_000) + _w(100) + _w(200), "address": pool,
            "blockNumber": hex(1000), "transactionHash": "0xdeadbeef", "logIndex": hex(3),
            "blockHash": "0xblockhash", "blockTimestamp": hex(1_700_000_000), "removed": removed}


class _CutDecision:
    action = "CUT"
    reason_codes = ("cut", "break_confirmed")   # NOT unsellable/edge -> a vault-delta sale needs decode


class _StubEngineSet:
    """Minimal engine that emits a CUT on any row — the guard under test is the supervisor's, not the
    engine's rule tree (that is covered elsewhere)."""
    def __init__(self):
        self.rows = []
    def on_tick(self, key, row, *, now_ts, cfg=None):
        self.rows.append(row); return _CutDecision()
    def on_ticks(self, key, rows, *, now_ts, cfg=None):
        self.rows.extend(rows); return _CutDecision()
    def engine(self, key):
        return None
    def corroborate(self, key, slots, decoded_rows, *, now_ts, cfg=None):
        return None                                # no Solana decode context -> pull covers


def _supervisor():
    emitted = []
    sup = HotSupervisor(_StubEngineSet(), now_fn=lambda: 1_700_000_001.0,
                        on_decision=lambda key, emit, row, preview: emitted.append((key, emit)))
    return sup, emitted


# --- provenance helper -------------------------------------------------------------------------

def test_row_is_transaction_grounded_requires_complete_log_identity():
    full = {"transaction_decoded": True, "tx_sig": "0xh", "block_hash": "0xb",
            "block_time_true": True, "block_no": 1, "log_index": 0}
    assert row_is_transaction_grounded(full) is True
    for drop in ("transaction_decoded", "tx_sig", "block_hash", "block_time_true",
                 "block_no", "log_index"):
        assert row_is_transaction_grounded({**full, drop: None}) is False


# --- feeder ------------------------------------------------------------------------------------

def test_evm_hot_row_grounded_with_header_time():
    r = eh.evm_hot_row("4663", _burn_log(), "t", block_ts=1_700_000_000, block_time_true=True)
    assert r["kind"] == "v3_burn" and r["network_id"] == "4663"
    assert r["transaction_decoded"] is True and r["block_time_true"] is True
    assert r["tx_sig"] == "0xdeadbeef" and r["block_hash"] == "0xblockhash"


def test_evm_hot_row_without_header_time_is_ungrounded():
    r = eh.evm_hot_row("4663", _burn_log(), "t", block_ts=None, block_time_true=False)
    assert r["transaction_decoded"] is False        # receive-time only -> cannot authorize a sale


def test_evm_hot_row_without_log_identity_is_ungrounded():
    log = _burn_log()
    log.pop("logIndex")
    r = eh.evm_hot_row("4663", log, "t", block_ts=1_700_000_000, block_time_true=True)
    assert r["transaction_decoded"] is False


def test_evm_hot_row_drops_reorged_log():
    assert eh.evm_hot_row("4663", _burn_log(removed=True), "t",
                          block_ts=1_700_000_000, block_time_true=True) is None


# --- sale authority through the supervisor -----------------------------------------------------

def test_grounded_evm_sale_authorizes_directly():
    sup, emitted = _supervisor()
    row = eh.evm_hot_row("4663", _burn_log(), "t", block_ts=1_700_000_000, block_time_true=True)
    sup.on_ws_row(row)
    assert len(emitted) == 1 and emitted[0][1].action == "CUT"   # authorized WITHOUT Solana decode


def test_ungrounded_evm_sale_falls_to_pull_never_authorizes():
    sup, emitted = _supervisor()
    row = eh.evm_hot_row("4663", _burn_log(), "t", block_ts=None, block_time_true=False)
    sup.on_ws_row(row)
    assert emitted == []                                         # not grounded -> pull lane covers it
    assert sup.gap.needs_pull_fallback(1_700_000_100.0)         # decision gap stays open


def test_feed_evm_block_drops_reorg_and_feeds_confirmed():
    sup, emitted = _supervisor()
    logs = [_burn_log(removed=True), _burn_log()]
    eh.feed_evm_block(sup, "4663", logs, block_ts=1_700_000_000, block_time_true=True, observed_at="t")
    assert len(emitted) == 1                                    # only the confirmed (non-reorg) row


def test_feed_evm_block_decides_once_after_all_same_pool_logs_are_loaded():
    sup, emitted = _supervisor()
    second = {**_burn_log(), "transactionHash": "0xsecond", "logIndex": hex(4)}

    eh.feed_evm_block(
        sup, "4663", [_burn_log(), second], block_ts=1_700_000_000,
        block_time_true=True, observed_at="t")

    assert len(sup.engines.rows) == 2
    assert len(emitted) == 1


def test_poll_scopes_to_held_pools_and_needs_a_chain():
    sup, _ = _supervisor()
    # unknown chain -> no-op, cursor unchanged
    assert eh.poll_evm_hot_once(sup, "999", ["0xp"], lambda *_a: None, lambda: "t", cursor=5) == 5
    # no held pools -> no-op even on a hot chain
    assert eh.poll_evm_hot_once(sup, "4663", [], lambda *_a: None, lambda: "t", cursor=5) == 5


def test_fetch_caps_backlog_and_advances_only_through_scanned_window():
    """A provider range limit must not strand the hot cursor after an outage-sized backlog."""
    scans = []

    def rpc_call(method, params):
        if method == "eth_blockNumber":
            return hex(1_000)
        assert method == "eth_getLogs"
        scans.append(params[0])
        return []

    cursor, blocks = eh.fetch_evm_hot_logs("4663", ["0xpool"], rpc_call, cursor=100)

    assert blocks == []
    assert len(scans) == 1
    assert int(scans[0]["fromBlock"], 16) == 101
    assert int(scans[0]["toBlock"], 16) == 500
    assert cursor == 500


def test_fetch_failure_holds_cursor_for_exact_retry():
    def rpc_call(method, _params):
        if method == "eth_blockNumber":
            return hex(1_000)
        raise RuntimeError("provider rejected range")

    cursor, blocks = eh.fetch_evm_hot_logs("4663", ["0xpool"], rpc_call, cursor=100)

    assert cursor == 100
    assert blocks == []
