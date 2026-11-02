"""Funded Solana RPC call counter (spec 2026-10-01 burst read coalescing, §5).

Counts every request the funded pool sends, per label and per JSON-RPC method, under the pool's own
lock; keeps a per-minute ring for the last hour; reaches the heartbeat through the whitelist
projection. Telemetry only: the rotation rules must be exactly as before.
"""
import json
import threading

from sniper.execution import solana_rpc_pool as pool_mod
from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp, rpc_label

A, B = "https://a/rpc", "https://b/rpc"


def _body(*methods):
    reqs = [{"jsonrpc": "2.0", "id": i, "method": m, "params": []} for i, m in enumerate(methods)]
    return json.dumps(reqs[0] if len(reqs) == 1 else reqs)


class Http:
    def __init__(self, fail=()):
        self.fail, self.seen = set(fail), []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        self.seen.append(url)
        if url in self.fail:
            err = OSError("down")
            err.code = 429
            raise err
        return 200, {"result": 1}, {}


class Wall:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _pool(http=None, wall=None):
    return RotatingSolanaRpcHttp(http or Http(), (A, B), wall=wall or Wall())


def test_each_request_counts_per_label_and_method_and_a_batch_counts_each_member():
    p = _pool()
    with rpc_label("entry:j7_wallets"):
        p("POST", A, body=_body("getAccountInfo"))
        p("POST", A, body=_body("getBalance", "getTokenAccountBalance"))
    p("POST", "https://jupiter/quote", body="{}")             # not the pool's anchor: never counted
    calls = p.snapshot()["calls"]
    assert calls["entry:j7_wallets"] == {"getAccountInfo": 1, "getBalance": 1, "getTokenAccountBalance": 1}
    assert sum(sum(v.values()) for v in calls.values()) == 3


def test_a_rotation_retry_is_a_second_request_and_counts_twice():
    p = _pool(Http(fail={A}))
    with rpc_label("exit:periodic_sweep"):
        p("POST", A, body=_body("getAccountInfo"))
    assert p.snapshot()["calls"]["exit:periodic_sweep"] == {"getAccountInfo": 2}


def test_a_write_is_counted_and_still_never_rotated():
    p = _pool(Http(fail={A}))
    try:
        with rpc_label("entry:j7_wallets"):
            p("POST", A, body=_body("sendTransaction"))
    except OSError:
        pass
    assert p.snapshot()["calls"]["entry:j7_wallets"] == {"sendTransaction": 1}
    assert p.snapshot()["active_index"] == 0                 # the write did not move the pool


def test_an_unlabelled_thread_is_named_by_its_thread_class():
    p = _pool()
    t = threading.Thread(target=lambda: p("POST", A, body=_body("getSlot")), name="exit-preview_3")
    t.start()
    t.join()
    assert p.snapshot()["calls"] == {"thread:exit-preview": {"getSlot": 1}}


def test_labels_never_leak_between_threads():
    p = _pool()
    go, done = threading.Event(), []

    def other():
        go.wait()
        p("POST", A, body=_body("getSlot"))                  # no label of its own
        done.append(1)
    t = threading.Thread(target=other, name="worker")
    t.start()
    with rpc_label("entry:onchain_watch"):
        go.set()
        t.join()
        p("POST", A, body=_body("getSlot"))
    assert p.snapshot()["calls"] == {"thread:worker": {"getSlot": 1}, "entry:onchain_watch": {"getSlot": 1}}
    assert pool_mod.current_label().startswith("thread:")    # restored after the block


def test_an_unparseable_body_counts_as_unknown():
    p = _pool()
    with rpc_label("x"):
        p("POST", A, body="not json")
    assert p.snapshot()["calls"]["x"] == {"?": 1}


