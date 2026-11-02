"""Item 10 — EVM real-entry leg wrapper: arm-gate -> S1-EVM reserve -> token-safety -> executor, with
Tier-0 per-stage latency stamped on every EVM order (mirrors the Solana real_entry). This is also the
evm_entry_fn the network router dispatches to. Injected seams; DISARMED unless MEMECOIN_REAL_ENABLED."""
import collections
import json
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg


class _Ev:
    def __init__(self, mint="0xtok"):
        self.mint = mint; self.source_user_id = "u1"; self.network_id = "8453"


@pytest.mark.parametrize("early,approved,expected,queries,funded_calls", [
    (False, 40.0, False, [100.0, 40.0], [40.0]),
    (True, 40.0, False, [40.0], [40.0]),
    (False, 50.0, True, [100.0, 50.0], []),
    (False, 100.0, True, [100.0], []),
])
def test_parallel_safety_answers_the_reserved_amount_before_mutation(
        monkeypatch, tmp_path, early, approved, expected, queries, funded_calls):
    result, seen = _reservation_safety_run(monkeypatch, tmp_path, early=early, approved=approved)
    assert seen["safe"] is expected
    assert seen["executed_usd"] == approved
    assert seen["queries"] == queries
    assert seen["funded_calls"] == funded_calls
    assert seen["mutations"] == int(expected)
    if early:
        assert seen["early_lookups"] == [100.0, 40.0]
    assert result["status"] == ("filled" if expected else "unsafe")


@pytest.mark.parametrize("expires", ["reservation", "recheck"])
def test_reserved_amount_recheck_keeps_the_original_deadline(monkeypatch, tmp_path, expires):
    result, seen = _reservation_safety_run(monkeypatch, tmp_path, expires=expires)
    assert seen["safe"] is False and seen["mutations"] == 0
    assert seen["reason"] == "safety_deadline_exceeded"
    assert result["status"] == "unsafe"
    if expires == "reservation":
        assert seen["queries"] == [100.0] and seen["funded_calls"] == []
    else:
        assert seen["queries"] == [100.0, 40.0] and seen["funded_calls"] == [40.0]
        assert len(set(seen["deadlines"])) == 1


@pytest.mark.parametrize("approved,expected_queries", [(40.0, [100.0, 40.0]), (100.0, [100.0])])
def test_delayed_worker_keeps_its_amount_question(monkeypatch, tmp_path, approved, expected_queries):
    _, seen = _reservation_safety_run(monkeypatch, tmp_path, approved=approved, delayed=True)
    # The approved clip's check now starts at the reservation, before the delayed preliminary
    # worker runs (Gamma MEDIUM on 1b46ef794), so the ORDER moved; each worker still asks its own.
    assert sorted(seen["queries"]) == sorted(expected_queries)
    assert seen["safe"] is (approved == 100.0)


def test_completed_safety_can_be_joined_after_its_deadline(monkeypatch, tmp_path):
    result, seen = _reservation_safety_run(
        monkeypatch, tmp_path, approved=100.0, expires="after_completion")
    assert result["status"] == "filled" and seen["mutations"] == 1
    assert seen["queries"] == [100.0] and seen["funded_calls"] == []


def test_final_amount_exception_cannot_reuse_the_preliminary_pass(monkeypatch, tmp_path):
    result, seen = _reservation_safety_run(monkeypatch, tmp_path, raises=True)
    assert result["status"] == "unsafe" and seen["mutations"] == 0
    assert seen["queries"] == [100.0, 40.0] and seen["funded_calls"] == [40.0]
    assert "safety_exception" in seen["reason"]


def _reservation_safety_run(monkeypatch, tmp_path, *, early=False, approved=40.0,
                            expires=None, delayed=False, raises=False):
    from types import SimpleNamespace
    from sniper.services.memecoin_rh_fast_lane import cache_first_safety
    from sniper.services.memecoin_risk import real_trade_size, scaled_funded_clip
    conn, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    clock = [1000.0]
    if expires is not None:
        monkeypatch.setattr(leg.time, "monotonic", lambda: clock[0])
    initial_done = threading.Event()
    real_thread, workers, delayed_start = threading.Thread, [], []
    def thread(**kwargs):
        worker = real_thread(**kwargs)
        workers.append(worker)
        if delayed and len(workers) == 1:
            delayed_start.append(worker.start)
            worker.start = lambda: None
        return worker
    monkeypatch.setattr(leg.threading, "Thread", thread)
    seen = dict(queries=[], funded_calls=[], early_lookups=[], deadlines=[], mutations=0)
    actual = scaled_funded_clip(base_clip_usd=real_trade_size(
        usdc_balance=80.0, open_notional_usd=0.0, max_concurrent=2,
        max_clip_usd=175.0, mode="bankroll"), risk_scale=1.0, floor_usd=30.0)
    assert actual == 40.0

    def funded(mint, *, usd=None, deadline=None, timings=None):
        seen["funded_calls"].append(usd)
        if raises:
            raise RuntimeError("final amount read failed")
        if expires == "recheck":
            clock[0] = 1040.0
            return True, "late_pass"
        return False, "exact_clip_refused"

    cache = SimpleNamespace(usd=175.0, ttl_s=60.0,
                            verdict=lambda mint: (True, "ok", 1000.0, 0.0))
    cached = cache_first_safety(funded, cache, now_fn=lambda: 1010.0)

    def safety(mint, *, usd=None, deadline=None, timings=None):
        seen["queries"].append(usd)
        seen["deadlines"].append(deadline)
        result = cached(mint, usd=usd, deadline=deadline, timings=timings)
        initial_done.set()
        return result

    class Early:
        def safety_for(self, mint, usd):
            seen["early_lookups"].append(usd)
            if usd != 100.0:
                return None
            def wait():
                initial_done.set()
                return True, "early_ok"
            return SimpleNamespace(wait=wait, duration_ms=1.0, timings={})

    def reserve(*args, **kwargs):
        if not delayed:
            assert initial_done.wait(2), "safety must finish while reservation is in progress"
            workers[0].join(2)
            assert not workers[0].is_alive()
        if expires == "reservation":
            clock[0] = 1040.0
        return SimpleNamespace(allowed=True, approved_size_usdc=approved), 1
    monkeypatch.setattr(leg, "reserve_entry_evm", reserve)

    def execute(conn, pid, mint, usd, now_iso, safety_gate_fn=None, early_quote=None):
        if delayed:
            delayed_start[0]()
        if expires == "after_completion":
            clock[0] = 1040.0
        seen["executed_usd"] = usd
        safe, reason = safety_gate_fn()
        seen.update(safe=safe, reason=reason)
        if not safe:
            return dict(status="unsafe_gate", reason=reason)
        seen["mutations"] += 1
        return dict(status="filled")

    ev = _Ev()
    ev.network_id = "4663"
    result = leg.evm_real_entry(
        conn, ev, route="evm:4663", day="2026-10-15", usdc_balance=80.0,
        native_balance=1.0, now_iso="2026-10-15T00:00:00Z", trade_size=100.0,
        token_safety_fn=safety, execute_fn=execute, kill_path=kill,
        early_quote=Early() if early else None)
    conn.close()
    return result, seen


