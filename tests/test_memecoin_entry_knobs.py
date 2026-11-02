"""Operator-adjustable entry knobs (2026-09-08).

The cockpit could read the entry thresholds and not move them, so every change meant a `.env` edit
and a restart of a release-pinned real-money process. These tests pin the two properties that make
the overlay safe to hand to an operator:

  1. SELECTION is adjustable, CAPITAL SAFETY is not — the same line `EXEMPTIBLE_GATE_FAMILIES`
     already draws, enforced in one place instead of three.
  2. Every path that is not a valid, in-bounds, adjustable override falls back to the `.env`
     default. A corrupt row, an out-of-bounds row, a locked name and an unreadable table must all
     leave the runner on the value it already ran on.
"""
import sqlite3

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_entry_knobs as K
from sniper.settings import memecoin as cfg


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


@pytest.fixture()
def client(conn, monkeypatch):
    monkeypatch.setattr("sniper.routes.api_memecoin._notify_operator", lambda *args: None)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    cl = app.test_client()
    return cl


def _restore(name):
    """Put the settings module back on its pristine default after a test mutates it."""
    default = K.default_value(cfg, name)
    if default is not None:
        setattr(cfg, name, default)


# --- the selection / safety line -------------------------------------------------------------

def test_capital_safety_knobs_are_not_adjustable():
    for name in ("MEMECOIN_REAL_MAX_EXEC_PREMIUM", "MEMECOIN_ENTRY_IMPACT_CAP_PCT",
                 "MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT", "MEMECOIN_MIN_TRADE_FLOOR_USD"):
        assert name in K.KNOBS, f"{name} must be LISTED so the operator can see it"
        assert name not in K.ADJUSTABLE, f"{name} protects capital and must stay locked"


def test_every_adjustable_gate_family_is_one_the_system_already_treats_as_selection():
    """A knob may not name a gate family that per-wallet exemptions refuse to waive.

    Both surfaces answer "may an operator overrule this judgement". If they disagree, the cockpit
    offers a change the exemption layer would call a safety violation.
    """
    for name in K.ADJUSTABLE:
        family = K.KNOBS[name].gate_family
        if family:
            assert family in m.EXEMPTIBLE_GATE_FAMILIES, (
                f"{name} names {family}, which is not an exemptible selection family")


def test_locked_knob_is_refused_by_the_store_not_only_by_the_route(conn):
    with pytest.raises(ValueError, match="locked"):
        m.set_entry_knob_override(conn, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 9.0,
                                  active=True, actor="t", reason="", now_iso="now")


def test_unknown_knob_is_refused(conn):
    with pytest.raises(ValueError, match="unknown"):
        m.set_entry_knob_override(conn, "MEMECOIN_NOT_A_KNOB", 1,
                                  active=True, actor="t", reason="", now_iso="now")


# --- fail-safe reads -------------------------------------------------------------------------

def test_a_valid_override_reaches_the_settings_module(conn):
    name = "MEMECOIN_REAL_CHASE_MAX_PCT"
    try:
        m.set_entry_knob_override(conn, name, 25, active=True, actor="t", reason="", now_iso="now")
        assert K.apply_overrides(conn, cfg)["changed"][name] == 25.0
        assert getattr(cfg, name) == 25.0
    finally:
        _restore(name)


def test_clearing_an_override_restores_the_env_default_not_the_last_applied_value(conn):
    name = "MEMECOIN_REAL_CHASE_MAX_PCT"
    default = K.default_value(cfg, name)
    try:
        m.set_entry_knob_override(conn, name, 25, active=True, actor="t", reason="", now_iso="now")
        K.apply_overrides(conn, cfg)
        m.set_entry_knob_override(conn, name, None, active=False, actor="t", reason="",
                                  now_iso="now")
        K.apply_overrides(conn, cfg)
        assert getattr(cfg, name) == default
    finally:
        _restore(name)


def test_an_out_of_bounds_row_written_straight_to_the_table_is_ignored_on_read(conn):
    """Bounds are checked on WRITE and on READ. A row that bypassed the route must still be inert."""
    name = "MEMECOIN_REAL_CHASE_MAX_PCT"
    default = K.default_value(cfg, name)
    try:
        conn.execute("INSERT INTO memecoin_entry_knob_overrides(name,value,active) VALUES(?,?,1)",
                     (name, "9999"))
        conn.commit()
        K.apply_overrides(conn, cfg)
        assert getattr(cfg, name) == default
    finally:
        _restore(name)


