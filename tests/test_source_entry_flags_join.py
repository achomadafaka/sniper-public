"""Tier W flags join: selected_wallet / pair_first from memecoin_events identity columns only; absent event is NULL, never false."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("flagsjoin", ROOT / "scripts/research/source_entry_flags_join.py")
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)


def test_flags(tmp_path, monkeypatch):
    db = tmp_path / "l.db"
    c = sqlite3.connect(db)
    c.execute("create table memecoin_events(id integer primary key, side text, mint text, network_id text, created_at text, source_user_id text)")
    c.executemany("insert into memecoin_events values(?,?,?,?,?,?)", [(1, "buy", "M", "solana", "2026-10-01T00:00:00", "u1"), (2, "buy", "M", "solana", "2026-10-01T00:00:05", "u2"),
                                                                    (3, "sell", "M", "solana", "2026-10-01T00:00:09", "u1")])
    c.commit()
    c.close()
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"wallets": [{"uid": "u2"}]}))
    src = tmp_path / "in.jsonl"
    src.write_text("".join(json.dumps({"event_id": i, "swaps_fwd": [1]}) + "\n" for i in (1, 2, 3, 99)))
    out = tmp_path / "out.jsonl"
    monkeypatch.setattr(J, "LEDGER", str(db))
    monkeypatch.setattr(sys, "argv", ["x", str(src), str(out), str(sel)])
    J.main()
    got = {r["event_id"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert (got[1]["selected_wallet"], got[1]["pair_first"]) == (False, True)
    assert (got[2]["selected_wallet"], got[2]["pair_first"]) == (True, False)       # u2's buy is not the earliest on the pair
    assert (got[3]["selected_wallet"], got[3]["pair_first"]) == (False, None)       # a sell is not a first buy
    assert (got[99]["selected_wallet"], got[99]["pair_first"]) == (None, None)      # event missing from the ledger: unknown
    assert got[1]["swaps_fwd"] == [1] and len({r["selected_list_sha"] for r in got.values()}) == 1
