import os
import inspect
import sqlite3
import subprocess
import sys

import pytest

from _source_guard import code_of

import scripts.memecoin_runner_loop as runner_loop
from sniper.persistence import db_memecoin as m


def test_observer_main_connection_is_readonly_autocommit(tmp_path):
    path = tmp_path / "main.sqlite"
    writer = sqlite3.connect(path)
    writer.execute("CREATE TABLE facts(value INTEGER)")
    writer.execute("INSERT INTO facts(value) VALUES (1)")
    writer.commit()
    writer.close()

    conn = runner_loop.connect_observer_main(path)
    try:
        assert conn.isolation_level is None
        assert conn.execute("SELECT value FROM facts").fetchone()[0] == 1
        assert conn.in_transaction is False
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO facts(value) VALUES (2)")
    finally:
        conn.close()


def test_finish_db_cycle_releases_writer_snapshot_for_checkpoint(tmp_path):
    path = tmp_path / "main.sqlite"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE facts(value INTEGER)")
    writer.commit()
    writer.execute("INSERT INTO facts(value) VALUES (1)")
    assert writer.in_transaction is True

    assert runner_loop.finish_db_cycle(writer) is True
    assert writer.in_transaction is False

    checkpointer = sqlite3.connect(path, isolation_level=None)
    try:
        busy, log_pages, checkpointed = checkpointer.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        assert (busy, log_pages, checkpointed) == (0, 0, 0)
    finally:
        checkpointer.close()
        writer.close()


def test_finish_db_cycle_rolls_back_failed_worker_iteration(tmp_path):
    path = tmp_path / "main.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE facts(value INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO facts(value) VALUES (1)")

    assert runner_loop.finish_db_cycle(conn, failed=True) is True
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    conn.close()


def test_main_releases_status_read_snapshot_before_sleep():
    source = inspect.getsource(runner_loop.main)
    status_read = source.index("curated = len(m.curated_user_ids(conn))")
    cycle_finish = source.index("finish_db_cycle(conn)", status_read)
    cycle_sleep = source.index("time.sleep(", status_read)

    assert status_read < cycle_finish < cycle_sleep


def test_thread_dump_uses_nonterminating_usr2_handler(monkeypatch):
    calls = []
    monkeypatch.setattr(runner_loop.faulthandler, "register", lambda *a, **k: calls.append((a, k)))

    runner_loop.register_thread_dump(file="runner-log")

    assert calls == [((runner_loop.signal.SIGUSR2,), {
        "file": "runner-log",
        "all_threads": True,
    })]


def test_follow_sync_releases_prior_transaction_before_network(tmp_path):
    conn = sqlite3.connect(tmp_path / "main.sqlite")
    conn.execute("CREATE TABLE facts(value INTEGER)")
    conn.commit()
    conn.execute("INSERT INTO facts(value) VALUES (1)")
    observed = []

    result = runner_loop.follow_sync_without_snapshot(
        conn,
        object(),
        pass_fn=lambda c, _auth: observed.append(c.in_transaction) or {"ok": True},
    )

    assert observed == [False]
    assert result == {"ok": True}
    conn.close()


def test_candidate_failure_cannot_prevent_bracket_cadence_advancing():
    class BrokenEngine:
        def sweep(self, _now_iso):
            raise RuntimeError("paper engine failed")

    logged = []
    advanced = runner_loop.finish_bracket_cycle(
        BrokenEngine(), cycle=123.0, now_fn=lambda: "2026-07-11T12:00:00+00:00",
        log_fn=logged.append,
    )

    assert advanced == 123.0
    assert logged and "paper engine failed" in logged[0]


def test_follow_sync_report_names_repairs_and_failed_wallets():
    logged = []

    runner_loop.report_follow_sync({
        "armed_followed": 2,
        "armed_follow_failed": 1,
        "armed_follow_failures": ["u3"],
    }, log_fn=logged.append, state={})

    assert len(logged) == 2
    assert "followed 2" in logged[0]
    assert "u3" in logged[1]


