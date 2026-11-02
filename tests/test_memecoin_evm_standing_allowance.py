"""BSC entries skip the per-entry WBNB approve through a standing allowance (2026-10-07).

Operator YES via Alpha; spec docs/superpowers/specs/2026-10-07-bsc-standing-allowance-design.md.
A 56 entry sent approve(spender, sell_atomic) every time: evm.approval p50 186 / p90 505 ms since
10-01 (HUSH 589 ms). Now each pinned router (Pancake V2 Router, Smart Router, V3 SwapRouter, Kyber;
the 56 fills since 09-25 pulled through only these) keeps a standing WBNB allowance worth $500,
counted in a cache that moves only when we approve or spend. An entry the cache covers skips the
approve; any other entry runs today's exact approve. Every rule errs low: a short count only falls
back to today. Top-ups run on their own thread, never while an executor holds the wallet lock.
"""
import json
import sqlite3
import threading

import pytest
from eth_account import Account

import sniper.execution.evm_real_seams as S
from sniper.execution import evm_standing_allowance as sa
from sniper.execution.evm_wallet import CHAIN_QUOTE

CHAIN = "56"
WBNB = CHAIN_QUOTE[CHAIN]["token"].lower()
V2_ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"
KEY = ("56", "0x" + "aa" * 20, WBNB, V2_ROUTER)
E18 = 10 ** 18


@pytest.fixture(autouse=True)
def _clean():
    sa.reset_state()
    yield
    sa.reset_state()


# --- the book: every rule errs low ----------------------------------------------------------

def test_the_pinned_spenders_are_the_four_routers_56_fills_pulled_through():
    assert sa.PINNED_SPENDERS[CHAIN] == frozenset({
        V2_ROUTER, "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",
        "0x1b81d678ffb9c0263b24a97847620c99d213eb14", "0x6131b5fae19ea4f9d964eac0408e4408b66337b5"})
    assert "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0" not in sa.PINNED_SPENDERS[CHAIN]  # flap: native


def test_an_unknown_or_short_count_falls_back():
    assert sa.reserve(KEY, E18 // 10, token_usd=600.0) is None, "unknown at start"
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=E18 // 10)
    assert sa.reserve(KEY, E18 // 10 + 1, token_usd=600.0) is None
    assert sa.value(KEY) == E18 // 10, "a fallback never spends the count"


def test_two_parallel_entries_cannot_both_spend_the_last_clip():
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=E18)
    got, gate = [], threading.Barrier(8)

    def entry():
        gate.wait()
        got.append(sa.reserve(KEY, E18, token_usd=600.0))
    threads = [threading.Thread(target=entry) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is not None for r in got) == 1 and sa.value(KEY) == 0


def test_a_confirmed_topup_subtracts_every_reservation_open_at_or_after_its_start():
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=5 * E18)
    early = sa.reserve(KEY, E18, token_usd=600.0)        # open when the next top-up starts
    sa.topup_begin(KEY)
    sa.reserve(KEY, 2 * E18, token_usd=600.0)            # after it started: a later nonce
    sa.topup_end(KEY, "confirmed", cap_atomic=10 * E18)
    assert sa.value(KEY) == 10 * E18 - 3 * E18
    sa.settle(KEY, early, True)
    assert sa.value(KEY) == 7 * E18, "a fill never adds back"


def test_an_exact_approve_while_a_topup_is_in_flight_leaves_zero():
    sa.topup_begin(KEY)
    sa.exact_approved(KEY, E18, token_usd=600.0)
    assert sa.value(KEY) == 0
    sa.topup_end(KEY, "confirmed", cap_atomic=10 * E18)
    assert sa.value(KEY) == 0 and sa.snapshot()["topups_tainted"] == 1


@pytest.mark.parametrize("outcome", ["failed", "rejected", "ambiguous", "not_sent", "dropped"])
def test_a_topup_that_did_not_confirm_never_raises_the_count(outcome):
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=3 * E18)
    sa.reserve(KEY, E18, token_usd=600.0)
    sa.topup_begin(KEY)
    sa.topup_end(KEY, outcome, cap_atomic=50 * E18)
    assert sa.value(KEY) == 2 * E18 and sa.snapshot()["topups_failed"] == 1


def test_a_standing_entry_that_did_not_fill_makes_the_count_unknown():
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=3 * E18)
    rid = sa.reserve(KEY, E18, token_usd=600.0)
    sa.settle(KEY, rid, False)
    assert sa.value(KEY) is None
    assert sa.reserve(KEY, E18, token_usd=600.0) is None


