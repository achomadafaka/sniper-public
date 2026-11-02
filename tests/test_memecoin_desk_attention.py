"""Desk engine attention, runner side (pre-registration 2026-10-22): quote_collapse rows (fired / dry /
unknown) in data/desk/attention/runner.db, the ONE auto-tighten function, the attention_auto rule via
risk_apply, and the (source, decision_id) ack namespace."""
import json
import sqlite3
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_attention as att
from sniper.services import memecoin_desk_risk_apply as A
from sniper.services.memecoin_funded_policy import effective_funded_policy
from sniper.settings import memecoin as cfg
from test_memecoin_desk_risk_apply import desk, _quote  # noqa: F401  (fixture: real e2e desk position)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    att._LAST.clear()
    att.flush()
    for k in ("N_S", "Q_PCT", "Q2_PCT", "S_PCT"):
        monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_" + k, None)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_THRESHOLDS_VERSION", "")


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_" + k, v)


def _rows(path):
    assert att.flush()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM attention ORDER BY observed_at")]


POS = {"id": 7, "mint": "M", "signal_source": "desk", "executable_peak_price": 2.0}


def test_nothing_runs_while_every_knob_is_unset(tmp_path):
    db = tmp_path / "runner.db"
    att.on_exact_price(POS, 1.0, now_epoch=100.0, path=db)
    assert att.flush() and not db.exists()


def test_q_above_q2_disables_q2(monkeypatch):
    _set(monkeypatch, Q_PCT=40.0, Q2_PCT=30.0)
    assert att.thresholds()["Q2_pct"] is None


def test_dry_mode_records_the_measured_drop_with_fired_null(tmp_path, monkeypatch):
    _set(monkeypatch, S_PCT=5.0)                                  # armed, but Q/N unset: dry rows
    db = tmp_path / "runner.db"
    att.on_exact_price(POS, 1.0, now_epoch=100.0, path=db)
    att.on_exact_price(POS, 0.8, now_epoch=103.0, path=db)
    first, second = _rows(db)
    assert json.loads(first["value_json"])["unknown"] == "no_prior_exact" and first["fired"] is None
    v = json.loads(second["value_json"])
    assert v["drop_pct"] == pytest.approx(20.0) and v["peak_drawdown_pct"] == pytest.approx(-60.0)
    assert second["fired"] is None and second["thresholds_version"] == "unset"


def test_with_thresholds_a_drop_at_q_fires_and_a_stale_prior_is_unknown(tmp_path, monkeypatch):
    _set(monkeypatch, N_S=10.0, Q_PCT=15.0, THRESHOLDS_VERSION="v1")
    db = tmp_path / "runner.db"
    for t, p in ((100.0, 1.0), (103.0, 0.85), (106.0, 0.84), (120.0, 0.5)):
        att.on_exact_price(POS, p, now_epoch=t, path=db)
    rows = _rows(db)
    assert [r["fired"] for r in rows] == [None, 1, 0, None]
    assert json.loads(rows[3]["value_json"])["unknown"] == "stale_mark"


def test_a_copy_position_writes_nothing_and_a_missing_price_is_unknown(tmp_path, monkeypatch):
    _set(monkeypatch, S_PCT=5.0)
    db = tmp_path / "runner.db"
    att.on_exact_price({**POS, "signal_source": "follow_activity"}, 1.0, now_epoch=1.0, path=db)
    assert att.flush() and not db.exists()
    att.on_exact_price(POS, None, now_epoch=2.0, path=db)
    assert json.loads(_rows(db)[0]["value_json"])["unknown"] == "no_exact_price"


def test_the_event_id_is_stable_so_a_restart_replays_nothing(tmp_path, monkeypatch):
    _set(monkeypatch, S_PCT=5.0)
    db = tmp_path / "runner.db"
    att.on_exact_price(POS, 1.0, now_epoch=100.2, path=db)
    att._LAST.clear()
    att.flush()
    att.on_exact_price(POS, 1.0, now_epoch=100.7, path=db)                 # same second bucket
    assert len(_rows(db)) == 1


def test_an_unwritable_db_is_counted_never_raised(tmp_path, monkeypatch):
    _set(monkeypatch, S_PCT=5.0)
    before = att.STATS["attention_insert_dropped"]
    (tmp_path / "blocker").write_text("x")
    att.on_exact_price(POS, 1.0, now_epoch=1.0, path=tmp_path / "blocker" / "runner.db")
    assert att.flush() and att.STATS["attention_insert_dropped"] == before + 1 and att.latency_ms()["n"] >= 1
    assert not (tmp_path / "blocker" / "runner.db").exists()


