"""Desk U assembler: emission -> ingest -> safety + Beta's cluster row + tape probe -> hard_filter (real step-3 DBs)."""
import importlib.util
import json
import sqlite3
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_pipeline import databases, api, AT, POLICY  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_assembler", ROOT / "scripts/memecoin_desk_assembler.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)
REAL_CHECK_TICKET = A.check_ticket

MINT = "14YMUuZfdm8uUTtbJbMbszxme2M2xHbnteSZqUkRpump"
SAFE = {"token_safety": {"status": "pass", "reason": "mint_read", "authority_mint": "closed",
                         "authority_freeze": "closed", "transfer_hook": None},
        "holders": {"status": "pass", "reason": "largest_accounts", "top10_pct": 20.0, "creator_pct": 1.0,
                    "exclusions_verified": True},
        "market_data": {"status": "pass", "reason": "curve_reserves"}}


def emission(at):
    F = U.Field
    snap = U.build_snapshot(emission_at=at, fields={
        "mcap_usd": F(41000.0, "pumpfun_grpc:curve_reserves", at, 7, "complete"),
        "liq_usd": F((55_123_429_969 - 30_000_000_000) / 1e9 * 180,
                     "pumpfun_grpc:real_quote_reserves", at, 7, "complete"),
        "flow_60s": F({"buy_n": 3, "sell_n": 1, "buy_usd": 120.0, "sell_usd": 30.0, "makers": 3, "largest_buy": 60.0},
                      "pumpfun_grpc:swaps", at, 7, "complete")})
    sig = "5" * 88
    return {"schema": "desk-candidate/v1", "producer": "U", "network_id": "solana", "mint": MINT,
            "pool": S.curve_address(MINT), "band_entry_slot": 7, "trigger_signature": sig, "trigger_ordinal": 0,
            "ticket_id": U.ticket_id("solana", MINT, 7, sig, 0), "gate_state": None,
            "emitted_at": datetime.fromtimestamp(at, timezone.utc).isoformat(), "snapshot": snap}


@pytest.fixture
def world(databases, tmp_path, monkeypatch):
    desk_conn, ledger_conn = databases
    cohort = api().provision_cohort(desk_conn, ledger_conn, producer="U",
                                    policy={**POLICY, "evidence_wait_s": 2, "judge_p_admit": 1.0},
                                    prompt_version="desk-judge-v12", created_at=AT)
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_ASSEMBLER_ENABLED", "1")
    monkeypatch.setattr(A, "state_root", lambda: str(tmp_path))
    monkeypatch.setattr(A, "check_ticket", lambda cand, rpc, sol, **kw: (dict(SAFE), {"mayhem": False}))
    now = time.time()
    desk = tmp_path / "data" / "desk"
    day = A.utc_day(now)
    for sub in ("candidate_emissions", "swaps", "cluster_checks"):
        (desk / sub).mkdir(parents=True)
    cand = emission(datetime.fromtimestamp(now - 0.5, timezone.utc).replace(microsecond=0).timestamp())
    (desk / "candidate_emissions" / f"{day}.jsonl").write_text(json.dumps(cand) + "\n")
    (desk / "universe.heartbeat").write_text(json.dumps({"ts": now, "sol_usd": 180.0, "sol_age_s": 1.0}))
    sw = sqlite3.connect(desk / "swaps" / f"{day}.db")
    sw.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
               "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
               "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, "
               "PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")
    sw.execute("INSERT INTO swaps VALUES(990,'s',0,?,'c',1,1,1,'a',55123429969,583962211444407,NULL,?,"
               "304062211444407,95,30)", (MINT, now - 1))
    sw.execute("INSERT INTO swaps VALUES(989,'s2',0,?,'c',1,1,1,'b',55123429969,583962211444407,NULL,?,"      # a second regular trade: the fee is
               "304062211444407,95,30)", (MINT, now - 2))                                                      # proven from >= 2 non-agent trades
    sw.commit()
    sw.close()
    return {"cohort": cohort, "desk": desk, "day": day, "cand": cand, "tmp": tmp_path, "conn": desk_conn}


