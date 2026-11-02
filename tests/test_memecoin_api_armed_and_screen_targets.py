"""Discovery-surface remediation (2026-08-03).

Two behaviours the board previously got wrong:

1. `POST /api/memecoin/screen {mode: candidates}` re-queried the candidate list with the
   CLI's own defaults (min_usd=0, status='new', offset=0), so it screened a different set
   than the filtered/paged board in front of the operator and the visible rows never moved.
2. There was no per-chain funded-authority surface. Since arming went per chain
   (2026-07-30) `real_approved=1, real_chains=NULL` means armed NOWHERE, and nothing on the
   board said so.
"""
import sqlite3
import subprocess

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


class _DoneProcess:
    def poll(self):
        return 0


def _client(tmp_path):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": c,
        "screen_lock_path": str(tmp_path / "screen.lock"),
        "screen_log_path": str(tmp_path / "screen.log"),
    }))
    return csrf_client(app), c


def _candidate(conn, uid, handle, buy_usd, seen):
    conn.execute(
        "INSERT INTO memecoin_candidates(source_user_id,handle,sighting_count,total_buy_usd,"
        "max_buy_usd,first_seen,last_seen,status) VALUES (?,?,1,?,?,?,?,'new')",
        (uid, handle, buy_usd, buy_usd, seen, seen))
    conn.commit()


def test_screen_candidates_targets_the_rows_the_board_shows(monkeypatch, tmp_path):
    cl, c = _client(tmp_path)
    # `small` is the most recent (so it leads the default CLI query) but is below the board's
    # $1k floor; `big` is what the operator is actually looking at.
    _candidate(c, "u-big", "big", 5_000.0, "2026-08-01T00:00:00+00:00")
    _candidate(c, "u-small", "small", 10.0, "2026-08-02T00:00:00+00:00")
    launches = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: (launches.append(a), _DoneProcess())[1])

    r = cl.post("/api/memecoin/screen",
                json={"mode": "candidates", "n": 20, "min_usd": 1000, "status": "new"})

    assert r.status_code == 200 and r.get_json()["targets"] == 1
    argv = launches[0][0]
    assert "--ids" in argv and argv[argv.index("--ids") + 1] == "u-big"
    assert "--candidates" not in argv


def test_screen_candidates_refuses_an_empty_target_set(monkeypatch, tmp_path):
    cl, _c = _client(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not launch an analyzer with no targets")))

    r = cl.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20, "min_usd": 1000})

    assert r.status_code == 409 and r.get_json()["error"] == "no_candidates_match"


def test_screen_status_reports_the_last_analyzer_write(tmp_path):
    cl, c = _client(tmp_path)
    assert cl.get("/api/memecoin/screen").get_json()["last_run"] is None
    m.upsert_trader_analytics(c, {"source_user_id": "u1", "handle": "alice",
                                  "computed_at": "2026-08-01T00:00:00+00:00"})
    body = cl.get("/api/memecoin/screen").get_json()
    assert body["running"] is False
    assert body["last_run"] == "2026-08-01T00:00:00+00:00"


def test_armed_roster_is_per_chain_and_never_pooled(tmp_path):
    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "u2", "bob")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
    m.set_wallet_chain_armed(c, "u2", "4663", True, force=True)

    sol = cl.get("/api/memecoin/armed?chain=solana").get_json()
    rob = cl.get("/api/memecoin/armed?chain=4663").get_json()

    assert sol["chain"] == "solana" and rob["chain"] == "robin"
    assert sol["armed_count"] == 1 and rob["armed_count"] == 1
    assert {r["handle"] for r in sol["rows"] if r["armed_here"]} == {"alice"}
    assert {r["handle"] for r in rob["rows"] if r["armed_here"]} == {"bob"}
    # Both wallets are listed on both chains — a wallet armed elsewhere must stay visible,
    # otherwise "armed nowhere here" reads as "not on the roster".
    assert sol["roster_count"] == 2 and rob["roster_count"] == 2
    assert cl.get("/api/memecoin/armed").status_code == 400


def test_armed_roster_shows_approved_but_armed_nowhere(tmp_path):
    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")
    # The exact state every pre-2026-07-30 approval was left in: approved, real_chains NULL.
    c.execute("UPDATE memecoin_wallets SET real_approved=1, real_chains=NULL "
              "WHERE source_user_id='u1'")
    c.commit()

    body = cl.get("/api/memecoin/armed?chain=solana").get_json()

    assert body["approved_count"] == 1 and body["armed_count"] == 0
    row = body["rows"][0]
    assert row["real_approved"] is True          # the flag says yes...
    assert row["armed_here"] is False and row["armed_chains"] == []   # ...authority says nowhere
    assert row["effective"] is False


