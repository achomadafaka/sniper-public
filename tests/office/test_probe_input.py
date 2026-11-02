"""office_probe_input: newest usable v3 ticket, stale snapshots skipped, hard_filter_row shaped like the sheet
parity test, allow_model_calls off by default."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("office_probe_input", ROOT / "scripts/office_probe_input.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def ticket(tid, cohort):
    return {"ticket_id": tid, "cohort_id": cohort, "opportunity_id": "o" + tid, "candidate": {"mint": tid},
            "stages": [{"stage": "hard_filter", "ts": "T" + tid, "inputs": {"evidence": {"e": tid}}}]}


def fake_store(tickets):
    return SimpleNamespace(
        list_awaiting_judge=lambda conn, limit, newest_first: tickets[:limit] if newest_first else 1 / 0,
        get_cohort=lambda conn, cid: {"policy": {"ps_inputs": "pumpswap-facts-v3" if cid == "D" else "v2",
                                                 "hard_filter": {}}},
        get_intake_source_receipt=lambda conn, oid, **kw: {"source_metadata": {"src": oid}})


def test_picks_newest_refreshable_v3_ticket_and_shapes_the_row():
    store = fake_store([ticket("3", "OLD"), ticket("2", "D"), ticket("1", "D")])          # newest first
    def refresh(c, *, desk_dir, now):
        if c["mint"] == "2":                               # newest v3 is stale: skipped, next one taken
            raise ValueError("stale")
        return dict(c, refreshed=True), {"receipt_cut": 5.0, "cut_slot": 9}
    doc = M.pick(None, desk_dir="/d", now=10.0, limit=50, refresh=refresh, store=store)
    assert doc["candidate"] == {"mint": "1", "refreshed": True} and doc["strategy_epoch"] == "D"
    row = doc["hard_filter_row"]
    assert row == {"verdict": "PASS", "ts": "T1", "evidence": {"e": "1"},
                   "market_cut": {"receipt_cut": 5.0, "cut_slot": 9}, "source_metadata": {"src": "o1"},
                   "emission_candidate": {"mint": "1"}}
    assert doc["decision_id"].startswith("probe-1-") and "allow_model_calls" not in doc


def test_no_usable_ticket_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "pick", lambda *a, **k: None)
    (tmp_path / "data").mkdir()
    import sqlite3
    sqlite3.connect(tmp_path / "data/memecoin_desk.db").close()
    assert M.main(["--state-root", str(tmp_path)]) == 2
    assert not (tmp_path / "data/office/probe/input.json").exists()


def test_writes_input_with_model_calls_off_unless_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "pick", lambda *a, **k: {"decision_id": "probe-x"})
    (tmp_path / "data").mkdir()
    import sqlite3
    sqlite3.connect(tmp_path / "data/memecoin_desk.db").close()
    assert M.main(["--state-root", str(tmp_path)]) == 0
    out = tmp_path / "data/office/probe/input.json"
    assert json.loads(out.read_text())["allow_model_calls"] is False
    M.main(["--state-root", str(tmp_path), "--allow-model-calls"])
    assert json.loads(out.read_text())["allow_model_calls"] is True