def test_follow_sync_report_surfaces_skipped_pass():
    logged = []

    runner_loop.report_follow_sync(
        {"skipped": True, "reason": "no_user"}, log_fn=logged.append, state={},
    )

    assert logged == ["[warn] Fomo follow sync skipped: no_user (1 in a row)"]


def test_follow_sync_escalates_persistent_skips_not_transient():
    # a single/few skips = transient WARN; a run of them (~30min) = a real outage -> [err]
    logged, state = [], {}
    for _ in range(5):
        runner_loop.report_follow_sync({"skipped": True, "reason": "empty_or_error"},
                                       log_fn=logged.append, state=state)
    assert all(l.startswith("[warn]") for l in logged)          # 1-5 in a row: transient
    runner_loop.report_follow_sync({"skipped": True, "reason": "empty_or_error"},
                                   log_fn=logged.append, state=state)
    assert logged[-1].startswith("[err]") and "DOWN 6" in logged[-1]   # 6th -> escalate


def test_follow_sync_reports_recovery_after_skips():
    logged, state = [], {"consecutive_skips": 4}
    runner_loop.report_follow_sync({"armed_followed": 0, "armed_already_followed": 60},
                                   log_fn=logged.append, state=state)
    assert any("RECOVERED after 4" in l for l in logged)
    assert state["consecutive_skips"] == 0


def test_follow_sync_heartbeat_makes_healthy_visible():
    # a healthy no-op used to log NOTHING (looked dead). Now a throttled heartbeat proves liveness.
    logged, state = [], {}
    runner_loop.report_follow_sync({"armed_followed": 0, "armed_already_followed": 60},
                                   log_fn=logged.append, state=state)
    assert any("follow sync ok" in l and "60 armed" in l for l in logged)   # first run heartbeats


def test_report_evm_context_gaps_logs_and_alerts_once():
    logged, alerted = [], []
    gaps = runner_loop.report_evm_context_gaps(
        {"solana": {}, "4663": {}}, allowlist={"8453", "4663"},
        log_fn=logged.append, alert_fn=lambda chains: alerted.append(chains),
    )
    assert gaps == ["8453"]
    assert logged == ["[warn] funded EVM config gap: no context for allowlisted chain(s) 8453"]
    assert alerted == [["8453"]]


def test_funded_authority_counts_each_active_chain():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "one")
    m.add_wallet(conn, "u2", "two")
    m.set_wallet_real_approved(
        conn, "u1", True, chains=["solana", "4663"], force=True)
    m.set_wallet_real_approved(conn, "u2", True, chains=["4663"], force=True)

    assert runner_loop.funded_authority_counts(
        conn, {"4663": {}, "solana": {}}) == {"solana": 1, "4663": 2}


def test_operator_digest_worker_stays_off_when_disabled_or_period_invalid():
    created = []

    assert runner_loop.start_operator_digest_worker(
        enabled=False, period_s=3600, worker_fn=lambda: None,
        thread_factory=lambda **kwargs: created.append(kwargs),
    ) is False
    assert runner_loop.start_operator_digest_worker(
        enabled=True, period_s=0, worker_fn=lambda: None,
        thread_factory=lambda **kwargs: created.append(kwargs),
    ) is False
    assert runner_loop.start_operator_digest_worker(
        enabled=True, period_s=float("nan"), worker_fn=lambda: None,
        thread_factory=lambda **kwargs: created.append(kwargs),
    ) is False
    assert runner_loop.start_operator_digest_worker(
        enabled=True, period_s=float("inf"), worker_fn=lambda: None,
        thread_factory=lambda **kwargs: created.append(kwargs),
    ) is False
    assert created == []


