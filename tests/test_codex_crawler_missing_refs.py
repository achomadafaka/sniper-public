"""Lost already-ingested evidence is unreadable, not an empty successful crawl."""
import json
import sqlite3

import pytest

import test_memecoin_desk_crawler as F  # noqa: E402
from sniper.services import memecoin_desk_crawler as C


@pytest.mark.parametrize("loss", ["deleted_row", "renamed_dayfile"])
def test_lost_ingested_meta_is_missing_without_a_model_call(tmp_path, loss):
    desk = tmp_path / "desk"
    x_dir = desk / "x"
    x_dir.mkdir(parents=True)
    dayfile = x_dir / (F.DAY + ".db")
    src = sqlite3.connect(dayfile)
    src.execute("CREATE TABLE posts(id INTEGER PRIMARY KEY,event TEXT,tweet_id TEXT,received_at REAL,"
                "handle TEXT,payload_json TEXT)")
    src.execute("CREATE TABLE token_meta(id INTEGER PRIMARY KEY,tweet_id TEXT,mint TEXT,chain TEXT,"
                "symbol TEXT,received_at REAL,payload_json TEXT)")
    src.execute("INSERT INTO token_meta(id,mint,chain,symbol,received_at,payload_json) "
                "VALUES(1,?,NULL,'NEW',?,?)",
                (F.MINT, F.T, json.dumps({"tokenMeta": {"name": "New coin"}})))
    src.commit()
    src.close()
    assert not (desk / "launches").exists()  # launch metadata cannot mask lost J7 evidence
    db = C.connect(tmp_path / "crawls.db")
    try:
        assert F.W.ingest(db, x_dir, desk, F.T + 1) == 1
        assert db.execute("SELECT kind,day,ref_id FROM inputs").fetchall() == [("meta", F.DAY, 1)]
        posts, metas, ids, unreadable = F.W.evidence(db, x_dir, desk, F.MINT, F.T + 1)
        assert not posts and metas == [("j7_token_meta", "NEW", "New coin")] and unreadable == 0

        if loss == "deleted_row":
            src = sqlite3.connect(dayfile)
            src.execute("DELETE FROM token_meta WHERE id=1")
            src.commit()
            src.close()
        else:
            dayfile.rename(dayfile.with_suffix(".moved"))
        calls = []
        def model(system, user):
            calls.append(user)
            return {"text": F.answer(intent="analysis"), "model_echo": C.MODEL}
        stats = F.W.crawl_pass(db, x_dir=x_dir, desk_dir=desk, call_model=model,
                              per_hour=40, per_day=600, now=F.T + 10, clock=lambda: F.T + 20)
        row = db.execute("SELECT intent,call_error FROM crawls ORDER BY id DESC LIMIT 1").fetchone()
        field = C.stamp_fields(tmp_path / "crawls.db", F.MINT, F.T + 21)["crawler_intent"]
        assert calls == [], {"unexpected_prompts": calls, "crawl": row,
                             "stamp": (field.value, field.completeness), "stats": stats}
        assert row == (None, "evidence_unreadable")
        assert stats["failed"] == 1 and stats["crawled"] == 0 and stats["unreadable_rows"] == 1
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert field.value is None and field.completeness == "missing"
    finally:
        db.close()