def _c(monkeypatch, tmp_path, armed=True):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", armed)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", armed)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, str(tmp_path / "kill")


def _chain_identity_http(chain):
    def http(_method, _url, **_kwargs):
        return 200, {"result": hex(int(chain))}, None
    return http


def test_default_funded_evm_read_http_caps_a_route_timeout(monkeypatch):
    """A slow quote endpoint must not consume the full retry window."""
    seen = {}

    class Client:
        def __init__(self, *, read_timeout_s):
            seen["read_timeout_s"] = read_timeout_s

        def request(self, _method, _url, **kwargs):
            seen["request_timeout"] = kwargs["timeout"]
            return 200, {}, None

        def request_with_headers(self, _method, _url, **kwargs):
            seen["headers_timeout"] = kwargs["timeout"]
            return 200, {}, {"ratelimit-remaining": "11999"}

    import sniper.execution.fast_http as fast_http
    import sniper.execution.submission_backends as submission_backends
    monkeypatch.setattr(fast_http, "PooledHttpClient", Client)
    monkeypatch.setattr(submission_backends, "register_client", lambda client: client)

    http = leg._funded_evm_read_http(None)
    http("POST", "https://route", body="{}", timeout=12.0)
    _status, _body, headers = http.response_headers(
        "GET", "https://quote", timeout=12.0)

    assert seen == {
        "read_timeout_s": 3.0, "request_timeout": 3.0, "headers_timeout": 3.0,
    }
    assert headers["ratelimit-remaining"] == "11999"


def test_funded_evm_read_http_stops_after_the_safety_deadline():
    calls = []

    def base(*args, **kwargs):
        calls.append((args, kwargs))
        return 200, {}, None

    from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded

    http = leg._funded_evm_read_http(base)
    with http.safety_deadline(time.monotonic() - 0.01):
        with pytest.raises(EvmSafetyDeadlineExceeded):
            http("POST", "https://route", body="{}")
    assert calls == []


def test_funded_safety_read_rotates_without_an_unbounded_hedge(monkeypatch):
    """A 429 must reach the verified backup within the one safety deadline."""
    from sniper.execution import evm_rpc_pool, evm_wallet

    primary = "https://primary.example"
    backup = "https://backup.example"
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": (primary, backup)})
    evm_rpc_pool.reset_for_tests()
    for url in (primary, backup):
        evm_rpc_pool.mark_identity(url, True)
    seen = []

    def base(_method, url, *, timeout=None, **_kwargs):
        seen.append((url, timeout))
        if url == primary:
            return 429, {"error": "rate limited"}, None
        return 200, {"result": "0x6000"}, None

    http = leg._funded_evm_read_http(base)
    with http.safety_deadline(time.monotonic() + 0.4):
        assert evm_wallet._rpc(primary, "eth_getCode", ["0xtoken", "latest"], http=http) == {
            "result": "0x6000",
        }

    assert [url for url, _timeout in seen] == [primary, backup]
    assert all(timeout <= 0.4 for _url, timeout in seen)


def _run(c, kill, ev, *, safe=(True, "ok"), native=1.0, usdc=300.0, exec_result=None):
    calls = {"exec": 0}

    def execute_fn(conn, pid, mint, size, now_iso):
        calls["exec"] += 1
        return exec_result or {"status": "filled", "tx_hash": "0xh", "position_id": pid}
    r = leg.evm_real_entry(c, ev, route="evm:8453", day="2026-07-09", usdc_balance=usdc,
                           native_balance=native, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=lambda mint, **kw: safe, execute_fn=execute_fn, kill_path=kill)
    return r, calls


# --- EVM entry must not read wallet tax policy after the round-trip tax probe is removed. ---

def test_evm_entry_does_not_read_wallet_tax_policy(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    calls = {"stats": 0, "safety": 0}

    def copy_stats(_conn, _uid, network_id=None, *, chain=None):
        calls["stats"] += 1
        return {"n_closes": 99, "pnl": 999.0}

    monkeypatch.setattr(m, "alpha_b_copy_stats", copy_stats)

    def safety(mint):
        calls["safety"] += 1
        return (True, "ok")

    def execute_fn(*_a, **k):
        # `**k` makes the leg's kwarg introspection say this executor supports `safety_gate_fn`,
        # which puts token-safety on a worker THREAD. A stub that then returns "filled" without
        # consuming the gate races the thread for `calls["safety"]`: under CPU contention the
        # assertion read 0 on 12 of 12 runs, on releases with and without any related change
        # (2026-09-03, refused a funded deploy). The real executor joins the verdict via
        # safety_gate_fn before its first wallet mutation; do the same, which is also the only
        # way this test asserts the contract it names.
        if k.get("safety_gate_fn") is not None:
            k["safety_gate_fn"]()
        return {"status": "filled", "position_id": 1}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=safety, execute_fn=execute_fn, kill_path=kill,
    )

    assert result["status"] == "filled"
    assert calls == {"stats": 0, "safety": 1}


def test_disarmed_no_reserve(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path, armed=False)
    r, calls = _run(c, kill, _Ev())
    assert r["status"] == "disarmed" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_happy_path_reaches_executor_records_latency(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev())
    assert r["status"] == "filled" and calls["exec"] == 1
    assert "timing" in r and r["timing"]["total_ms"] >= 0
    row = c.execute("SELECT mint, status FROM memecoin_exec_latency").fetchone()
    assert row["mint"] == "0xtok" and row["status"] == "filled"


