"""HOOKERS (4663, attempt 92da4a9a, 2026-09-30T11:24:51Z): a wrong chase refusal.

The source bought on the hooked V4 pool 0x3954914e (native ETH / HOOKERS, dynamic fee, tickSpacing 60,
hook 0xd0005624). Our V4 rung had no PoolKey for it: the pool is 54 days old, below the 7-day offline
index, and the rung's own Initialize scan ended `poolkey_read_failed` after 29 calls and 2,831 ms. The
ladder cut it at grace (2,043 ms) and took Kyber, whose price equalled the pool's state about 20 blocks
earlier. That read 1.82x the source against a 1.50x cap, and we refused. Gamma's chain grade: the
real executable price for our clip was 1.46x.

What changed:
1. The source receipt worker (off the entry path) now warms the PoolKey of a V4 pool it cannot decode.
   It rebuilds the key from the receipt's own logs and verifies it by hash. For a hook that does not
   emit, it submits one full-range Initialize read to a side thread.
2. A price refusal decided by an aggregator quote (kyber, relay), while an on-chain rung was cut by
   the ladder, re-asks the ladder once before it refuses.

Rule 5 (CLAUDE.md):
1. Money path: yes, funded EVM entry. A key is only ever accepted when keccak(abi.encode(key)) equals
   the pool id, so a wrong key cannot be stored. The recheck can admit an entry the first quote
   refused; every price gate runs again on the second quote.
2. Fill-path speed: nothing synchronous is added to an entry that passes. The warm runs on the
   receipt worker and a side thread. The recheck predicate is a dict read; the second ladder runs
   only where the first verdict was a refusal.
3. Concurrency: the key caches are written under the existing lock or by single dict assignment; the
   Initialize read has its own one-worker executor and a once-per-pool guard.
4. Absent vs zero: no key derived and no log found is None, and today's `v4_key_unknown` path stays.
5. Dependencies: a failed receipt read, RPC error, timeout or store error leaves today's behaviour. A
   second ladder that answers nothing keeps the ORIGINAL refusal, never "no quote" or LI.FI.
6. Units and identity: pool id = keccak of the encoded PoolKey, currency0 < currency1, native ETH is
   the zero address, the dynamic-fee flag is 0x800000.
7. Sample: none; nothing here produces a statistic.
8. Rollback: revert the commit. Stored keys stay valid (they are hash-verified and immutable).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sniper.execution import evm_v4_quote as V4Q
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_post_trade as PT

_SHIPPED_DELAY_S = getattr(V4Q, "_OFFPATH_DELAY_S", None)   # read before any fixture zeroes it
CHAIN = "4663"
MINT = "0xfb83b59cb15b9d8bb6d96380702580a313af9455"
OWNER = "0x838a336d19ae121a0ca754ebbdd57282de656dfa"
POOL = "0x3954914e309f7e4ada15d5c71a305cea4c86fcc2843485c8691c6a6e4bb5977c"
KEY = {"currency0": "0x0000000000000000000000000000000000000000", "currency1": MINT,
       "fee": 0x800000, "tick_spacing": 60, "hooks": "0xd0005624da88a688bcab3dbfb4d1cb23d32ca0cc"}
RECEIPT = json.loads((Path(__file__).parent / "fixtures" / "evm_4663_hookers_receipt_15a6df27.json").read_text())


@pytest.fixture
def stored(monkeypatch):
    """No test touches the ledger: the durable store and both in-process caches are isolated."""
    rows = []
    monkeypatch.setattr(V4Q, "_store_pool_key", lambda chain, pool, key: rows.append((chain, pool, key)))
    monkeypatch.setattr(V4Q, "_stored_pool_key", lambda chain, pool: None)     # an empty table
    monkeypatch.setattr(V4Q, "_key_cache", {})
    monkeypatch.setattr(PT, "_KEY_CACHE", {})
    monkeypatch.setattr(V4Q, "_OFFPATH_SEEN", set(), raising=False)
    monkeypatch.setattr(V4Q, "_OFFPATH_DELAY_S", 0.0, raising=False)
    monkeypatch.setattr(V4Q, "_archive_rpc_urls", lambda chain, primary: (primary,))   # none configured
    return rows


def _cache_only(chain, pool):
    return PT._KEY_CACHE.get((str(chain), str(pool).lower()))


def _drain():
    """Wait for the side threads to finish what they were handed (the durable store is asynchronous)."""
    for pool in list(getattr(V4Q, "_OFFPATH_POOLS", {}).values()) or [getattr(V4Q, "_OFFPATH_POOL", None)]:
        if pool is not None:
            pool.submit(lambda: None).result(timeout=10)


# ---------------------------------------------------------------- 1. the key from the source receipt

def test_the_hookers_key_is_rebuilt_from_the_source_receipt_and_published(stored):
    assert PT.source_post_trade(CHAIN, RECEIPT, MINT, owner=OWNER, pool_key_fn=_cache_only) == {
        "reason": "v4_key_unknown"}                       # the state at 11:24:51Z
    assert PT.warm_v4_keys(CHAIN, RECEIPT, MINT, offpath_fn=lambda chain, pool: None) == 1
    assert V4Q._key_cache[(CHAIN, POOL)] == KEY           # the V4 rung's own cache: no log scan
    _drain()
    assert stored == [(CHAIN, POOL, KEY)]                 # and the durable table, for every process
    after = PT.source_post_trade(CHAIN, RECEIPT, MINT, owner=OWNER, pool_key_fn=_cache_only)
    assert after.get("reason") != "v4_key_unknown"


def test_a_key_is_accepted_only_when_it_hashes_to_the_pool_id(stored):
    other = POOL[:-1] + ("0" if POOL[-1] != "0" else "1")
    logs = RECEIPT["logs"]
    assert V4Q.derive_pool_key_from_receipt(CHAIN, POOL, logs, MINT, swap_fee=3000) == KEY
    assert V4Q.derive_pool_key_from_receipt(CHAIN, other, logs, MINT, swap_fee=3000) is None
    assert V4Q.derive_pool_key_from_receipt(CHAIN, POOL, [], MINT, swap_fee=3000) is None


def test_a_hook_that_does_not_emit_goes_to_the_off_path_initialize_read(stored):
    quiet = dict(RECEIPT, logs=[log for log in RECEIPT["logs"]
                                if str(log["address"]).lower() != KEY["hooks"]])
    asked = []
    assert PT.warm_v4_keys(CHAIN, quiet, MINT, offpath_fn=lambda chain, pool: asked.append((chain, pool))) == 0
    assert asked == [(CHAIN, POOL)] and stored == []


def test_the_receipt_worker_warms_then_decodes_again(stored, monkeypatch):
    monkeypatch.setattr(PT, "stored_v4_key", _cache_only)
    future = PT.submit_receipt_reference(CHAIN, RECEIPT["transactionHash"], MINT, OWNER,
                                         read_fn=lambda method, params: RECEIPT)
    out = future.result(timeout=10)
    _drain()
    assert out.get("reason") != "v4_key_unknown" and stored == [(CHAIN, POOL, KEY)]


def test_the_durable_store_runs_on_the_side_thread_not_the_receipt_worker(stored, monkeypatch):
    """Follow-up 3 (Gamma LOW on 1490bcc24): the entry waits on the receipt worker's answer, and a
    ledger write can wait on a busy database. The in-process caches are published at once."""
    threads = []
    monkeypatch.setattr(V4Q, "_store_pool_key",
                        lambda chain, pool, key: threads.append(threading.current_thread().name))
    assert PT.warm_v4_keys(CHAIN, RECEIPT, MINT, offpath_fn=lambda chain, pool: None) == 1
    assert V4Q._key_cache[(CHAIN, POOL)] == KEY and PT._KEY_CACHE[(CHAIN, POOL)] == KEY
    _drain()
    assert len(threads) == 1 and threads[0].startswith("v4-key-offpath-store"), threads


# ---------------------------------------------------------------- 2. the off-path Initialize read

def _initialize_log(key, pool=POOL):
    pad = lambda a: "0x" + "0" * 24 + a[2:]                # noqa: E731
    word = lambda n: format(n % (1 << 256), "064x")        # noqa: E731
    return {"address": V4Q.V4[CHAIN]["pool_manager"],
            "topics": [V4Q._INIT_TOPIC, pool, pad(key["currency0"]), pad(key["currency1"])],
            "data": "0x" + word(key["fee"]) + word(key["tick_spacing"]) + "0" * 24 + key["hooks"][2:]
                    + word(1 << 96) + word(0)}


def test_the_initialize_read_is_one_full_range_call_for_one_pool_id(stored, monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "https://onchain.example")
    seen = []

    def post(url, method, params):
        seen.append((url, method, params[0]))
        return [_initialize_log(KEY)]
    assert V4Q.read_pool_key_offpath(CHAIN, POOL, post_fn=post) == KEY
    assert seen == [("https://onchain.example", "eth_getLogs", {
        "address": V4Q.V4[CHAIN]["pool_manager"], "topics": [V4Q._INIT_TOPIC, POOL],
        "fromBlock": "0x0", "toBlock": "latest"})]
    assert V4Q._key_cache[(CHAIN, POOL)] == KEY and stored == [(CHAIN, POOL, KEY)]


def test_a_log_that_does_not_hash_back_or_a_failed_read_stores_nothing(stored, monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "https://onchain.example")
    wrong = _initialize_log(dict(KEY, tick_spacing=200))
    assert V4Q.read_pool_key_offpath(CHAIN, POOL, post_fn=lambda *a: [wrong]) is None

    def down(url, method, params):
        raise OSError("down")
    assert V4Q.read_pool_key_offpath(CHAIN, POOL, post_fn=down) is None
    assert V4Q.read_pool_key_offpath(CHAIN, POOL, post_fn=lambda *a: []) is None
    assert stored == [] and (CHAIN, POOL) not in V4Q._key_cache


def test_the_off_path_read_is_submitted_once_per_pool(stored):
    ran = []
    first = V4Q.warm_pool_key_offpath(CHAIN, POOL, read_fn=lambda chain, pool: ran.append(pool))
    assert first is not None and first.result(timeout=10) is None
    assert V4Q.warm_pool_key_offpath(CHAIN, POOL, read_fn=lambda chain, pool: ran.append(pool)) is None
    assert ran == [POOL]


# Follow-up 2 (Gamma MEDIUM on 1490bcc24). The off-path read is NOT on a host of its own: with .env
# loaded, 4663 resolves to rpc.mainnet.chain.robinhood.com (custody, and the only member that serves
# a full-range eth_getLogs) and 8453 to base-rpc.publicnode.com (a funded pool member). So the read
# waits out the entry burst, skips itself when the key arrived meanwhile, and says why it failed.

def test_the_off_path_read_waits_out_the_entry_burst(stored, monkeypatch):
    # Reserved attempts 2026-09-23..30: 4663 p50 4.9 s, p90 12.3 s, max 18.3 s (n=80);
    # 8453 p50 3.2 s, p90 8.2 s, max 16.3 s (n=48).
    assert _SHIPPED_DELAY_S is not None and _SHIPPED_DELAY_S >= 20.0
    monkeypatch.setattr(V4Q, "_OFFPATH_DELAY_S", 0.3)
    started, ran = time.monotonic(), []
    future = V4Q.warm_pool_key_offpath(CHAIN, POOL, read_fn=lambda chain, pool: ran.append(time.monotonic()))
    future.result(timeout=10)
    assert len(ran) == 1 and ran[0] - started >= 0.3


def test_a_key_cached_during_the_wait_is_not_read_again(stored, monkeypatch):
    monkeypatch.setattr(V4Q, "_OFFPATH_DELAY_S", 0.3)
    ran = []
    future = V4Q.warm_pool_key_offpath(CHAIN, POOL, read_fn=lambda chain, pool: ran.append(pool))
    V4Q._key_cache[(CHAIN, POOL)] = KEY                    # the rung's own scan answered meanwhile
    assert future.result(timeout=10) == KEY and ran == []


def test_a_key_in_the_durable_table_is_not_read_again_after_a_restart(stored, monkeypatch):
    """Gamma LOW on a5a5fe62b: a fresh process has an empty in-memory cache while the table already
    holds the key. The custody read is skipped for it too, and the key is back in memory."""
    monkeypatch.setattr(V4Q, "_stored_pool_key", lambda chain, pool: dict(KEY) if pool == POOL else None)
    ran = []
    future = V4Q.warm_pool_key_offpath(CHAIN, POOL, read_fn=lambda chain, pool: ran.append(pool))
    assert future.result(timeout=10) == KEY and ran == []
    assert V4Q._key_cache[(CHAIN, POOL)] == KEY


def test_a_durable_store_does_not_wait_behind_a_read_that_is_waiting_out_its_delay(stored, monkeypatch):
    """Gamma LOW on a5a5fe62b: `_stored_pools_for_mint` lists a pool only once its key row exists, so
    a store queued behind a sleeping read hides the pool from entry discovery and the dip watch."""
    monkeypatch.setattr(V4Q, "_OFFPATH_DELAY_S", 1.5)
    other = POOL[:-1] + ("0" if POOL[-1] != "0" else "1")
    read = V4Q.warm_pool_key_offpath(CHAIN, other, read_fn=lambda chain, pool: None)
    started = time.monotonic()
    V4Q.store_pool_key_offpath(CHAIN, POOL, KEY).result(timeout=10)
    assert time.monotonic() - started < 1.0 and stored == [(CHAIN, POOL, KEY)]
    assert not read.done()                                  # the read is still waiting
    read.result(timeout=10)


def test_a_refused_off_path_read_logs_the_host_and_the_http_status(stored, monkeypatch, caplog):
    from sniper.execution import evm_wallet as W
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "https://onchain.example/v2/SECRETKEY")
    monkeypatch.setattr(W, "_http", lambda method, url, **kw: (
        429, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}, {}))
    assert V4Q.read_pool_key_offpath(CHAIN, POOL) is None
    lines = [r.getMessage() for r in caplog.records if "off-path read failed" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "host=onchain.example" in lines[0] and "http=429" in lines[0] and "SECRETKEY" not in lines[0]
    assert stored == [] and (CHAIN, POOL) not in V4Q._key_cache


# ---------------------------------------------------------------- 3. the stale aggregator recheck

_BASE = {"sellAmount": "10000000", "transaction": {"to": "0xrouter", "data": "0xdead"},
         "issues": {"allowance": {"spender": "0xspender"}}}
_V4_CUT = [{"rung": "v4", "hit": False, "blind_reason": "ladder_grace"},
           {"rung": "kyber", "hit": True, "blind_reason": None}]
KYBER_STALE = {**_BASE, "buyAmount": "5000000", "_sniper_winner": "kyber", "_sniper_rungs": _V4_CUT}
V4_FRESH = {**_BASE, "buyAmount": "8000000", "_sniper_winner": "v4",
            "_sniper_rungs": [{"rung": "v4", "hit": True, "blind_reason": None}]}


class _Trace:
    """Records every trace event in order, with its metadata."""
    def __init__(self):
        self.events = []

    def mark_substage(self, name, **metadata):
        self.events.append((name, metadata))
        return True

    def mark_submission(self, stage, attempt_no, **metadata):
        self.events.append(("submission:" + stage, dict(metadata, attempt_no=attempt_no)))

    def mark(self, stage):
        pass

    def stage_completed_at(self, stage):
        return "2026-07-09T00:00:01+00:00"

    def reconciled_now(self):
        return "2026-07-09T00:00:01+00:00"


def _entry(quotes, *, token_out=8.0, trace=None):
    """source price 1.0, chase cap 50%: a $2.00 quote is +100% (refused), a $1.25 quote is +25%."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b", notional=10.0,
                            reserved_at="2026-07-09T00:00:00Z", network_id="4663", source_price=1.0)
    asked = []

    def quote_fn(mint, usd, **kw):
        asked.append(kw)
        quote = quotes[min(len(asked), len(quotes)) - 1]    # the last one repeats (post-approval requote)
        if isinstance(quote, Exception):
            raise quote
        return dict(quote) if quote else None
    calls = {"sign": 0, "ladders": None}

    def approve_fn(spender, amount):
        if calls["ladders"] is None:
            calls["ladders"] = len(asked)                   # ladders spent before any wallet mutation
        return True

    def sign_fn(tx):
        calls["sign"] += 1
        return ("0xraw", "0xhash", 7)
    sent = {"done": False}

    def send_fn(raw):
        sent["done"] = True
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-07-09T00:00:01Z", chase_max_pct=50.0,
        quote_fn=quote_fn, approve_fn=approve_fn, sign_fn=sign_fn, send_fn=send_fn,
        confirm_fn=lambda h: "confirmed", trace=trace,
        usdc_fn=lambda: 90.0 if sent["done"] else 100.0,
        token_fn=lambda mint: ((token_out if sent["done"] else 0.0), 6))
    return result, (len(asked) if calls["ladders"] is None else calls["ladders"]), calls["sign"], c


