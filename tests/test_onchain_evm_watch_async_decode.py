"""BSC socket admission stays live while canonical receipt decoding is unfinished."""
import json
import threading
import time
from concurrent.futures import Future

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as push
from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
A, B = ("0x" + part * 32 for part in ("11", "22"))


def log(tx=A, block=100):
    return dict(transactionHash=tx, address=MINT, blockNumber=hex(block))


@pytest.fixture
def env(monkeypatch, tmp_path):
    roster = {WALLET: ("u1", "h1")}
    calls, publications, handles, signals = [], [], [], []
    origin_factory = w._relay_origin_worker

    class Conn:
        def __init__(self):
            self.owner = threading.get_ident()
            self.closed = False
            handles.append(self)

        def close(self):
            assert self.owner == threading.get_ident()
            self.closed = True

    monkeypatch.setattr(w, "get_connection", lambda path: Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: dict(roster))
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: dict(roster))
    monkeypatch.setattr(w, "_roster_connection", lambda: Conn())
    monkeypatch.setattr(w, "_relay_origin_worker", lambda *args, **kwargs: None)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    monkeypatch.setattr(w, "erc20_decimals", lambda *args: 18)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS", frozenset(), raising=False)
    monkeypatch.setattr(cfg, "memecoin_onchain_evm_j7_xcheck_chains", lambda: frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 60.)

    def signal(observe_only):
        owner = threading.get_ident()
        result = lambda *args, **kwargs: None
        signals.append((owner, result))
        return result

    monkeypatch.setattr(w, "_bsc_lane_signal", signal)

    def publish(conn, row, **kwargs):
        assert conn.owner == threading.get_ident()
        assert any(owner == conn.owner and sig is kwargs["bsc_signal"] for owner, sig in signals)
        publications.append(row)

    monkeypatch.setattr(w, "_publish_detection", publish)
    monkeypatch.setattr(w, "_defer_relay_origin", lambda *args, **kwargs: False)
    w._configure("56")
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "heartbeat.json")
    price = w.NativePrice(lambda: 600., log_fn=lambda line: None)
    price.refresh()
    yield dict(roster=roster, calls=calls, publications=publications, handles=handles,
               signals=signals, price=price, origin_factory=origin_factory)
    w._configure("4663")


def detection(env, *, block_a=None, reason="detected"):
    def detect(rpc_url, source_log, watched, *, now, outcome, decimals_cache, **kwargs):
        env["calls"].append((source_log["transactionHash"], now, dict(watched),
                             threading.get_ident(), decimals_cache))
        if source_log["transactionHash"] == A and block_a is not None:
            entered, release = block_a
            entered.set()
            assert release.wait(2.)
        outcome.update(reason=reason)
        if reason != "detected":
            return None
        return dict(tx_hash=source_log["transactionHash"], wallet=WALLET, source_user_id="u1", handle="h1",
                    buy=dict(mint=MINT, token_delta=1., source_size_usd=100., source_price=1.),
                    log=source_log, now=now)
    return detect


def run(monkeypatch, env, stream):
    monkeypatch.setattr(push, "run_push_stream", stream)
    return w.run_push("wss://offline", price=env["price"])


def head(kwargs, number=102):
    kwargs["state"].head(time.time(), number, int(time.time()))
    kwargs["on_head"]()


def test_bsc_queue_and_proof_decoding_keep_original_boot_bound_receipt(monkeypatch, env):
    monkeypatch.setattr(w, "push_detection", detection(env))

    def stream(url, **kwargs):
        assert kwargs["on_transfer"](log()).result(1.) is True
        return kwargs["state"]

    run(monkeypatch, env, stream)
    row = env["publications"][0]
    from datetime import datetime
    assert abs((datetime.fromisoformat(row["receipt_clock"]["raw_utc"])
                - datetime.fromisoformat(row["now"])).total_seconds()) < .02
    assert row["receipt_clock"]["boot_id"]
    stages = {stage["name"]: stage for stage in row["receipt_stages"]}
    assert stages["decode_queue"]["monotonic_ns"] <= stages["decode_start"]["monotonic_ns"]
    assert stages["decode_start"]["monotonic_ns"] <= stages["decode_end"]["monotonic_ns"]
    assert row["valuation"]["receipt_clock"] == row["receipt_clock"]