def test_operator_digest_worker_starts_daemon_without_running_inline():
    state = {"worker_calls": 0, "started": 0, "thread": None}

    def worker():
        state["worker_calls"] += 1

    class FakeThread:
        def __init__(self, **kwargs):
            state["thread"] = kwargs

        def start(self):
            state["started"] += 1

    assert runner_loop.start_operator_digest_worker(
        enabled=True, period_s=3600, worker_fn=worker, thread_factory=FakeThread,
    ) is True
    assert state["started"] == 1
    assert state["worker_calls"] == 0
    assert state["thread"] == {
        "target": worker,
        "name": "memecoin-operator-digest",
        "daemon": True,
    }


def test_operator_digest_thread_start_failure_is_fail_soft():
    logged = []

    def broken_thread(**_kwargs):
        raise RuntimeError("threads unavailable")

    assert runner_loop.start_operator_digest_worker(
        enabled=True, period_s=3600, worker_fn=lambda: None,
        thread_factory=broken_thread, log_fn=logged.append,
    ) is False
    assert logged and "threads unavailable" in logged[0]


def test_invalid_optional_digest_period_cannot_break_settings_import():
    env = dict(os.environ)
    env["MEMECOIN_TG_DIGEST_PERIOD_S"] = "bad-value"
    result = subprocess.run(
        [sys.executable, "-c", (
            "from sniper.settings.memecoin import MEMECOIN_TG_DIGEST_PERIOD_S as p; "
            "print(p)"
        )],
        cwd=os.getcwd(), env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.0"


def test_operator_digest_repository_default_is_disabled():
    env = dict(os.environ)
    env.pop("MEMECOIN_TG_DIGEST_ENABLED", None)
    result = subprocess.run(
        [sys.executable, "-c", (
            "from sniper.settings.memecoin import MEMECOIN_TG_DIGEST_ENABLED as enabled; "
            "print(int(enabled))"
        )],
        cwd=os.getcwd(), env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_reconciliation_period_uses_elapsed_seconds():
    source = code_of(runner_loop)

    assert "time.monotonic() - last_reconcile > MEMECOIN_RECONCILE_PERIOD_S" in source


# --- a failed Privy refresh must back off (2026-09-02) ----------------------------------------
#
# `refresh_retry_after` was assigned ONLY in the `not extended` branch, which a raise can never
# reach. An HTTP 429 therefore bypassed the backoff and the next cycle retried at poll cadence.
# Measured over the runner log: 3,723 rate-limit rejections since 2026-08-17, still arriving, with
# 44% of consecutive retries faster than the 60s they were supposed to obey. Retrying into a rate
# limit is what sustains it.


def _refresh_try_node():
    """The INNERMOST `try` wrapping the LOOP's auth.refresh_token(), from the parsed source.

    Disambiguation matters twice over. There is a second refresh at startup whose `try` is
    SMALLER, so "innermost overall" picks the wrong one; and the per-cycle `try` also contains
    the call, so "any enclosing try" would pass on the defect. The loop's call is the one guarded
    by `refresh_due`, so that guard is the anchor.
    """
    import ast
    import pathlib as _pl
    tree = ast.parse(code_of(_pl.Path(runner_loop.__file__)))
    guarded = [n for n in ast.walk(tree) if isinstance(n, ast.If)
               and any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "refresh_due"
                       for c in ast.walk(n.test))]
    assert guarded, "no `if refresh_due(...)` guard found; the loop's refresh cannot be located"
    best = None
    for node in ast.walk(guarded[0]):
        if not isinstance(node, ast.Try):
            continue
        if not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == "refresh_token" for c in ast.walk(node)):
            continue
        span = (node.end_lineno or node.lineno) - node.lineno
        if best is None or span < best[0]:
            best = (span, node)
    return best[1] if best else None


def test_a_failed_refresh_arms_the_backoff():
    """A 429 RAISES. If the handler does not arm the backoff, the next cycle retries immediately."""
    import ast
    node = _refresh_try_node()
    assert node is not None, "no try wraps auth.refresh_token(); a raise cannot arm the backoff"
    armed = any(
        isinstance(stmt, ast.Assign)
        and any(getattr(t, "id", None) == "refresh_retry_after" for t in stmt.targets)
        for handler in node.handlers for stmt in ast.walk(handler))
    assert armed, (
        "the exception path does not set refresh_retry_after, so a rate-limited refresh retries "
        "at poll cadence — the defect measured at 3,723 rejections since 2026-08-17")


