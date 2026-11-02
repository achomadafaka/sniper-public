"""RISK-live engine side: the funded sweep applies the watcher's live rows. TIGHTEN merges per field to
the stricter rail as a desk_risk override (frozen contract untouched, operator override refuses it);
EXIT is nominated only when no rail fired; every decision is acked once, with its lag."""
import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_desk_risk_apply as A
from sniper.services.memecoin_funded_policy import effective_funded_policy
from sniper.settings import memecoin as cfg
from test_memecoin_desk_audit_fixes import _e2e
from test_memecoin_desk_origin import MINT

DDL = ("CREATE TABLE risk_decisions(id INTEGER PRIMARY KEY, cycle_at REAL NOT NULL, call_id INTEGER, "
       "position_id INTEGER NOT NULL, mint TEXT, key TEXT NOT NULL, inputs_hash TEXT, pnl_pct_exact REAL, "
       "tape_state TEXT, action TEXT NOT NULL, rails_json TEXT, reason TEXT, reason_code INTEGER, "
       "call_error TEXT, decided_at REAL, expires_at REAL, live INTEGER NOT NULL, risk_version TEXT NOT NULL, "
       "UNIQUE(cycle_at, position_id))")


@pytest.fixture
def desk(monkeypatch, tmp_path):
    from test_memecoin_desk_origin import BOUNDS
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", cfg.parse_desk_rail_bounds(BOUNDS))
    c, _iid, _seen, _buy = _e2e(monkeypatch)
    pos = dict(c.execute("SELECT * FROM memecoin_positions WHERE signal_source='desk'").fetchone())
    qty = float(pos["entry_notional"]) / float(pos["entry_price"])
    m.insert_fill(c, position_id=pos["id"], kind="entry", price=pos["entry_price"], qty=qty,
                  notional=pos["entry_notional"], fill_source="chain", fraction=1.0,
                  created_at=pos["opened_at"])
    c.execute("UPDATE memecoin_positions SET executable_peak_price=entry_price WHERE id=?", (pos["id"],))
    pos = m.get_position(c, pos["id"])
    _quote(c, pos)
    db = tmp_path / "risk_live.db"
    r = sqlite3.connect(db)
    r.execute(DDL)
    r.commit()
    return c, pos, db, r


def _quote(c, pos, *, price=None, now=None, qty=None, **changes):
    now = time.time() if now is None else now
    at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    qty = m.open_qty(c, pos["id"]) if qty is None else qty
    price = float(pos["entry_price"]) if price is None else price
    row = {"position_id": pos["id"], "mint": pos["mint"], "network_id": pos["network_id"],
           "seam": "exit_decision", "observed_at": at, "quoted_at": at,
           "expected_usdc": price * qty, "open_qty": qty, "unit_price": price, **changes}
    c.execute("DELETE FROM memecoin_route_series WHERE position_id=?", (pos["id"],))
    c.execute("INSERT INTO memecoin_route_series(%s) VALUES (%s)" %
              (",".join(row), ",".join("?" for _ in row)), tuple(row.values()))
    c.commit()
    return row


def _order(r, pos, action, rails=None, *, age=1.0, live=1, mint=None, now=None, reason=None):
    now = time.time() if now is None else now
    cur = r.execute("INSERT INTO risk_decisions(cycle_at, call_id, position_id, mint, key, inputs_hash, action, "
                    "rails_json, decided_at, expires_at, live, risk_version, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now - age + r.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0] * 1e-3, 7,
                     pos["id"], mint or pos["mint"], "k", "h", action,
                     json.dumps(rails) if rails else None, now - age, now - age + 10.0, live, "desk-risk-live-v1", reason))
    r.commit()
    return cur.lastrowid


def _acks(c):
    return {int(r["decision_id"]): dict(r) for r in c.execute("SELECT * FROM memecoin_desk_risk_applies WHERE source='risk_live'")}


def _effective(c, pos):
    return effective_funded_policy(pos, override=m.effective_exit_override(c, pos["id"]))[1]


def test_a_tighten_writes_the_stricter_rail_per_field_and_keeps_the_rest(desk):
    c, pos, db, r = desk
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 12, "tp_pct": 90, "trail_pct": 10})
    assert A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db) is None
    eff = _effective(c, pos)
    assert (eff["stop_pct"], eff["tp_pct"], eff["trail_pct"]) == (12.0, 60.0, 10.0)    # tp 90 is looser: kept 60
    ack = _acks(c)[d]
    assert ack["status"] == "applied" and ack["lag_s"] >= 0.9
    assert json.loads(ack["rails_before_json"])["stop_pct"] == 25.0
    contract = json.loads(pos["funded_contract_json"])["policy"]
    assert contract["stop_pct"] == 25.0                                           # frozen contract untouched


def test_a_wider_rail_is_refused_and_nothing_changes(desk):
    c, pos, db, r = desk
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 40})
    A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db)
    assert _acks(c)[d]["reason"] == "not_stricter" and (m.effective_exit_override(c, pos["id"]) or {}).get("source") != "desk_risk"


