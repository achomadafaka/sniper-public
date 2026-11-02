"""Tick-stream ingest v0 (observation-only data plane) — decode, cursor, dedupe, retention.
Spec: docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md."""
import sqlite3

from sniper.persistence import db_ticks as t
from sniper.services import memecoin_tick_stream as tick_stream
from sniper.services.memecoin_tick_stream import (
    V2_SWAP, V2_SYNC, V3_SWAP, decode_evm_log, evm_tick_pass, watched_base_pools,
)


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    return c


def _w(v, signed=False):
    if signed and v < 0:
        v += 1 << 256
    return f"{v:064x}"


def _log(topic0, words, *, addr="0xP00L", topics_extra=("0x" + "ab" * 32,), block=100, idx=1,
         tx="0xTX"):
    return {"address": addr, "topics": [topic0, *topics_extra],
            "data": "0x" + "".join(words), "blockNumber": hex(block),
            "logIndex": hex(idx), "transactionHash": tx}


def test_robinhood_whole_chain_scan_uses_the_slower_period():
    assert tick_stream.chain_tick_period("4663", 3.0, 10.0) == 10.0
    assert tick_stream.chain_tick_period("8453", 3.0, 10.0) == 3.0


# --- decode ---

def test_decode_v3_swap_signed_amounts():
    lg = _log(V3_SWAP, [_w(-500, signed=True), _w(1200), _w(0), _w(0), _w(0)])
    row = decode_evm_log("4663", lg, "2026-07-23T00:00:00+00:00")
    assert row["kind"] == "v3_swap"
    assert row["amount0"] == "-500" and row["amount1"] == "1200"
    assert row["pool"] == "0xp00l" and row["block_no"] == 100 and row["log_index"] == 1


def test_decode_v2_swap_normalizes_in_minus_out():
    # amount0In=1000, amount1In=0, amount0Out=0, amount1Out=750  ->  +1000 / -750
    lg = _log(V2_SWAP, [_w(1000), _w(0), _w(0), _w(750)])
    row = decode_evm_log("4663", lg, "t")
    assert row["kind"] == "v2_swap"
    assert row["amount0"] == "1000" and row["amount1"] == "-750"


def test_decode_v2_sync_reserves_and_foreign_topic():
    lg = _log(V2_SYNC, [_w(11), _w(22)], topics_extra=())
    row = decode_evm_log("8453", lg, "t")
    assert row["kind"] == "v2_sync" and row["amount0"] == "11" and row["amount1"] == "22"
    assert decode_evm_log("8453", _log("0x" + "ff" * 32, [_w(1)]), "t") is None


# --- tick pass: cursor, batching, fail-closed ---

def _rpc(head, logs_by_range=None, fail=None):
    calls = []

    def call(method, params):
        calls.append((method, params))
        if fail == method:
            raise RuntimeError("rpc down")
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getLogs":
            return (logs_by_range or {}).get(
                (int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)), [])
        raise AssertionError(method)
    call.calls = calls
    return call


def test_first_tick_starts_at_tip_not_history():
    tc = _tc()
    call = _rpc(1000)
    assert evm_tick_pass(tc, "4663", call, "t") == 0
    assert t.get_cursor(tc, "4663") == 1000
    scans = [p for m, p in call.calls if m == "eth_getLogs"]
    assert scans and int(scans[0][0]["fromBlock"], 16) == 1000     # (999, 1000] only


def test_tick_ingests_dedupes_and_advances():
    tc = _tc()
    t.set_cursor(tc, "4663", 99, "t0")
    lg = _log(V3_SWAP, [_w(-5, signed=True), _w(9), _w(0), _w(0), _w(0)])
    call = _rpc(101, {(100, 101): [lg]})
    assert evm_tick_pass(tc, "4663", call, "t1") == 1
    assert t.get_cursor(tc, "4663") == 101
    # a rescan of the same range must not duplicate
    t.set_cursor(tc, "4663", 99, "t2")
    assert evm_tick_pass(tc, "4663", _rpc(101, {(100, 101): [lg]}), "t3") == 0
    n = tc.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0]
    assert n == 1


def test_failed_getlogs_skips_tick_without_moving_cursor():
    tc = _tc()
    t.set_cursor(tc, "4663", 99, "t0")
    assert evm_tick_pass(tc, "4663", _rpc(105, fail="eth_getLogs"), "t1") is None
    assert t.get_cursor(tc, "4663") == 99                          # resume, no gap invented