def test_a_stale_aggregator_refusal_asks_the_ladder_again_and_fills_on_the_pool():
    result, asked, signed, _c = _entry([KYBER_STALE, V4_FRESH])
    assert (result["status"], asked, signed) == ("filled", 2, 1)


def test_an_on_chain_winner_or_an_uncut_ladder_is_refused_at_once():
    result, asked, signed, c = _entry([dict(KYBER_STALE, _sniper_winner="v4"), V4_FRESH])
    assert (result["status"], asked, signed) == ("skipped_chase", 1, 0)
    uncut = dict(KYBER_STALE, _sniper_rungs=[{"rung": "v4", "hit": False, "blind_reason": None}])
    result, asked, signed, c = _entry([uncut, V4_FRESH])
    assert (result["status"], asked, signed) == ("skipped_chase", 1, 0)
    assert m.pending_totals(c) == (0, 0.0)


def test_a_second_ladder_that_is_still_too_expensive_refuses_without_a_third():
    result, asked, signed, c = _entry([KYBER_STALE, KYBER_STALE, V4_FRESH])
    assert (result["status"], asked, signed) == ("skipped_chase", 2, 0)
    assert result["quote_chase_pct"] == pytest.approx(100.0) and m.pending_totals(c) == (0, 0.0)


def test_a_second_ladder_that_answers_nothing_keeps_the_original_refusal():
    result, asked, signed, c = _entry([KYBER_STALE, None])
    assert (result["status"], asked, signed) == ("skipped_chase", 2, 0)
    assert result["quote_chase_pct"] == pytest.approx(100.0) and m.pending_totals(c) == (0, 0.0)