def run(w, **kw):
    return A.main(["--desk-db", str(w["tmp"] / "desk.db"), "--cohort-id", w["cohort"], "--loop", "--max-tickets", "1"],
                  env_root=w["tmp"], rpc=SimpleNamespace(calls=0), from_start=True, **kw)


def log_rows(w):
    return [json.loads(l) for l in (w["desk"] / "assembler" / f"{w['day']}.jsonl").read_text().splitlines()]


def stage(w, tid):
    return [s for s in store.get_ticket(w["conn"], tid)["stages"] if s["stage"] == "hard_filter"]


def test_bound_cluster_row_and_live_tape_reach_a_hard_filter_pass(world):
    c = world["cand"]
    row = {"schema": "desk-cluster/v1", **{k: c[k] for k in ("ticket_id", "network_id", "mint", "pool")},
           "snapshot_hash": c["snapshot"]["hash"], "known_at": datetime.now(timezone.utc).isoformat(),
           "check": {"status": "pass", "reason": "cluster_clear"}}
    (world["desk"] / "cluster_checks" / f"{world['day']}.jsonl").write_text(json.dumps(row) + "\n")
    assert run(world) == 0
    out = [r for r in log_rows(world) if r["stage"] == "hard_filter"][0]
    assert out["verdict"] == "PASS" and out["cluster_wait"] == "row" and out["cluster_latency_s"] is not None
    assert out["checks"]["sell_probe"] == "pass:curve_math" and out["probe_evidence"]["fee_bps"] == [95, 30]
    assert stage(world, c["ticket_id"])[0]["verdict"] == "PASS"


def test_no_or_forged_cluster_row_is_absent_evidence_never_a_wait_or_a_bound_row_hf2(world):
    c = world["cand"]
    forged = {"schema": "desk-cluster/v1", **{k: c[k] for k in ("ticket_id", "network_id", "mint", "pool")},
              "snapshot_hash": "0" * 64, "known_at": AT, "check": {"status": "pass", "reason": "cluster_clear"}}
    (world["desk"] / "cluster_checks" / f"{world['day']}.jsonl").write_text(json.dumps(forged) + "\n")
    assert run(world) == 0
    out = [r for r in log_rows(world) if r["stage"] == "hard_filter"][0]
    assert out["cluster_wait"] == "unbound_row" and "cluster" not in out["checks"]       # forged row never enters evidence
    assert out["verdict"] == "PASS" and stage(world, c["ticket_id"])[0]["reason"] == "pass"


def test_restart_mid_wait_closes_the_ingested_ticket_as_a_skip(world):
    c = world["cand"]
    api().ingest_candidate(world["conn"], c, cohort_id=world["cohort"], now_iso=datetime.now(timezone.utc).isoformat())
    (world["desk"] / "assembler").mkdir()
    (world["desk"] / "assembler" / f"{world['day']}.jsonl").write_text(json.dumps(
        {"ticket_id": c["ticket_id"], "cohort_id": world["cohort"], "stage": "ingested", "candidate": c}) + "\n"
        + json.dumps({"ticket_id": "desk:U:other", "cohort_id": "smoke", "stage": "ingested", "candidate": c}) + "\n")
    (world["desk"] / "candidate_emissions" / f"{world['day']}.jsonl").write_text("")
    assert A.main(["--desk-db", str(world["tmp"] / "desk.db"), "--cohort-id", world["cohort"]],
                  env_root=world["tmp"], rpc=SimpleNamespace(calls=0), from_start=True) == 0
    assert stage(world, c["ticket_id"])[0]["verdict"] == "SKIP"
    assert [r for r in log_rows(world) if r.get("recovered") == "assembler_restart"]


def test_missing_cohort_or_wait_is_refused_never_defaulted(world):
    with pytest.raises(SystemExit):
        A.main(["--desk-db", str(world["tmp"] / "desk.db"), "--cohort-id", "nope"], env_root=world["tmp"], rpc=SimpleNamespace(calls=0))