@pytest.mark.parametrize("gap", ["full", "expired"])
def test_rejected_or_expired_first_ingress_survives_successful_redelivery(monkeypatch, env, gap):
    entered, release = threading.Event(), threading.Event()
    now, mono = [0.], [100]
    original = w._CanonicalDecodeWork
    monkeypatch.setattr(w, "_CanonicalDecodeWork", lambda *args, **kwargs: original(
        *args, workers=1, cap=1 if gap == "full" else 2, now_fn=lambda: now[0], **kwargs))
    monkeypatch.setattr(w.time, "monotonic_ns", lambda: mono[0])
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))

    def stream(url, **kwargs):
        first = kwargs["on_transfer"](log(A))
        try:
            assert entered.wait(1)
            mono[0] = 200
            dropped = kwargs["on_transfer"](log(B, 101))
            if gap == "expired":
                now[0] = 100.
            release.set()
            assert first.result(1) is True
            assert dropped.result(1) is None
            mono[0] = 300
            assert kwargs["on_transfer"](log(B, 101)).result(1) is True
            return kwargs["state"]
        finally:
            release.set()

    run(monkeypatch, env, stream)
    row = next(row for row in env["publications"] if row["tx_hash"] == B)
    assert row["receipt_clock"]["monotonic_ns"] == 200
    assert env["calls"][-1][1] == row["now"], "keep the successful delivery's age timestamp"


def test_different_transfer_logs_for_one_source_transaction_keep_transaction_ingress(monkeypatch, env):
    mono = [100]
    monkeypatch.setattr(w.time, "monotonic_ns", lambda: mono[0])
    original = detection(env)
    seen = []
    def decode(*args, **kwargs):
        seen.append(args[1]["address"])
        if len(seen) == 1:
            kwargs["outcome"].update(reason="receipt_null")
            return None
        return original(*args, **kwargs)
    monkeypatch.setattr(w, "push_detection", decode)
    def stream(url, **kwargs):
        assert kwargs["on_transfer"](log(A)).result(1) is False
        mono[0] = 200
        other_log = dict(log(A), address="0x" + "ef" * 20,
                         topics=["0x" + "aa" * 32, "0x" + "bb" * 32, "0x" + "cc" * 32])
        assert kwargs["on_transfer"](other_log).result(1) is True
        return kwargs["state"]
    run(monkeypatch, env, stream)
    assert env["publications"][0]["receipt_clock"]["monotonic_ns"] == 100


def test_blocked_decode_does_not_delay_next_event_or_head_and_keeps_socket_clock(monkeypatch, env):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))
    socket_owner = threading.get_ident()

    def stream(url, **kwargs):
        before = w.now_iso()
        started = time.perf_counter()
        first = kwargs["on_transfer"](log())
        try:
            assert time.perf_counter() - started < .1, "receipt decode blocked socket delivery"
            assert isinstance(first, Future) and entered.wait(1.)
            env["roster"]["0x" + "ef" * 20] = ("u2", "h2")
            kwargs["addresses_fn"]()                # roster changes after A's socket arrival
            second = kwargs["on_transfer"](log(B, 101))
            assert second.result(1.) is True
            head(kwargs)
            beat = json.loads(w.HEARTBEAT.read_text())
            assert beat["last_block"] == 99, "a head may not cover A's unfinished receipt"
            assert [row["tx_hash"] for row in env["publications"]] == [B]
            assert env["calls"][0][1] >= before
            assert env["calls"][0][2] == {WALLET: ("u1", "h1")}
            assert env["calls"][1][2] == env["roster"]
        finally:
            release.set()
        assert first.result(1.) is True
        head(kwargs, 103)
        assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 103
        return kwargs["state"]

    run(monkeypatch, env, stream)
    assert all(call[3] != socket_owner for call in env["calls"])
    assert env["calls"][0][4] is not env["calls"][1][4]
    assert len({owner for owner, _ in env["signals"]}) == 3
    # Worker-owned SQLite handles close on their owning thread.
    deadline = time.monotonic() + 1.
    while not all(conn.closed for conn in env["handles"]) and time.monotonic() < deadline:
        time.sleep(.001)
    assert all(conn.closed for conn in env["handles"])


