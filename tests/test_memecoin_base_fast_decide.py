"""Base 8453 fast lane, step 0 (spec 2026-10-02-memecoin-base-8453-fast-lane-design.md rev 3): the
sign-less decide in the 8453 watcher, recorded only. The transactions are REAL Base source buys the
8453 watcher detected (fixture base_v4_relay_buys.json); nothing here reads a network host."""
import json
import queue
import sqlite3
import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from sniper.services import memecoin_base_fast_decide as base
from sniper.services.memecoin_evm_v4_events import v4_pool_id

FX = json.loads((Path(__file__).resolve().parent / "fixtures" / "memecoin" / "base_v4_relay_buys.json").read_text())
KNOBS = dict(eth_usd=3500.0, eth_usd_age_s=3.0, clip_usd=50.0, chase_pct=40.0, premium=0.0)


def _decide(name, key_fn=None, **kw):
    case = FX[name]
    return base.decide(case["tx"], case["receipt"], mint=case["mint"], key_fn=key_fn, **dict(KNOBS, **kw))


@pytest.mark.parametrize("name, kind", [("native", "v4_native"), ("weth", "v4_weth"), ("bridge", "v4_bridge"),
                                        ("hook_sells", "v4_weth"), ("two_pools", "v4_native")])
def test_a_v4_buy_decides_from_its_own_receipt_with_its_key_from_the_calldata(name, kind):
    rec = _decide(name)
    assert (rec["reason"], rec["route_kind"], rec["key_source"]) == ("would_send", kind, "calldata")
    assert v4_pool_id(rec["key"]) == rec["pool"], "a calldata key is accepted only when it hashes to B"
    assert int(rec["min_out"]) > 0 and rec["decide_ms"] < 50


def test_cap_and_min_out_are_the_4663_rules_on_the_post_trade_price():
    rec = _decide("native")
    num, den = rec["cap"]
    post = Fraction(*rec["post_price"])
    assert Fraction(num, den) == post * Fraction(140, 100)                   # premium off: chase only
    assert int(rec["min_out"]) == int(rec["clip_x"]) * den // num
    assert int(rec["clip_x"]) == int(Fraction(50) / Fraction(3500) * 10 ** 18)
    src = Fraction(int(rec["src_in"]), int(rec["src_out"]))
    assert src < Fraction(num, den), "the source's own average sits under the cap"
    capped = _decide("native", premium=1.05)
    assert Fraction(*capped["cap"]) == min(post * Fraction(140, 100), src * Fraction("1.05"))


def test_a_hook_trading_the_pool_after_the_source_leaves_the_last_state_and_the_buy_legs_only():
    """0x00219c23: the source bought, then the pool's dynamic-fee hook sold coin back twice in the
    same transaction. B's post-state is the last swap; the source's average is its buy leg alone."""
    rec = _decide("hook_sells")
    swaps = base.v4_swaps(FX["hook_sells"]["receipt"])
    assert (rec["legs_on_pool"], rec["buy_legs"]) == (3, 1)
    assert rec["post_sqrt"] == str(swaps[-1]["sqrt"])
    assert (int(rec["src_in"]), int(rec["src_out"])) == (3612071319639821, 980008832694330912074824)


def test_the_bridge_prices_hop_a_at_the_eth_usd_mark_with_usdc_at_one_dollar():
    rec = _decide("bridge")
    assert int(rec["clip_x"]) == int(Fraction(int(rec["clip_wei"])) * Fraction("3500") / 10 ** 12)


@pytest.mark.parametrize("name, reason", [("unsupported", "bridge_unsupported"), ("no_v4", "no_v4_leg")])
def test_shapes_the_lane_cannot_take_are_named(name, reason):
    assert _decide(name)["reason"] == reason


def test_a_key_absent_from_the_calldata_comes_from_the_store_or_is_unknown_never_guessed():
    case = FX["native"]
    tx = dict(case["tx"], input="0x0a2b8f36" + "00" * 64)          # the key is gone from the calldata
    rec = base.decide(tx, case["receipt"], mint=case["mint"], **KNOBS)
    assert rec["reason"] == "key_unknown"
    key = _decide("native")["key"]
    stored = base.decide(tx, case["receipt"], mint=case["mint"], key_fn=lambda chain, pool: key, **KNOBS)
    assert (stored["reason"], stored["key_source"]) == ("would_send", "store")