def test_manual_evm_entry_freezes_the_no_automatic_exit_policy(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sniper.services.memecoin_market_state import EntryClassification

    c, kill = _c(monkeypatch, tmp_path)
    ev = _Ev(); ev.signal_source = "manual"
    classification = EntryClassification(
        classification_version=1, network_id="8453", mint=ev.mint,
        signal_source="manual", decision_at="2026-07-09T00:00:00Z", coin_band="small",
        lifecycle="young", liquidity_class="balanced", momentum_phase="early",
        market_regime="normal", data_quality="complete", risk_scale=1.0,
        reason_codes=(), facts={"network_id": "8453", "mint": ev.mint},
    )
    seen = {}
    trace = SimpleNamespace(attempt_id="manual-attempt")
    m.start_entry_attempt(
        c, attempt_id=trace.attempt_id, decision_started_at="2026-07-09T00:00:00Z",
        network_id="8453", mint=ev.mint,
    )

    def execute(conn, pid, *_args):
        seen["pending"] = m.pending_row(conn, pid)
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, ev, route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"), execute_fn=execute, kill_path=kill,
        classification=classification, trace=trace,
    )

    assert result["status"] == "filled", result
    assert json.loads(seen["pending"]["funded_contract_json"])["policy"]["policy_id"] == "manual-unmanaged-v1"