def test_an_unparseable_row_is_ignored(conn):
    name = "MEMECOIN_REAL_CHASE_MAX_PCT"
    default = K.default_value(cfg, name)
    try:
        conn.execute("INSERT INTO memecoin_entry_knob_overrides(name,value,active) VALUES(?,?,1)",
                     (name, "{{not json"))
        conn.commit()
        K.apply_overrides(conn, cfg)
        assert getattr(cfg, name) == default
    finally:
        _restore(name)


def test_a_locked_name_written_straight_to_the_table_never_reaches_settings(conn):
    name = "MEMECOIN_REAL_MAX_EXEC_PREMIUM"
    default = K.default_value(cfg, name)
    conn.execute("INSERT INTO memecoin_entry_knob_overrides(name,value,active) VALUES(?,?,1)",
                 (name, "99"))
    conn.commit()
    K.apply_overrides(conn, cfg)
    assert getattr(cfg, name) == default


def test_an_unreadable_table_yields_no_overrides_and_does_not_raise():
    class Dead:
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("no such table")

    assert K.read_overrides(Dead()) == {}


# --- the API contract ------------------------------------------------------------------------

def test_the_route_lists_locked_knobs_with_their_reason(client):
    rows = client.get("/api/memecoin/entry-knobs").get_json()["knobs"]
    locked = [r for r in rows if not r["adjustable"]]
    assert locked, "locked knobs must be visible, not hidden"
    assert all(r["locked_reason"] for r in locked)


def test_the_route_reports_default_beside_effective(client):
    rows = {r["name"]: r for r in client.get("/api/memecoin/entry-knobs").get_json()["knobs"]}
    row = rows["MEMECOIN_REAL_CHASE_MAX_PCT"]
    assert row["source"] == "default"
    assert row["default"] == row["value"]


def test_the_route_refuses_a_locked_write_and_an_out_of_bounds_write(client):
    headers = {"X-CSRF-Token": client.get("/api/memecoin/entry-knobs").get_json()["csrf_token"]}
    assert client.post("/api/memecoin/entry-knobs/MEMECOIN_REAL_MAX_EXEC_PREMIUM",
                       json={"value": 9}, headers=headers).status_code == 400
    assert client.post("/api/memecoin/entry-knobs/MEMECOIN_REAL_CHASE_MAX_PCT",
                       json={"value": 99999}, headers=headers).status_code == 400


def test_every_write_leaves_an_audit_row(client, conn):
    headers = {"X-CSRF-Token": client.get("/api/memecoin/entry-knobs").get_json()["csrf_token"]}
    name = "MEMECOIN_REAL_MIN_TXNS_M5"
    try:
        client.post("/api/memecoin/entry-knobs/" + name, json={"value": 4, "reason": "why"}, headers=headers)
        client.post("/api/memecoin/entry-knobs/" + name, json={"active": False}, headers=headers)
        audit = m.entry_knob_audit(conn)
        assert [a["action"] for a in audit][:2] == ["clear", "set"]
        assert any(a["reason"] == "why" for a in audit)
    finally:
        _restore(name)


# --- the runner reads it ---------------------------------------------------------------------

def test_the_entry_path_never_writes_the_settings_module():
    """ONE writer (2026-09-08, Alpha review).

    `real_hook` is handed to several independent daemon threads in the runner loop. Applying the
    overlay from the entry path put every one of them into the settings module. The applier is the
    main cycle now, and the entry path must not write at all.
    """
    import inspect
    from sniper.services import memecoin_real_runner as R
    assert "apply_overrides" not in inspect.getsource(R.real_entry_hook), (
        "the entry path must not apply the overlay — the main cycle is the only writer")
    assert "entry_decision" in inspect.getsource(R.make_real_hook), (
        "a funded buy must register itself so an apply cannot land inside the decision")


def test_every_funded_buy_call_site_registers_as_a_decision():
    """TWO call sites reach `real_entry_hook`, and both must block a knob apply.

    `make_real_hook`'s `_attempt` covers the copy path. `make_real_buy_fn` — the MANUAL buy — calls
    `real_entry_hook` directly and was missed when the overlay shipped, so an operator knob change
    could land inside a manual funded decision (found 2026-09-08 while diagnosing TOPBLAST).
    """
    import inspect
    from sniper.services import memecoin_real_runner as R
    for fn in (R.make_real_hook, R.make_real_buy_fn):
        assert "entry_decision" in inspect.getsource(fn), (
            f"{fn.__name__} reaches real_entry_hook without registering the decision")