def test_tick_retry_backoff_is_bounded_and_chain_local():
    backoff = tick_stream.TickRetryBackoff(base_s=3.0, cap_s=60.0)
    assert backoff.ready("4663", 0.0)
    assert backoff.ready("8453", 0.0)

    assert backoff.record("4663", None, 0.0) == 3.0
    assert not backoff.ready("4663", 2.9)
    assert backoff.ready("4663", 3.0)
    assert backoff.ready("8453", 2.9)

    now = 3.0
    for expected in (6.0, 12.0, 24.0, 48.0, 60.0, 60.0):
        assert backoff.record("4663", None, now) == expected
        now += expected
    assert backoff.record("4663", 0, now) == 0.0
    assert backoff.ready("4663", now)


def test_deep_gap_skips_to_tip_not_ingest_stamped():
    """2026-07-23 review P1: observed_at is ingest-stamped, so a DEEP gap (post-downtime) must be
    skipped to the tip, not replayed stamped-as-current — that would poison the feature windows.
    A gap deeper than MAX_CATCHUP_BLOCKS jumps the cursor to head-1 without scanning."""
    tc = _tc()
    t.set_cursor(tc, "4663", 0, "t0")
    call = _rpc(10_000)                                            # 10k-block gap, far > 900
    assert evm_tick_pass(tc, "4663", call, "t1") == 0
    assert t.get_cursor(tc, "4663") == 9_999                       # resumed at tip; deep history skipped
    gap = t.unresolved_gaps(tc, "4663")[0]
    assert (gap["from_block"], gap["to_block"], gap["reason"]) == (
        1, 9_999, "deep_catchup_skipped")
    assert not [p for mth, p in call.calls if mth == "eth_getLogs"]  # no deep scan


def test_shallow_gap_still_sliced_normally():
    """A gap within the catch-up bound is still consumed — its ingest-stamp error is small."""
    tc = _tc()
    t.set_cursor(tc, "4663", 9_700, "t0")
    call = _rpc(10_000)                                            # 300-block gap, < 900
    evm_tick_pass(tc, "4663", call, "t1")
    assert t.get_cursor(tc, "4663") == 10_000                      # sliced to head, not skipped


def test_watched_empty_set_idles_but_advances_cursor():
    tc = _tc()
    t.set_cursor(tc, "8453", 50, "t0")
    call = _rpc(60)
    assert evm_tick_pass(tc, "8453", call, "t1", watched_pools=set()) == 0
    assert t.get_cursor(tc, "8453") == 60
    assert not [p for m, p in call.calls if m == "eth_getLogs"]    # no wasted scan


def test_watched_pools_filter_is_passed_to_getlogs():
    tc = _tc()
    t.set_cursor(tc, "8453", 50, "t0")
    call = _rpc(60)
    evm_tick_pass(tc, "8453", call, "t1", watched_pools={"0xAAA", "0xbbb"})
    scans = [p for m, p in call.calls if m == "eth_getLogs"]
    assert scans[0][0]["address"] == ["0xaaa", "0xbbb"]


# --- retention + watch set ---

def test_prune_keeps_linked_pools():
    tc = _tc()
    t.insert_ticks(tc, [
        {"chain_id": "4663", "pool": "0xkeep", "kind": "v3_swap", "block_no": 1, "tx_sig": "a",
         "log_index": 0, "maker": None, "amount0": "1", "amount1": "1",
         "observed_at": "2026-07-01T00:00:00+00:00"},
        {"chain_id": "4663", "pool": "0xdrop", "kind": "v3_swap", "block_no": 1, "tx_sig": "b",
         "log_index": 0, "maker": None, "amount0": "1", "amount1": "1",
         "observed_at": "2026-07-01T00:00:00+00:00"},
    ])
    deleted = t.prune_unlinked(tc, keep_pools={"0xKEEP"}, older_than_iso="2026-07-10T00:00:00+00:00")
    assert deleted == 1
    left = {r["pool"] for r in tc.execute("SELECT pool FROM pool_ticks")}
    assert left == {"0xkeep"}


def test_watched_base_pools_reads_open_real_route_targets():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xmint", ticker="",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="t", strategy="alpha_b",
                          execution_mode="real", network_id="8453")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="t",
                  quote_route_target="0xRouTe")
    assert watched_base_pools(c) == {"0xroute"}


# --- solana lane parsing ---

def test_parse_solana_ack_and_tick():
    from sniper.services.memecoin_tick_stream import parse_solana_ws_message
    ack = parse_solana_ws_message({"jsonrpc": "2.0", "id": 2, "result": 771}, {}, "t")
    assert ack == ("ack", 2, 771)
    sub_map = {771: "MintA"}
    ev = {"params": {"subscription": 771,
                     "result": {"context": {"slot": 123}, "value": {"signature": "SIG1"}}}}
    kind, row = parse_solana_ws_message(ev, sub_map, "t")
    assert kind == "tick"
    assert row["pool"] == "MintA" and row["tx_sig"] == "SIG1" and row["block_no"] == 123
    assert row["kind"] == "sol_tx" and row["chain_id"] == "solana"
    # unmapped subscription or missing signature -> None
    assert parse_solana_ws_message(ev, {}, "t") is None
    assert parse_solana_ws_message({"params": {"subscription": 771, "result": {}}}, sub_map, "t") is None