def test_an_operator_override_stands_whole(desk):
    c, pos, db, r = desk
    m.set_position_exit_override(c, pos["id"], "operator", sl_pct=30, actor="operator")
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 5})
    A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db)
    assert _acks(c)[d]["reason"] == "operator_override_active"
    assert m.effective_exit_override(c, pos["id"])["source"] == "operator"


@pytest.mark.parametrize("change", [{"live": 0}, {"age": 11.0}, {"mint": "OtherMint11111111111111111111111111111111"}])
def test_shadow_expired_or_wrong_mint_rows_never_act(desk, change):
    c, pos, db, r = desk
    _order(r, pos, "EXIT", **change)
    assert A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db) is None
    assert (m.effective_exit_override(c, pos["id"]) or {}).get("source") != "desk_risk"


def test_an_exit_is_returned_once_and_only_the_newest(desk):
    c, pos, db, r = desk
    old = _order(r, pos, "EXIT", age=3.0)
    new = _order(r, pos, "EXIT", age=1.0)
    order = A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db)
    assert order["decision_id"] == new and _acks(c)[old]["reason"] == "superseded"
    A.ack_exit(c, pos, order, now_epoch=time.time(), now_iso="t", status="applied", reason="nominated")
    assert A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db) is None   # acted once


def test_a_tighten_is_applied_once_across_sweeps(desk):
    c, pos, db, r = desk
    _order(r, pos, "TIGHTEN", {"sl_pct": 12})
    A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db)
    first = m.effective_exit_override(c, pos["id"])
    A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t2", path=db)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides WHERE source='desk_risk'").fetchone()[0] == 1
    assert m.effective_exit_override(c, pos["id"]) == first


def test_the_kill_knob_and_a_missing_file_do_nothing(desk, monkeypatch, tmp_path):
    c, pos, db, r = desk
    _order(r, pos, "EXIT")
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_APPLY", False)
    assert A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=db) is None
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RISK_APPLY", True)
    assert A.apply_orders(c, pos, now_epoch=time.time(), now_iso="t", path=tmp_path / "absent.db") is None
    assert _acks(c) == {}


def test_a_copy_position_is_never_read(desk):
    c, pos, db, r = desk
    _order(r, pos, "EXIT")
    assert A.apply_orders(c, {**pos, "signal_source": "follow_activity"}, now_epoch=time.time(),
                          now_iso="t", path=db) is None


@pytest.mark.parametrize("rails, want", [
    ({"sl_pct": 0}, "rails_malformed"), ({"x": 1}, "rails_malformed"), ({}, "rails_malformed"),
    ({"trail_pct": 20}, "not_stricter")])
def test_tightened_refuses_malformed_or_looser(rails, want):
    assert A.tightened({"stop_pct": 25, "tp_pct": 60, "trail_pct": 15, "derisk_pct": 0}, rails) == (None, want)


def test_tightened_turns_an_off_rail_on():
    out, why = A.tightened({"stop_pct": 25, "tp_pct": 0, "trail_pct": 0, "derisk_pct": 0}, {"trail_pct": 20})
    assert why == "tightened" and out["trail_pct"] == 20.0 and out["tp_pct"] == 0.0


def _sweep(c, monkeypatch, db, price, *, no_preview=False, **quote_fields):
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setattr(A, "risk_db_path", lambda: db)
    calls = []
    quantities = {p["id"]: m.open_qty(c, p["id"]) for p in c.execute("SELECT id FROM memecoin_positions")}
    R.real_exit_sweep(c, {"solana": {
        "preview_exit_fn": lambda p: None if no_preview else {"expected_usdc": quantities[p["id"]] * price,
                                     "input_quantity": quantities[p["id"]], **quote_fields},
        "exit_fn": lambda _c, _p, reason, _now: calls.append(reason) or {"status": "unknown"}}},
        price_fn=lambda _m: {MINT: {"price": price}})
    return calls


@pytest.mark.parametrize("valid", [True, False])
def test_model_tightening_uses_current_sweep_quote_before_applying(desk, monkeypatch, valid):
    c, pos, db, r = desk
    before = m.effective_exit_override(c, pos["id"])
    now = time.time()
    _quote(c, pos, price=pos["entry_price"] * 0.95, now=now - 2)
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 9.75}, now=now)
    quote_fields = {"quoted_at": datetime.fromtimestamp(now, timezone.utc).isoformat()} if valid else {}
    assert _sweep(c, monkeypatch, db, pos["entry_price"] * 0.9, **quote_fields) == []
    assert _acks(c)[d]["status"] == "refused"
    assert _acks(c)[d]["reason"] == ("would_trigger:sl_pct" if valid else "quote_time_invalid")
    assert m.effective_exit_override(c, pos["id"]) == before


@pytest.mark.parametrize("quantity", [None, 1.0])
def test_current_preview_must_prove_its_own_residual_size(desk, monkeypatch, quantity):
    c, pos, db, r = desk
    before = m.effective_exit_override(c, pos["id"])
    now = time.time()
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 12}, now=now)
    assert _sweep(c, monkeypatch, db, pos["entry_price"], input_quantity=quantity,
                  quoted_at=datetime.fromtimestamp(now, timezone.utc).isoformat()) == []
    assert _acks(c)[d]["status"] == "refused"
    assert _acks(c)[d]["reason"] == "quote_quantity_mismatch"
    assert m.effective_exit_override(c, pos["id"]) == before