def test_a_stale_emission_is_ingested_and_skipped_never_dropped(world, monkeypatch):
    c = emission(datetime.fromtimestamp(time.time() - 30, timezone.utc).replace(microsecond=0).timestamp())
    (world["desk"] / "candidate_emissions" / f"{world['day']}.jsonl").write_text(json.dumps(c) + "\n")
    monkeypatch.setattr(A, "check_ticket", lambda *a, **k: pytest.fail("no RPC for a stale emission"))
    assert run(world) == 0
    out = [r for r in log_rows(world) if r["stage"] == "hard_filter"][0]
    assert out["verdict"] == "SKIP" and out["checks"]["token_safety"] == "unknown:emission_stale"


def test_a_second_assembler_exits_on_the_lock(world, capsys):
    import fcntl
    (world["desk"] / "assembler").mkdir(exist_ok=True)
    held = open(world["desk"] / "assembler" / "assembler.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert run(world) == 0 and "holds the lock" in capsys.readouterr().out


def test_restart_resumes_from_the_cursor_and_the_backlog_skips_as_stale(world, monkeypatch):
    """Gamma BLOCKING on 49609f921: emissions written while the assembler was down are still ingested."""
    c = emission(datetime.fromtimestamp(time.time() - 30, timezone.utc).replace(microsecond=0).timestamp())
    (world["desk"] / "candidate_emissions" / f"{world['day']}.jsonl").write_text(json.dumps(c) + "\n")
    (world["desk"] / "assembler").mkdir()
    (world["desk"] / "assembler" / "cursor.json").write_text(json.dumps({"day": world["day"], "offset": 0}))
    monkeypatch.setattr(A, "check_ticket", lambda *a, **k: pytest.fail("no RPC for the stale backlog"))
    assert A.main(["--desk-db", str(world["tmp"] / "desk.db"), "--cohort-id", world["cohort"], "--loop",
                   "--max-tickets", "1"], env_root=world["tmp"], rpc=SimpleNamespace(calls=0)) == 0
    rows = log_rows(world)
    assert rows[0]["stage"] == "coverage" and rows[0]["backlog_lines"] == 1
    assert [r["verdict"] for r in rows if r["stage"] == "hard_filter"] == ["SKIP"]
    cur = json.loads((world["desk"] / "assembler" / "cursor.json").read_text())
    assert cur["offset"] == len(json.dumps(c)) + 1


def test_first_start_names_what_it_did_not_take(world):
    (world["desk"] / "assembler").mkdir()
    assert A.main(["--desk-db", str(world["tmp"] / "desk.db"), "--cohort-id", world["cohort"]],
                  env_root=world["tmp"], rpc=SimpleNamespace(calls=0)) == 0
    first = log_rows(world)[0]
    assert first["verdict"] == "FIRST_START" and first["skipped_lines"] == 1


def test_budget_counters_survive_a_restart(world):
    (world["desk"] / "assembler").mkdir()
    now = time.time()
    rows = [{"stage": "safety_reserve", "reserved_at": now - 5, "rpc_calls": 2},
            {"stage": "safety_reserve", "reserved_at": now - 120, "rpc_calls": 2},
            {"stage": "safety", "safety_at": now - 5, "rpc_calls": 2}]       # actual-call rows are not the charge
    (world["desk"] / "assembler" / f"{world['day']}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    calls, window = A.counters(world["desk"] / "assembler", now)
    assert calls == 4 and len(window) == 1


def test_an_unreadable_gap_file_is_an_open_gap(world):
    (world["desk"] / "gaps").mkdir()
    (world["desk"] / "gaps" / f"{world['day']}.db").write_text("not a database")
    gaps = A.tape_state(world["desk"], MINT, time.time())[3]
    assert None in gaps


class _Crash(BaseException):
    """A process death between two durable writes (not an Exception: nothing may catch it)."""


def _args(w, *extra):
    return ["--desk-db", str(w["tmp"] / "desk.db"), "--cohort-id", w["cohort"], *extra]


def _other(w):
    import copy
    c = copy.deepcopy(w["cand"])
    c["mint"] = "So11111111111111111111111111111111111111112"
    c["pool"] = S.curve_address(c["mint"])
    c["ticket_id"] = U.ticket_id(c["network_id"], c["mint"], c["band_entry_slot"],
                                 c["trigger_signature"], c["trigger_ordinal"])
    c["opportunity_id"] = c["ticket_id"]
    return c


def test_a_crash_before_ingest_re_reads_the_line(world, monkeypatch):
    # Codex 066e25c29: the cursor advanced before ingest, so a crash dropped the emission
    with monkeypatch.context() as m:
        m.setattr(api(), "ingest_candidate", lambda *a, **kw: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0), from_start=True)
    assert A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0)) == 0
    assert store.get_ticket(world["conn"], world["cand"]["ticket_id"]) is not None