def test_tick_pass_captures_blocktimestamp_into_block_times():
    """2026-07-24 (probed live): this node's eth_getLogs carries `blockTimestamp` per log, so
    head timestamps land at ingest with zero extra RPC — the fix for event-time features on a
    ~10 blocks/s chain where 64-header/pass enrichment can never catch up. Logs without the
    field add nothing (fail-closed)."""
    tc = _tc()
    t.set_cursor(tc, "4663", 99, "t0")
    lg = _log(V3_SWAP, [_w(-5, signed=True), _w(9), _w(0), _w(0), _w(0)])
    lg["blockTimestamp"] = hex(1_700_000_000)
    bare = _log(V3_SWAP, [_w(-7, signed=True), _w(9), _w(0), _w(0), _w(0)], block=101, idx=2)
    assert evm_tick_pass(tc, "4663", _rpc(101, {(100, 101): [lg, bare]}), "t1") == 2
    got = t.block_ts_map(tc, "4663", [100, 101])
    assert 100 in got and got[100].startswith("2023-11-14")   # captured from the log, free
    assert 101 not in got                                     # no field -> no invented timestamp


def test_conflicting_log_headers_cannot_form_a_synthetic_hash_time_pair():
    tc = _tc()
    t.ensure_block_times(tc)
    t.set_cursor(tc, "4663", 99, "t0")
    a = _log(V3_SWAP, [_w(-5, signed=True), _w(9), _w(0), _w(0), _w(0)])
    a.update(blockNumber=hex(100), blockTimestamp=hex(1_700_000_000), blockHash="0xaaa")
    b = dict(a, blockTimestamp=hex(1_700_000_100), blockHash="0xbbb", logIndex="0x2")
    call = _rpc(101, {(100, 101): [a, b]})
    evm_tick_pass(tc, "4663", call, "t1")
    assert tc.execute("SELECT block_hash FROM block_times WHERE block_no=100").fetchone() is None



def test_base_v4_watches_executed_pool_not_router():
    import json
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pool = "0x" + "ab" * 32
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xmint", ticker="",
                          entry_price=1., entry_notional=10., entry_fill_source="chain",
                          entry_lag_s=0., their_size_usd=0., opened_at="t", strategy="alpha_b",
                          execution_mode="real", network_id="8453",
                          evm_route_json=json.dumps({"protocol": "uniswap_v4", "pool_id": pool}))
    c.execute("UPDATE memecoin_positions SET executed_pool=?,executed_pool_source='receipt_v4_swap' WHERE id=?",
              (pool, pid)); c.commit()
    m.insert_fill(c, position_id=pid, kind="entry", price=1., qty=10., notional=10.,
                  fill_source="chain", fraction=1., created_at="t", quote_route_target="0xrouter")
    assert watched_base_pools(c) == {pool}


def test_base_v4_scan_addresses_manager_and_filters_exact_pool_ids():
    from sniper.execution.evm_v4_quote import V4
    pool = "0x" + "ab" * 32
    tc = _tc(); t.set_cursor(tc, "8453", 99, "t0")
    call = _rpc(101)
    assert evm_tick_pass(tc, "8453", call, "t1", watched_pools={pool}) == 0
    [params] = [p[0] for method, p in call.calls if method == "eth_getLogs"]
    assert params["address"] == [V4["8453"]["pool_manager"].lower()]
    assert params["topics"][1] == [pool]
    assert set(params["topics"][0]) == {tick_stream.V4_SWAP, tick_stream.V4_INITIALIZE}


def test_base_mixed_pool_failure_preserves_cursor_and_inserts_nothing():
    pool = "0x" + "ab" * 32
    tc = _tc(); t.set_cursor(tc, "8453", 99, "t0")
    scans = []
    lg = _log(V3_SWAP, [_w(-5, signed=True), _w(9), _w(0), _w(0), _w(0)])
    def call(method, params):
        if method == "eth_blockNumber": return hex(101)
        scans.append(params[0])
        if len(scans) == 2: raise TimeoutError("manager read failed")
        return [lg]
    assert evm_tick_pass(tc, "8453", call, "t1", watched_pools={pool, "0xp00l"}) is None
    assert t.get_cursor(tc, "8453") == 99
    assert tc.execute("SELECT COUNT(*) FROM pool_ticks").fetchone()[0] == 0