def test_a_near_miss_key_in_the_calldata_is_refused():
    """A key whose hash is not B's pool id is not accepted, however key-shaped it looks."""
    case = FX["native"]
    receipt = json.loads(json.dumps(case["receipt"]))
    for log in receipt["logs"]:
        if len(log["topics"]) > 1 and log["topics"][1] == "0x" + _decide("native")["pool"][2:]:
            log["topics"][1] = "0x" + "ab" * 32
    assert base.decide(case["tx"], receipt, mint=case["mint"], **KNOBS)["reason"] == "key_unknown"


@pytest.mark.parametrize("name, age, reason", [("native", 31.0, "eth_usd_stale"), ("bridge", 31.0, "bridge_state_stale"),
                                               ("bridge", None, "bridge_state_stale")])
def test_a_stale_or_absent_mark_is_named_never_priced(name, age, reason):
    assert _decide(name, eth_usd_age_s=age)["reason"] == reason


def test_the_decide_never_raises_on_garbage():
    for tx, receipt in ((None, None), ({}, {"status": "0x1", "logs": [{"topics": None}]}),
                        ({"input": "0xzz"}, {"status": "0x1", "logs": [{"address": 5, "topics": [1, 2]}]})):
        rec = base.decide(tx, receipt, mint="0x" + "11" * 20, **KNOBS)
        assert rec["reason"] and "decide_ms" in rec


# --- the record, off the socket thread ---------------------------------------------------------------

def test_the_writer_records_off_the_socket_thread_and_a_full_queue_drops_without_blocking(tmp_path):
    w = base.DecisionWriter(tmp_path / "d.db")
    rec = dict(_decide("native"), decided_at="2026-10-02T00:00:00+00:00", tx_hash="0xaa", wallet="w")
    assert w.put(rec) is True
    w.flush()
    db = sqlite3.connect(str(tmp_path / "d.db"))
    row = db.execute("SELECT reason, route_kind, key_source, cap_num, min_out FROM base_fast_decisions").fetchone()
    assert row[:3] == ("would_send", "v4_native", "calldata") and row[3] == str(rec["cap"][0])
    w.close()

    full = base.DecisionWriter.__new__(base.DecisionWriter)
    full._q, full.dropped = queue.Queue(maxsize=1), 0
    full._q.put_nowait({})
    started = time.perf_counter()
    assert full.put(rec) is False and full.dropped == 1
    assert time.perf_counter() - started < 0.05


def test_the_lane_is_off_unless_the_knob_says_step0(monkeypatch):
    # Step 1 (plan 2026-10-14) adds the runner's two modes; anything else is still off.
    for raw, mode in (("", "off"), ("on", "off"), ("live", "live"), ("runner_shadow", "runner_shadow"),
                      ("STEP0", "step0"), ("step0", "step0")):
        monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", raw)
        assert base.mode() == mode


# --- in the 8453 watcher: after the hand-off, the mark by peek() only -------------------------------

def test_peek_never_fetches_and_reads_age():
    import scripts.research.onchain_evm_wallet_watch as w
    t = [100.0]
    calls = []
    price = w.NativePrice(lambda: calls.append(1) or 3500.0, clock=lambda: t[0], log_fn=lambda line: None)
    assert price.peek() == (None, None) and calls == []
    price.refresh()
    t[0] += 45.0
    assert price.peek() == (3500.0, 45.0) and calls == [1], "stale stays stale: no fetch"


@pytest.fixture
def watch8453(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w
    from test_onchain_evm_watch_detection_latency import _Conn, WALLET
    events = []
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(events))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "send_wake", lambda address: events.append(("wake", w.now_iso())) or True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(w, "_roster_connection", lambda: sqlite3.connect(":memory:"))
    yield w, events, WALLET, tmp_path
    w._configure("4663")
    w._BASE_LANE_BEAT.clear()