def test_an_ingest_committed_before_a_crash_is_closed_on_restart(world, monkeypatch):
    # Codex 066e25c29: committed ticket with no 'ingested' row stayed open forever
    real = api().ingest_candidate

    def committed_then_crashed(*a, **kw):
        real(*a, **kw)
        raise _Crash()
    with monkeypatch.context() as m:
        m.setattr(api(), "ingest_candidate", committed_then_crashed)
        with pytest.raises(_Crash):
            A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0), from_start=True)
    assert A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0)) == 0
    head = store.get_ticket(world["conn"], world["cand"]["ticket_id"])
    assert head["stages"] and head["stages"][0]["verdict"] == "SKIP"
    assert [r for r in log_rows(world) if r.get("recovered") == "assembler_restart"]


def test_a_crash_mid_read_cannot_reset_the_daily_cap(world, monkeypatch):
    # Codex 066e25c29: the charge was journaled after the reads, so a restart spent the cap twice
    rpc = SimpleNamespace(calls=0)

    def spent_then_crashed(*a, **kw):
        rpc.calls += 2
        raise _Crash()
    with monkeypatch.context() as m:
        m.setattr(A, "check_ticket", spent_then_crashed)
        with pytest.raises(_Crash):
            A.main(_args(world, "--calls-per-day", "2"), env_root=world["tmp"], rpc=rpc, from_start=True)
    with (world["desk"] / "candidate_emissions" / f"{world['day']}.jsonl").open("a") as fh:
        fh.write(json.dumps(_other(world)) + "\n")
    monkeypatch.setattr(A, "check_ticket", lambda *a, **kw: pytest.fail("restart spent past the daily cap"))
    assert A.main(_args(world, "--calls-per-day", "2", "--loop", "--max-tickets", "1"),
                  env_root=world["tmp"], rpc=rpc) == 0
    assert rpc.calls == 2


def test_http_429_is_a_typed_rate_limit_with_a_pause(monkeypatch):
    import urllib.error
    paused = []

    def limited(*a, **kw):
        raise urllib.error.HTTPError("https://isolated.example", 429, "limited", {}, None)
    monkeypatch.setattr(A.urllib.request, "urlopen", limited)
    rpc = A.Rpc("https://isolated.example", SimpleNamespace(acquire=lambda: None, pause=paused.append))
    assert rpc("getTokenLargestAccounts", []) == (None, "rate_limited")
    assert paused == [A.PAUSE_ON_429_S]


def test_a_line_that_crashes_every_start_is_refused_after_crash_tries(world, monkeypatch):
    # Gamma LOW 1 on 2f4dd197e: a deterministic non-ValueError in ingest pinned the cursor forever
    with (world["desk"] / "candidate_emissions" / f"{world['day']}.jsonl").open("a") as fh:
        fh.write(json.dumps(_other(world)) + "\n")
    real = api().ingest_candidate

    def poison(desk_conn, cand, **kw):
        if cand["ticket_id"] == world["cand"]["ticket_id"]:
            raise _Crash()
        return real(desk_conn, cand, **kw)
    monkeypatch.setattr(api(), "ingest_candidate", poison)
    monkeypatch.setattr(A, "check_ticket", lambda *a, **kw: (dict(SAFE), {"mayhem": False}))
    with pytest.raises(_Crash):
        A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0), from_start=True)
    for _ in range(A.CRASH_TRIES - 1):
        with pytest.raises(_Crash):
            A.main(_args(world), env_root=world["tmp"], rpc=SimpleNamespace(calls=0))
    A.main(_args(world, "--loop", "--max-tickets", "1"), env_root=world["tmp"], rpc=SimpleNamespace(calls=0))
    refused = [r for r in log_rows(world) if r.get("reason") == "crash_loop"]
    assert [r["ticket_id"] for r in refused] == [world["cand"]["ticket_id"]]
    assert store.get_ticket(world["conn"], _other(world)["ticket_id"])["stages"]


AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"


def test_mayhem_coin_whose_last_trade_is_the_fee_free_agent_is_priced_with_the_regular_fee(world, monkeypatch):
    monkeypatch.setattr(A, "check_ticket", lambda cand, rpc, sol, **kw: (dict(SAFE), {"mayhem": True}))
    sw = sqlite3.connect(world["desk"] / "swaps" / f"{world['day']}.db")
    sw.execute("INSERT INTO swaps VALUES(991,'agent',0,?,'c',1,1,1,?,55123429969,583962211444407,NULL,?,"
               "304062211444407,0,0)", (MINT, AGENT, __import__("time").time() - 0.5))                          # the LAST trade: agent, (0, 0)
    sw.commit()
    sw.close()
    assert run(world) == 0
    out = [r for r in log_rows(world) if r["stage"] == "hard_filter"][0]
    assert out["checks"]["sell_probe"] == "pass:curve_math" and out["probe_evidence"]["fee_bps"] == [95, 30]
    assert out["probe_evidence"]["fee_basis"] == "non_agent_trades"


def test_mayhem_coin_with_only_agent_flow_stays_fee_unknown(world, monkeypatch):
    monkeypatch.setattr(A, "check_ticket", lambda cand, rpc, sw_, **kw: (dict(SAFE), {"mayhem": True}))
    sw = sqlite3.connect(world["desk"] / "swaps" / f"{world['day']}.db")
    sw.execute("UPDATE swaps SET actor=?, fee_bps=0, creator_fee_bps=0", (AGENT,))
    sw.commit()
    sw.close()
    assert run(world) == 0
    out = [r for r in log_rows(world) if r["stage"] == "hard_filter"][0]
    assert out["reason"] == "unknown:sell_probe:fee_unknown"


def test_pacer_gap_knob_clamps_and_a_429_restores_the_shared_gap(monkeypatch):
    import urllib.error
    assert A.pacer_gap({}) == A.SHARED_GAP_S
    assert A.pacer_gap({"MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S": "0.3"}) == 0.3
    assert A.pacer_gap({"MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S": "0.01"}) == 0.2
    assert A.pacer_gap({"MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S": "9"}) == A.SHARED_GAP_S
    assert A.pacer_gap({"MEMECOIN_DESK_ASSEMBLER_PACER_GAP_S": "x"}) == A.SHARED_GAP_S
    pacer = SimpleNamespace(acquire=lambda: None, pause=lambda s: None, gap_s=0.3)

    def limited(*a, **kw):
        raise urllib.error.HTTPError("https://isolated.example", 429, "limited", {}, None)
    monkeypatch.setattr(A.urllib.request, "urlopen", limited)
    A.Rpc("https://isolated.example", pacer)("getTokenLargestAccounts", [])
    assert pacer.gap_s == A.SHARED_GAP_S


def test_idle_poll_is_fast_and_cluster_reads_keep_their_cadence():
    assert A.POLL_IDLE_S <= 0.05 and A.CLUSTER_POLL_S >= 0.5


def _retry_rpc(monkeypatch, replies):
    import io
    calls, pauses = [], []

    def send(req, **kw):
        calls.append(json.loads(req.data))
        value = replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr(A.urllib.request, "urlopen", send)
    rpc = A.Rpc("https://research.example/private-secret?api-key=secret-key",
                SimpleNamespace(acquire=lambda: None, pause=pauses.append),
                clock=lambda: 100.0, sleep=lambda s: None)
    monkeypatch.setattr(A.S, "batch_addresses", lambda *a: ["mint", "curve", "fee"])
    monkeypatch.setattr(A.S, "evaluate", lambda *a, **kw: (dict(SAFE), {}))
    return rpc, calls, pauses