def test_same_transaction_has_one_decode_while_queued_or_active(monkeypatch, env):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))

    def stream(url, **kwargs):
        first = kwargs["on_transfer"](log())
        try:
            assert isinstance(first, Future) and entered.wait(1.)
            duplicate = kwargs["on_transfer"](log())
            assert duplicate is first
            head(kwargs)
            assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 99
        finally:
            release.set()
        assert first.result(1.) is True
        assert kwargs["on_transfer"](log()).result(1.) is True
        return kwargs["state"]

    run(monkeypatch, env, stream)
    assert len(env["calls"]) == len(env["publications"]) == 1


@pytest.mark.parametrize("replacement", [None, ("u2", "h2")], ids=["revoked", "rebound"])
def test_revoked_wallet_never_publishes_a_queued_detection(monkeypatch, env, replacement):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))

    def stream(url, **kwargs):
        future = kwargs["on_transfer"](log())
        try:
            assert entered.wait(1.)
            env["roster"].clear()
            if replacement is not None:
                env["roster"][WALLET] = replacement
            kwargs["addresses_fn"]()
        finally:
            release.set()
        assert future.result(1.) is True       # an explicit revocation is a completed decision
        return kwargs["state"]

    run(monkeypatch, env, stream)
    assert env["calls"][0][2] == {WALLET: ("u1", "h1")}
    assert env["publications"] == []


def test_queued_decode_keeps_original_socket_utc_and_roster(monkeypatch, env):
    third_tx = "0x" + "33" * 32
    entered_a, entered_b, release = threading.Event(), threading.Event(), threading.Event()
    decoded = []

    def detect(rpc_url, source_log, watched, *, now, outcome, **kwargs):
        tx = source_log["transactionHash"]
        if tx in (A, B):
            (entered_a if tx == A else entered_b).set()
            assert release.wait(2.)
        decoded.append((tx, now, w.now_iso(), dict(watched)))
        outcome.update(reason="no_buy")
        return None

    monkeypatch.setattr(w, "push_detection", detect)

    def stream(url, **kwargs):
        a, b = kwargs["on_transfer"](log()), kwargs["on_transfer"](log(B, 101))
        try:
            assert entered_a.wait(1.) and entered_b.wait(1.)
            before = w.now_iso()
            c = kwargs["on_transfer"](log(third_tx, 102))
            after = w.now_iso()
            env["roster"]["0x" + "ef" * 20] = ("u2", "h2")
            kwargs["addresses_fn"]()
            time.sleep(.02)
        finally:
            release.set()
        assert all(future.result(1.) is True for future in (a, b, c))
        tx, arrival, began, roster = next(row for row in decoded if row[0] == third_tx)
        assert before <= arrival <= after < began
        assert roster == {WALLET: ("u1", "h1")}
        return kwargs["state"]

    run(monkeypatch, env, stream)


def test_failed_publication_closes_writer_before_worker_accepts_another_tx(monkeypatch, env):
    original = w._CanonicalDecodeWork
    monkeypatch.setattr(w, "_CanonicalDecodeWork", lambda *args, **kwargs:
                        original(*args, workers=1, **kwargs))
    monkeypatch.setattr(w, "push_detection", detection(env))
    conn_used = []

    def publish(conn, row, **kwargs):
        conn_used.append(conn)
        if row["tx_hash"] == A:
            raise RuntimeError("sqlite publication failed")
        assert conn_used[0].closed and conn is not conn_used[0]
        env["publications"].append(row)

    monkeypatch.setattr(w, "_publish_detection", publish)

    def stream(url, **kwargs):
        with pytest.raises(RuntimeError, match="sqlite publication failed"):
            kwargs["on_transfer"](log()).result(1.)
        assert kwargs["on_transfer"](log(B, 101)).result(1.) is True
        return kwargs["state"]

    run(monkeypatch, env, stream)
    assert [row["tx_hash"] for row in env["publications"]] == [B]