def test_armed_roster_reports_real_record_for_this_chain_only(tmp_path):
    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
    for net, pnl in (("solana", 12.5), ("4663", -40.0)):
        c.execute(
            "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
            "is_open,realized_pnl,opened_at,closed_at,strategy) "
            "VALUES ('u1','mint-' || ?,?, 'real',0,?,?,?,'alpha_b')",
            (net, net, pnl, "2026-08-01T00:00:00+00:00", "2026-08-01T01:00:00+00:00"))
    c.commit()

    sol = cl.get("/api/memecoin/armed?chain=solana").get_json()["rows"][0]
    rob = cl.get("/api/memecoin/armed?chain=4663").get_json()["rows"][0]

    assert sol["real"]["realized_pnl"] == 12.5 and sol["real"]["closed_count"] == 1
    assert rob["real"]["realized_pnl"] == -40.0
    # A wallet with no closed paper arm reports None, never a fabricated zero-PnL "best arm".
    assert sol["best_arm"] is None


def test_armed_roster_reports_whether_the_best_arm_column_is_load_bearing(tmp_path, monkeypatch):
    """`best_arm` is a report by default and CHOOSES REAL EXIT RAILS once the copy-arm policy is
    on. Same number, different consequence, so the board has to be told which regime it is in —
    a column that silently changes meaning is the 2026-08-03 badge trap again."""
    from sniper.settings import memecoin as cfg

    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", False)
    assert cl.get("/api/memecoin/armed?chain=solana").get_json()["copy_arm_policy"] is False

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", True)
    assert cl.get("/api/memecoin/armed?chain=solana").get_json()["copy_arm_policy"] is False


def _matrix_client(tmp_path):
    return _client(tmp_path)


def test_open_exposure_failure_reports_unavailable_not_zero_inventory(tmp_path, monkeypatch):
    """An empty dict renders as "every wallet holds nothing", which is a CLAIM. The previous
    defect in this area was exactly "API returned it, UI never rendered it" — so assert the flag
    the UI keys on, not just the absence of a crash."""
    from sniper.services import memecoin_paper_report as rep

    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")

    def boom(conn, chain):
        raise RuntimeError("exposure exploded")

    monkeypatch.setattr(rep, "open_exposure_by_wallet", boom)
    payload = cl.get("/api/memecoin/traders/arm-matrix?chain=solana").get_json()
    assert payload["open_exposure_available"] is False
    assert payload["open_marked_total"] is None
    for row in payload["wallets"]:
        assert row["open_marked_pnl"] is None      # unknown, never 0.0


def test_arm_matrix_reports_an_exactly_zero_marked_total_rather_than_hiding_it(tmp_path):
    """An exactly-zero marked total is a real answer. The caption used JS truthiness and vanished
    on it — so this builds a REAL open position whose mark equals entry (PnL exactly 0.0) instead
    of asserting against an empty ledger, which would pass even if the bug were still present."""
    cl, c = _client(tmp_path)
    m.add_wallet(c, "u1", "alice")
    pid = m.open_position(
        c, source_user_id="u1", source_handle="alice", mint="MINT", ticker="T",
        entry_price=0.001, entry_notional=100.0, their_size_usd=10.0,
        opened_at="2026-08-04T00:00:00+00:00", strategy="control", network_id="solana",
        origin_event_id="ev1", entry_fill_source="route_quote", entry_lag_s=0.5)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-04T00:00:00+00:00")
    c.execute("UPDATE memecoin_positions SET last_quote_price=0.001 WHERE id=?", (pid,))
    c.commit()

    payload = cl.get("/api/memecoin/traders/arm-matrix?chain=solana").get_json()
    assert payload["open_exposure_available"] is True
    assert payload["open_scope"] == "whole_ledger"
    assert payload["open_marked_total"] == 0.0     # a real zero, NOT None and NOT hidden
    assert "open_only" in payload


def test_ui_actually_executes_the_open_exposure_render_branches():
    """A field-name grep passes even when the branch is unreachable — the original defect here was
    "API returned open_only, the board never drew it". Execute the real template strings against
    each payload shape instead."""
    import re
    from pathlib import Path

    app = Path("static/app.js").read_text()

    # open_only must be rendered inside a table body, not merely mentioned.
    section = re.search(r"\(d\.open_only \|\| \[\]\)\.length \?(.{0,1200})", app, re.S)
    assert section, "open_only has no render branch"
    assert "open_marked_pnl" in section.group(1) and "<tbody>" in section.group(1)

    # The zero-total caption must test `!= null`, never truthiness.
    assert "d.open_marked_total != null" in app, "caption hides an exactly-zero total"
    # The unavailable branch must render something other than a number.
    assert "open_exposure_available !== false" in app
    assert "Open inventory unavailable" in app


def test_armed_board_blocks_legacy_paper_rails_even_at_old_floor(tmp_path,monkeypatch):
    from sniper.services import memecoin_paper_report as rep
    cl,c=_client(tmp_path);m.add_wallet(c,'u1','alice')
    m.set_wallet_chain_armed(c,'u1','solana',True,force=True)
    monkeypatch.setattr(rep,'wallet_arm_cells_from_forward_cohort',lambda *a: (_ for _ in ()).throw(AssertionError('obsolete projection used')))
    payload=cl.get('/api/memecoin/armed?chain=solana').get_json()
    assert payload['rows'][0]['funded_arm'] is None
    assert payload['rows'][0]['funded_arm_available'] is True
    assert payload['copy_arm_policy'] is False