def test_a_topup_is_due_under_two_clips_with_a_price_and_never_twice():
    assert not sa.topup_due(KEY), "no clip or price seen yet"
    sa.reserve(KEY, E18, token_usd=600.0)                # unknown: falls back, learns clip + price
    assert sa.topup_due(KEY)
    sa.topup_begin(KEY)
    assert not sa.topup_due(KEY), "one in flight"
    sa.topup_end(KEY, "confirmed", cap_atomic=3 * E18)
    assert not sa.topup_due(KEY)
    sa.reserve(KEY, 2 * E18, token_usd=600.0)            # 1 E18 left, under 2 clips of 2 E18
    assert sa.topup_due(KEY)


def test_the_cap_is_the_floor_of_cap_usd_in_token_atoms():
    assert sa.cap_atomic(500, 600.0, 18) == 833333333333333333
    assert sa.cap_atomic(500, 0.0, 18) is None and sa.cap_atomic(500, None, 18) is None


def test_the_snapshot_carries_the_count_per_spender_and_the_topup_counts():
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=3 * E18)
    snap = sa.snapshot()
    assert snap["spenders"]["56:" + V2_ROUTER]["v_atomic"] == str(3 * E18)
    assert snap["topups_confirmed"] == 1 and snap["topups_tainted"] == 0 and snap["topups_failed"] == 0


def test_the_heartbeat_carries_the_snapshot(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / "hb"
    write_heartbeat({}, True, path=str(path), standing_allowance={"topups_confirmed": 2})
    assert json.loads(path.read_text())["standing_allowance"] == {"topups_confirmed": 2}


# --- the executor seams -------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 600.0)
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS", CHAIN)
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_default_lock_dir", lambda: str(tmp_path))
    from sniper.persistence import db_memecoin as m
    db_path = tmp_path / "standing.sqlite"
    with sqlite3.connect(db_path) as conn:
        m.ensure_memecoin_tables(conn)
    monkeypatch.setenv("DB_PATH", str(db_path))
    return monkeypatch


def _http(*, send=None):
    seen = {"sends": [], "methods": []}

    def http(method, url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        seen["methods"].append(meth)
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x5"}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_sendRawTransaction":
            seen["sends"].append(req["params"][0])
            if send == "exception":
                raise RuntimeError("send connection lost")
            if send == "ambiguous":
                return 200, {"error": {"message": "gateway timeout, please retry"}}, None
            if send == "rejected":
                return 200, {"error": {"code": -32000, "message": "insufficient funds"}}, None
            return 200, {"result": "0x" + "cd" * 32}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1", "gasUsed": "0xb4a0",
                                    "effectiveGasPrice": "0x3b9aca00", "logs": []}}, None
        return 200, {"result": "0x0"}, None
    return http, seen


def _ex(http):
    acct = Account.create()
    return S.build_real_evm_executor(acct=acct, chain_id=CHAIN, rpc_url="https://rpc", http=http), acct


def _key(acct, spender=V2_ROUTER):
    return sa.book_key(CHAIN, acct.address, WBNB, spender)