def test_the_failed_refresh_still_propagates():
    """Swallowing would hide a real auth death: the outer handler writes the heartbeat and pages."""
    import ast
    node = _refresh_try_node()
    assert node is not None
    assert any(isinstance(stmt, ast.Raise) for handler in node.handlers for stmt in ast.walk(handler)), (
        "the handler must re-raise; the outer handler owns the heartbeat and the auth-death page")


def test_the_backoff_uses_the_declared_constant():
    """A hand-written literal here would drift from REFRESH_RETRY_BACKOFF_S silently."""
    import ast
    node = _refresh_try_node()
    names = {n.id for handler in node.handlers for n in ast.walk(handler) if isinstance(n, ast.Name)}
    assert "REFRESH_RETRY_BACKOFF_S" in names, names


@pytest.mark.parametrize("j7_source,due", [(True, True), (True, False),
                                         (False, True), (False, False)])
def test_proactive_privy_refresh_cannot_block_j7_drain(j7_source, due):
    """Execute the real loop guard: an expired retired token must not interrupt J7."""
    import ast

    tree = ast.parse(code_of(runner_loop))
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "refresh_due"
                      for c in ast.walk(n.test))]
    assert len(guards) == 1
    calls = []

    class ExpiredFomoAuth:
        def access_expires_in(self):
            calls.append("expiry")
            return -1

        def refresh_token(self):
            calls.append("refresh")
            raise RuntimeError("expired Privy session")

    def refresh_due(**kwargs):
        calls.append("due")
        return due

    scope = dict(_J7_SOURCE=j7_source, cycle=1000.0, refresh_retry_after=0.0,
                 last_refresh=0.0, REFRESH_EVERY_S=2700, REFRESH_MARGIN_S=900,
                 REFRESH_RETRY_BACKOFF_S=60.0, auth=ExpiredFomoAuth(),
                 refresh_due=refresh_due, drain=lambda: calls.append("drain"))
    body = ast.Module(body=[guards[0], *ast.parse("drain()").body], type_ignores=[])
    code = compile(ast.fix_missing_locations(body), runner_loop.__file__, "exec")
    if not j7_source and due:
        with pytest.raises(RuntimeError, match="expired Privy session"):
            exec(code, scope)
        assert calls == ["expiry", "due", "refresh"]
        assert scope["refresh_retry_after"] == 1060.0
    else:
        exec(code, scope)
        assert calls == (["drain"] if j7_source else ["expiry", "due", "drain"])
        assert scope["refresh_retry_after"] == 0.0


def _run_evm_holdings_block(monkeypatch, *, contexts, last, start_fn,
                            queued=(), unresolved=(), failing_read=None):
    import ast
    import traceback
    from types import SimpleNamespace
    from sniper.services import memecoin_evm_holdings as eh

    tree = ast.parse(code_of(runner_loop))
    blocks = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(isinstance(t, ast.Name) and t.id == "_HOLDINGS_ENABLED"
                      for t in ast.walk(n.test))
              and any(isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute)
                      and isinstance(t.func.value, ast.Name) and t.func.value.id == "_eh"
                      and t.func.attr == "start_pass" for t in ast.walk(n))]
    assert len(blocks) == 1
    monkeypatch.setattr(eh, "supported_chains", lambda: ("56", "4663", "8453"), raising=False)
    monkeypatch.setattr(eh, "start_pass", start_fn)
    reads, logs = [], []
    conn = object()

    def read(kind, connection, chain):
        assert connection is conn
        reads.append((kind, chain))
        if failing_read == (kind, chain):
            raise RuntimeError("chain read failed")
        return chain in (queued if kind == "queued" else unresolved)

    scope = dict(_HOLDINGS_ENABLED=True, contexts=contexts, last_evm_holdings=last,
                 cycle=1000.0, _HOLDINGS_PERIOD_S=300.0, conn=conn,
                 m=SimpleNamespace(holding_sell_queued=lambda c, net: read("queued", c, net),
                     unresolved_holding_sells=lambda c, net: read("unresolved", c, net)),
                 real_utc_now_iso=lambda: "now", _p=logs.append, traceback=traceback)
    body = ast.Module(body=[blocks[0]], type_ignores=[])
    exec(compile(ast.fix_missing_locations(body), runner_loop.__file__, "exec"), scope)
    return scope["last_evm_holdings"], reads, logs