def test_the_main_cycle_is_the_writer():
    from pathlib import Path
    loop = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "apply_overrides as _apply_knobs" in loop, "the main cycle must apply the overlay"
    assert loop.count("apply_overrides") == 1, "exactly one writer"


def test_an_apply_is_deferred_while_an_entry_decision_is_in_flight(conn):
    """The property that makes this safe: no funded decision straddles a knob change.

    One writer is necessary and not sufficient — a single writer moving 28 attributes is still not
    atomic to a reader part-way through a decision. So the applier waits for an idle runner.
    """
    name = "MEMECOIN_REAL_CHASE_MAX_PCT"
    default = K.default_value(cfg, name)
    try:
        m.set_entry_knob_override(conn, name, 25, active=True, actor="t", reason="",
                                  now_iso="now")
        with K.entry_decision():
            res = K.apply_overrides(conn, cfg)
            assert res["deferred"] is True
            assert res["changed"] == {}
            assert getattr(cfg, name) == default, "nothing may be written mid-decision"
            assert K.pending_state()["deferred"] is True
        res = K.apply_overrides(conn, cfg)          # runner idle again
        assert res["deferred"] is False
        assert getattr(cfg, name) == 25.0
        assert K.pending_state()["deferred"] is False
    finally:
        _restore(name)


def test_concurrent_decisions_all_have_to_finish_before_an_apply_lands(conn):
    import threading
    name = "MEMECOIN_REAL_MIN_TXNS_M5"
    default = K.default_value(cfg, name)
    release = threading.Event()
    started = threading.Barrier(4)

    def decision():
        with K.entry_decision():
            started.wait(timeout=5)
            release.wait(timeout=5)

    try:
        m.set_entry_knob_override(conn, name, 7, active=True, actor="t", reason="", now_iso="now")
        threads = [threading.Thread(target=decision) for _ in range(3)]
        for t in threads:
            t.start()
        started.wait(timeout=5)
        assert K.apply_overrides(conn, cfg)["deferred"] is True
        assert getattr(cfg, name) == default
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert K.apply_overrides(conn, cfg)["deferred"] is False
        assert getattr(cfg, name) == 7.0
    finally:
        release.set()
        _restore(name)


def test_the_cockpit_renders_the_panel_and_its_controls():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    assert "/api/memecoin/entry-knobs" in js
    for hook in ("data-mc-knob-save", "data-mc-knob-state", "data-mc-knob-revert"):
        assert hook in js, f"{hook} control missing from the cockpit"
    # a blank box must not be written as zero: zero DISABLES several of these gates
    assert "blank is not zero" in js
    # OFF is a switch, never a typed number (2026-09-08, Alpha review): on the chase caps the
    # minimum IS the off value, so typing it reads as "no chasing" and means "chase anything".
    assert "data-mc-knob-off" not in js, "the typed Disable button is the footgun; it must be gone"
    assert "is the OFF value for this gate" in js, "a typed off value must be refused with a reason"


def test_every_knob_whose_minimum_is_its_off_value_is_protected_by_the_switch():
    """The footgun is exactly this shape: minimum == off_value, in a free-text number field."""
    exposed = [k for k in K.KNOBS.values()
               if k.adjustable and k.off_value is not None and k.minimum == k.off_value]
    assert exposed, "the chase caps have this shape; if none do, this test is checking nothing"
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    assert "hasOff && Number(v) === Number(k.off_value)" in js


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("request_case", ["missing", "bad_token", "cross_origin", "cross_site"])
def test_knob_set_and_reset_reject_csrf_before_any_write(client, conn, active, request_case):
    import json

    name = "MEMECOIN_DAILY_LOSS_LIMIT_USD"
    m.set_entry_knob_override(conn, name, 250, active=True, actor="test", reason="", now_iso="now")
    before = K.read_overrides(conn), m.entry_knob_audit(conn)
    token = client.get("/api/memecoin/entry-knobs").get_json()["csrf_token"]
    headers = {"X-CSRF-Token": token}
    if request_case == "missing":
        headers = {}
    elif request_case == "bad_token":
        headers["X-CSRF-Token"] = "invalid"
    elif request_case == "cross_origin":
        headers["Origin"] = "https://untrusted.invalid"
    else:
        headers["Sec-Fetch-Site"] = "cross-site"
    response = client.post("/api/memecoin/entry-knobs/" + name,
                           data=json.dumps({"value": 0, "active": active}),
                           content_type="text/plain", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "csrf rejected"
    assert (K.read_overrides(conn), m.entry_knob_audit(conn)) == before
