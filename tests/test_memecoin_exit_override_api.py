"""API: operator per-position exit override on the Real tab.

POST /api/memecoin/real/positions/<id>/exit sets/clears the operator override; GET /real/positions
surfaces it as `effective_exit`. Additive to the existing cockpit endpoints.
"""
import sqlite3
import pytest
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "last_quote_price, network_id, is_open, execution_mode, opened_at, strategy) "
              "VALUES(1,'MINTA','AAA',0.001,50.0,0.0012,'solana',1,'real','2026-07-14T00:00:00+00:00','alpha_b')")
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, is_open, execution_mode, opened_at) "
              "VALUES(2,'MINTB','BBB',0.001,1,'paper','2026-07-14T00:00:00+00:00')")
    c.commit()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app), c


def test_set_operator_override_then_visible_on_positions():
    cli, c = _client()
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"tp_pct": 30, "sl_pct": 40, "actor": "me"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    eff = r.get_json()["effective_exit"]
    assert eff["source"] == "operator" and eff["tp_pct"] == 30.0
    assert eff["sl_pct"] == -40.0            # positive 40 accepted, stored as -40% drawdown
    # GET surfaces it
    g = cli.get("/api/memecoin/real/positions").get_json()
    pos1 = next(p for p in g["positions"] if p["id"] == 1)
    assert pos1["effective_exit"]["tp_pct"] == 30.0


def test_target_mcap_override_accepted():
    cli, c = _client()
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"tp_target_mcap": 5_000_000})
    assert r.status_code == 200
    assert r.get_json()["effective_exit"]["tp_target_mcap"] == 5_000_000


def test_bare_trail_pins_tp_and_derisk_off_instead_of_inheriting_runtime_defaults():
    cli, _ = _client()
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"trail_pct": 30})
    assert r.status_code == 200
    eff = r.get_json()["effective_exit"]
    assert eff["trail_pct"] == 30.0
    assert eff["tp_pct"] == 0.0
    assert eff["derisk_pct"] == 0.0


def test_staged_trail_persists_explicit_derisk_threshold():
    cli, _ = _client()
    r = cli.post(
        "/api/memecoin/real/positions/1/exit",
        json={"trail_pct": 30, "derisk_pct": 50},
    )
    assert r.status_code == 200
    assert r.get_json()["effective_exit"]["derisk_pct"] == 50.0


def test_clear_reverts_to_default():
    cli, c = _client()
    cli.post("/api/memecoin/real/positions/1/exit", json={"tp_pct": 30})
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"clear": True})
    assert r.status_code == 200 and r.get_json()["effective_exit"] is None


def test_empty_body_rejected():
    cli, c = _client()
    r = cli.post("/api/memecoin/real/positions/1/exit", json={})
    assert r.status_code == 400


def test_nonfinite_negative_and_conflicting_exit_values_are_rejected():
    cli, _ = _client()
    bad = [
        {"tp_pct": "nan"},
        {"tp_pct": "inf"},
        {"tp_pct": -1},
        {"tp_target_mcap": -5},
        {"sl_pct": 101},
        {"sl_target_mcap": 0},
        {"trail_pct": 0},
        {"trail_pct": 150},   # out of (0,100); 20 is now VALID — real_trail rail live 2026-07-17
        {"trail_pct": 20, "derisk_pct": -1},
        {"tp_pct": 20, "tp_target_mcap": 5_000_000},
        {"sl_pct": 20, "sl_target_mcap": 100_000},
    ]

    for body in bad:
        response = cli.post("/api/memecoin/real/positions/1/exit", json=body)
        assert response.status_code == 400, body


def test_zero_percent_explicitly_disables_the_leg():
    cli, _ = _client()
    response = cli.post(
        "/api/memecoin/real/positions/1/exit", json={"tp_pct": 0, "sl_pct": 0}
    )

    assert response.status_code == 200
    assert response.get_json()["effective_exit"]["tp_pct"] == 0
    assert response.get_json()["effective_exit"]["sl_pct"] == 0


def test_paper_or_missing_position_is_404():
    cli, c = _client()
    assert cli.post("/api/memecoin/real/positions/2/exit", json={"tp_pct": 30}).status_code == 404  # paper
    assert cli.post("/api/memecoin/real/positions/999/exit", json={"tp_pct": 30}).status_code == 404  # missing


# --- ONE take-profit, last-write-wins (2026-07-18): a TP edit supersedes a stale mcap band ---
# Root cause of "KITTY TP won't change": a 07-13 operator band (5.5-6.5M) outranked a 07-18 $4.6M TP
# override in both the /real/positions display (_tpDisplay checks the band first) and the exit sweep.