def test_evm_holdings_cadence_starts_with_per_chain_state():
    import ast
    tree = ast.parse(code_of(runner_loop))
    initial = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "last_evm_holdings"
                       for t in n.targets)]
    assert len(initial) == 1
    assert ast.literal_eval(initial[0].value) == {}


def test_evm_holdings_dispatches_only_present_supported_contexts_independently(monkeypatch):
    contexts = {net: {"chain_id": net} for net in ("56", "4663", "999", "solana")}
    contexts["8453"] = None
    calls = {}

    def start(ctx, **kw):
        calls[ctx["chain_id"]] = (ctx, kw)
        return True

    last, reads, logs = _run_evm_holdings_block(
        monkeypatch, contexts=contexts, last={"56": 0.0, "4663": 990.0}, start_fn=start,
        queued={"4663"}, unresolved={"56"})
    assert set(calls) == {"56", "4663"}
    assert calls["56"][0] is contexts["56"] and calls["4663"][0] is contexts["4663"]
    assert {net: {k: kw[k] for k in ("snapshot_due", "sell_due", "resolve_due")}
            for net, (_ctx, kw) in calls.items()} == {
        "56": {"snapshot_due": True, "sell_due": False, "resolve_due": True},
        "4663": {"snapshot_due": False, "sell_due": True, "resolve_due": False}}
    assert reads == [("queued", "56"), ("unresolved", "56"),
                     ("queued", "4663"), ("unresolved", "4663")]
    assert last == {"56": 1000.0, "4663": 990.0} and logs == []


@pytest.mark.parametrize("failing_at", ["queued", "unresolved", "start"])
def test_evm_holdings_failure_on_one_chain_does_not_skip_another(monkeypatch, failing_at):
    contexts = {net: {"chain_id": net} for net in ("56", "4663")}
    calls = []

    def start(ctx, **kw):
        if ctx["chain_id"] == "56" and failing_at == "start":
            raise RuntimeError("worker start failed")
        calls.append(ctx["chain_id"])
        return True

    last, _reads, logs = _run_evm_holdings_block(
        monkeypatch, contexts=contexts, last={}, start_fn=start,
        failing_read=(failing_at, "56") if failing_at != "start" else None)
    assert calls == ["4663"]
    assert last == {"4663": 1000.0}
    assert len(logs) == 1 and "56 other holdings failed" in logs[0]


@pytest.mark.parametrize("started,due", [(False, True), (True, False),
                                       (False, False), (True, True)])
def test_evm_holdings_advances_only_a_started_due_chain(monkeypatch, started, due):
    last = {"56": 0.0 if due else 990.0, "4663": 985.0}
    before = dict(last)
    got, _reads, logs = _run_evm_holdings_block(
        monkeypatch, contexts={"56": {"chain_id": "56"}}, last=last,
        start_fn=lambda ctx, **kw: started)
    assert got == ({**before, "56": 1000.0} if started and due else before)
    assert logs == []


@pytest.mark.parametrize("enabled,threaded,worker_status", [
    (True, False, None), (True, True, None),
    (True, True, {"alive": True, "failures": 2}),
    (False, True, {"alive": True, "failures": 2})])
@pytest.mark.parametrize("contexts", [None, {
    "56": {"chain_id": "56", "address": "not-for-heartbeat"},
    "4663": None, "999": {"chain_id": "999"}, "solana": {"owner": "private"}}])