def test_a_covered_entry_skips_the_approve(env):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    used = []
    got = ex["standing_approve_fn"](V2_ROUTER, E18 // 10, 600.0, used)
    assert got == {"standing": True} and seen["sends"] == [] and len(used) == 1
    assert sa.value(k) == E18 - E18 // 10


@pytest.mark.parametrize("spender", ["0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0", "0x" + "77" * 20])
def test_an_unpinned_spender_keeps_todays_exact_approve(env, spender):
    http, seen = _http()
    ex, acct = _ex(http)
    got = ex["standing_approve_fn"](spender, E18 // 10, 600.0, [])
    assert isinstance(got, dict) and got.get("pipelined") and len(seen["sends"]) == 1


def test_an_uncovered_pinned_entry_runs_the_exact_approve_and_zeroes_the_count(env):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18 // 20)
    got = ex["standing_approve_fn"](V2_ROUTER, E18 // 10, 600.0, [])
    assert got.get("pipelined") and len(seen["sends"]) == 1 and sa.value(k) == 0


def test_knob_off_never_reads_or_writes_the_book(env):
    env.delenv("MEMECOIN_EVM_STANDING_ALLOWANCE_CHAINS")
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    got = ex["standing_approve_fn"](V2_ROUTER, E18 // 10, 600.0, [])
    assert got.get("pipelined") and len(seen["sends"]) == 1 and sa.value(k) == E18


def test_settle_marks_an_unfilled_standing_entry_unknown(env):
    http, _ = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.topup_begin(k)
    sa.topup_end(k, "confirmed", cap_atomic=E18)
    used = []
    ex["standing_approve_fn"](V2_ROUTER, E18 // 10, 600.0, used)
    ex["standing_settle_fn"](used, {"status": "aborted"})
    assert sa.value(k) is None


def test_a_topup_sends_cap_and_sets_the_count(env):
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)            # learns clip and price; falls back
    assert ex["standing_topup_fn"](V2_ROUTER) == "confirmed"
    assert len(seen["sends"]) == 1 and sa.value(k) == sa.cap_atomic(500, 600.0, 18)


def test_a_topup_never_signs_while_an_executor_holds_the_wallet_lock(env):
    """Alpha (1): the ordering check. An entry holds evm-56-<wallet> across quote, send and confirm
    (serialize_executor); a top-up asking then takes no nonce and signs nothing."""
    from sniper.execution.exec_lock import evm_exec_lock_key, wallet_exec_lock
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    with wallet_exec_lock(evm_exec_lock_key(CHAIN, acct.address)):
        assert ex["standing_topup_fn"](V2_ROUTER) == "wallet_busy"
        assert seen["sends"] == [] and sa.value(k) is None and sa.topup_due(k)
    assert ex["standing_topup_fn"](V2_ROUTER) == "confirmed" and len(seen["sends"]) == 1


def test_an_ambiguous_topup_send_never_raises_the_count(env):
    http, seen = _http(send="ambiguous")
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    status = ex["standing_topup_fn"](V2_ROUTER)
    assert status != "confirmed" and sa.value(k) is None


def test_a_rejected_topup_never_raises_the_count(env):
    http, seen = _http(send="rejected")
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    assert ex["standing_topup_fn"](V2_ROUTER) == "rejected" and sa.value(k) is None


@pytest.mark.parametrize("state", ["submitted", "approval_submitted"])
def test_unknown_holding_transaction_blocks_standing_approval_under_lock(env, state):
    from sniper.execution.exec_lock import evm_exec_lock_key, wallet_exec_lock
    from sniper.persistence import db_core
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    with sqlite3.connect(db_core.get_db_path()) as conn:
        conn.execute("INSERT INTO memecoin_holding_sell_intents "
                     "(intent_id, network_id, mint, requested_at, expires_at, state, sig) "
                     "VALUES('holding', '56', '0xtoken', 'now', 'later', ?, '0xunknown')", (state,))
    original = S.m.unresolved_holding_sell_count
    checks, connections = [], []

    def guarded_count(conn, chain):
        with wallet_exec_lock(evm_exec_lock_key(CHAIN, acct.address), blocking=False) as held:
            checks.append(not held)
        connections.append(conn)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        return original(conn, chain)

    env.setattr(S.m, "unresolved_holding_sell_count", guarded_count)
    seen["methods"].clear()
    assert ex["standing_topup_fn"](V2_ROUTER) == "holding_unresolved"
    assert seen["methods"] == [] and seen["sends"] == []
    assert sa.value(k) is None and sa.topup_due(k)
    assert checks == [True]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("failure", ["open", "query"])
def test_unreadable_holding_ledger_blocks_standing_approval(env, failure):
    from sniper.persistence import db_core
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    opened = []
    original = db_core.get_readonly_connection

    def open_conn():
        if failure == "open":
            raise sqlite3.OperationalError("holding ledger unavailable")
        conn = original()
        opened.append(conn)
        return conn

    def unreadable(*_):
        raise sqlite3.OperationalError("holding ledger unavailable")

    env.setattr(db_core, "get_readonly_connection", open_conn)
    if failure == "query":
        env.setattr(S.m, "unresolved_holding_sell_count", unreadable)
    seen["methods"].clear()
    with pytest.raises(sqlite3.OperationalError, match="holding ledger unavailable"):
        ex["standing_topup_fn"](V2_ROUTER)
    assert seen["methods"] == [] and seen["sends"] == []
    assert sa.value(k) is None and sa.topup_due(k)
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("intent_knob", [False, True])
@pytest.mark.parametrize("failure", ["unknown", "ambiguous", "send_exception", "confirm_exception"])
def test_standing_signed_intent_survives_unknown_result_and_restart(env, intent_knob, failure):
    from pathlib import Path
    from sniper.execution import exec_lock
    from sniper.settings import memecoin as cfg
    env.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", intent_knob)
    http, seen = _http(send={"ambiguous": "ambiguous", "send_exception": "exception"}.get(failure))
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)

    def receipt(*_a, **_k):
        if failure == "confirm_exception":
            raise RuntimeError("receipt unavailable")
        return "unknown"

    env.setattr(S, "eth_confirm", receipt)
    if failure == "confirm_exception":
        with pytest.raises(RuntimeError, match="receipt unavailable"):
            ex["standing_topup_fn"](V2_ROUTER)
    else:
        assert ex["standing_topup_fn"](V2_ROUTER) != "confirmed"
    intent_dir = exec_lock.standing_intent_lock_dir()
    records = list((Path(intent_dir) / "exec_intents" / CHAIN).glob("*"))
    assert len(records) == 1
    raw = json.loads(records[0].read_text())["raw"]
    assert raw == seen["sends"][0]
    assert Account.recover_transaction(raw).lower() == acct.address.lower()
    assert exec_lock.intents_outstanding(CHAIN, lock_dir=intent_dir, required=True) is True
    assert exec_lock.intents_outstanding(CHAIN) is False, "ordinary fast lanes use another namespace"
    sa.reset_state()
    rebuilt = S.build_real_evm_executor(acct=acct, chain_id=CHAIN, rpc_url="https://rpc", http=http)
    other_spender = "0x13f4ea83d0bd40e75c8222255bc855a974568dd4"
    other_key = _key(acct, other_spender)
    sa.reserve(other_key, E18 // 10, token_usd=600.0)
    sends_before = len(seen["sends"])
    assert rebuilt["standing_topup_fn"](other_spender) == "standing_unresolved"
    assert len(seen["sends"]) == sends_before and sa.topup_due(other_key)


@pytest.mark.parametrize("intent_knob", [False, True])
def test_explicitly_rejected_standing_topup_clears_journal_without_receipt_or_retry(env, intent_knob):
    from pathlib import Path
    from sniper.execution import exec_lock
    from sniper.settings import memecoin as cfg
    env.setattr(cfg, "MEMECOIN_EXEC_INTENT_STAMP", intent_knob)
    http, seen = _http(send="rejected")
    recorded = []
    intent_dir = exec_lock.standing_intent_lock_dir()

    def checked_http(*a, **kw):
        request = json.loads(kw.get("body") or a[3])
        if request["method"] == "eth_sendRawTransaction":
            records = list((Path(intent_dir) / "exec_intents" / CHAIN).glob("*"))
            assert len(records) == 1
            raw = json.loads(records[0].read_text())["raw"]
            assert raw == request["params"][0]
            recorded.append(raw)
        return http(*a, **kw)

    def no_receipt(*_a, **_kw):
        raise AssertionError("an explicit send rejection needs no confirmation")

    env.setattr(S, "eth_confirm", no_receipt)
    ex, acct = _ex(checked_http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    seen["methods"].clear()
    assert ex["standing_topup_fn"](V2_ROUTER) == "rejected"
    assert len(recorded) == len(seen["sends"]) == 1
    assert Account.recover_transaction(recorded[0]).lower() == acct.address.lower()
    assert set(seen["methods"]) <= {"eth_getTransactionCount", "eth_gasPrice", "eth_sendRawTransaction"}
    assert exec_lock.intents_outstanding(CHAIN, lock_dir=intent_dir, required=True) is False
    assert sa.value(k) is None and sa.topup_due(k)


@pytest.mark.parametrize("receipt_state", ["confirmed", "failed"])
def test_standing_intent_is_written_before_send_and_cleared_only_by_terminal_receipt(
        env, receipt_state):
    from pathlib import Path
    from sniper.execution import exec_lock
    http, seen = _http()
    observed = []

    def read_intents():
        return list((Path(exec_lock.standing_intent_lock_dir()) / "exec_intents" / CHAIN).glob("*"))

    def checked_http(*a, **kw):
        request = json.loads(kw.get("body") or a[3])
        if request["method"] == "eth_sendRawTransaction":
            records = read_intents()
            assert len(records) == 1
            assert json.loads(records[0].read_text())["raw"] == request["params"][0]
            observed.append("record_before_send")
        return http(*a, **kw)

    def terminal(*_a, **_kw):
        assert len(read_intents()) == 1, "the network-post completion must not clear the fence"
        return receipt_state

    env.setattr(S, "eth_confirm", terminal)
    ex, acct = _ex(checked_http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    assert ex["standing_topup_fn"](V2_ROUTER) == receipt_state
    assert observed == ["record_before_send"] and len(seen["sends"]) == 1
    assert read_intents() == []
    assert sa.value(k) == (sa.cap_atomic(500, 600.0, 18) if receipt_state == "confirmed" else None)


def test_standing_intent_record_failure_prevents_broadcast(env):
    from sniper.execution import exec_lock
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)

    original = exec_lock.intent_send

    def unwritable(*a, **kw):
        if kw.get("required"):
            raise exec_lock.IntentUnwritable("disk unavailable")
        return original(*a, **kw)

    env.setattr(exec_lock, "intent_send", unwritable)
    assert ex["standing_topup_fn"](V2_ROUTER) == "not_sent"
    assert seen["sends"] == [] and sa.value(k) is None and sa.topup_due(k)


def test_unreadable_standing_intent_namespace_blocks_another_approval(env):
    from sniper.execution import exec_lock
    http, seen = _http()
    ex, acct = _ex(http)
    k = _key(acct)
    sa.reserve(k, E18 // 10, token_usd=600.0)
    env.setattr(exec_lock, "intents_outstanding", lambda *_a, **_kw: None)
    seen["methods"].clear()
    assert ex["standing_topup_fn"](V2_ROUTER) == "standing_unresolved"
    assert seen["methods"] == [] and seen["sends"] == [] and sa.topup_due(k)


# --- the executor: a standing approval is traced and costs no requote -----------------------------

def test_the_executor_traces_a_standing_approval_and_signs_without_a_requote():
    from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _reserve, _seams
    from sniper.execution.evm_swap_executor import execute_entry_evm
    c = _c()
    pid = _reserve(c, network_id="56")
    seams_, calls = _seams(quote=dict(_QUOTE), approve={"standing": True})
    quotes = []
    original = seams_["quote_fn"]

    def quote_fn(*a, **k):
        quotes.append(1)
        return original(*a, **k)
    seams_["quote_fn"] = quote_fn
    trace = _TraceRecorder()
    result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="56", usd_amount=10.0,
                               strategy="alpha_b", source_user_id="trader1",
                               now_iso="2026-09-30T00:00:01Z", trace=trace, **seams_)
    assert result["status"] == "filled" and calls["send"] == 1
    assert [md["outcome"] for n, md in trace.substages if n == "evm.approval"] == ["standing"]
    assert not [n for n, _ in trace.substages if n == "evm.quote_post_approval"]
    assert len(quotes) == 1


# --- the revoke script: dry-run by default --------------------------------------------------------

def test_the_revoke_script_is_dry_run_without_confirm():
    import ast
    from pathlib import Path
    src = Path(__file__).parent.parent / "scripts" / "memecoin_evm_revoke_standing_allowance.py"
    tree = ast.parse(src.read_text())
    text = src.read_text()
    assert '"--confirm"' in text and "if not args.confirm" in text
    assert "PINNED_SPENDERS" in text and "encode_approve" in text
    assert any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body)


def test_a_second_topup_never_replaces_one_in_flight():
    """Gamma LOW: two top-up threads (a context rebuild) must not drop the first one's post."""
    sa.topup_begin(KEY)
    sa.topup_end(KEY, "confirmed", cap_atomic=5 * E18)
    assert sa.topup_begin(KEY) is True
    sa.reserve(KEY, E18, token_usd=600.0)
    assert sa.topup_begin(KEY) is False, "the in-flight top-up keeps its post"
    sa.topup_end(KEY, "confirmed", cap_atomic=10 * E18)
    assert sa.value(KEY) == 9 * E18


def test_startup_bootstrap_seeds_all_pinned_spenders_without_a_trade(env):
    http, seen = _http()
    ex, acct = _ex(http)
    assert ex['standing_bootstrap_fn']() == 'seeded'
    assert seen['sends'] == []
    for spender in sa.PINNED_SPENDERS[CHAIN]:
        key = _key(acct, spender)
        assert sa.value(key) is None
        assert sa.topup_due(key)
        assert ex['standing_topup_fn'](spender) == 'confirmed'
        assert sa.value(key) == sa.cap_atomic(500, 600, 18)
    assert len(seen['sends']) == 4


@pytest.mark.parametrize('price', [None, 0, -1, float('nan'), float('inf'), True])
def test_startup_bootstrap_invalid_price_never_enables_topup(env, monkeypatch, price):
    monkeypatch.setattr(S, 'quote_usd_price', lambda *a, **kw: price)
    http, seen = _http()
    ex, acct = _ex(http)
    assert ex['standing_bootstrap_fn']() == 'no_price'
    assert not sa.topup_due(_key(acct))
    assert seen['sends'] == []