def test_retry_retains_error_and_reserves_before_identical_send(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [TimeoutError("secret-key"),
        {"result": {"value": []}}, {"result": {"value": [None] * 2}}])
    reserved = []
    def reserve():
        reserved.append(len(calls))
        return True
    checks, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25,
                                  deadline=102, reserve_retry=reserve)
    assert checks == SAFE and reserved == [1] and len(calls) == 3
    assert calls[0] == calls[1]
    error = meta["rpc_errors"][0]
    assert error["method"] == "getTokenLargestAccounts"
    assert error["hostname"] == "research.example" and error["exception_class"] == "TimeoutError"
    assert error["attempt"] == 1 and "secret-key" not in json.dumps(meta)
    assert error["message"] == "[redacted]" and error["redacted"]


@pytest.mark.parametrize("error", [
    {"code": 429, "message": "too many requests"},
    {"code": -32429, "message": "quota"},
    {"code": -32005, "message": "credit usage limit secret-key"},
])
def test_json_quota_pauses_without_retry(monkeypatch, error):
    rpc, calls, pauses = _retry_rpc(monkeypatch, [{"error": error}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25,
                             deadline=102, reserve_retry=lambda: pytest.fail("quota retry"))
    assert len(calls) == 1 and pauses == [60]
    assert meta["error"] == "rate_limited"
    assert meta["rpc_errors"][0]["code"] == error["code"]
    assert "secret-key" not in json.dumps(meta)


@pytest.mark.parametrize("deadline,budget", [(99, True), (102, False)])
def test_retry_requires_original_deadline_and_extra_budget(monkeypatch, deadline, budget):
    rpc, calls, _ = _retry_rpc(monkeypatch, [TimeoutError()])
    A.check_ticket(emission(99), rpc, None, clip_usd=25,
                   deadline=deadline, reserve_retry=lambda: budget)
    assert len(calls) <= 1


def test_retry_allowance_is_shared_across_both_reads(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [TimeoutError(),
        {"result": {"value": []}}, TimeoutError()])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25,
                             deadline=102, reserve_retry=lambda: True)
    assert len(calls) == 3 and len(meta["rpc_errors"]) == 2


def test_extra_retry_charge_survives_restart_without_counting_another_ticket(world, monkeypatch):
    def check(cand, rpc, sol, **kw):
        assert kw["reserve_retry"]()
        rows = log_rows(world)
        assert rows[-1]["stage"] == "safety_retry_reserve" and rows[-1]["rpc_calls"] == 1
        return dict(SAFE), {}
    monkeypatch.setattr(A, "check_ticket", check)
    assert run(world) == 0
    calls, window = A.counters(world["desk"] / "assembler", time.time())
    assert calls == 3 and len(window) == 1


def test_daily_budget_refuses_extra_retry_reservation(world, monkeypatch):
    def check(cand, rpc, sol, **kw):
        assert kw["reserve_retry"]() is False
        return dict(SAFE), {}
    monkeypatch.setattr(A, "check_ticket", check)
    assert A.main(_args(world, "--calls-per-day", "2", "--loop", "--max-tickets", "1"),
                  env_root=world["tmp"], rpc=SimpleNamespace(calls=0), from_start=True) == 0
    assert A.counters(world["desk"] / "assembler", time.time())[0] == 2


def test_http_quota_text_wins_over_retryable_status(monkeypatch):
    import urllib.error
    rpc, calls, pauses = _retry_rpc(monkeypatch, [urllib.error.HTTPError(
        "https://research.example", 503, "too many requests secret-key", {}, None)])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: pytest.fail("quota retry"))
    assert len(calls) == 1 and pauses == [60] and meta["error"] == "rate_limited"
    assert meta["rpc_errors"][0]["http_status"] == 503