def _set_band(c, pid, low, high):
    m.set_exit_target(c, pid, low, high, now_iso="2026-07-13T00:00:00+00:00", actor="operator",
                      note="stale band")


def test_tp_edit_supersedes_active_band():
    cli, c = _client()
    _set_band(c, 1, 5_500_000, 6_500_000)
    assert m.active_exit_target(c, 1) is not None
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"tp_target_mcap": 4_600_000, "supersede_band": True})
    assert r.status_code == 200
    assert r.get_json()["band_cancelled"] == 1
    assert m.active_exit_target(c, 1) is None                        # band gone
    # GET now surfaces the $4.6M TP as the position's TP (no band to shadow it)
    g = cli.get("/api/memecoin/real/positions").get_json()
    pos1 = next(p for p in g["positions"] if p["id"] == 1)
    assert pos1["effective_exit"]["tp_target_mcap"] == 4_600_000
    assert pos1["exit_target"] is None


def test_sl_edit_leaves_band_untouched():
    cli, c = _client()
    _set_band(c, 1, 5_500_000, 6_500_000)
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"sl_pct": 40})   # no supersede_band flag
    assert r.status_code == 200
    assert r.get_json()["band_cancelled"] == 0
    assert m.active_exit_target(c, 1) is not None                   # SL edit must not drop the band


def test_clear_also_cancels_band():
    cli, c = _client()
    _set_band(c, 1, 5_500_000, 6_500_000)
    cli.post("/api/memecoin/real/positions/1/exit", json={"tp_pct": 30})
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"clear": True})
    assert r.status_code == 200
    assert m.active_exit_target(c, 1) is None                       # revert-to-default drops the band too


@pytest.mark.parametrize("carried", [{}, {"tp_pct": 0, "trail_pct": 1, "derisk_pct": 10}])
def test_single_leg_edit_preserves_current_exact_other_rails(carried):
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_pct=0.4, sl_pct=-30,
                                 trail_pct=25.5, derisk_pct=0)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={**carried, "edited_leg": "sl", "sl_pct": 35})
    assert r.status_code == 200
    eff = r.get_json()["effective_exit"]
    assert (eff["tp_pct"], eff["sl_pct"], eff["trail_pct"], eff["derisk_pct"]) == (0.4, -35, 25.5, 0)
    assert not c.in_transaction


def test_stale_other_leg_cannot_overwrite_a_newer_session_edit():
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_target_mcap=1_234_567, tp_pct=0, sl_pct=-30)
    assert cli.post("/api/memecoin/real/positions/1/exit",
                    json={"edited_leg": "tp", "tp_pct": 75.5}).status_code == 200
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"edited_leg": "sl", "sl_pct": 35, "tp_target_mcap": 1_200_000,
                       "mcap_basis": "circ"})
    assert r.status_code == 200
    eff = r.get_json()["effective_exit"]
    assert eff["tp_pct"] == 75.5 and eff["tp_target_mcap"] is None
    assert eff["sl_pct"] == -35


@pytest.mark.parametrize("value", [None, 0])
def test_single_leg_default_and_disabled_keep_the_other_rails(value):
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_pct=60, sl_pct=-30, trail_pct=25, derisk_pct=0)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"edited_leg": "tp", "tp_pct": value, "tp_target_mcap": None})
    assert r.status_code == 200
    eff = r.get_json()["effective_exit"]
    assert (eff["tp_pct"], eff["tp_target_mcap"], eff["sl_pct"], eff["trail_pct"], eff["derisk_pct"]) == (
        value, None, -30, 25, 0)


def test_legacy_whole_row_save_still_replaces_omitted_fields():
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", sl_pct=-30, trail_pct=25, derisk_pct=0)
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"tp_pct": 60})
    assert r.status_code == 200
    eff = r.get_json()["effective_exit"]
    assert eff["tp_pct"] == 60 and all(eff[k] is None for k in ("sl_pct", "trail_pct", "derisk_pct"))


def test_single_leg_edit_preserves_template_rails_and_default_can_remove_the_last_override():
    cli, c = _client()
    m.set_position_exit_override(c, 1, "micro", tp_pct=0, sl_pct=-40, trail_pct=25, derisk_pct=50)
    r = cli.post("/api/memecoin/real/positions/1/exit", json={"edited_leg": "tp", "tp_pct": 60})
    eff = r.get_json()["effective_exit"]
    assert (eff["tp_pct"], eff["sl_pct"], eff["trail_pct"], eff["derisk_pct"]) == (60, -40, 25, 50)
    m.clear_position_exit_override(c, 1, "micro")
    m.set_position_exit_override(c, 1, "operator", tp_pct=60)
    r = cli.post("/api/memecoin/real/positions/1/exit",
                 json={"edited_leg": "tp", "tp_pct": None, "tp_target_mcap": None})
    assert r.status_code == 200 and r.get_json()["effective_exit"] is None


