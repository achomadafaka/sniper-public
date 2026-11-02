"""Slice C of docs/superpowers/specs/2026-09-30-memecoin-funded-executable-extremes-design.md: every
funded reader of `max_gain_pct` / `time_to_peak_s` is repointed, and the set of files that name
either column is pinned.

On a funded row the two columns are NULL (the normal funded close never wrote them, and the extremes
pass clears the two paths that do). A reader that still took them would show a blank, or worse, a
display number beside executable ones. The funded source is `memecoin_position_exec_extremes`, or
the same aggregate run live for an open position.
"""
import importlib.util
import inspect
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_radar_view as radar

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-07-09T12:00:00+00:00"
SERIES = [("2026-07-09T11:00:02+00:00", 1.40), ("2026-07-09T11:00:04+00:00", 0.60),
          ("2026-07-09T11:00:06+00:00", 0.90)]


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, mint, *, mode="real", network="solana", strategy="alpha_b"):
    return m.open_position(c, source_user_id="u-" + mint, source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=1.0, their_size_usd=0.0, opened_at="2026-07-09T11:00:00+00:00",
                           opened_recorded_at="2026-07-09T11:00:00+00:00", strategy=strategy,
                           execution_mode=mode, network_id=network)


def _series(c, pid, points=SERIES):
    m.record_route_series(c, [
        {"position_id": pid, "mint": "x", "network_id": "solana", "seam": "exit_decision",
         "observed_at": at, "quoted_at": at, "expected_usdc": price * 10.0, "open_qty": 10.0,
         "unit_price": price} for at, price in points])


def _client(c):
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client()


# ------------------------------------------------------------------ 1, 2: the funded cockpit API

@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats_batch_keyed",
                        lambda keys, http=None: {})


def test_the_positions_payload_never_falls_back_to_the_column(offline):
    c = _conn()
    pid = _open(c, "So1")
    c.execute("UPDATE memecoin_positions SET peak_price=NULL, max_gain_pct=77 WHERE id=?", (pid,))
    c.commit()
    row = _client(c).get("/api/memecoin/real/positions").get_json()["positions"][0]
    assert row["max_gain_pct"] is None and row["peak_basis"] == "exact"
    m.update_position_peak(c, pid, 1.5, NOW, 0)        # the DISPLAY peak alone: no max gain (2026-10-17)
    row = _client(c).get("/api/memecoin/real/positions").get_json()["positions"][0]
    assert row["max_gain_pct"] is None and row["peak_basis"] == "exact"
    m.update_position_executable_peak(c, pid, 1.5)
    row = _client(c).get("/api/memecoin/real/positions").get_json()["positions"][0]
    assert row["max_gain_pct"] == pytest.approx(50.0) and row["peak_basis"] == "exact"


def test_the_positions_route_itself_never_falls_back_when_the_ticker_has_no_row(offline, monkeypatch):
    """/real/positions overlays the ticker's fields on each row. Without a ticker row the route's
    own derivation shows, and it must not take the column either."""
    from sniper.routes import api_memecoin as api
    ticker = api._real_ticker_payload
    monkeypatch.setattr(api, "_real_ticker_payload", lambda *a, **k: dict(ticker(*a, **k), positions=[]))
    c = _conn()
    pid = _open(c, "So1")
    c.execute("UPDATE memecoin_positions SET peak_price=NULL, max_gain_pct=77 WHERE id=?", (pid,))
    c.commit()
    row = _client(c).get("/api/memecoin/real/positions").get_json()["positions"][0]
    assert row["max_gain_pct"] is None and row["peak_basis"] == "exact"


def test_the_positions_payload_carries_the_executable_top_and_bottom(offline):
    c = _conn()
    with_series, without = _open(c, "So1"), _open(c, "So2")
    _series(c, with_series)
    rows = {r["id"]: r for r in _client(c).get("/api/memecoin/real/positions").get_json()["positions"]}
    got = {k: rows[with_series][k] for k in ("exec_high_pct", "exec_low_pct", "exec_high_at",
                                             "exec_low_at", "exec_samples")}
    assert got == {"exec_high_pct": pytest.approx(40.0), "exec_low_pct": pytest.approx(-40.0),
                   "exec_high_at": "2026-07-09T11:00:02+00:00",
                   "exec_low_at": "2026-07-09T11:00:04+00:00", "exec_samples": 3}
    assert {rows[without][k] for k in got} == {None}      # unknown, never zero