def test_a_quote_that_passes_costs_one_ladder():
    result, asked, signed, _c = _entry([dict(KYBER_STALE, buyAmount="8000000")])
    assert (result["status"], asked, signed) == ("filled", 1, 1)


def test_a_second_ladder_that_raises_keeps_the_original_refusal(caplog):
    """Follow-up 1 (Gamma LOW on 1490bcc24): the recheck ladder is a second provider call. A raise
    there must not replace the refusal the first quote already earned with a crashed entry that
    leaves its reservation held."""
    result, asked, signed, c = _entry([KYBER_STALE, OSError("rpc down")])
    assert (result["status"], asked, signed) == ("skipped_chase", 2, 0)
    assert result["quote_chase_pct"] == pytest.approx(100.0) and m.pending_totals(c) == (0, 0.0)
    logged = [r for r in caplog.records if "stale-aggregator recheck" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info and "pending_id=1" in logged[0].getMessage()


def test_a_first_ladder_that_raises_is_not_swallowed():
    with pytest.raises(OSError):
        _entry([OSError("rpc down")])


# Follow-up 4 (Gamma LOW on 1490bcc24): the recheck repeats four trace events inside attempt 1. Some
# readers take the FIRST chase event and some the LAST, so the second pass says which one it is.
_REPEATED = ("evm.quote_initial", "submission:quote", "evm.balance_pre", "chase_quote_confirmation")


def test_the_recheck_pass_tags_its_repeated_trace_events():
    trace = _Trace()
    result, _asked, _signed, _c = _entry([KYBER_STALE, V4_FRESH], trace=trace)
    assert result["status"] == "filled"
    for name in _REPEATED:
        tags = [md.get("stale_recheck") for n, md in trace.events if n == name]
        assert tags[:2] == [None, True] and True not in tags[2:], (name, tags)
    tagged = {n for n, md in trace.events if "stale_recheck" in md}
    assert tagged == set(_REPEATED)                       # nothing after the gates carries the tag


def test_the_recheck_tag_reaches_the_execution_premium_event(monkeypatch):
    from sniper.execution import evm_swap_executor as E
    trace = _Trace()
    E._execution_premium({"funded_contract_json": "{}"}, 1.0, trace, attempt_no=1,
                         revision="initial", tag={"stale_recheck": True})
    E._execution_premium({"funded_contract_json": "{}"}, 1.0, trace, attempt_no=1, revision="initial")
    assert [md.get("stale_recheck") for _n, md in trace.events] == [True, None]
    assert "stale_recheck" not in trace.events[1][1]
    passed = []
    monkeypatch.setattr(E, "_execution_premium", lambda *a, **kw: passed.append(kw.get("tag")))
    _entry([KYBER_STALE, V4_FRESH])
    assert not passed[0] and passed[1] == {"stale_recheck": True}
    assert {"stale_recheck": True} not in passed[2:]


def test_an_entry_without_a_recheck_carries_no_tag():
    trace = _Trace()
    result, _asked, _signed, _c = _entry([V4_FRESH], trace=trace)
    assert result["status"] == "filled" and trace.events
    assert not [n for n, md in trace.events if "stale_recheck" in md]


def test_ethereum_source_receipt_warms_hash_verified_hook_key_off_entry_thread(stored):
    manager = '0x000000000004444c5dc75cb358380d2e3de08a90'
    receipt = dict(RECEIPT, logs=[dict(log, address=manager)
        if log['address'].lower() == PT.V4_POOL_MANAGERS[CHAIN] else dict(log)
        for log in RECEIPT['logs']])
    try:
        assert PT.warm_v4_keys('1', receipt, MINT, offpath_fn=lambda *_: None) == 1
        _drain()
        assert stored == [('1', POOL, KEY)]
    finally:
        PT._KEY_CACHE.pop(('1', POOL), None)
        V4Q._key_cache.pop(('1', POOL), None)