@pytest.mark.parametrize("eff, price, want", [
    ({"stop_pct": 25, "trail_pct": 15}, 0.9, {"trail_pct": 8.0, "sl_pct": pytest.approx(14.5)}),   # 0.9*0.95=0.855
    ({"stop_pct": 10, "trail_pct": 15}, 0.9, {"trail_pct": 8.0}),                          # current stop stricter
    ({"stop_pct": 25, "trail_pct": 15}, 1.2, {"trail_pct": 8.0}),                          # above entry: trail only
    ({"stop_pct": 25, "trail_pct": 5}, 0.9, {"sl_pct": pytest.approx(14.5)}),              # trail already tighter
])
def test_the_one_tighten_function(eff, price, want):
    assert att.auto_tighten_rails(eff, price=price, entry=1.0, s_pct=5.0, trail_min=8.0) == want


def test_auto_trigger_needs_creator_and_burst_within_ten_seconds_or_q2():
    c = {"id": "c", "signal": "creator_sell", "observed_at": 100.0}
    b = {"id": "b", "signal": "sell_burst", "observed_at": 109.0}
    late = {"id": "l", "signal": "sell_burst", "observed_at": 111.0}
    assert A.auto_trigger([c, b], [], q2_pct=None) == (c, b)
    assert A.auto_trigger([c, late], [], q2_pct=None) is None
    assert A.auto_trigger([b], [], q2_pct=None) is None
    q = {"id": "q", "signal": "quote_collapse", "observed_at": 1.0, "value_json": json.dumps({"drop_pct": 31})}
    assert A.auto_trigger([], [q], q2_pct=30) == (q, q) and A.auto_trigger([], [q], q2_pct=32) is None


def _tape(tmp_path, rows):
    db = tmp_path / "tape.db"
    c = sqlite3.connect(db)
    c.execute(att.DDL)
    c.executemany("INSERT INTO attention VALUES (?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return db


def _auto_rows(c):
    return [dict(r) for r in c.execute("SELECT * FROM memecoin_desk_risk_applies WHERE source='attention_auto'")]


def test_the_auto_rule_tightens_through_risk_apply_once_with_its_evidence(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1")])
    entry = float(pos["entry_price"])
    _quote(c, pos, now=now - 1, price=entry * 0.95)
    merged = A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=entry * 0.95, tape_path=tape,
                          runner_path=tmp_path / "none.db")
    eff = effective_funded_policy(pos, override=m.effective_exit_override(c, pos["id"]))[1]
    assert merged is not None and eff["trail_pct"] == 8.0 and eff["stop_pct"] == pytest.approx(9.75)
    [ack] = _auto_rows(c)
    detail = json.loads(ack["detail_json"])
    assert ack["decision_id"] == "b1" and ack["status"] == "applied"
    assert detail["events"] == ["c1", "b1"] and detail["thresholds_version"] == "v1" and detail["rule"] == A.AUTO_RULE
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t2", price=entry * 0.9, tape_path=tape,
                        runner_path=tmp_path / "none.db") is None and len(_auto_rows(c)) == 1
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 1


@pytest.mark.parametrize("case", ["dry", "kill", "operator", "copy"])
def test_the_auto_rule_never_acts_dry_killed_under_an_operator_or_on_copy(desk, monkeypatch, tmp_path, case):  # noqa: F811
    c, pos, _db, _r = desk
    if case != "dry":
        _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    if case == "kill":
        monkeypatch.setattr(cfg, "MEMECOIN_DESK_ATTENTION_AUTO_TIGHTEN", False)
    if case == "operator":
        m.set_position_exit_override(c, pos["id"], "operator", sl_pct=30, actor="operator")
    p = {**pos, "signal_source": "follow_activity"} if case == "copy" else pos
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1")])
    assert A.apply_auto(c, p, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9,
                        tape_path=tape, runner_path=tmp_path / "none.db") is None
    src = (m.effective_exit_override(c, pos["id"]) or {}).get("source")
    assert src != "desk_risk"
    if case == "operator":
        assert _auto_rows(c)[0]["reason"] == "operator_override_active"