def test_the_minute_ring_keeps_the_last_hour():
    wall = Wall()
    p = _pool(wall=wall)
    with rpc_label("exit:periodic_sweep"):
        for minute in range(65):
            wall.t = 1_790_000_000.0 + 60 * minute
            p("POST", A, body=_body("getSlot"))
            p("POST", A, body=_body("getSlot"))
    ring = p.snapshot()["minutes"]
    assert len(ring) == 60 and all(counts == {"exit": 2} for _m, counts in ring)
    assert ring[-1][0] == int(wall.t // 60) * 60


def test_the_ring_counts_requests_by_lane_class_while_calls_keep_the_full_label():
    p = _pool()
    for label in ("entry:j7_wallets", "entry:onchain_watch", "entry_preread:j7_wallets",
                  "exit:fastlane_wake", "thread:worker", "entryx:y"):
        with rpc_label(label):
            p("POST", A, body=_body("getBalance", "getSlot"))       # one request, two methods
    snap = p.snapshot()
    assert snap["minutes"][-1][1] == {"entry": 2, "entry_preread": 1, "exit": 1, "other": 2}
    assert len(snap["calls"]) == 6 and snap["calls"]["entry:onchain_watch"] == {"getBalance": 1, "getSlot": 1}


def test_the_lane_class_is_restored_after_the_block():
    """Gamma LOW: a stale lane would count the main loop after real_entry_hook as entry."""
    p = _pool()
    with rpc_label("entry:x"):
        p("POST", A, body=_body("getSlot"))
    p("POST", A, body=_body("getSlot"))
    assert p.snapshot()["minutes"][-1][1] == {"entry": 1, "other": 1}


def test_a_new_key_past_the_cap_collapses_into_other(monkeypatch):
    monkeypatch.setattr(pool_mod, "COUNTER_MAX_KEYS", 2)
    p = _pool()
    for label in ("a", "b", "c", "d", "a"):
        with rpc_label(label):
            p("POST", A, body=_body("getSlot"))
    assert p.snapshot()["calls"] == {"a": {"getSlot": 2}, "b": {"getSlot": 1}, "other": {"other": 2}}


def test_the_heartbeat_projection_carries_the_counter():
    from sniper.services.memecoin_real_runner import _rpc_pool_status
    p = _pool()
    with rpc_label("entry:j7_wallets"):
        p("POST", A, body=_body("getBalance"))
    out = _rpc_pool_status(p.snapshot())
    assert out["calls_by_label"] == {"entry:j7_wallets": {"getBalance": 1}}
    assert len(out["calls_per_minute"]) == 1 and out["calls_per_minute"][0][1] == {"entry": 1}
    assert out["call_count_failures"] == 0


def test_rotatable_methods_are_unchanged():
    assert pool_mod._rotatable_method(_body("getAccountInfo"))
    assert pool_mod._rotatable_method(_body("getAccountInfo", "getSlot"))
    assert not pool_mod._rotatable_method(_body("sendTransaction"))
    assert not pool_mod._rotatable_method(_body("getAccountInfo", "sendTransaction"))
    assert not pool_mod._rotatable_method("[]")
    assert not pool_mod._rotatable_method("not json")
    assert not pool_mod._rotatable_method(json.dumps([1]))


def test_a_broken_counter_never_changes_a_result(monkeypatch, caplog):
    """Alpha (4): reads, a rotation and a write answer exactly as without the counter."""
    def boom(self, *args):
        raise RuntimeError("counter broken")
    results = {}
    for broken in (False, True):
        if broken:
            monkeypatch.setattr(RotatingSolanaRpcHttp, "_count", boom)
        out = []
        p = _pool(Http())
        out.append(p("POST", A, body=_body("getAccountInfo")))
        p2 = _pool(Http(fail={A}))
        out.append(p2("POST", A, body=_body("getBalance")))       # rotates to B
        out.append((p2.snapshot()["active_index"], p2.key_status()[1]["calls"]))
        p3 = _pool(Http(fail={A}))
        try:
            p3("POST", A, body=_body("sendTransaction"))
            out.append("returned")
        except OSError as exc:
            out.append(("raised", getattr(exc, "code", None)))
        results[broken] = out
    assert results[True] == results[False]
    assert sum("call counter failed" in r.getMessage() for r in caplog.records) >= 1
    # Gamma L3: a broken counter is visible, so its empty `calls` reads as unknown, not zero.
    from sniper.services.memecoin_real_runner import _rpc_pool_status
    p4 = _pool()
    p4("POST", A, body=_body("getSlot"))
    assert p4.snapshot()["calls"] == {} and _rpc_pool_status(p4.snapshot())["call_count_failures"] == 1


def test_a_broken_label_never_changes_a_result(monkeypatch):
    monkeypatch.setattr("sniper.execution.solana_rpc_pool.current_label",
                        lambda: (_ for _ in ()).throw(RuntimeError("label broken")))
    p = _pool()
    assert p("POST", A, body=_body("getSlot")) == (200, {"result": 1}, {})
    assert p.snapshot()["calls"] == {"?": {"getSlot": 1}}


# ------------------------------------------------------------------ labels on the runner's paths
def test_the_exit_sweep_labels_its_preview_reads_by_lane():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.open_position(c, source_user_id="u", source_handle="h", mint="0x" + "44" * 20, ticker="T",
                    entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0,
                    their_size_usd=0.0, opened_at="2026-10-01T00:00:00Z", strategy=cfg.MEMECOIN_REAL_POLICY,
                    execution_mode="real", network_id="4663")
    p = _pool()

    def preview(pos):
        p("POST", A, body=_body("getTokenAccountBalance"))
        return {"expected_usdc": 10.0}
    R.real_exit_sweep(c, {"4663": {"exit_fn": lambda *a: {"status": "closed"}, "preview_exit_fn": preview}},
                      price_fn=lambda mints: {}, now_fn=lambda: "2026-10-01T00:00:05Z", lane="fastlane_wake")
    assert "exit:fastlane_wake" in p.snapshot()["calls"]


def test_the_entry_prereads_label_both_reads_with_the_decisions_lane():
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as R
    p = _pool()
    sol = {"usdc_fn": lambda: p("POST", A, body=_body("getTokenAccountBalance")) and 100.0,
           "sol_fn": lambda: p("POST", A, body=_body("getBalance")) and 1.0,
           "mint_read_fn": lambda mint: p("POST", A, body=_body("getAccountInfo"))}
    ev = SimpleNamespace(delivery_lane="j7_wallets", mint="M")
    pre = R._EntryPreReads(sol, "M", object(), read_mint=True, label=R._decision_label("entry_preread", ev))
    for t in pre._threads.values():
        t.join(5)
    assert p.snapshot()["calls"] == {"entry_preread:j7_wallets": {
        "getTokenAccountBalance": 1, "getBalance": 1, "getAccountInfo": 1}}


def test_the_entry_hook_labels_the_decision_threads_reads_with_its_lane(monkeypatch):
    """Gamma L1: without the decorator these reads would count as thread:<class>."""
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as R

    class Stop(BaseException):                 # not an Exception: the hook's own handlers pass it
        pass
    p = _pool()

    def exemptions(*a, **k):                   # the hook's first read; make it a pool read
        p("POST", A, body=_body("getAccountInfo"))
        raise Stop
    monkeypatch.setattr(R.m, "entry_gate_exemptions", exemptions)
    ev = SimpleNamespace(mint="M", delivery_lane="j7_wallets", signal_source="j7", source_user_id="u",
                         network_id="solana")
    try:
        R.real_entry_hook(None, ev, {}, now_fn=lambda: "2026-10-01T00:00:00Z", trade_size=1.0, max_age_s=5)
    except Stop:
        pass
    assert p.snapshot()["calls"] == {"entry:j7_wallets": {"getAccountInfo": 1}}
    assert pool_mod.current_label().startswith("thread:")    # restored after the hook