def _drive8453(monkeypatch, w, wallet):
    from sniper.services import memecoin_onchain_push as P
    case = FX["native"]
    w._configure("8453")

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, **_kw):
        outcome.update(reason="detected", _receipt=case["receipt"], _transaction=case["tx"], decimals=18)
        return dict(tx_hash=case["tx"]["hash"], wallet=wallet, source_user_id="u1", handle="h1",
                    buy={"mint": case["mint"], "token_delta": 1, "source_size_usd": 50.0, "source_price": 1e-9},
                    log={"blockNumber": case["receipt"]["blockNumber"]}, now=now)
    monkeypatch.setattr(w, "push_detection", detect)

    def stream(ws_url, **kwargs):
        kwargs["on_transfer"]({"transactionHash": case["tx"]["hash"], "address": case["mint"]})
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", stream)
    price = w.NativePrice(lambda: 3500.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)


def test_the_8453_watcher_records_a_decision_after_its_commit_and_wake(watch8453, monkeypatch, capsys):
    w, events, wallet, root = watch8453
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "step0")
    _drive8453(monkeypatch, w, wallet)
    assert [e[0] for e in events] == ["insert", "commit", "wake"], "the decide adds nothing to today's path"
    w._BASE_DECIDE.submit(lambda: None).result(timeout=5)      # the decide runs on its own thread
    out = capsys.readouterr().out
    assert "[base-lane] hook ARMED (record only): mode=step0" in out
    assert "reason=would_send kind=v4_native key=calldata" in out
    db = root / "data" / "base_fast_decisions.db"
    deadline = time.monotonic() + 5
    rows = []
    while time.monotonic() < deadline and not rows:
        try:
            rows = sqlite3.connect(str(db)).execute("SELECT reason FROM base_fast_decisions").fetchall()
        except sqlite3.OperationalError:
            pass
        time.sleep(0.05)
    assert rows == [("would_send",)]


def test_off_the_8453_watcher_records_nothing(watch8453, monkeypatch, capsys):
    w, events, wallet, root = watch8453
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "")
    _drive8453(monkeypatch, w, wallet)
    out = capsys.readouterr().out
    assert "[base-lane] hook off: mode=off" in out and "reason=" not in out.split("hook off")[1]
    assert not (root / "data" / "base_fast_decisions.db").exists()


def test_a_broken_step0_module_never_stops_funded_8453_detection(watch8453, monkeypatch, capsys):
    """Knob on, the step-0 module fails to import: today's insert, commit and wake still run, one
    line names it, and the heartbeat says step0_unavailable (Alpha, Gamma LOW 1)."""
    import sys
    import sniper.services
    w, events, wallet, root = watch8453
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "step0")
    monkeypatch.delattr(sniper.services, "memecoin_base_fast_decide", raising=False)
    monkeypatch.setitem(sys.modules, "sniper.services.memecoin_base_fast_decide", None)
    _drive8453(monkeypatch, w, wallet)
    assert [e[0] for e in events] == ["insert", "commit", "wake"]
    assert "[base-lane] step0_unavailable: ModuleNotFoundError" in capsys.readouterr().out
    monkeypatch.setattr(w, "HEARTBEAT", root / "hb.json")      # _configure re-points it
    w._write_beat({"ts": "t"})
    assert json.loads(w.HEARTBEAT.read_text())["base_step0"] == "step0_unavailable"


# --- the Relay path: decided in the origin worker's publish, after verification (Alpha 2026-10-07) --

class _Writer:
    def __init__(self):
        self.recs = []

    def put(self, rec):
        self.recs.append(rec)
        return True