def test_retry_pacer_wait_crossing_deadline_never_sends(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [TimeoutError(), {"result": {"value": []}}])
    now = [100.0]
    rpc.clock = lambda: now[0]
    def pace():
        if calls:
            now[0] = 103.0
    rpc.pacer.acquire = pace
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: True)
    assert len(calls) == 1 and meta["error"] == "deadline"


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_transient_http_retry_succeeds_and_keeps_exact_error(monkeypatch, status):
    import urllib.error
    rpc, calls, _ = _retry_rpc(monkeypatch, [urllib.error.HTTPError(
        "https://research.example", status, "upstream unavailable", {}, None),
        {"result": {"value": []}}, {"result": {"value": [None] * 2}}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: True)
    assert len(calls) == 3 and meta["rpc_errors"][0]["http_status"] == status


def test_batch_retry_and_next_ticket_do_not_share_error_context(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [{"result": {"value": []}}, TimeoutError(),
        {"result": {"value": [None] * 2}}, {"result": {"value": []}},
        {"result": {"value": [None] * 2}}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: True)
    assert meta["rpc_errors"][0]["method"] == "getMultipleAccounts"
    _, next_meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                                  reserve_retry=lambda: True)
    assert len(calls) == 5 and "rpc_errors" not in next_meta and rpc.last_error is None


def test_permanent_certificate_error_is_not_retried(monkeypatch):
    import ssl
    import urllib.error
    rpc, calls, _ = _retry_rpc(monkeypatch, [urllib.error.URLError(
        ssl.SSLCertVerificationError("bad certificate"))])
    A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                   reserve_retry=lambda: pytest.fail("certificate retry"))
    assert len(calls) == 1


def test_original_error_message_is_bounded_and_redacted(monkeypatch):
    rpc, _, _ = _retry_rpc(monkeypatch, [{"error": {"code": -32602,
        "message": "https://elsewhere.example/token private-secret secret-key " + "x" * 500}}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: pytest.fail("permanent RPC retry"))
    error = meta["rpc_errors"][0]
    assert error["code"] == -32602 and error["truncated"] and error["redacted"]
    assert len(error["message"]) <= 240
    assert all(s not in error["message"] for s in ("private-secret", "secret-key", "https://"))


def test_failed_retry_reservation_prevents_another_send(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [TimeoutError()])
    def fail():
        raise OSError("journal unavailable")
    with pytest.raises(OSError, match="journal unavailable"):
        A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102, reserve_retry=fail)
    assert len(calls) == 1


@pytest.mark.parametrize("sol_usd", [None, 100.0])
def test_only_unpriced_assembler_omits_unused_fee_account(monkeypatch, sol_usd):
    largest = [{"address": "holder-a"}, {"address": "holder-b"}]
    requested, evaluated = [], []
    mint, curve, fee, holder_a, holder_b = ({"raw": name} for name in ("mint", "curve", "fee", "a", "b"))
    def rpc(method, params):
        if method == "getTokenLargestAccounts":
            return {"value": largest}, None
        requested.extend(params[0])
        accounts = [mint, curve] + ([] if sol_usd is None else [fee]) + [holder_a, holder_b]
        return {"value": accounts, "context": {"slot": 123}}, None
    def evaluate(*a, **kw):
        evaluated.append(kw)
        return dict(SAFE), {}
    monkeypatch.setattr(A.S, "batch_addresses", lambda *a: ["mint", "curve", "fee", "holder-a", "holder-b"])
    monkeypatch.setattr(A.S, "evaluate", evaluate)
    A.check_ticket(emission(99), rpc, sol_usd, clip_usd=25)
    assert requested == ["mint", "curve"] + ([] if sol_usd is None else ["fee"]) + ["holder-a", "holder-b"]
    assert evaluated[0]["accounts"] == [mint, curve, None if sol_usd is None else fee, holder_a, holder_b]
    assert evaluated[0]["ctx_slot"] == 123 and evaluated[0]["largest"] == largest