def test_tape_reader_and_signal_close_the_owned_handle(monkeypatch):
    from sniper.services import memecoin_bsc_v2_live as tape
    closed, conns = [], []

    class Conn:
        def close(self):
            closed.append(self)

    monkeypatch.setattr(tape, "connect", lambda **kwargs: conns.append(Conn()) or conns[-1])
    monkeypatch.setattr(bsc, "tape_hop_state", lambda conn, pair, seq, **kwargs: "missing")
    reader = bsc.tape_hop_reader()
    signal = bsc.make_watcher_signal("live", "unused", hop_state_fn=reader)
    assert reader("pair", 1) == "missing"
    signal.close()
    assert closed == conns
    signal.close()                  # close is idempotent; next use owns a new handle
    assert closed == conns
    assert reader("pair", 2) == "missing" and len(conns) == 2
    reader.close()
    assert closed == conns


def test_shutdown_timeout_is_explicit_and_keeps_active_publication_frontier(monkeypatch, env):
    original = w._PublicationFence

    class FastTimeout(original):
        def close(self, **kwargs):
            return super().close(timeout_s=.01)

    monkeypatch.setattr(w, "_PublicationFence", FastTimeout)
    monkeypatch.setattr(w, "push_detection", detection(env))
    entered, release = threading.Event(), threading.Event()
    future = []

    def publish(conn, row, **kwargs):
        entered.set()
        assert release.wait(2.)

    monkeypatch.setattr(w, "_publish_detection", publish)

    def stream(url, **kwargs):
        head(kwargs)
        future.append(kwargs["on_transfer"](log()))
        assert entered.wait(1.)
        return kwargs["state"]

    try:
        with pytest.raises(RuntimeError, match="watcher_publication_drain_timeout"):
            run(monkeypatch, env, stream)
        assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 99
    finally:
        release.set()
        if future:
            future[0].result(1.)


def test_publication_claims_do_not_serialize_local_work_and_close_refuses_new_claims():
    fence = w._PublicationFence()
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(2.)

    thread = threading.Thread(target=lambda: fence.call(blocked))
    thread.start()
    try:
        assert entered.wait(1.)
        assert fence.call(lambda: "second") == "second"
        assert fence.close(timeout_s=.01) is False
        with pytest.raises(RuntimeError, match="watcher_stopped"):
            fence.call(lambda: pytest.fail("closed publication ran"))
    finally:
        release.set()
        thread.join(1.)
    assert fence.close(timeout_s=.1) is True


def test_tx_read_host_closes_only_after_decoder_workers_stop(monkeypatch, env):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))

    class Host:
        def close(self):
            assert release.is_set(), "read host closed beneath an unfinished decode"
            closed.set()

    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: Host())
    future = []

    def stream(url, **kwargs):
        future.append(kwargs["on_transfer"](log()))
        assert entered.wait(1.)
        return kwargs["state"]

    try:
        run(monkeypatch, env, stream)
        assert not closed.is_set()
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="watcher_stopped"):
        future[0].result(1.)
    assert closed.wait(1.)