def test_an_unfired_or_absent_tape_is_no_action(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", '{"unknown":"tape_gap"}', now - 5, "tape", None, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", '{"unknown":"tape_gap"}', now - 3, "tape", None, "v1")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=1.0, tape_path=tape,
                        runner_path=tmp_path / "none.db") is None
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=1.0, tape_path=tmp_path / "absent.db",
                        runner_path=tmp_path / "none.db") is None and _auto_rows(c) == []


def test_a_v1_ack_table_migrates_to_the_source_namespace(tmp_path):
    db = tmp_path / "l.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE memecoin_desk_risk_applies(decision_id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, "
              "action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, decided_at REAL, applied_at TEXT NOT NULL, "
              "lag_s REAL, rails_before_json TEXT, rails_after_json TEXT, call_id INTEGER, inputs_hash TEXT)")
    c.execute("INSERT INTO memecoin_desk_risk_applies(decision_id, position_id, action, status, applied_at) "
              "VALUES (5, 1, 'EXIT', 'applied', 't')")
    c.commit()
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    [row] = [dict(r) for r in c.execute("SELECT * FROM memecoin_desk_risk_applies")]
    assert (row["source"], row["decision_id"]) == ("risk_live", 5)   # untyped column keeps the int (rollback reads)
    assert m.desk_risk_acted_ids(c, [5]) == {5} and m.desk_risk_acted_ids(c, ["5"], source="attention_auto") == set()


@pytest.mark.parametrize("valid", [True, False])
def test_the_sweep_writes_the_row_and_runs_the_auto_rule_on_its_exact_price(desk, monkeypatch, tmp_path, valid):  # noqa: F811
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_desk_origin import MINT
    c, pos, db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1")])
    runner = tmp_path / "runner.db"
    monkeypatch.setattr(att, "runner_db_path", lambda: runner)
    monkeypatch.setattr(A, "tape_db_path", lambda: tape)
    qty = float(pos["entry_notional"]) / float(pos["entry_price"])
    monkeypatch.setattr(A, "risk_db_path", lambda: db)
    c.execute("DELETE FROM memecoin_route_series")
    c.commit()
    from datetime import datetime, timezone
    quoted_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    R.real_exit_sweep(c, {"solana": {
        "preview_exit_fn": lambda _p: None if not valid else {"expected_usdc": float(pos["entry_notional"]) * 0.9,
                                      "input_quantity": qty, "quoted_at": quoted_at},
        "exit_fn": lambda *a: {"status": "unknown"}}}, price_fn=lambda _m: {MINT: {"price": float(pos["entry_price"]) * 0.9}})
    if valid:
        assert _rows(runner), _rows(runner)
    assert _auto_rows(c)
    assert _auto_rows(c)[0]["status"] == ("applied_partial" if valid else "refused")
    if not valid:
        assert _auto_rows(c)[0]["reason"] == "no_current_exact_quote"
    assert m.effective_exit_override(c, pos["id"])["sl_pct"] == pytest.approx(14.5 if valid else 25)


def test_rows_of_another_position_or_outside_the_window_never_act(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"] + 1, "Other", "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"] + 1, "Other", "sell_burst", "{}", now - 2, "tape", 1, "v1"),
                            ("c2", pos["id"], pos["mint"], "creator_sell", "{}", now - 300, "tape", 1, "v1"),
                            ("b2", pos["id"], pos["mint"], "sell_burst", "{}", now - 295, "tape", 1, "v1")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9,
                        tape_path=tape, runner_path=tmp_path / "none.db") is None and _auto_rows(c) == []



def test_a_locked_db_never_delays_the_sweep_thread(tmp_path, monkeypatch):
    _set(monkeypatch, S_PCT=5.0)
    """Gamma BLOCK: hold a write lock 200 ms; on_exact_price must return in < 1 ms (p90 over calls)."""
    import threading
    db = tmp_path / "runner.db"
    att.on_exact_price(POS, 1.0, now_epoch=1.0, path=db)
    assert att.flush()
    holder = sqlite3.connect(db, timeout=5, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.2, holder.rollback)
    release.start()
    took = []
    for i in range(50):
        t0 = time.perf_counter()
        att.on_exact_price(POS, 1.0 - i * 0.001, now_epoch=2.0 + i, path=db)
        took.append((time.perf_counter() - t0) * 1000.0)
    release.join()
    took.sort()
    assert took[int(0.9 * len(took))] < 1.0, took
    assert att.flush(10)