def test_model_can_apply_safe_current_preview_without_buffered_history(desk, monkeypatch):
    c, pos, db, r = desk
    c.execute("DELETE FROM memecoin_route_series")
    c.commit()
    now = time.time()
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 12}, now=now)
    assert _sweep(c, monkeypatch, db, pos["entry_price"] * 0.95,
                  quoted_at=datetime.fromtimestamp(now, timezone.utc).isoformat()) == []
    assert _acks(c)[d]["status"] == "applied"
    assert m.effective_exit_override(c, pos["id"])["sl_pct"] == 12


def test_model_without_current_preview_refuses_with_a_countable_reason(desk, monkeypatch):
    c, pos, db, r = desk
    before = m.effective_exit_override(c, pos["id"])
    d = _order(r, pos, "TIGHTEN", {"sl_pct": 12})
    assert _sweep(c, monkeypatch, db, pos["entry_price"], no_preview=True) == []
    assert _acks(c)[d]["status"] == "refused"
    assert _acks(c)[d]["reason"] == "no_current_exact_quote"
    assert m.effective_exit_override(c, pos["id"]) == before


def test_the_sweep_sends_a_model_exit_through_the_shared_exit_call(desk, monkeypatch):
    c, pos, db, r = desk
    d = _order(r, pos, "EXIT")
    calls = _sweep(c, monkeypatch, db, 0.0001)                   # flat: no rail fires
    assert calls == [A.EXIT_REASON]
    assert _acks(c)[d]["status"] == "applied"


def test_a_deterministic_rail_wins_over_a_model_exit(desk, monkeypatch):
    c, pos, db, r = desk
    d = _order(r, pos, "EXIT")
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setattr(R, "_exit_anchor_redrive", lambda _pos: "mirror_exit")   # a rail fired this sweep
    calls = _sweep(c, monkeypatch, db, 0.0001)
    assert calls == ["mirror_exit"]
    assert _acks(c)[d]["status"] == "refused" and _acks(c)[d]["reason"].startswith("rail_won:")


def test_an_older_release_still_inserts_and_reads_its_int_ids_on_the_new_table():
    """Gamma LOW3 rollback: the pre-attention release's exact SQL (int decision_id, no source column, IN json_each
    of ints) against the (source, TEXT decision_id) table this release creates."""
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    old_insert = ("INSERT OR IGNORE INTO memecoin_desk_risk_applies(decision_id, position_id, action, status, reason, "
                  "decided_at, applied_at, lag_s, rails_before_json, rails_after_json, call_id, inputs_hash) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")
    row = (7, 1, "TIGHTEN", "applied", "tightened", 1.0, "t", 0.1, None, None, 3, "h")
    c.execute(old_insert, row)
    c.execute(old_insert, row)                                                    # replay: still one row
    old_read = ("SELECT decision_id FROM memecoin_desk_risk_applies WHERE decision_id IN "
                "(SELECT value FROM json_each(?))")
    assert [r[0] for r in c.execute(old_read, (json.dumps([7, 8]),))] == [7]      # untyped column: an int stays an int
    assert c.execute("SELECT COUNT(*), MIN(source) FROM memecoin_desk_risk_applies").fetchone() == (1, "risk_live")
    assert m.desk_risk_acted_ids(c, [7]) == {7}                                   # and the new reader sees it


def test_every_stats_bump_takes_the_lock():
    """Gamma LOW1: STATS is bumped from the writer and the sweep threads; bump waits for _LOCK."""
    import threading
    from sniper.services import memecoin_desk_attention as att
    done = threading.Event()
    before = att.STATS.get("attention_rows", 0)
    with att._LOCK:
        t = threading.Thread(target=lambda: (att.bump("attention_rows"), done.set()))
        t.start()
        assert not done.wait(0.2)                                                 # blocked on the lock
    t.join(2)
    assert done.is_set() and att.STATS["attention_rows"] == before + 1


def test_a_decision_id_has_one_type_per_source():
    """Gamma LOW on f0ca20369: risk_live '7' and 7 are one ack (INTEGER); attention ids stay TEXT."""
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    kw = dict(position_id=1, action="TIGHTEN", status="applied", reason="r", decided_at=1.0, applied_at="t")
    m.record_desk_risk_apply(c, decision_id="7", **kw)
    m.record_desk_risk_apply(c, decision_id=7, **kw)
    m.record_desk_risk_apply(c, decision_id=12345, source="attention_auto", **kw)
    assert c.execute("SELECT source, typeof(decision_id) FROM memecoin_desk_risk_applies ORDER BY source").fetchall() == [
        ("attention_auto", "text"), ("risk_live", "integer")]
    assert m.desk_risk_acted_ids(c, ["7"]) == {"7"} and m.desk_risk_acted_ids(c, ["12345"], source="attention_auto") == {"12345"}
    with pytest.raises(ValueError):
        m.record_desk_risk_apply(c, decision_id="abc", **kw)