def test_the_ticker_never_falls_back_to_the_column_and_runs_no_aggregate(offline, monkeypatch):
    c = _conn()
    pid = _open(c, "So1")
    _series(c, pid)
    c.execute("UPDATE memecoin_positions SET peak_price=NULL, max_gain_pct=77 WHERE id=?", (pid,))
    c.commit()
    monkeypatch.setattr(m, "exec_extremes_live",
                        lambda *a, **k: pytest.fail("the 1 s ticker must not run the aggregate"))
    row = _client(c).get("/api/memecoin/real/ticker").get_json()["positions"][0]
    assert row["max_gain_pct"] is None and row["peak_basis"] == "exact"
    assert "exec_high_pct" not in row


# ------------------------------------------------------------------ 4, 5, 6: the radar view

def _episode(c, mint):
    c.execute("INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, started_ts, "
              "status, consensus_cluster_count, first_source_price, first_executable_price, "
              "last_signal_ts) VALUES(?,?,?,?,?,?,?,?,?)",
              ("ep-" + mint, "solana", mint, "2026-07-09T11:00:00+00:00", "open", 1, 1.0, 1.0,
               "2026-07-09T11:58:00+00:00"))


def test_a_funded_hold_shows_the_sell_quote_high_not_the_column():
    c = _conn()
    _episode(c, "HOLD")
    pid = _open(c, "HOLD")
    c.execute("UPDATE memecoin_positions SET last_quote_price=1.2, max_gain_pct=77 WHERE id=?", (pid,))
    c.commit()
    detail = {r["mint"]: r for r in radar.radar_rows(c, now_iso=NOW)}["HOLD"]["us_detail"]
    assert detail == "+20%"                                 # no series yet: no peak part at all
    _series(c, pid)
    detail = {r["mint"]: r for r in radar.radar_rows(c, now_iso=NOW)}["HOLD"]["us_detail"]
    assert detail == "+20% · sell-quote high +40%"
    assert "max_gain_pct" not in inspect.getsource(radar.radar_rows)


def test_the_timeline_gives_a_funded_row_executable_numbers_and_a_paper_row_its_display_one():
    c = _conn()
    funded, paper = _open(c, "MINT"), _open(c, "MINT", mode="paper", strategy="control")
    _series(c, funded)
    for pid in (funded, paper):
        c.execute("UPDATE memecoin_positions SET is_open=0, closed_at='2026-07-09T11:59:00+00:00', "
                  "realized_pnl=1, max_gain_pct=33 WHERE id=?", (pid,))
    c.commit()
    m.upsert_exec_extremes(c, funded, NOW)
    c.commit()
    arms = {a["execution_mode"]: a for a in radar.coin_timeline(c, "solana", "MINT", now_iso=NOW)["arm_positions"]}
    assert (arms["real"]["max_gain_pct"], arms["real"]["max_gain_basis"]) == (None, None)
    assert arms["real"]["exec_high_pct"] == pytest.approx(40.0)
    assert arms["real"]["exec_low_pct"] == pytest.approx(-40.0)
    assert (arms["paper"]["max_gain_pct"], arms["paper"]["max_gain_basis"]) == (33.0, "display")
    assert (arms["paper"]["exec_high_pct"], arms["paper"]["exec_low_pct"]) == (None, None)


# ------------------------------------------------------------------ 7, 8: research

def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "research" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_funded_winners_losers_reads_the_extremes_table(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    with_series, without = _open(c, "A"), _open(c, "B")
    _series(c, with_series)
    for pid in (with_series, without):
        c.execute("INSERT INTO memecoin_entry_attempts(attempt_id, decision_started_at) VALUES(?,?)",
                  ("att-%d" % pid, "2026-07-09T10:59:59+00:00"))
        c.execute("UPDATE memecoin_positions SET is_open=0, closed_at='2026-07-09T11:59:00+00:00', "
                  "realized_pnl=1, attempt_id=?, signal_source='j7', max_gain_pct=33, time_to_peak_s=5 "
                  "WHERE id=?", ("att-%d" % pid, pid))
    c.commit()
    m.exec_extremes_pass(c, NOW)
    c.close()
    script = _script("funded_winners_losers")
    monkeypatch.setattr(script, "LEDGER", path)
    rows = {r["id"]: r for r in script.load("2026-07-01T00:00:00")}
    assert rows[with_series]["exec_high_pct"] == pytest.approx(40.0)
    assert rows[with_series]["exec_low_pct"] == pytest.approx(-40.0)
    assert rows[with_series]["time_to_high_s"] == pytest.approx(2.0)
    assert [rows[without][k] for k in ("exec_high_pct", "exec_low_pct", "time_to_high_s")] == [None] * 3
    assert "max_gain_pct" not in rows[with_series] and "time_to_peak_s" not in rows[with_series]
    assert script.OUTCOME_SIDE == ("exit_reason", "hold_s", "exec_high_pct", "exec_low_pct", "time_to_high_s")


# ------------------------------------------------------------------ 3: the cockpit table

def _exec_range(cases):
    app = (ROOT / "static" / "app.js").read_text()
    start = app.index("    _execRange(p) {")
    helper = app[start:app.index("    _mergeLastKnown(p) {", start)]
    node = shutil.which("node")
    assert node is not None, "node must test the funded cockpit JavaScript"
    script = ("const view = {\n%s};\nconsole.log(JSON.stringify(%s.map(p => view._execRange(p))));"
              % (helper, json.dumps(cases)))
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                     timeout=60, check=True).stdout)