def test_holdings_heartbeat_names_only_enabled_supported_actual_contexts(
        monkeypatch, enabled, threaded, worker_status, contexts):
    import ast
    from types import SimpleNamespace
    from sniper.services import memecoin_evm_holdings as eh

    definitions = [n for n in ast.walk(ast.parse(code_of(runner_loop)))
                   if isinstance(n, ast.FunctionDef) and n.name == "_other_holdings_status"]
    assert len(definitions) == 1
    monkeypatch.setattr(eh, "supported_chains", lambda: ("56", "4663", "8453"))
    reads = []
    scope = dict(_HOLDINGS_ENABLED=enabled, contexts=contexts,
                 _oh_worker=SimpleNamespace(thread_enabled=lambda: threaded,
                     worker_status=lambda: reads.append("status") or worker_status))
    body = ast.Module(body=definitions, type_ignores=[])
    exec(compile(ast.fix_missing_locations(body), runner_loop.__file__, "exec"), scope)
    status = scope["_other_holdings_status"]()
    expected = dict(worker_status or {}) if enabled and threaded else {}
    expected["evm_ready_chains"] = ["56"] if enabled and contexts else []
    assert status == expected
    assert reads == (["status"] if enabled and threaded else [])
    assert worker_status is None or "evm_ready_chains" not in worker_status


def test_both_runner_heartbeats_preserve_actual_holdings_readiness():
    import ast
    writes = [n for n in ast.walk(ast.parse(code_of(runner_loop)))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "write_heartbeat"]
    assert len(writes) == 2
    for write in writes:
        value = next((kw.value for kw in write.keywords if kw.arg == "other_holdings"), None)
        assert isinstance(value, ast.Call), "success and exception heartbeats need holdings status"
        assert isinstance(value.func, ast.Name) and value.func.id == "_other_holdings_status"


# --- the refresh herd (2026-09-02) --------------------------------------------------------------
#
# `fomo_request` refreshes on EVERY 401 from the fomo API, uncapped. One expired token turns N
# concurrent request lanes into N sequential Privy POSTs, even though the first already fixed it.
# Measured: 122 rate-limit rejections in the peak hour, 1,565 auth failures over a two-hour storm,
# against an intended cadence of one refresh per 45 minutes.


def _auth_with_token(remaining_s):
    from sniper.feeds.fomo_feed import FomoAuth
    a = FomoAuth.__new__(FomoAuth)
    a.access, a.refresh, a.ca_id = "tok", "r", None
    a.store_path, a.extended = None, False
    a.access_expires_in = lambda now_ts=None: remaining_s
    return a


def test_a_token_another_caller_just_minted_is_REUSED_not_reposted():
    """The whole point: the lock winner already fixed it, so the queue behind it must not post."""
    a = _auth_with_token(600.0)
    posted = []
    a._do_refresh = lambda http: posted.append(1)
    got = a.refresh_token(http=None)
    assert got == "tok" and posted == [], "posted again over a token with 10 minutes left"


def test_a_genuinely_DUE_refresh_still_posts():
    """Suppressing the herd must never suppress the refresh that is actually needed."""
    a = _auth_with_token(5.0)
    posted = []
    a._do_refresh = lambda http: posted.append(1) or "new"
    a.refresh_token(http=None)
    assert posted == [1], "a token about to expire was not refreshed"


def test_an_unreadable_expiry_falls_through_to_a_REAL_refresh():
    """Unknown remaining life is not evidence the token is good."""
    from sniper.feeds.fomo_feed import FomoAuth
    a = FomoAuth.__new__(FomoAuth)
    a.access, a.refresh, a.ca_id, a.store_path, a.extended = "tok", "r", None, None, False

    def boom(now_ts=None):
        raise RuntimeError("cannot parse")
    a.access_expires_in = boom
    posted = []
    a._do_refresh = lambda http: posted.append(1) or "new"
    a.refresh_token(http=None)
    assert posted == [1]