@pytest.mark.parametrize("body", [{"edited_leg": "tp", "tp_pct": -1},
                                  {"edited_leg": "tp", "tp_target_mcap": 1_234_567, "mcap_basis": "fdv"}])
def test_rejected_single_leg_edit_releases_its_transaction(body):
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_pct=60, sl_pct=-30)
    r = cli.post("/api/memecoin/real/positions/1/exit", json=body)
    assert r.status_code in (400, 409)
    assert not c.in_transaction
    eff = m.effective_exit_override(c, 1)
    assert (eff["tp_pct"], eff["sl_pct"]) == (60, -30)


@pytest.mark.parametrize("body", [{"edited_leg": "tp", "sl_pct": -10},
                                  {"edited_leg": "sl", "tp_pct": 10},
                                  {"edited_leg": "bad", "tp_pct": 10},
                                  {"edited_leg": None, "tp_pct": 10}])
def test_missing_or_invalid_edited_leg_cannot_reset_other_thresholds(body):
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_pct=60, sl_pct=-30, trail_pct=25)
    r = cli.post("/api/memecoin/real/positions/1/exit", json=body)
    assert r.status_code == 400
    eff = m.effective_exit_override(c, 1)
    assert (eff["tp_pct"], eff["sl_pct"], eff["trail_pct"]) == (60, -30, 25)
    assert not c.in_transaction


def test_simultaneous_edits_to_different_legs_keep_both_changes(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    _, source = _client()
    path = str(tmp_path / "exits.db")
    first = sqlite3.connect(path, check_same_thread=False, timeout=2)
    source.backup(first)
    second = sqlite3.connect(path, check_same_thread=False, timeout=2)
    first.row_factory = second.row_factory = sqlite3.Row
    m.set_position_exit_override(first, 1, "operator", tp_pct=60, sl_pct=-30)
    clients = []
    for connection in (first, second):
        app = Flask(__name__)
        app.register_blueprint(create_api_memecoin_blueprint({"conn": connection}))
        clients.append(csrf_client(app))
    snapshot, started, finished = threading.Event(), threading.Event(), threading.Event()
    original = m.effective_exit_override

    def read(connection, pid):
        value = original(connection, pid)
        if connection is first and not snapshot.is_set():
            snapshot.set()
            assert started.wait(2)
            finished.wait(0.05)
        return value

    monkeypatch.setattr(m, "effective_exit_override", read)

    def save_second():
        started.set()
        try:
            return clients[1].post("/api/memecoin/real/positions/1/exit",
                                   json={"edited_leg": "sl", "sl_pct": 35})
        finally:
            finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(clients[0].post, "/api/memecoin/real/positions/1/exit",
                                 json={"edited_leg": "tp", "tp_pct": 75})
            assert snapshot.wait(2)
            two = workers.submit(save_second)
            assert one.result(timeout=2).status_code == two.result(timeout=2).status_code == 200
        eff = original(first, 1)
        assert (eff["tp_pct"], eff["sl_pct"]) == (75, -35)
        assert not first.in_transaction and not second.in_transaction
    finally:
        source.close()
        first.close()
        second.close()


@pytest.mark.parametrize("signed", [False, True])
@pytest.mark.parametrize("body", [{"edited_leg": "tp", "tp_pct": 80}, {"tp_pct": 80}, {"clear": True}])
def test_an_owned_close_refuses_exit_changes_and_keeps_its_identity(signed, body):
    cli, c = _client()
    m.set_position_exit_override(c, 1, "operator", tp_pct=60, sl_pct=-30, trail_pct=25)
    _set_band(c, 1, 1_000_000, 1_500_000)
    claim = m.claim_position_close(c, 1, "2026-10-19T14:00:00+00:00", "real_tp")
    assert claim
    if signed:
        assert m.mark_position_close_submitted(c, 1, claim, "2026-10-19T14:00:01+00:00", "SIGNED")
    position, override, band = m.get_position(c, 1), m.effective_exit_override(c, 1), m.active_exit_target(c, 1)
    r = cli.post("/api/memecoin/real/positions/1/exit", json=body)
    assert r.status_code == 409
    assert "Exit already in progress" in r.get_json()["error"]
    assert (m.get_position(c, 1), m.effective_exit_override(c, 1), m.active_exit_target(c, 1)) == (
        position, override, band)
    assert not c.in_transaction