def test_the_hook_labels_its_path_and_counts_the_wait_since_arrival():
    """Every row says how it was decided: direct (socket thread) or relay_verified (after the origin
    verifier), and since_arrival_ms counts the verify wait from the source's arrival."""
    case, writer = FX["native"], _Writer()
    hook = base.make_hook(writer, clip_usd_fn=lambda: 50.0, clock=lambda: "2026-10-07T21:00:02.500000+00:00")
    row = dict(tx_hash=case["tx"]["hash"], buy={"mint": case["mint"]}, now="2026-10-07T21:00:00+00:00")
    direct = hook(row, case["tx"], case["receipt"], 3500.0, 3.0, "mark")
    relayed = hook(row, case["tx"], case["receipt"], 3500.0, 3.0, "mark", path="relay_verified", verify_ms=2400.0)
    assert (direct["path"], direct["verify_ms"]) == ("direct", None)
    assert (relayed["path"], relayed["verify_ms"]) == ("relay_verified", 2400.0)
    assert relayed["since_arrival_ms"] == pytest.approx(2500.0)
    assert relayed["reason"] == "would_send"


def test_a_verified_relay_buy_is_decided_after_its_commit_and_a_refused_one_is_not(monkeypatch, capsys):
    from scripts.research import onchain_evm_wallet_watch as w
    from sniper.services import memecoin_relay_initiation as relay
    events, calls, done = [], [], threading.Event()

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(relay, "WatcherVerifier", lambda *a, **k: (lambda item: item))
    monkeypatch.setattr(w, "get_connection", lambda *_: Conn())
    monkeypatch.setattr(w, "_commit_detections", lambda conn, rows: events.append("commit"))

    def hook(row, tx, receipt, eth_usd, age, price_source, *, path="direct", verify_ms=None):
        events.append("hook")
        calls.append(dict(thread=threading.get_ident(), path=path, verify_ms=verify_ms, tx=tx,
                          price_source=price_source))
        done.set()
        return {"reason": "would_send", "route_kind": "v4_native", "key_source": "calldata", "decide_ms": 1.0}
    price = w.NativePrice(lambda: 3500.0, log_fn=lambda line: None)
    price.refresh()
    worker = w._relay_origin_worker(None, base_hook=hook, price=price)
    try:
        case = FX["native"]
        row = dict(tx_hash=case["tx"]["hash"], buy={"source_initiation": {"proof_version": "relay-v1"},
                                                   "mint": case["mint"]},
                   valuation={"quote_price_source": "mark"}, now="2026-10-07T21:00:00+00:00")
        assert w._defer_relay_origin(worker, row, {"_transaction": case["tx"], "_receipt": case["receipt"]}, 1., 1.)
        assert done.wait(2)
    finally:
        worker.close()
    assert events == ["commit", "hook"], "the decide follows the publication, never before it"
    (call,) = calls
    assert call["path"] == "relay_verified" and call["verify_ms"] is not None
    assert call["thread"] != threading.get_ident() and call["tx"] is case["tx"]
    assert call["price_source"] == "mark"

    events.clear()
    monkeypatch.setattr(relay, "WatcherVerifier", lambda *a, **k: (lambda item: (_ for _ in ()).throw(
        relay.ProofError("origin_wallet_unverified"))))
    worker = w._relay_origin_worker(None, base_hook=hook, price=price)
    try:
        row2 = dict(row, tx_hash="0x" + "ab" * 32, valuation={})
        assert w._defer_relay_origin(worker, row2, {"_transaction": case["tx"]}, 1., 1.)
        deadline = time.monotonic() + 2
        while "commit" not in events and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
    finally:
        worker.close()
    assert events == ["commit"], "a refused origin is never decided: a live lane could not act on it"
    assert "[base-lane] 0xababababab skipped origin_refused=origin_wallet_unverified" in capsys.readouterr().out