def test_the_cockpit_shows_the_sell_quote_range_and_a_dash_when_it_is_unknown():
    out = _exec_range([{"exec_high_pct": 13.72, "exec_low_pct": -41.04, "exec_samples": 63},
                       {"exec_high_pct": -3.7, "exec_low_pct": -12.0, "exec_samples": 1},
                       {"exec_high_pct": None, "exec_low_pct": None, "exec_samples": None}, {}])
    assert out[0] == {"txt": "+13.7% / -41.0%",
                      "title": "best and worst whole-bag sell quote against the fill (63 quotes); "
                               "net of our own exit impact, so it is not the market peak"}
    assert out[1]["txt"] == "-3.7% / -12.0%" and "(1 quotes)" in out[1]["title"]
    assert out[2] == out[3] == {"txt": "—", "title": "no exit-decision quote recorded yet"}
    app = (ROOT / "static" / "app.js").read_text()
    assert "{ k: 'exec_high_pct', l: 'Sell-quote hi / lo', num: true }" in app
    assert "this._execRangeHtml(p)" in app


# ------------------------------------------------------------------ the guard

# Every file under these trees that names either column, with its class. A new file, or a file that
# changes class, fails here until it is read and classified in the spec's slice C list.
_TREES = ("sniper", "scripts", "static", "templates", "bot")
_NAMES = re.compile(r"max_gain_pct|time_to_peak_s")
CLASSIFIED = {
    # funded rows reach the code, which must not use the column for them
    "sniper/routes/api_memecoin.py": "funded payloads derive the display peak from peak_price",
    "static/app.js": "renders the payload's display peak; also memecoin_signal_outcomes",
    "sniper/services/memecoin_radar_view.py": "timeline: paper rows only, funded forced to None",
    "scripts/research/position_shape.py": "selects both modes, discards the column on funded rows",
    # paper rows only
    "sniper/services/memecoin_paper_report.py": "paper rows only",
    "sniper/services/memecoin_exits.py": "paper writer",
    "sniper/services/memecoin_copy_trader.py": "paper writer",
    "scripts/research/memecoin_conditioning_study.py": "paper rows and memecoin_signal_outcomes",
    "scripts/research/memecoin_real_vs_shadow.py": "the paper shadow arm",
    "scripts/research/memecoin_bigcap_scalp_policy_study.py": "paper rows, a local variable",
    # the schema, both writers, the paper arm summary and memecoin_signal_outcomes
    "sniper/persistence/db_memecoin.py": "schema, writers, clear, paper arm summary, signal outcomes",
    "scripts/memecoin_exec_extremes_history.py": "the operator script that clears them; docstring only",
    # another table
    "sniper/services/memecoin_pump.py": "memecoin_signal_outcomes",
    "scripts/research/memecoin_feature_store.py": "memecoin_signal_outcomes",
    "scripts/research/memecoin_feature_scan.py": "memecoin_signal_features",
    "scripts/research/memecoin_pattern_study.py": "memecoin_signal_outcomes",
    # no column read
    "sniper/research/memecoin_strategy/pattern_paths.py": "computed locally",
    "sniper/analysis/exit_flex_v5.py": "a frozen constant's comment",
    "sniper/analysis/wallet_protocol/position_building.py": "substring of time_to_peak_size_ms",
}


def test_the_files_that_name_the_two_columns_are_the_classified_ones():
    found = set()
    for tree in _TREES:
        for path in (ROOT / tree).rglob("*"):
            if path.suffix in (".py", ".js", ".html", ".sh", ".sql") and path.is_file():
                if _NAMES.search(path.read_text(errors="ignore")):
                    found.add(path.relative_to(ROOT).as_posix())
    found |= {p.name for p in ROOT.glob("*.py") if _NAMES.search(p.read_text(errors="ignore"))}
    assert found == set(CLASSIFIED), (sorted(found - set(CLASSIFIED)), sorted(set(CLASSIFIED) - found))


def test_the_repointed_research_scripts_no_longer_name_the_columns():
    for name in ("funded_winners_losers", "entry_side_money_map"):
        assert not _NAMES.search((ROOT / "scripts" / "research" / (name + ".py")).read_text()), name