def test_no_access_token_at_all_still_posts():
    from sniper.feeds.fomo_feed import FomoAuth
    a = FomoAuth.__new__(FomoAuth)
    a.access, a.refresh, a.ca_id, a.store_path, a.extended = None, "r", None, None, False
    a.access_expires_in = lambda now_ts=None: 600.0
    posted = []
    a._do_refresh = lambda http: posted.append(1) or "new"
    a.refresh_token(http=None)
    assert posted == [1]


def test_a_reused_token_is_not_reported_as_EXTENDED():
    """`.extended` drives the runner's interval re-arm. Reporting a reuse as an extension would
    push the next real refresh out by a full cycle."""
    a = _auth_with_token(600.0)
    a.extended = True
    a._do_refresh = lambda http: None
    a.refresh_token(http=None)
    assert a.extended is False


def test_execution_observer_default_off_never_starts_or_loads_custody(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_execution_observer as obs
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', False, raising=False)
    monkeypatch.setattr(obs, 'start_execution_observer', lambda **_kw: pytest.fail('started while disabled'))
    status = {}
    assert runner_loop.start_execution_measurement({'solana': {'pubkey': 'PUBLIC'}}, status=status) is None
    assert status['state'] == 'disabled'


@pytest.mark.parametrize('contexts', [{}, {'solana': {}}, {'solana': {'pubkey': None}}])
def test_execution_observer_missing_public_context_stays_unknown(monkeypatch, contexts):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_execution_observer as obs
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', True)
    monkeypatch.setattr(obs, 'start_execution_observer', lambda **_kw: pytest.fail('missing public key'))
    status = {}
    assert runner_loop.start_execution_measurement(contexts, status=status) is None
    assert status == {'state': 'unknown', 'reason': 'public_context_unavailable'}


def test_execution_observer_uses_public_context_and_separate_read_endpoint(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_execution_observer as obs
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', True, raising=False)
    monkeypatch.setenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', 'https://research.example/rpc')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', 'https://custody.example/rpc')
    calls = []
    monkeypatch.setattr(obs, 'start_execution_observer', lambda **kw: calls.append(kw))
    source = ReceiptJournal(producer_run_id='S', boot_id='B', clock_domain='C')
    journal = runner_loop.start_execution_measurement({'solana': {'pubkey': 'PUBLIC'}}, journals=(source,), status={})
    assert calls[0]['custody_pubkey'] == 'PUBLIC' and calls[0]['ws_url'] == 'wss://research.example/rpc'
    assert journal is not source and journal.boot_id == 'B' and journal.clock_domain == 'C'


@pytest.mark.parametrize('url', ['', 'https://custody.example/rpc'])
def test_execution_observer_missing_or_custody_read_url_fails_offpath(monkeypatch, url):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_execution_observer as obs
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', True, raising=False)
    monkeypatch.setenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', url)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', 'https://custody.example/rpc')
    monkeypatch.setattr(obs, 'start_execution_observer', lambda **_kw: pytest.fail('custody fallback'))
    status = {}
    journal = runner_loop.start_execution_measurement({'solana': {'pubkey': 'PUBLIC'}}, status=status)
    assert journal.snapshot()['rows'][0]['kind'] == 'gap'
    assert status['state'] == 'unknown' and status['reason']


def test_execution_observer_start_failure_stays_offpath_and_hides_endpoint(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services import memecoin_execution_observer as obs
    monkeypatch.setattr(cfg, 'MEMECOIN_EXECUTION_OBSERVER_ENABLED', True)
    monkeypatch.setenv('MEMECOIN_RESEARCH_HELIUS_RPC_URL', 'https://research.example/secret')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', 'https://custody.example')
    def fail(**_kw): raise ValueError('https://research.example/secret')
    monkeypatch.setattr(obs, 'start_execution_observer', fail)
    status = {}
    journal = runner_loop.start_execution_measurement({'solana': {'pubkey': 'PUBLIC'}}, status=status)
    assert status['reason'] == 'observer_start_failed'
    assert journal.snapshot()['rows'][0]['reason'] == 'observer_start_failed'