def test_socket_delivery_crosscheck_is_recorded_before_decode_finishes(monkeypatch, env):
    delivered = []
    entered, release = threading.Event(), threading.Event()

    class CrossCheck:
        def __init__(self, *args, **kwargs):
            self.stats, self.stop, self.force = {}, threading.Event(), threading.Event()

        def delivered_tx(self, tx):
            delivered.append((tx, threading.get_ident()))

    monkeypatch.setattr(cfg, "memecoin_onchain_evm_j7_xcheck_chains", lambda: frozenset({"56"}))
    monkeypatch.setattr(w, "J7CrossCheck", CrossCheck)
    monkeypatch.setattr(w, "_start_j7_xcheck", lambda *args: None)
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))
    socket_thread = threading.get_ident()

    def stream(url, **kwargs):
        future = kwargs["on_transfer"](log())
        try:
            assert delivered == [(A, socket_thread)]
            assert entered.wait(1.)
        finally:
            release.set()
        assert future.result(1.) is True
        assert delivered == [(A, socket_thread)], "worker completion is not another socket arrival"
        return kwargs["state"]

    run(monkeypatch, env, stream)


def test_unknown_receipt_recovers_on_existing_bounded_backfill(monkeypatch, env):
    calls, pages = [], []
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_CHAINS", frozenset({"56"}))
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", 1)
    monkeypatch.setattr(w, "_backfill_url", lambda chain: "https://offline.invalid")
    monkeypatch.setattr(w, "_backfill_thread", lambda target: target())

    def detect(rpc_url, source_log, watched, *, outcome, **kwargs):
        calls.append(source_log)
        outcome.update(reason="read_error" if len(calls) == 1 else "no_buy")
        return None

    def backfill(url, method, params):
        assert method == "eth_getLogs"
        pages.append(params[0])
        return [dict(log(), blockTimestamp="0x64")]

    monkeypatch.setattr(w, "push_detection", detect)
    monkeypatch.setattr(w, "_backfill_post", backfill)

    def stream(url, **kwargs):
        assert kwargs["on_transfer"](log()).result(1.) is False
        for number in (101, 102):
            kwargs["state"].head(time.time(), number, number)
            kwargs["on_head"]()
        assert len(pages) == 1 and int(pages[0]["fromBlock"], 16) == 100
        assert len(calls) == 2
        assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 102
        return kwargs["state"]

    run(monkeypatch, env, stream)


def test_missing_backfill_host_retains_hole_until_named_lane_expiry(monkeypatch, env, capsys):
    monkeypatch.setattr(w, "push_detection", detection(env, reason="read_error"))

    def stream(url, **kwargs):
        assert kwargs["on_transfer"](log()).result(1.) is False
        for number in range(101, 148):
            kwargs["state"].head(time.time(), number, number)
            kwargs["on_head"]()
            if number == 105:
                assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 99
        assert json.loads(w.HEARTBEAT.read_text())["last_block"] == 147
        return kwargs["state"]

    run(monkeypatch, env, stream)
    assert "expired unread: no read host" in capsys.readouterr().out


@pytest.mark.parametrize("stopped", [False, True], ids=["authority_revoked", "watcher_stopped"])
def test_late_relay_verification_cannot_publish_after_revocation_or_shutdown(monkeypatch, env, stopped):
    from sniper.services import memecoin_relay_initiation as relay
    entered, release = threading.Event(), threading.Event()
    allowed = [True]
    fence = w._PublicationFence()

    def verify(item):
        entered.set()
        assert release.wait(2.)
        return item

    monkeypatch.setattr(relay, "WatcherVerifier", lambda *args, **kwargs: verify)
    monkeypatch.setattr(w, "_deposit_store", lambda: None)
    worker = env["origin_factory"](None, publication_fence=fence, authority_fn=lambda row: allowed[0])
    row = dict(tx_hash=A, wallet=WALLET, source_user_id="u1", valuation={},
               buy=dict(source_initiation=dict(block_number=100)))
    try:
        assert worker.submit(dict(row=row, outcome={}, quote_usd=600., price_age=0.,
                                  submitted_mono=time.monotonic()))
        assert entered.wait(1.) and worker.frontier(105) == 99
        if stopped:
            assert fence.close()
        else:
            allowed[0] = False
        release.set()
        deadline = time.monotonic() + 1.
        while worker.stats["verified"] == 0 and time.monotonic() < deadline:
            time.sleep(.001)
        while worker.stats["callback_failed"] == 0 and worker.frontier(105) == 99 and time.monotonic() < deadline:
            time.sleep(.001)
        assert env["publications"] == []
        assert worker.frontier(105) == (99 if stopped else 105)
        assert worker.stats["callback_failed"] == (1 if stopped else 0)
    finally:
        release.set()
        worker.close()