def test_exec_latency_uses_executor_legacy_fallback_source_price(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    result = {
        "status": "filled", "tx_hash": "0xh", "position_id": 9,
        "source_price": 1.25, "price_attribution": "legacy_source_chain",
        "quote_price": 1.5, "fill_price": 1.75,
        "source_drift_pct": 20.0, "execution_slippage_pct": 16.6666666667,
        "total_copy_drift_pct": 40.0,
    }
    _run(c, kill, _Ev(), exec_result=result)
    row = dict(c.execute("SELECT * FROM memecoin_exec_latency").fetchone())
    assert row["source_price"] == 1.25
    assert row["price_attribution"] == "legacy_source_chain"
    assert row["execution_slippage_pct"] == pytest.approx(16.6666666667)


def test_reservation_tagged_with_chain(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    # capture the reservation before the executor confirms it by having the executor read the row
    seen = {}

    def execute_fn(conn, pid, mint, size, now_iso):
        seen["row"] = m.pending_row(conn, pid)
        return {"status": "filled", "position_id": pid}
    leg.evm_real_entry(c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                       native_balance=1.0, now_iso="t", trade_size=10.0,
                       token_safety_fn=lambda mint, **kw: (True, "ok"), execute_fn=execute_fn, kill_path=kill)
    assert seen["row"]["network_id"] == "8453"


def test_automatic_capacity_clip_becomes_the_reserved_clip(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    seen = {}

    def execute_fn(conn, pid, _mint, _size, _now):
        seen["notional"] = m.pending_row(conn, pid)["reserved_notional_usd"]
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=96.66,
        capacity_clip=12.08,
        token_safety_fn=lambda _mint: (True, "ok"), execute_fn=execute_fn, kill_path=kill,
    )

    assert result["status"] == "filled"
    assert seen["notional"] == pytest.approx(12.08)


def test_insufficient_native_rejects_before_executor(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev(), native=0.0)
    assert r["status"] == "rejected" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_unsafe_token_releases_reservation(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    r, calls = _run(c, kill, _Ev(), safe=(False, "sell simulation reverted (honeypot)"))
    assert r["status"] == "unsafe" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


def test_missing_configured_quote_is_route_failure_not_token_safety(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    reason = ("configured sell venue returned no executable quote "
              "(route=direct_v3_single_hop; other venues not tested)")

    r, calls = _run(c, kill, _Ev(), safe=(False, reason))

    assert r["status"] == "route_failed"
    assert r["reason"] == reason
    assert calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)


# --- arm-time context constructor (mirrors Solana build_real_context) ---

def test_build_evm_context_none_when_disarmed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    assert leg.build_real_evm_context("8453") is None


@pytest.mark.parametrize('chain', ['4663', '56', '8453', '1'])
def test_fast_safety_quotes_are_wired_to_every_funded_evm_chain(monkeypatch, tmp_path, chain):
    """8453 since 2026-10-07 (KARP 8b609206): its safety sell probe ran the priority ladder and held
    kyber's 352 ms hit for v4_multihop's 3.8 s no-bridge search. Chain 1 since 2026-10-08
    (a1e93afb): its probe waited for every rung and took the largest output, so v4's 376 ms hit
    sat behind v4_multihop's 7.3 s bridge search and safety took 7.5 s."""
    from sniper.execution import evm_token_safety
    from sniper.execution.evm_wallet import generate_keypair_file
    path = str(tmp_path / 'evm.json')
    generate_keypair_file(path)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_KEYPAIR', path)
    monkeypatch.setenv('MEMECOIN_EVM_EXEC_VENUE_' + chain, 'direct')
    monkeypatch.setenv('MEMECOIN_EVM_RPC_URL_' + chain, 'https://rpc')
    captured = {}
    original = evm_token_safety.build_evm_token_safety
    def build(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(evm_token_safety, 'build_evm_token_safety', build)
    assert leg.build_real_evm_context(chain, http=_chain_identity_http(chain)) is not None
    fast = captured['fast_quote_fn']
    assert fast.func is captured['quote_fn'] and fast.keywords == {'first_available': True}


def test_build_evm_context_none_when_no_rpc(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_URL_8453", raising=False)
    assert leg.build_real_evm_context("8453") is None


def test_build_evm_context_builds_direct_4663_when_armed_with_creds(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    rpc_calls = []

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        if request["method"] == "eth_chainId":
            return 200, {"result": hex(4663)}, None
        rpc_calls.append(request)
        return 200, {"result": "0x"}, None

    rc = leg.build_real_evm_context("4663", http=http)
    assert rc is not None and rc["chain"] == "4663"
    assert rc["submission_backend"] == "auto_free"
    assert rc["approval_pipeline"] is False
    assert set(rc["lifi_local"]) == {
        "nonce_fn", "gas_price_fn", "sign_fn", "send_fn", "receipt_fn",
        "allowance_fn", "approve_gas", "estimate_gas_fn", "construct_tx_fn",
        "allowance_at_block_fn", "simulate_fn", "sign_complete_fn",
        "send_swap_fn_factory", "balance_raw_fn", "balance_raw_checked_fn",
        "native_balance_fn", "recover_confirmed_fn", "reconcile_drop_fn",
    }
    for key in ("execute_fn", "exit_fn", "token_safety_fn", "usdc_fn", "native_fn", "token_fn",
                "receipt_proceeds_fn", "entry_quote_probe_fn", "confirmed_nonce_fn"):
        assert callable(rc[key]), key
    assert rc["lifi_local"]["simulate_fn"]({
        "from": "0x" + "1" * 40, "to": "0x" + "2" * 40, "data": "0xdead",
        "value": 0, "gas": 518_413, "gasPrice": 641_406_000,
    }, 42)
    call = rpc_calls[-1]["params"][0]
    assert call["value"] == "0x0"
    assert call["gas"] == "0x7e90d"
    assert call["gasPrice"] == "0x263b1430"


def test_build_evm_context_none_for_5042_without_v3_addresses(monkeypatch, tmp_path):
    """Same gate every chain gets: no router+quoter, no context, however else it's configured."""
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_5042", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_5042", "https://arc-rpc")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_ROUTER",
                        {k: v for k, v in cfg.MEMECOIN_EVM_V3_ROUTER.items() if k != "5042"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_QUOTER",
                        {k: v for k, v in cfg.MEMECOIN_EVM_V3_QUOTER.items() if k != "5042"})
    assert leg.build_real_evm_context("5042") is None


def test_build_evm_context_builds_direct_5042_when_armed_with_creds(monkeypatch, tmp_path):
    """Mirrors test_build_evm_context_builds_direct_4663_when_armed_with_creds: with the exec
    venue, RPC, keypair, and the (now-cited, on-chain-verified) V3 addresses all present, Arc
    builds a real context the same as any other direct chain."""
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_5042", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_5042", "https://arc-rpc")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        if request["method"] == "eth_chainId":
            return 200, {"result": hex(5042)}, None
        return 200, {"result": "0x"}, None

    rc = leg.build_real_evm_context("5042", http=http)
    assert rc is not None and rc["chain"] == "5042"
    for key in ("execute_fn", "exit_fn", "token_safety_fn", "usdc_fn", "native_fn", "token_fn",
               "receipt_proceeds_fn", "entry_quote_probe_fn", "confirmed_nonce_fn"):
        assert callable(rc[key]), key


def test_authenticated_4663_context_passes_all_runtime_and_recovery_callbacks(
        monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams, evm_token_safety
    from sniper.execution.evm_wallet import generate_keypair_file

    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.setattr(cfg, "memecoin_lifi_mode", lambda: "authenticated_funded")
    monkeypatch.setattr(cfg, "validate_lifi_config", lambda **_kwargs: {
        "quota_limit": "12000", "quota_remaining": "11990", "quota_reset": "7200",
    })
    callbacks = {
        "entry_fn": lambda **_kwargs: {"status": "entry"},
        "preview_fn": lambda **_kwargs: {"status": "preview"},
        "exit_fn": lambda **_kwargs: {"status": "exit"},
        "recovery_context": {"authenticated": True},
    }
    seen = {}
    recover_confirmed = lambda *_args, **_kwargs: True
    reconcile_drop = lambda *_args, **_kwargs: {"complete": True}

    def runtime_factory(**kwargs):
        seen["runtime"] = kwargs
        return callbacks

    def fake_executor(**kwargs):
        seen["executor"] = kwargs
        noop = lambda *_args, **_kwargs: None
        return {
            "execute_fn": noop, "exit_fn": noop, "derisk_fn": noop,
            "preview_exit_fn": noop, "venue_quote_fn": noop,
            "usdc_fn": lambda: 1.0, "token_fn": lambda _mint: 0.0,
            "token_checked_fn": lambda _mint: 0.0,
            "_confirm": noop, "_confirm_once": noop,
            "lifi_local": {
                "recover_confirmed_fn": recover_confirmed,
                "reconcile_drop_fn": reconcile_drop,
            },
        }

    monkeypatch.setattr(evm_real_seams, "build_real_evm_executor", fake_executor)
    monkeypatch.setattr(
        evm_token_safety, "build_evm_token_safety",
        lambda **_kwargs: (lambda _mint, **_kw: (True, "ok")),
    )
    conn = object()
    rc = leg.build_real_evm_context(
        "4663", http=_chain_identity_http("4663"), conn=conn,
        lifi_runtime_factory=runtime_factory,
    )

    assert seen["runtime"]["conn"] is conn
    assert seen["executor"]["lifi_entry_fn"] is callbacks["entry_fn"]
    assert seen["executor"]["lifi_preview_fn"] is callbacks["preview_fn"]
    assert seen["executor"]["lifi_exit_fn"] is callbacks["exit_fn"]
    assert rc["lifi_enabled"] is True
    assert rc["lifi_recovery_context"] == callbacks["recovery_context"]
    assert callbacks["recovery_context"].get("recover_confirmed_fn") is recover_confirmed
    assert callbacks["recovery_context"].get("reconcile_drop_fn") is reconcile_drop


def test_canary_context_keeps_full_token_safety_with_lifi_code_safety(
        monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams, evm_token_safety
    from sniper.execution.evm_wallet import generate_keypair_file

    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    seen = {}
    callbacks = {
        "entry_fn": lambda **_kwargs: {"status": "entry"},
        "preview_fn": lambda **_kwargs: {"status": "preview"},
        "exit_fn": lambda **_kwargs: {"status": "exit"},
        "recovery_context": {"authenticated": True},
    }

    def runtime_factory(**kwargs):
        seen["runtime"] = kwargs
        return callbacks

    def fake_executor(**kwargs):
        seen["executor"] = kwargs
        noop = lambda *_args, **_kwargs: None
        return {
            "execute_fn": noop, "exit_fn": noop, "derisk_fn": noop,
            "preview_exit_fn": noop, "venue_quote_fn": noop,
            "usdc_fn": lambda: 1.0, "token_fn": lambda _mint: 0.0,
            "token_checked_fn": lambda _mint: 0.0,
            "_confirm": noop, "_confirm_once": noop, "lifi_local": {},
        }

    monkeypatch.setattr(evm_real_seams, "build_real_evm_executor", fake_executor)
    monkeypatch.setattr(
        evm_token_safety, "build_evm_token_safety",
        lambda **_kwargs: (lambda _mint, **_kw: (False, "direct missing")),
    )
    monkeypatch.setattr(
        evm_token_safety, "build_lifi_token_safety",
        lambda **_kwargs: (lambda _mint, **_kw: (True, "ok")),
    )
    quota = {"quota_limit": "12000", "quota_remaining": "11990", "quota_reset": "7200"}
    rc = leg.build_real_evm_context(
        "4663", http=_chain_identity_http("4663"), conn=object(),
        lifi_runtime_factory=runtime_factory, lifi_canary_activation=quota,
    )

    assert seen["runtime"]["activation"] == quota
    assert seen["runtime"]["canary"] is True
    assert seen["executor"]["force_lifi"] is True
    assert seen["executor"]["lifi_safety_fn"]("0xtok") == (False, "direct missing")
    assert rc["lifi_enabled"] is True


def test_build_evm_context_exposes_chain_scoped_approval_pipeline(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663")
    rc = leg.build_real_evm_context("4663", http=_chain_identity_http("4663"))
    assert rc["submission_backend"] == "auto_free"
    assert rc["approval_pipeline"] is True


def test_build_evm_context_shares_executor_venue_quote_with_token_safety(monkeypatch, tmp_path):
    """Direct execution and pre-entry safety must use one venue selector, never split direct/0x."""
    from sniper.execution import evm_real_seams, evm_token_safety
    from sniper.execution.evm_wallet import generate_keypair_file

    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    proof_db = tmp_path / "proofs.sqlite"
    proof_conn = sqlite3.connect(proof_db)
    proof_conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(proof_conn)
    monkeypatch.setenv("DB_PATH", str(proof_db))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    venue_quote = lambda *_a, **_k: {"buyAmount": "1"}
    probe_tokens = lambda _mint: 123
    noop = lambda *_a, **_k: None

    def fake_executor(**_kwargs):
        return {
            "execute_fn": noop, "exit_fn": noop, "derisk_fn": None,
            "preview_exit_fn": noop, "venue_quote_fn": venue_quote,
            "entry_probe_tokens_fn": probe_tokens,
            "usdc_fn": lambda: 1.0, "token_fn": lambda _mint: 0.0,
            "_confirm": noop, "_confirm_once": noop,
        }

    seen = {}

    def fake_safety(*, quote_fn, probe_tokens_fn, sell_slippage_bps, sell_route_label,
                    slot_proof_load, slot_proof_store, **_kwargs):
        seen["quote_fn"] = quote_fn
        seen["probe_tokens_fn"] = probe_tokens_fn
        seen["sell_slippage_bps"] = sell_slippage_bps
        seen["sell_route_label"] = sell_route_label
        key = ("4663", "0xtok", "0xspender")
        proof = ("0x" + "11" * 32, "0x" + "22" * 32)
        assert slot_proof_load(key) is None
        slot_proof_store(key, proof)
        assert slot_proof_load(key) == proof
        return lambda _mint: (True, "ok")

    monkeypatch.setattr(evm_real_seams, "build_real_evm_executor", fake_executor)
    monkeypatch.setattr(evm_token_safety, "build_evm_token_safety", fake_safety)

    rc = leg.build_real_evm_context("4663", http=_chain_identity_http("4663"))

    assert rc is not None
    assert seen["quote_fn"] is venue_quote
    assert seen["probe_tokens_fn"] is probe_tokens
    assert seen["sell_slippage_bps"] == int(
        round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100))
    assert seen["sell_route_label"] == (
        "direct_v3_single_hop+v2_fallback+pons_v2+v4+v4_multihop"
    )
    proof_conn.close()


def test_built_context_shares_decimals_cache_between_safety_and_executor(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import SEL_BALANCE_OF, SEL_DECIMALS, generate_keypair_file

    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    # The Permit2 layout prewarm's balanceOf slot probes are not this test's reads (2026-10-16).
    monkeypatch.setenv("MEMECOIN_EVM_PERMIT2_SLOT_PREWARM_CHAINS", "")
    calls = {"decimals": 0, "balanceOf": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            return 200, {"liquidityAvailable": False, "transaction": None}, None
        req = json.loads(body)
        if req["method"] == "eth_chainId":
            return 200, {"result": hex(4663)}, None
        if req["method"] == "eth_getCode":
            return 200, {"result": "0x6000"}, None
        data = (req.get("params") or [{}])[0].get("data", "")
        if data.startswith("0x" + SEL_DECIMALS):
            calls["decimals"] += 1
            return 200, {"result": hex(18)}, None
        if data.startswith("0x" + SEL_BALANCE_OF):
            calls["balanceOf"] += 1
            return 200, {"result": hex(2 * 10**18)}, None
        raise AssertionError(req)

    rc = leg.build_real_evm_context("4663", http=http)
    assert rc["token_safety_fn"]("0x" + "22" * 20)[0] is False
    assert rc["token_fn"]("0x" + "22" * 20) == pytest.approx(2.0)
    assert rc["token_fn"]("0x" + "22" * 20) == pytest.approx(2.0)
    assert calls == {"decimals": 1, "balanceOf": 2}


# --- token-safety: an executor that accepts safety_gate_fn starts token-safety in a worker thread
#     and hands the executor
#     a join-gate; the executor checks it before the first wallet mutation. Refusal statuses and
#     alerting must be IDENTICAL to the sequential path. ---

def _parallel_run(monkeypatch, tmp_path, *, safety=(True, "ok"), exec_result=None,
                  safety_raises=None, alert=None):
    c, kill = _c(monkeypatch, tmp_path)
    seen = {"gate": None, "safety_calls": 0}

    def safety_fn(mint, *, tax_cap=None):
        seen["safety_calls"] += 1
        if safety_raises is not None:
            raise safety_raises
        return safety

    def execute_fn(conn, pid, mint, size, now_iso, safety_gate_fn=None):
        seen["gate"] = safety_gate_fn
        if exec_result is not None:
            return exec_result
        safe, reason = safety_gate_fn()
        if not safe:
            from sniper.persistence import db_memecoin as mm
            mm.release_pending(conn, pid, "token_safety")
            return {"status": "unsafe_gate", "reason": reason}
        return {"status": "filled", "tx_hash": "0xh", "position_id": pid}

    r = leg.evm_real_entry(c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                           native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill,
                           alert_fn=alert)
    return r, seen, c


def test_parallel_safety_hands_gate_to_executor_and_fills(monkeypatch, tmp_path):
    r, seen, _c2 = _parallel_run(monkeypatch, tmp_path)
    assert r["status"] == "filled"
    assert callable(seen["gate"])
    assert seen["safety_calls"] == 1


def test_parallel_safety_starts_before_the_reservation(monkeypatch, tmp_path):
    """GHOST: moving the worker after the reservation restores the measured 0.6 s serial wait."""
    c, kill = _c(monkeypatch, tmp_path)
    safety_started = threading.Event()
    reserve = leg.reserve_entry_evm

    def reserve_after_safety(*args, **kwargs):
        assert safety_started.wait(0.2), "safety must overlap the reservation"
        return reserve(*args, **kwargs)

    monkeypatch.setattr(leg, "reserve_entry_evm", reserve_after_safety)

    def safety_fn(_mint):
        safety_started.set()
        return True, "ok"

    def execute_fn(_conn, pid, _mint, _size, _now, safety_gate_fn=None):
        assert safety_gate_fn is not None
        assert safety_gate_fn() == (True, "ok")
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill,
    )

    assert result["status"] == "filled"


def test_parallel_safety_unsafe_maps_to_unsafe_with_alert(monkeypatch, tmp_path):
    alerts = []
    r, _seen, c = _parallel_run(monkeypatch, tmp_path, safety=(False, "sell tax 40%"),
                                alert=lambda mint, reason: alerts.append((mint, reason)))
    assert r["status"] == "unsafe"
    assert r["reason"] == "sell tax 40%"
    assert alerts == [("0xtok", "sell tax 40%")]


def test_parallel_unknown_sell_revert_uses_recent_confirmed_exit(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(
        m, "recent_confirmed_real_exit_for_coin",
        lambda *_args, **_kwargs: {
            "position_id": 47, "closed_at": "2026-07-09T00:00:00Z", "close_sig": "0xexit",
        },
        raising=False,
    )
    seen = {}

    def execute_fn(_conn, _pid, _mint, _size, _now, safety_gate_fn=None):
        seen["safety"] = safety_gate_fn()
        return {"status": "filled", "position_id": 1}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:10Z", trade_size=10.0,
        token_safety_fn=lambda _mint: (False, "sell_sim_reverted_unknown: detail=execution reverted"),
        execute_fn=execute_fn, kill_path=kill,
    )

    assert result["status"] == "filled"
    assert seen["safety"] == (True, "recent_confirmed_real_exit")


def test_bsc_current_sell_failure_cannot_be_overridden_by_an_older_exit(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", set(cfg.MEMECOIN_EVM_ALLOWLIST) | {"56"})
    lookups = []

    def old_exit(*args, **kwargs):
        lookups.append(1)
        return {"position_id": 47, "closed_at": "2026-07-09T00:00:00Z", "close_sig": "0xexit"}

    monkeypatch.setattr(m, "recent_confirmed_real_exit_for_coin", old_exit)

    def execute_fn(_conn, _pid, _mint, _size, _now, safety_gate_fn=None):
        ok, reason = safety_gate_fn()
        return ({"status": "filled", "position_id": 1} if ok else
                {"status": "unsafe_gate", "reason": reason})

    ev = _Ev()
    ev.network_id = "56"
    reason = "sell_sim_reverted_unknown: detail=execution reverted; token_pull_ok=true"
    result = leg.evm_real_entry(
        c, ev, route="evm:56", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:10Z", trade_size=10.0,
        token_safety_fn=lambda _mint: (False, reason),
        execute_fn=execute_fn, kill_path=kill,
    )
    assert result["status"] == "unsafe"
    assert result["reason"] == reason
    assert lookups == []


def test_parallel_safety_route_unavailable_maps_to_route_failed(monkeypatch, tmp_path):
    from sniper.execution.evm_token_safety import EVM_ROUTE_UNAVAILABLE_PREFIX
    reason = EVM_ROUTE_UNAVAILABLE_PREFIX + " no executable sell route"
    r, _seen, _c2 = _parallel_run(monkeypatch, tmp_path, safety=(False, reason))
    assert r["status"] == "route_failed"
    assert r["reason"] == reason


def test_entry_simulation_refusal_is_a_route_failure(monkeypatch, tmp_path):
    reason = "entry_simulation_transfer_failed: exact route token transfer failed"
    r, _seen, _c2 = _parallel_run(
        monkeypatch, tmp_path,
        exec_result={"status": "unsafe_gate", "reason": reason},
    )
    assert r["status"] == "route_failed"
    assert r["reason"] == reason


def test_parallel_safety_exception_fails_closed(monkeypatch, tmp_path):
    r, _seen, _c2 = _parallel_run(monkeypatch, tmp_path, safety_raises=RuntimeError("rpc down"))
    assert r["status"] in ("unsafe", "route_failed")
    assert "safety_exception" in r["reason"]


def test_parallel_safety_bounds_a_stalled_read_to_twelve_seconds(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    seen = {}

    class StalledThread:
        def __init__(self, *, target, daemon, name):
            self.target = target

        def start(self):
            return None

        def join(self, timeout):
            seen["timeout"] = timeout

        def is_alive(self):
            return True

    monkeypatch.setattr(leg.threading, "Thread", StalledThread)

    def execute_fn(_conn, _pid, _mint, _size, _now, safety_gate_fn=None):
        safe, reason = safety_gate_fn()
        assert safe is False
        return {"status": "unsafe_gate", "reason": reason}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"), execute_fn=execute_fn, kill_path=kill,
    )

    assert seen["timeout"] == pytest.approx(12.0, abs=0.1)
    assert result["reason"] == "safety_deadline_exceeded"


def test_parallel_safety_4663_multihop_uses_the_configured_deadline(monkeypatch, tmp_path):
    """4663 was hardcoded at 30s against 12s on every other chain, and the safety worker runs
    INSIDE the main loop — so the chain with the worst routing held the loop longest. On
    2026-09-02 the loop went 143s without a cycle, the watchdog killed the runner for
    `sweep_stale`, and position 48565 sold at -81.3% having had a CONFIRMED stop at -41.5%.

    Asserts against the module constant rather than a literal: the value is now env-settable
    (`MEMECOIN_FUNDED_EVM_4663_SAFETY_WAIT_S`) precisely so it can be tuned during an incident
    without a deploy, and a test that pins a literal makes the knob undeployable."""
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    seen = {}

    class StalledThread:
        def __init__(self, *, target, daemon, name):
            self.target = target

        def start(self):
            return None

        def join(self, timeout):
            seen["timeout"] = timeout

        def is_alive(self):
            return True

    monkeypatch.setattr(leg.threading, "Thread", StalledThread)

    def execute_fn(_conn, _pid, _mint, _size, _now, safety_gate_fn=None):
        return {"status": "unsafe_gate", "reason": safety_gate_fn()[1]}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:4663", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda _mint: (True, "ok"), execute_fn=execute_fn, kill_path=kill,
    )

    assert seen["timeout"] == pytest.approx(
        leg._FUNDED_EVM_4663_MULTIHOP_SAFETY_WAIT_S, abs=0.1)
    assert result["reason"] == "safety_deadline_exceeded"


def test_parallel_safety_passes_the_single_deadline_to_nested_reads(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    seen = {}

    def safety_fn(_mint, *, deadline):
        seen["deadline"] = deadline
        return True, "ok"

    def execute_fn(_conn, _pid, _mint, _size, _now, safety_gate_fn=None):
        assert safety_gate_fn() == (True, "ok")
        return {"status": "filled", "position_id": 1}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill,
    )

    assert result["status"] == "filled"
    assert seen["deadline"] >= time.monotonic()


def test_parallel_safety_falls_back_sequential_when_executor_lacks_gate(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    order = []

    def safety_fn(mint, *, tax_cap=None):
        order.append("safety")
        return (True, "ok")

    def execute_fn(conn, pid, mint, size, now_iso):     # NO safety_gate_fn support
        order.append("exec")
        return {"status": "filled", "position_id": pid}

    r = leg.evm_real_entry(c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                           native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill)
    assert r["status"] == "filled"
    assert order == ["safety", "exec"]                  # sequential: safety BEFORE executor


# --- the quote-token price warm (2026-09-02) -----------------------------------------------------
# `execute_fn` opens with one live Binance ETH/USD read that the entry quote cannot start without,
# because it sizes the input amount: 411ms p50 / 7350ms p90 across 63 filled 4663 entries, 27.4% of
# the serial path, and 66 of 66 missed the executor's 1.0s memo because nothing earlier in the
# attempt fetches it. The leg fires the same read beside the reservation so the executor hits the
# memo. It must never be able to block or fail an entry.

def test_the_price_warm_runs_before_the_executor(monkeypatch, tmp_path):
    c, kill = _c(monkeypatch, tmp_path)
    order = []
    warmed = threading.Event()

    def warm():
        order.append("warm")
        warmed.set()

    def execute_fn(conn, pid, mint, size, now_iso):
        warmed.wait(timeout=5)          # the warm is concurrent; wait rather than race the assert
        order.append("execute")
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0, native_balance=1.0,
        now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
        token_safety_fn=lambda mint, **kw: (True, "ok"), execute_fn=execute_fn, kill_path=kill,
        warm_price_fn=warm)

    assert result["status"] == "filled"
    assert order == ["warm", "execute"]


def test_a_failing_price_warm_never_fails_the_entry(monkeypatch, tmp_path):
    """Best-effort by construction: the executor still does its own live read on a memo miss."""
    c, kill = _c(monkeypatch, tmp_path)

    def warm():
        raise RuntimeError("binance is down")

    result, calls = None, None
    result, calls = _run_with_warm(c, kill, _Ev(), warm)
    assert result["status"] == "filled"
    assert calls["exec"] == 1


def test_no_warm_fn_is_not_an_error(monkeypatch, tmp_path):
    """Every non-EVM caller and every test that predates the warm passes nothing."""
    c, kill = _c(monkeypatch, tmp_path)
    result, calls = _run(c, kill, _Ev())
    assert result["status"] == "filled" and calls["exec"] == 1


def _run_with_warm(c, kill, ev, warm):
    calls = {"exec": 0}

    def execute_fn(conn, pid, mint, size, now_iso):
        calls["exec"] += 1
        return {"status": "filled", "position_id": pid}

    r = leg.evm_real_entry(c, ev, route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                           native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=lambda mint, **kw: (True, "ok"),
                           execute_fn=execute_fn, kill_path=kill, warm_price_fn=warm)
    return r, calls


def _armed_4663(monkeypatch, tmp_path):
    """The direct 4663 leg, armed, with LI.FI in authenticated_funded mode."""
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    monkeypatch.setattr(cfg, "memecoin_lifi_mode", lambda: "authenticated_funded")
    monkeypatch.setattr(cfg, "validate_lifi_config", lambda **_k: {"ok": True})
    monkeypatch.setattr(cfg, "load_lifi_manifest", lambda: ({}, "hash"))

    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        if request["method"] == "eth_chainId":
            return 200, {"result": hex(4663)}, None
        return 200, {"result": "0x"}, None
    return http


def test_a_lifi_failure_leaves_the_direct_routes_running(monkeypatch, tmp_path):
    """LI.FI is consulted ONLY after every direct rung has missed, so its outage must not take
    v3/v2/pons_v2/v4/v4_multihop with it. Live 2026-09-04: LI.FI lost authentication overnight and
    an uncaught raise here took the whole funded 4663 leg down with it."""
    http = _armed_4663(monkeypatch, tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("lifi auth failed")

    monkeypatch.setattr(cfg, "validate_lifi_config", boom)
    rc = leg.build_real_evm_context("4663", http=http, lifi_runtime_factory=boom)
    assert rc is not None, "the chain must still build without the aggregator"
    assert rc["chain"] == "4663"
    assert callable(rc["execute_fn"]) and callable(rc["exit_fn"]), "direct routes must survive"
    assert rc.get("lifi_entry_fn") in (None, rc.get("lifi_entry_fn"))


def test_a_lifi_runtime_that_raises_also_degrades(monkeypatch, tmp_path):
    """The failure can come from the runtime build rather than the config check."""
    http = _armed_4663(monkeypatch, tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("lifi runtime unavailable")

    rc = leg.build_real_evm_context("4663", http=http, lifi_runtime_factory=boom)
    assert rc is not None and callable(rc["execute_fn"])


def test_a_canary_activation_still_fails_loudly(monkeypatch, tmp_path):
    """An operator activated that path deliberately; degrading it silently would report a canary
    run that never had LI.FI in it."""
    http = _armed_4663(monkeypatch, tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("lifi runtime unavailable")

    with pytest.raises(RuntimeError):
        leg.build_real_evm_context("4663", http=http, lifi_runtime_factory=boom,
                                   lifi_canary_activation={"canary": True})


def test_a_hard_size_cap_bounds_the_reservation_and_the_executor(monkeypatch, tmp_path):
    """Codex P1 (2026-09-13): the Pons test lowered trade_size to $50, but an automatic entry
    with no capacity_clip sized its reservation from the bankroll and the executor spent that:
    a $50 input became a $150 reservation and a $150 request."""
    from sniper.services import memecoin_evm_risk as risk
    monkeypatch.setattr(risk, "real_trade_size", lambda **_k: 150.0)   # Codex's $150 case
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0)
    seen = {}

    def execute_fn(conn, pid, _mint, size, _now):
        seen.update(notional=m.pending_row(conn, pid)["reserved_notional_usd"], size=size)
        return {"status": "filled", "position_id": pid}

    def enter(cap, usdc=600.0, clip=None):
        c2, k2 = _c(monkeypatch, tmp_path)
        seen.clear()
        seen["result"] = leg.evm_real_entry(
            c2, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=usdc, native_balance=1.0,
            now_iso="2026-07-09T00:00:00Z", trade_size=50.0, capacity_clip=clip,
            token_safety_fn=lambda _mint: (True, "ok"), execute_fn=execute_fn, kill_path=k2,
            size_cap_usd=cap)
        return dict(seen)

    uncapped = enter(None)
    assert "notional" in uncapped, uncapped
    assert uncapped["notional"] > 50.0, "the fixture must size above the cap to prove anything"
    capped = enter(50.0)
    assert capped["notional"] == pytest.approx(50.0) and capped["size"] == pytest.approx(50.0)
    # Only downward: a smaller size from the normal rules stands.
    assert enter(50.0, clip=12.0)["notional"] == pytest.approx(12.0)


def test_arc_safety_uses_reserved_size_and_executor_quantity(monkeypatch, tmp_path):
    from types import SimpleNamespace
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", {"5042"})
    # evm_real_entry refuses a chain outside the allowlist (Gamma H3, 2026-09-30); production has a
    # context only for allowlisted chains, so the test allowlists the chain it drives.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "5042"})
    monkeypatch.setattr(leg, "reserve_entry_evm", lambda *a, **kw:
                        (SimpleNamespace(allowed=True, approved_size_usdc=50.0), 1))
    seen = []

    def safety(mint, *, usd=None, bought_amount_atomic=None):
        seen.append((usd, bought_amount_atomic))
        return True, "ok"

    def execute(conn, pid, mint, size, now_iso, safety_gate_fn=None, quote_safety_fn=None):
        assert not seen
        assert size == 50
        assert safety_gate_fn is None
        assert quote_safety_fn({"buyAmount": "777"}) == (True, "ok")
        return {"status": "filled", "tx_hash": "0xh", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:5042", day="2026-07-09", usdc_balance=100,
        native_balance=100, now_iso="2026-07-09T00:00:00Z", trade_size=150,
        token_safety_fn=safety, execute_fn=execute, kill_path=kill,
    )
    assert result["status"] == "filled"
    assert seen == [(50.0, 777)]


def test_the_leg_context_names_its_rpc_url_for_the_holdings_verifier(monkeypatch):
    """holding_routes.verify_holding_quote reads ctx["rpc_url"]. Until 2026-09-22 the context
    carried the url only inside gas_topup, so every queued 4663 holding sell failed with
    KeyError ("internal error; check the runner log")."""
    import inspect
    from sniper.services import memecoin_evm_leg as leg
    src = inspect.getsource(leg.build_real_evm_context)
    assert '"rpc_url": rpc,' in src and '"address": address' in src


def test_background_facts_seed_only_this_chains_existing_decimals_cache(monkeypatch, tmp_path):
    from sniper.execution.evm_wallet import generate_keypair_file
    path = str(tmp_path / 'evm.json')
    generate_keypair_file(path)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_KEYPAIR', path)
    monkeypatch.setenv('MEMECOIN_EVM_EXEC_VENUE_4663', 'direct')
    monkeypatch.setenv('MEMECOIN_EVM_RPC_URL_4663', 'https://rpc')
    rc = leg.build_real_evm_context('4663', http=_chain_identity_http('4663'))
    token = '0x' + 'ab' * 20
    assert rc['put_token_facts_fn'](token, (0, 123)) is None
    assert rc['decimals_fn'](token.upper()) == 0
    assert rc['put_token_facts_fn'](token, (18, 456)) is None
    assert rc['decimals_fn'](token) == 0


def test_token_facts_publish_verified_supply_for_the_ceiling_guard(monkeypatch, tmp_path):
    """4663 fast-lane misses C: the lane's supply read must reach the funded guard, not stop at decimals."""
    from sniper.services import memecoin_pons_metadata as pm
    from sniper.execution.evm_wallet import generate_keypair_file
    monkeypatch.setattr(pm, '_LIVE_SUPPLY', collections.OrderedDict())
    path = str(tmp_path / 'evm.json')
    generate_keypair_file(path)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_KEYPAIR', path)
    monkeypatch.setenv('MEMECOIN_EVM_EXEC_VENUE_4663', 'direct')
    monkeypatch.setenv('MEMECOIN_EVM_RPC_URL_4663', 'https://rpc')
    rc = leg.build_real_evm_context('4663', http=_chain_identity_http('4663'))
    token = '0x' + 'cd' * 20
    rc['put_token_facts_fn'](token.upper().replace('0X', '0x'), (18, 100 * 10**27))
    got = pm.read_live_supply('4663', token)
    assert got['outcome'] == 'ok' and got['supply'] == pytest.approx(1e11)
    assert pm.read_live_supply('56', token)['outcome'] == 'missing'


@pytest.mark.parametrize('bound', [False, True])
def test_native_funding_precheck_releases_only_unsigned_before_safety(monkeypatch, tmp_path, bound):
    c, kill = _c(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_QUIET_HOURS_UTC', '')
    monkeypatch.setattr(cfg, 'MEMECOIN_MAX_TOTAL_EXPOSURE_USD', 10000.0)
    sizes = []
    def precheck(size):
        sizes.append(size)
        if bound:
            c.execute("UPDATE memecoin_pending_entries SET tx_sig='0xsigned'")
            c.commit()
        return 'float_short'
    result = leg.evm_real_entry(
        c, _Ev(), route='evm:8453', day='2026-07-09', usdc_balance=300.0,
        native_balance=1.0, now_iso='2026-07-09T00:00:00Z', trade_size=10.0,
        token_safety_fn=lambda *_a, **_k: pytest.fail('funding must precede safety'),
        execute_fn=lambda conn, pid, mint, size, now_iso: pytest.fail('funding must precede execution'),
        kill_path=kill, native_funding_precheck_fn=precheck)
    assert sizes and result['approved_clip_usd'] == sizes[0]
    assert result['reason'] == ('funding_precheck_release_conflict' if bound else 'float_short')
    row = c.execute('SELECT status,tx_sig FROM memecoin_pending_entries').fetchone()
    assert row['status'] == ('reserved' if bound else 'failed')
    assert row['tx_sig'] == ('0xsigned' if bound else None)
