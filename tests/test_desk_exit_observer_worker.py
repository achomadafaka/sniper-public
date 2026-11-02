"""Forward observer integration: temporary observer/ledger files, no SDK or funded writes."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from test_desk_risk_exact_pnl import RS as S
from sniper.services import memecoin_desk_exit_observer as O

T = 1_800_000_000.0
CFG = {"version": "exit-observer-v1", "grid": [2, 3, 4, 6], "baseline_drop_pct": 10.0}


def cfg_db(config=CFG):
    c = sqlite3.connect(":memory:")
    body = json.dumps({"producer": "U", "prompt_version": "p", "policy": {"exit_observer": config},
                       "kill_limit_usd": 200.}, sort_keys=True, separators=(",", ":"))
    c.execute("CREATE TABLE memecoin_desk_cohorts(cohort_id TEXT,producer TEXT,policy_json TEXT,policy_hash TEXT)")
    c.execute("INSERT INTO memecoin_desk_cohorts VALUES('C','U',?,?)", (body, hashlib.sha256(body.encode()).hexdigest()))
    p = {"desk_cohort_id": "C", "funded_contract_json": json.dumps({"source": {"desk": {
        "cohort_id": "C", "producer": "U", "opportunity_id": "O", "snapshot_hash": "a" * 64}}})}
    return c, p


@pytest.mark.parametrize("change", [None, "off", "version", "grid", "drop", "identity", "hash"])
def test_only_exact_frozen_supported_cohort_contract_enables_observer(change):
    cfg = dict(CFG)
    if change == "version": cfg["version"] = "exit-observer-v2"
    if change == "grid": cfg["grid"] = [2, 3, 4, 5, 6]
    if change == "drop": cfg["baseline_drop_pct"] = 12
    c, p = cfg_db(None if change == "off" else cfg)
    if change == "identity": p["desk_cohort_id"] = "OTHER"
    if change == "hash": c.execute("UPDATE memecoin_desk_cohorts SET policy_hash='bad'")
    config, reason = S.exit_observer_config(c, p)
    assert config == (CFG if change is None else None)
    assert (reason is None) is (change is None)


@pytest.fixture
def observer(tmp_path, monkeypatch):
    now = [T]
    p = {"id": 1, "mint": "M", "network_id": "solana", "opened_at": datetime.fromtimestamp(T + 1, timezone.utc).isoformat(),
         "desk_cohort_id": "C", "_exit_observer_config": CFG, "_exit_observer_reason": None}
    opened = [(p, {"trail_pct": 10}, [], None)]
    snap = {"rows": [], "cut": T - 2, "coverage_from": T - 1000, "opened_at": T + 1,
            "residual_atomic": "1000000", "own_signatures": ("ours",), "own_complete": True, "launch_at": None,
            "venue": "curve", "quantity_reason": None, "source_reason": None}
    def source(*_a, **_kw):
        return {**snap, "cut": now[0] - 2}
    def facts(_rows, **kw):
        return {"rule_version": O.VERSION, "population": "own_excluded", "flags": {"SELL_BURST": {"2": None}},
                "unknown": {"SELL_BURST": {"2": "fixture_unknown"}}, "noise_5": .01,
                "value_sol": .8, "peak_value_sol": 1., "peak_covered_from_fill": True,
                "residual_atomic": kw["residual_atomic"], "own_sig_excluded_count": 1}
    monkeypatch.setattr(O, "evaluate", facts)
    out = tmp_path / "observer.db"
    def tick(dt=0, **kw):
        now[0] += dt
        return S.run_exit_observer_pass(tmp_path / "ledger.db", tmp_path, out, now_fn=lambda: now[0],
                                       positions_fn=lambda _: [row for row in opened if
                                           datetime.fromisoformat(row[0]["opened_at"]).timestamp() <= now[0]],
                                       snapshot_fn=source, fill_sigs_of=lambda _: ("ours",), **kw)
    return now, p, opened, snap, out, tick


def rows(path, table="exit_observations"):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM " + table)]


def test_kill_stops_before_any_read_or_write(observer):
    *_a, path, tick = observer
    assert tick(enabled=False)["enabled"] is False
    assert not path.exists()


def test_unknown_flags_are_kept_and_no_engine_action_table_exists(observer):
    now, p, opened, snap, path, tick = observer
    tick()  # freezes the forward enrollment boundary
    stats = tick(3)
    assert stats["recorded"] == 1 and stats["enabled_open"] == 1
    record = rows(path)[-1]
    facts = json.loads(record["facts_json"])
    assert facts["flags"]["SELL_BURST"]["2"] is None
    assert facts["unknown"]["SELL_BURST"]["2"] == "fixture_unknown"
    assert facts["value_units"] == "SOL" and record["residual_atomic"] == "1000000"
    with sqlite3.connect(path) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "risk_decisions" not in tables and "decisions" not in tables
    assert not {"action", "rails_json", "expires_at", "live"} & record.keys()


def test_repeated_same_sample_and_restart_never_duplicate_records(observer):
    now, p, opened, snap, path, tick = observer
    tick(); tick(); tick(3); tick(); tick(1)
    assert len(rows(path)) == 2
    assert rows(path)[-1]["observer_started_at"] == T


def test_pre_start_positions_and_changed_residual_cannot_claim_uncensored_baseline(observer):
    now, p, opened, snap, path, tick = observer
    p["opened_at"] = datetime.fromtimestamp(T - 10, timezone.utc).isoformat()
    tick()
    fact = json.loads(rows(path)[0]["facts_json"])
    assert fact["enrolled"] is False and fact["baseline_drop_now"] is None
    assert "pre_observer_position" in fact["censored"]
    p["opened_at"] = datetime.fromtimestamp(T + 1, timezone.utc).isoformat()
    tick(3)
    snap["residual_atomic"] = "500000"
    tick(1)
    fact = json.loads(rows(path)[-1]["facts_json"])
    assert fact["baseline_drop_now"] is None and fact["quantity_changed_since_first"] is True


def test_partial_keeps_fixed_current_quantity_history_eligible(observer):
    now, p, opened, snap, path, tick = observer
    tick(); tick(3)
    snap["residual_atomic"] = "500000"
    tick(1)
    fact = json.loads(rows(path)[-1]["facts_json"])
    assert fact["enrolled"] is True and fact["baseline_drop_now"] is True
    assert fact["quantity_changed_since_first"] is True and fact["residual_atomic"] == "500000"


def test_disabled_cohorts_are_counted_without_source_reads(observer, monkeypatch):
    now, p, opened, snap, path, tick = observer
    p["opened_at"] = datetime.fromtimestamp(T - 1, timezone.utc).isoformat()
    p["_exit_observer_config"] = None
    p["_exit_observer_reason"] = "cohort_off"
    from sniper.services import memecoin_desk_exit_observer_tape as tape
    monkeypatch.setattr(tape, "read_snapshot", lambda *_a, **_k: pytest.fail("disabled cohort read tape"))
    assert tick()["disabled_open"] == 1
    assert rows(path) == []
    opened.clear()
    assert tick(1)["disabled_closed"] == 1


def test_disabled_cohort_is_never_labelled_enrolled(observer):
    now, p, opened, snap, path, tick = observer
    p["_exit_observer_config"] = None
    p["_exit_observer_reason"] = "cohort_off"
    tick(); tick(3)
    assert rows(path, "exit_observer_population")[0]["enrolled"] == 0


def test_source_failure_records_unknown_and_does_not_invent_a_value(observer, monkeypatch):
    now, p, opened, snap, path, tick = observer
    snap.update(rows=[], coverage_from=None, residual_atomic=None, source_reason="capture_unproven")
    monkeypatch.undo()
    tick(); tick(3)
    fact = json.loads(rows(path)[0]["facts_json"])
    assert fact["value_sol"] is None and fact["source_reason"] == "capture_unproven"
    assert all(v is None for grid in fact["flags"].values() for v in grid.values())


def test_bad_retained_quote_is_logged_as_unknown_without_losing_the_denominator(observer):
    now, p, opened, snap, path, tick = observer
    snap["exact_quote"] = {"expected_usdc": float("inf"), "quoted_at": "bad", "input_atomic": b"bad"}
    p["executable_peak_price"] = float("nan")
    tick(); tick(3)
    fact = json.loads(rows(path)[0]["facts_json"])
    assert fact["exact_quote"]["expected_usdc"] is None
    assert fact["exact_quote"]["quoted_at"] == "bad"
    assert fact["executable_peak_price_usdc"] is None
    assert "expected_usdc" in fact["exact_quote_invalid_fields"]


def test_position_identity_change_never_promotes_frozen_enrollment(observer):
    now, p, opened, snap, path, tick = observer
    tick(); tick(3)
    p["desk_cohort_id"] = "OTHER"
    tick(1)
    fact = json.loads(rows(path)[-1]["facts_json"])
    assert fact["enrolled"] is False and fact["baseline_drop_now"] is None
    assert "position_identity_changed" in fact["censored"]


def test_invalid_fee_audit_never_rolls_back_the_denominator(observer):
    now, p, opened, snap, path, tick = observer
    snap["rows"] = [{"slot": 1, "tx_index": 0, "ordinal": 0, "fee_bps": float("inf"),
                     "creator_fee_bps": b"bad", "cashback_fee_bps": 0}]
    tick(); tick(3)
    fact = json.loads(rows(path)[0]["facts_json"])
    assert fact["fee_components"] == {"fee_bps": None, "creator_fee_bps": None, "cashback_fee_bps": 0}
    assert set(fact["fee_invalid_fields"]) == {"fee_bps", "creator_fee_bps"}


def test_observer_cli_never_imports_the_model_adapter(tmp_path, monkeypatch):
    import builtins
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RISK_SHADOW_ENABLED", "0")
    monkeypatch.setenv("MEMECOIN_DESK_EXIT_OBSERVER_ENABLED", "1")
    ledger = tmp_path / "ledger.db"
    ledger.touch()
    calls = []
    monkeypatch.setattr(S, "run_exit_observer_pass", lambda *a, **kw: calls.append((a, kw)) or {})
    old_import = builtins.__import__
    def guarded(name, *a, **kw):
        assert name not in {"memecoin_desk_judge_worker", "claude_agent_sdk"}
        return old_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", guarded)
    assert S.main(["--once", "--exit-observer", "--ledger-db", str(ledger)]) == 0
    assert len(calls) == 1 and calls[0][0][2] == tmp_path / "data/desk/exit_observer.db"


def test_observer_cli_kill_exits_before_ledger_or_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_EXIT_OBSERVER_ENABLED", "0")
    assert S.main(["--once", "--exit-observer"]) == 0
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("missing_partial_signature", [False, True])
@pytest.mark.parametrize("invalid_partial_clock", [False, True])
def test_position_loader_has_one_readonly_snapshot_for_quote_fills_and_cohort(tmp_path, monkeypatch, missing_partial_signature, invalid_partial_clock):
    from sniper.persistence import db_core, db_memecoin as m
    ledger = tmp_path / "ledger.db"
    c = sqlite3.connect(ledger)
    c.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(c)
    config_conn, p = cfg_db()
    envelope = config_conn.execute("SELECT * FROM memecoin_desk_cohorts").fetchone()
    c.execute("INSERT INTO memecoin_desk_cohorts(cohort_id,producer,policy_json,policy_hash,prompt_version,created_at,kill_limit_usd) "
              "VALUES(?,?,?,?,?,?,200)", (*envelope, "p", "2026-10-23T00:00:00+00:00"))
    c.execute("INSERT INTO memecoin_positions(id,mint,entry_price,entry_notional,opened_at,execution_mode,"
              "signal_source,network_id,is_open,desk_cohort_id,funded_contract_json) "
              "VALUES(1,'M',1,25,?,'real','desk','solana',1,'C',?)", ("2026-10-23T00:00:00+00:00", p["funded_contract_json"]))
    c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,created_at,tx_sig) VALUES(1,'entry',25,?,'ours')",
              ("2026-10-23T00:00:00+00:00",))
    c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,created_at,tx_sig) VALUES(1,'partial_exit',1,?,'part')",
              ("2026-10-23T00:00:01+00:00",))
    c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,created_at,tx_sig) VALUES(1,'partial_exit',0,?,'part')",
              ("2026-10-23T00:00:00+00:00",))  # later id is not a later completion
    if missing_partial_signature:
        c.execute("UPDATE memecoin_fills SET tx_sig=NULL WHERE kind='partial_exit'")
    if invalid_partial_clock:
        c.execute("UPDATE memecoin_fills SET created_at=NULL WHERE kind='partial_exit' AND qty=0")
    c.execute("INSERT INTO memecoin_route_series(position_id,mint,network_id,seam,observed_at,expected_usdc) "
              "VALUES(1,'M','solana','exit_decision',?,24)", ("2026-10-23T00:00:02+00:00",))
    c.commit()
    reads = []
    original_ro, original_sigs = db_core.get_readonly_connection, m.funded_fill_signatures
    def ro(path):
        conn = original_ro(path)
        conn.set_trace_callback(reads.append)
        return conn
    def sigs(conn, mint):
        # Concurrent publication cannot tear the observer's position/quote snapshot.
        c.execute("INSERT INTO memecoin_route_series(position_id,mint,network_id,seam,observed_at,expected_usdc) "
                  "VALUES(1,'M','solana','exit_decision','2026-10-23T00:00:03+00:00',99)")
        c.commit()
        return original_sigs(conn, mint)
    monkeypatch.setattr(db_core, "get_readonly_connection", ro)
    monkeypatch.setattr(m, "funded_fill_signatures", sigs)
    loaded = S.open_desk_positions(ledger, observer=True)
    pos, _, _, quote = loaded[0]
    assert pos["_exit_observer_config"] == CFG
    assert pos["_exit_observer_own_signatures"] == (None if missing_partial_signature else ("ours", "part"))
    assert pos["_exit_observer_partial_at"] == ("unproven" if invalid_partial_clock else "2026-10-23T00:00:01+00:00")
    assert quote["expected_usdc"] == 24 and pos["open_qty"] == 24
    assert reads.count("BEGIN") == 1
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in reads)
    c.close()


@pytest.mark.parametrize("count", [2, 501])
def test_closed_between_samples_and_during_outage_is_in_the_denominator(tmp_path, count):
    from sniper.persistence import db_memecoin as m
    ledger, observer = tmp_path / "ledger.db", tmp_path / "observer.db"
    with sqlite3.connect(ledger) as c:
        m.ensure_memecoin_tables(c)
        config_conn, pos = cfg_db()
        envelope = config_conn.execute("SELECT * FROM memecoin_desk_cohorts").fetchone()
        c.execute("INSERT INTO memecoin_desk_cohorts(cohort_id,producer,policy_json,policy_hash,prompt_version,created_at,kill_limit_usd) "
                  "VALUES(?,?,?,?,?,?,200)", (*envelope, "p", datetime.fromtimestamp(T, timezone.utc).isoformat()))
    S.run_exit_observer_pass(ledger, tmp_path, observer, now_fn=lambda: T)
    with sqlite3.connect(ledger) as c:
        for pid in range(1, count + 1):
            c.execute("INSERT INTO memecoin_positions(id,mint,opened_at,execution_mode,signal_source,network_id,"
                      "is_open,desk_cohort_id,funded_contract_json) VALUES(?,? ,?,'real','desk','solana',0,'C',?)",
                      (pid, "M"+str(pid), datetime.fromtimestamp(T+1, timezone.utc).isoformat(), pos["funded_contract_json"]))
    stats = S.run_exit_observer_pass(ledger, tmp_path, observer, now_fn=lambda: T+300)
    assert stats["enabled_closed"] == stats["missed_closed"] == min(count, 500)
    assert stats["census_complete"] is (count <= 500)
    census = rows(observer, "exit_observer_population")
    assert len(census) == min(count, 500) and all(r["sampling_reason"] == "closed_between_samples" for r in census)
    assert all(r["enrolled"] == 1 for r in census)
    assert rows(observer) == []  # no invented historical observations
    caught_up = S.run_exit_observer_pass(ledger, tmp_path, observer, now_fn=lambda: T+301)
    assert caught_up["missed_closed"] == count and caught_up["census_complete"] is True