def test_origin_worker_cleanup_runs_on_each_owning_thread_only_at_shutdown():
    from sniper.services.memecoin_relay_initiation import OriginWorker
    published, cleaned = [], []
    done = threading.Event()

    def publish(item):
        published.append(threading.get_ident())
        done.set()

    worker = OriginWorker(lambda item: item, publish, workers=1,
                          on_stop=lambda: cleaned.append(threading.get_ident()))
    try:
        assert worker.submit(dict(tx_hash=A)) and done.wait(1.)
        assert cleaned == [], "do not reopen a warmed tape connection for each publication"
    finally:
        worker.close()
    assert cleaned == published and cleaned[0] != threading.get_ident()


def test_shutdown_retains_unfinished_frontier_without_fresh_heartbeat_or_late_publication(monkeypatch, env):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(w, "push_detection", detection(env, block_a=(entered, release)))
    first = []
    stale_ts = "2026-10-10T00:00:00+00:00"

    def stream(url, **kwargs):
        head(kwargs)
        first.append(kwargs["on_transfer"](log()))
        assert isinstance(first[0], Future) and entered.wait(1.)
        beat = json.loads(w.HEARTBEAT.read_text())
        beat["ts"] = stale_ts
        w.HEARTBEAT.write_text(json.dumps(beat))
        return kwargs["state"]

    try:
        run(monkeypatch, env, stream)
        beat = json.loads(w.HEARTBEAT.read_text())
        assert beat["last_block"] == 99 and beat["ts"] == stale_ts
        assert w._restart_anchor()[0] == 99
    finally:
        release.set()
    with pytest.raises(RuntimeError, match="watcher_stopped"):
        first[0].result(1.)
    assert env["publications"] == []


@pytest.mark.parametrize("answer", [False, None, "error"])
def test_incomplete_decode_holds_until_bounded_recovery_is_drained(answer):
    def decode(item, arrival):
        if answer == "error":
            raise RuntimeError("receipt timeout")
        return answer
    work = w._CanonicalDecodeWork(decode, max_age_s=45.)
    try:
        future = work.submit(log(), "arrival", {WALLET: ("u1", "h1")})
        try:
            future.result(1.)
        except RuntimeError:
            assert answer == "error"
        assert work.frontier(105) == 99
        assert work.drain_failed() == (100, 100)
        assert work.frontier(105) == 105
    finally:
        work.close(wait=True)


def test_overflow_and_expiry_keep_bounded_unread_range():
    entered, release = threading.Event(), threading.Event()
    now = [0.]
    calls = []

    def decode(item, arrival):
        calls.append(item[0]["transactionHash"])
        entered.set()
        assert release.wait(2.)
        return True

    work = w._CanonicalDecodeWork(decode, workers=1, cap=2, max_age_s=45., now_fn=lambda: now[0])
    try:
        first = work.submit(log(), "arrival", {})
        assert entered.wait(1.)
        queued = work.submit(log(B, 101), "arrival", {})
        for block in range(102, 302):
            tx = "0x" + format(block, "064x")
            assert work.submit(log(tx, block), "arrival", {}).result() is None
        assert work.snapshot()["full"] == 200
        assert work.frontier(305) == 99
        now[0] = 45.1
        release.set()
        assert first.result(1.) is True and queued.result(1.) is None
        assert calls == [A], "expired queued observations may not reach decoder"
        assert work.snapshot()["expired"] == 1
        assert work.drain_failed() == (101, 301)
    finally:
        release.set()
        work.close(wait=True)
