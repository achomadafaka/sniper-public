"""Desk DexScreener context producer: one GET per endpoint, failures recorded, 429 pauses all, rows sourced."""
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("desk_dex", ROOT / "scripts" / "memecoin_desk_dexscreener.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

T = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()
BOOST = [{"chainId": "solana", "tokenAddress": "MintA", "amount": 10}, {"chainId": "base", "tokenAddress": "0xb"}]


def fake(answers):
    calls = []

    def get(url):
        calls.append(url)
        return answers.get(url.replace(D.BASE, ""), (200, [], None))
    return get, calls


def test_one_get_per_endpoint_with_items_sourced_and_stamped(tmp_path):
    get, calls = fake({"/token-boosts/top/v1": (200, BOOST, None),
                       "/metas/trending/v1": (200, {"data": [{"name": "frogs"}]}, None)})
    store = D.Store(tmp_path)
    stats, paused = D.poll_pass(store, get=get, clock=lambda: T)
    store.close()
    assert [c.replace(D.BASE, "") for c in calls] == list(D.ENDPOINTS) and paused == 0.0
    assert stats == {"ok": 6, "failed": 0, "items": 3, "paused": False}
    db = sqlite3.connect(tmp_path / "2026-10-21.db")
    rows = db.execute("SELECT endpoint, chain_id, token_address, source, received_at FROM items ORDER BY rowid").fetchall()
    assert rows[0] == ("/token-boosts/top/v1", "solana", "MintA", "dexscreener", T)
    assert rows[2][1:3] == (None, None)                                   # a meta has no token
    assert json.loads(db.execute("SELECT payload_json FROM items LIMIT 1").fetchone()[0])["amount"] == 10


def test_failures_are_poll_rows_never_empty_successes_and_429_pauses_everything(tmp_path):
    get, calls = fake({"/token-boosts/top/v1": (200, None, "malformed_json"),
                       "/token-boosts/latest/v1": (200, {"weird": 1}, None),
                       "/token-profiles/latest/v1": (429, None, "http_429")})
    store = D.Store(tmp_path)
    stats, paused = D.poll_pass(store, get=get, clock=lambda: T)
    assert stats["failed"] == 3 and paused == T + D.PAUSE_ON_429_S
    assert len(calls) == 3                                                 # stopped at the 429
    again, _ = D.poll_pass(store, get=get, clock=lambda: T + 60, paused_until=paused)
    assert again["paused"] is True and len(calls) == 3                     # nothing sent while paused
    store.close()
    db = sqlite3.connect(tmp_path / "2026-10-21.db")
    assert db.execute("SELECT error, items FROM polls ORDER BY id").fetchall() == \
        [("malformed_json", None), ("unexpected_shape", None), ("http_429", None)]
    assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_day_files_older_than_retention_are_pruned(tmp_path):
    (tmp_path / "2026-09-01.db").write_text("x")
    (tmp_path / "2026-10-01.db").write_text("x")
    store = D.Store(tmp_path)
    D.poll_pass(store, get=fake({})[0], clock=lambda: T)
    store.close()
    assert not (tmp_path / "2026-09-01.db").exists() and (tmp_path / "2026-10-01.db").exists()
