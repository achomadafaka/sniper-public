"""Every demotion-log row must name the epoch AND the rule that decided it.

Prereg 7.2 requires a departing wallet to record why and under which epoch. The file is append-only
and accumulates across weekly epochs, so a row without both cannot be attributed to a run — and the
gate parameters can move between runs, which makes the rule version load-bearing too.
"""
import importlib.util, json, sqlite3, sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "grr_v2", REPO / "scripts" / "research" / "golden_roster_refresh_v2.py")
G = importlib.util.module_from_spec(_spec)
sys.modules["grr_v2"] = G
_spec.loader.exec_module(G)

EPOCH = "2026-08-22T19:13:30Z"


def _signals_db(path):
    """Schema copied from data/runner_signals.db, not invented — the first draft of this fixture
    guessed `first_seen_at` and the refresh reads `created_at`, so it proved nothing."""
    c = sqlite3.connect(path)
    c.executescript("""
      CREATE TABLE runner_signals (id INTEGER PRIMARY KEY, mint TEXT, created_at TEXT,
        mcap_usd REAL, track_peak_x REAL, golden_buyers INTEGER DEFAULT 0);
      CREATE TABLE runner_signal_buyers (signal_id INTEGER, ts REAL, address TEXT, usd REAL);""")
    for i in range(40):
        # 8 of 40 clear 40k (10_000 * 5.0), so the forward base rate is a real 0.2
        c.execute("INSERT INTO runner_signals VALUES (?,?,?,?,?,?)",
                  (i, "m%d" % i, "2026-08-25T00:00:00Z", 10_000.0, 5.0 if i < 8 else 1.0, 0))
        c.execute("INSERT INTO runner_signal_buyers VALUES (?,?,?,?)", (i, 0.0, "ACTIVE", 10.0))
    c.commit()
    return c


def _rows(log):
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


@pytest.fixture()
def run(tmp_path, monkeypatch):
    log = tmp_path / "demotions.jsonl"
    monkeypatch.setattr(G, "DEMOTION_LOG", log)
    monkeypatch.setattr(G, "V2", tmp_path / "v2.json")
    conn = _signals_db(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    from sniper.research.wallet_execution.publication import diagnostic_mode
    with diagnostic_mode():
        G.refresh(conn, epoch=EPOCH, v1_wallets=["ACTIVE", "NEVER_SEEN"], write=True)
    return log


def test_every_row_carries_the_epoch_it_was_decided_under(run):
    rows = _rows(run)
    assert rows, "no rows written — the retained-absent branch did not fire, test is vacuous"
    for r in rows:
        assert r.get("graded_epoch") == EPOCH, f"row without its epoch: {r}"


def test_every_row_carries_the_rule_that_decided_it(run):
    for r in _rows(run):
        assert r.get("rule_version") == G.RULE_VERSION
        p = r.get("rule_params") or {}
        # the VALUES, not just a version string: the log outlives the code that wrote it
        assert p.get("demote_q") == G.DEMOTE_Q and p.get("admit_q") == G.ADMIT_Q
        assert p.get("demote_min_coins") == G.DEMOTE_MIN_COINS
        assert p.get("win_mcap") == G.WIN_MCAP


def test_the_stamp_can_actually_fail(tmp_path, monkeypatch):
    """MUTATION CHECK. Drop the version and the guard above must go red, or it proves nothing."""
    log = tmp_path / "d.jsonl"
    log.write_text(json.dumps({"at": "x", "graded_epoch": EPOCH, "address": "A",
                               "action": "demoted"}) + "\n")
    r = _rows(log)[0]
    assert r.get("rule_version") is None      # the pre-stamp shape...
    with pytest.raises(AssertionError):       # ...must not satisfy the guard
        assert r.get("rule_version") == G.RULE_VERSION


def test_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    """NO LIVE SWAP, and no log growth without --write."""
    log = tmp_path / "d.jsonl"
    monkeypatch.setattr(G, "DEMOTION_LOG", log)
    monkeypatch.setattr(G, "V2", tmp_path / "v2.json")
    conn = _signals_db(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    G.refresh(conn, epoch=EPOCH, v1_wallets=["ACTIVE", "NEVER_SEEN"], write=False)
    assert not log.exists() and not (tmp_path / "v2.json").exists()


def test_the_sealed_roster_is_never_an_output():
    src = (REPO / "scripts" / "research" / "golden_roster_refresh_v2.py").read_text()
    assert 'assert V2 != V1' in src
    assert "V1.write_text" not in src and "V1.open" not in src


def test_an_importing_caller_cannot_write_without_diagnostic_authorization(tmp_path, monkeypatch):
    # Codex night review W11: refresh() wrote the v2 admission roster with no diagnostic flag.
    from sniper.research.wallet_execution.publication import UnsupportedResearch, diagnostic_mode
    log, v2 = tmp_path / "demotions.jsonl", tmp_path / "v2.json"
    monkeypatch.setattr(G, "DEMOTION_LOG", log)
    monkeypatch.setattr(G, "V2", v2)
    conn = _signals_db(tmp_path / "s.db")
    conn.row_factory = sqlite3.Row
    with pytest.raises(UnsupportedResearch):
        G.refresh(conn, epoch=EPOCH, v1_wallets=["ACTIVE", "NEVER_SEEN"], write=True)
    assert not v2.exists() and not log.exists()
    with diagnostic_mode():
        G.refresh(conn, epoch=EPOCH, v1_wallets=["ACTIVE", "NEVER_SEEN"], write=True)
    assert json.loads(v2.read_text())["eligibility"]["eligible"] is False