def test_q2_acts_on_the_drop_even_when_the_row_did_not_fire_at_q(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, Q2_PCT=30.0, N_S=10.0, THRESHOLDS_VERSION="v1")      # Q unset: rows never fire
    now = time.time()
    runner = _tape(tmp_path, [("q1", pos["id"], pos["mint"], "quote_collapse", json.dumps({"drop_pct": 35.0}),
                               now - 1, "runner", None, "v1")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9,
                        tape_path=tmp_path / "absent.db", runner_path=runner) is not None


def test_a_failing_ack_rolls_the_override_back(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1")])
    monkeypatch.setattr(m, "record_desk_risk_apply", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9,
                        tape_path=tape, runner_path=tmp_path / "none.db") is None
    c.commit()
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 0


def test_detection_time_is_the_receipt_cut_not_observed_at(desk, monkeypatch, tmp_path):  # noqa: F811
    """Gamma contract (1): an absence event carries a stale observed_at; its detection time is value.receipt_cut.
    Pairing, the look-back window and decided_at all use it."""
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    cut = lambda t: json.dumps({"receipt_cut": t})
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", cut(now - 5), now - 600, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", cut(now - 2), now - 2, "tape", 1, "v1")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9, tape_path=tape,
                        runner_path=tmp_path / "none.db") is not None
    [ack] = _auto_rows(c)
    assert json.loads(ack["detail_json"])["events"] == ["c1", "b1"]
    old = {"id": "o", "signal": "creator_sell", "observed_at": 100.0, "value_json": cut(300.0)}
    burst = {"id": "b", "signal": "sell_burst", "observed_at": 305.0, "value_json": cut(305.0)}
    assert A.auto_trigger([old, burst], [], q2_pct=None) == (old, burst)        # 5 s apart by receipt cut


def test_a_tape_unknown_row_blinds_every_tape_signal(desk, monkeypatch, tmp_path):  # noqa: F811
    """Gamma contract (2): signal 'tape' unknown = unknown for EVERY signal of that position: creator+burst
    cannot fire; a per-signal unknown blinds that signal; the runner's own quote_collapse is not tape."""
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    unk = json.dumps({"state": "unknown", "reason": "tape_gap", "receipt_cut": now - 3})
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1"),
                            ("u1", pos["id"], pos["mint"], "tape", unk, now - 3, "tape", None, "v1")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9, tape_path=tape,
                        runner_path=tmp_path / "none.db") is None and _auto_rows(c) == []
    cr = {"id": "c", "signal": "creator_sell", "observed_at": 100.0, "fired": 1}
    bu = {"id": "b", "signal": "sell_burst", "observed_at": 101.0, "fired": 1}
    blind_creator = {"id": "u", "signal": "creator_sell", "observed_at": 100.0, "fired": None,
                     "value_json": json.dumps({"state": "unknown", "reason": "tape_gap_since_open"})}
    assert A.auto_trigger([cr, bu, blind_creator], [], q2_pct=None) is None
    q = {"id": "q", "signal": "quote_collapse", "observed_at": 1.0, "value_json": json.dumps({"drop_pct": 31})}
    tape_unknown = {"id": "t", "signal": "tape", "observed_at": 1.0, "fired": None, "value_json": unk}
    assert A.auto_trigger([tape_unknown], [q], q2_pct=30) == (q, q)


def test_a_tape_version_that_drifts_from_the_runner_version_never_tightens(desk, monkeypatch, tmp_path):  # noqa: F811
    c, pos, _db, _r = desk
    _set(monkeypatch, S_PCT=5.0, THRESHOLDS_VERSION="v1")
    now = time.time()
    tape = _tape(tmp_path, [("c1", pos["id"], pos["mint"], "creator_sell", "{}", now - 5, "tape", 1, "v1x"),
                            ("b1", pos["id"], pos["mint"], "sell_burst", "{}", now - 2, "tape", 1, "v1x")])
    assert A.apply_auto(c, pos, now_epoch=now, now_iso="t", price=float(pos["entry_price"]) * 0.9, tape_path=tape,
                        runner_path=tmp_path / "none.db") is None
    [ack] = _auto_rows(c)
    detail = json.loads(ack["detail_json"])
    assert (ack["status"], ack["reason"]) == ("refused", "thresholds_version_mismatch")
    assert detail["thresholds_version"] == "v1" and detail["event_thresholds_versions"] == ["v1x"]
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 0