def test_a_slow_decide_never_delays_the_next_publication_or_the_socket(monkeypatch):
    """Gamma LOW on 3fa583899: a decide can wait up to the 250 ms busy_timeout on a stored-key read.
    It runs on its own thread, so neither the origin worker's next funded publication nor the push
    socket's next delivery waits for it."""
    from scripts.research import onchain_evm_wallet_watch as w
    from sniper.services import memecoin_relay_initiation as relay
    release, events, hook_threads = threading.Event(), [], []

    class Conn:
        def close(self):
            pass
    monkeypatch.setattr(relay, "WatcherVerifier", lambda *a, **k: (lambda item: item))
    monkeypatch.setattr(w, "get_connection", lambda *_: Conn())
    monkeypatch.setattr(w, "_commit_detections", lambda conn, rows: events.append("commit:" + rows[0]["tx_hash"]))

    def slow_hook(row, *a, path="direct", verify_ms=None):
        hook_threads.append(threading.get_ident())
        release.wait(2)
        return {"reason": "would_send"}
    price = w.NativePrice(lambda: 3500.0, log_fn=lambda line: None)
    price.refresh()
    worker = w._relay_origin_worker(None, base_hook=slow_hook, price=price)
    try:
        case = FX["native"]
        for n in (1, 2, 3):                # the origin pool has 2 workers: a third buy waits if both block
            row = dict(tx_hash="0x%02d" % n + "cd" * 31, buy={"source_initiation": {"proof_version": "relay-v1"},
                                                            "mint": case["mint"]}, valuation={}, now="2026-10-07T21:00:00+00:00")
            assert w._defer_relay_origin(worker, row, {"_transaction": case["tx"]}, 1., 1.)
        deadline = time.monotonic() + 1
        while len(events) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(events) == 3, "every Relay buy publishes while the earlier decides are still running"
        t0 = time.perf_counter()
        w._signal_base_lane(slow_hook, {"tx_hash": "0x" + "ef" * 32}, {}, price, "mark")
        assert time.perf_counter() - t0 < 0.05, "the socket thread hands the decide off and returns"
    finally:
        release.set()
        worker.close()
    w._BASE_DECIDE.submit(lambda: None).result(timeout=5)
    assert hook_threads and threading.get_ident() not in hook_threads


def test_the_stored_key_read_uses_a_handle_of_the_calling_thread(monkeypatch):
    """The hook runs on the socket thread and on the origin workers; a sqlite handle belongs to the
    thread that opened it, so each thread reads the PoolKey store through its own."""
    from scripts.research import onchain_evm_wallet_watch as w
    opened = []

    def roster():
        conn = sqlite3.connect(":memory:")       # check_same_thread: a cross-thread use raises
        conn.execute("CREATE TABLE memecoin_evm_v4_pool_keys(network_id, pool_id, pool_manager, key_json)")
        opened.append(threading.get_ident())
        return conn
    monkeypatch.setattr(w, "_roster_connection", roster)
    seen = []
    import sniper.persistence.db_memecoin as m
    real = m.get_evm_v4_pool_key

    def spy(conn, **kw):
        seen.append(real(conn, **kw))                # raises ProgrammingError on a foreign handle
        return None
    monkeypatch.setattr(m, "get_evm_v4_pool_key", spy)
    key_fn = w._thread_key_fn()
    assert opened == [threading.get_ident()], "the building thread's handle opens at build, as before"
    key_fn("8453", "0x" + "11" * 32)
    t = threading.Thread(target=key_fn, args=("8453", "0x" + "22" * 32))
    t.start()
    t.join()
    assert len(opened) == 2 and opened[1] != opened[0]
    assert seen == [None, None], "both reads ran on their own thread's handle"


# --- the landing job (research, its own process) ------------------------------------------------------

def _landing():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "base_fast_landing", Path(__file__).resolve().parents[1] / "scripts" / "research" / "base_fast_landing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = _landing()


def _state_answer(states):
    """An extsload(bytes32[]) answer for [(sqrt, tick, lp_fee, liquidity), ...]."""
    words = [32, 2 * len(states)]
    for sqrt, tick, fee, liq in states:
        words += [sqrt | ((tick % (1 << 24)) << 160) | (fee << 208), liq]
    return "0x" + "".join("%064x" % w for w in words)


class _Gate:
    def __init__(self, states_by_block, logs=(), fail=None):
        self.states, self.logs, self.fail, self.calls, self.limited = states_by_block, list(logs), fail, [], 0

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.fail and len(self.calls) >= self.fail[0]:
            raise L.Censored(self.fail[1])
        if method == "eth_getLogs":
            return self.logs
        return _state_answer(self.states[int(params[1], 16)])


