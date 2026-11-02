"""Missing external tweet IDs still need distinct day-scoped row identities."""
import json
import sqlite3
from datetime import datetime, timezone

import test_memecoin_desk_source_scout as F


def test_null_tweet_ids_with_same_row_number_on_two_days_both_score(tmp_path):
    previous = "2026-10-20"
    old_at = F.T - 86400
    desk = F.world(tmp_path, posts=[("kol", F.B, F.T)],
                   swaps=[(F.B, F.T - 10, 1.0), (F.B, F.T + 6, 1.0), (F.B, F.T + 100, 2.1)])
    current = sqlite3.connect(desk / "x" / (F.DAY + ".db"))
    post_schema = current.execute("SELECT sql FROM sqlite_master WHERE name='posts'").fetchone()[0]
    current.execute("UPDATE posts SET tweet_id=NULL")
    assert current.execute("SELECT id,tweet_id FROM posts").fetchall() == [(1, None)]
    current.commit()
    current.close()
    older = sqlite3.connect(desk / "x" / (previous + ".db"))
    older.execute(post_schema)
    older.execute("INSERT INTO posts(id,event,tweet_id,received_at,handle,payload_json) VALUES(1,'tweet',NULL,?,'kol',?)",
                  (old_at, json.dumps({"text": F.A})))
    older.commit()
    older.close()
    swaps = sqlite3.connect(desk / "swaps" / (previous + ".db"))
    swaps.execute("CREATE TABLE swaps(mint TEXT,received_at REAL,virtual_quote INTEGER,virtual_token INTEGER,"
                  "real_token INTEGER,block_time INTEGER,ordinal INTEGER)")
    swaps.executemany("INSERT INTO swaps VALUES(?,?,?,?,?,?,0)",
                      [(F.A, t, int(price * 10**6), 10**6, 5, int(t))
                       for t, price in ((old_at - 10, 1.0), (old_at + 6, 1.0), (old_at + 100, 2.1))])
    swaps.commit()
    swaps.close()
    out, db = F._scout_db(tmp_path, pool=[("kol", "S", "2026-10-19T00:00:00+00:00")])
    try:
        inputs = list(F.SC.posts(desk / "x", [previous, F.DAY]))
        assert len(inputs) == 2 and all(p["id"] == 1 and p["tweet_id"] is None for p in inputs)
        stats = F.SC.score_day(F.DAY, x_dir=desk / "x", desk_dir=desk, out=out,
                               now=datetime.fromtimestamp(F.T + 1000, timezone.utc))
        rows = db.execute("SELECT mint,known_at,status FROM posts_scored ORDER BY known_at").fetchall()
        assert rows == [(F.A, old_at, "scored"), (F.B, F.T, "scored")], {"rows": rows, "stats": stats}
        assert db.execute("SELECT COUNT(DISTINCT post_id) FROM posts_scored").fetchone()[0] == 2
    finally:
        db.close()