def test_omitted_fee_request_rejects_a_response_with_an_extra_account(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [{"result": {"value": []}},
        {"result": {"value": [None] * 3}}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25)
    assert len(calls[1]["params"][0]) == 2 and meta["error"] == "malformed"


def test_expired_ticket_sends_no_initial_rpc(monkeypatch):
    rpc, calls, _ = _retry_rpc(monkeypatch, [{"result": {"value": []}}])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=99,
                             reserve_retry=lambda: pytest.fail("expired retry"))
    assert calls == [] and meta["error"] == "deadline"


def test_every_read_uses_original_deadline_and_rejects_late_results(monkeypatch):
    import io
    rpc, calls, _ = _retry_rpc(monkeypatch, [])
    now, timeouts = [100.8], []
    rpc.clock = lambda: now[0]
    def send(req, timeout):
        timeouts.append(timeout)
        now[0] = 103.0
        return io.BytesIO(json.dumps({"result": {"value": []}}).encode())
    monkeypatch.setattr(A.urllib.request, "urlopen", send)
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: pytest.fail("late retry"))
    assert len(timeouts) == 1 and timeouts[0] <= 1.2 + 1e-12
    assert meta["error"] == "deadline"


@pytest.mark.parametrize("code,message", [(-32429, "quota exhausted"), (-32005, "Too many requests secret-key")])
def test_quota_in_http_body_is_not_retried(monkeypatch, code, message):
    import io
    import urllib.error
    body = io.BytesIO(json.dumps({"error": {"code": code, "message": message}}).encode())
    rpc, calls, pauses = _retry_rpc(monkeypatch, [urllib.error.HTTPError(
        "https://research.example", 503, "Service Unavailable", {}, body)])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: pytest.fail("quota body retry"))
    assert len(calls) == 1 and pauses == [60] and meta["error"] == "rate_limited"
    error = meta["rpc_errors"][0]
    assert error["code"] == code and error["http_status"] == 503
    assert "secret-key" not in json.dumps(meta)


@pytest.mark.parametrize("body,paused", [(b"Too many requests", True), (b"x" * 9000, False)])
def test_http_error_body_is_bounded_and_plain_quota_also_pauses(monkeypatch, body, paused):
    import io
    import urllib.error
    reads = []
    class Body(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)
    rpc, calls, pauses = _retry_rpc(monkeypatch, [urllib.error.HTTPError(
        "https://research.example", 503, "Service Unavailable", {}, Body(body))])
    _, meta = A.check_ticket(emission(99), rpc, None, clip_usd=25, deadline=102,
                             reserve_retry=lambda: pytest.fail("quota/oversize retry"))
    assert reads == [8193] and len(calls) == 1 and bool(pauses) is paused
    assert meta["rpc_errors"][0]["truncated"] is (len(body) > 8192)


@pytest.mark.parametrize("result", [
    7, [1], {"value": 7}, {"value": "bad"}, {"value": {}},
    {"value": [None]}, {"value": [{}]}, {"value": [{"address": None}]},
    {"value": [{"address": 7}]}, {"value": [{"address": ""}]},
])
def test_malformed_largest_reply_preserves_ticket_with_typed_diagnostic(world, monkeypatch, result):
    import io
    calls = []
    def send(req, **kw):
        calls.append(json.loads(req.data)["method"])
        return io.BytesIO(json.dumps({"result": result}).encode())
    monkeypatch.setattr(A.urllib.request, "urlopen", send)
    monkeypatch.setattr(A, "check_ticket", REAL_CHECK_TICKET)
    rpc = A.Rpc("https://research.invalid", SimpleNamespace(acquire=lambda: None))
    assert A.main(_args(world, "--loop", "--max-tickets", "1"),
                  env_root=world["tmp"], rpc=rpc, from_start=True) == 0
    out = next(r for r in log_rows(world) if r["stage"] == "hard_filter")
    assert out["safety_meta"]["error"] == "malformed"
    assert out["verdict"] == "SKIP" and out["checks"]["token_safety"] == "unknown:rpc_failed"
    assert stage(world, world["cand"]["ticket_id"])[0]["verdict"] == "SKIP"
    assert calls == ["getTokenLargestAccounts"]