def _swap_log(rec, block, index):
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    return {"address": V4_POOL_MANAGERS["8453"], "topics": [V4_SWAP, rec["pool"]],
            "blockNumber": hex(block), "logIndex": hex(index), "removed": False}


def _would_send():
    rec = _decide("native")
    return dict(rec, tx_hash=FX["native"]["tx"]["hash"], wallet="w", decided_at="2026-10-02T00:00:00+00:00")


def test_landing_reads_b_at_the_end_of_s_s1_and_s2_and_counts_later_swaps():
    rec = _would_send()
    s, post, liq = rec["block"], int(rec["post_sqrt"]), int(rec["liquidity"])
    moved = post * 13 // 10                 # copiers pushed the coin up (currency1 per currency0 down)
    gate = _Gate({s: [(post, 0, 0, liq)], s + 1: [(moved, 0, 0, liq)], s + 2: [(moved, 0, 0, liq)]},
                 logs=[_swap_log(rec, s, rec["swap_log_index"]), _swap_log(rec, s + 1, 1),
                       _swap_log(rec, s + 1, 9)])
    row = L.grade(rec, gate)
    assert (row["clears_s"], row["other_swaps_s"], row["other_swaps_s1"]) == (1, 0, 2)
    assert row["clears_s1"] == row["clears_s2"] and row.get("censored") is None
    assert [int(c[1][1], 16) for c in gate.calls if c[0] == "eth_call"] == [s, s + 1, s + 2]


def test_a_landing_row_keeps_the_path_and_the_wait_so_the_two_cohorts_split():
    rec = dict(_would_send(), path="relay_verified", since_arrival_ms=2500.0)
    s, post, liq = rec["block"], int(rec["post_sqrt"]), int(rec["liquidity"])
    gate = _Gate({b: [(post, 0, 0, liq)] for b in (s, s + 1, s + 2)}, logs=[])
    row = L.grade(rec, gate)
    assert (row["path"], row["since_arrival_ms"]) == ("relay_verified", 2500.0)
    assert L.grade(_would_send(), gate)["path"] == "direct", "a row without a path was decided direct"


def test_landing_on_the_receipts_own_state_clears_and_a_price_run_fails():
    rec = _would_send()
    post, liq = int(rec["post_sqrt"]), int(rec["liquidity"])
    key = rec["key"]
    x_is_c0 = key["currency0"] == rec["x"]
    assert L.clears((post, 0, 0, liq), x_is_c0=x_is_c0, clip_x=rec["clip_x"], min_out=rec["min_out"],
                    lp_fee_pips=key["fee"]) == 1
    ran = post * 10 // 13 if x_is_c0 else post * 13 // 10       # the coin costs 1.69x
    assert L.clears((ran, 0, 0, liq), x_is_c0=x_is_c0, clip_x=rec["clip_x"], min_out=rec["min_out"],
                    lp_fee_pips=key["fee"]) == 0


def test_a_failed_landing_read_censors_the_row_never_a_clear(tmp_path):
    rec = _would_send()
    row = L.grade(rec, _Gate({}, fail=(1, "http_429")))
    assert row["censored"] == "http_429" and not any(k.startswith("clears_") for k in row)


def test_the_landing_job_reads_the_decisions_read_only_and_skips_fresh_and_graded_rows(tmp_path):
    from datetime import datetime, timezone
    w = base.DecisionWriter(tmp_path / "d.db")
    rec = _would_send()
    w.put(rec)
    w.put(dict(rec, tx_hash="0xfresh", decided_at="2026-10-02T00:00:50+00:00"))
    w.flush()
    w.close()
    s, post, liq = rec["block"], int(rec["post_sqrt"]), int(rec["liquidity"])
    gate = _Gate({b: [(post, 0, 0, liq)] for b in (s, s + 1, s + 2)})
    now = datetime(2026, 10, 2, 0, 1, 0, tzinfo=timezone.utc)
    assert L.run(tmp_path / "d.db", tmp_path / "l.db", call=gate, now=now)["graded"] == 1
    assert L.run(tmp_path / "d.db", tmp_path / "l.db", call=gate, now=now)["graded"] == 0
