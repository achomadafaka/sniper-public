"""The type classifier consumes the newest twenty posts across day boundaries."""
import json
import sqlite3

import test_memecoin_desk_source_scout as F


def test_type_label_last_twenty_include_latest_day_not_just_older_day(tmp_path):
    desk = F.world(tmp_path, posts=[("kol", "LATEST_POST_MARKER", F.T)])
    current = sqlite3.connect(desk / "x" / (F.DAY + ".db"))
    schema = current.execute("SELECT sql FROM sqlite_master WHERE name='posts'").fetchone()[0]
    current.close()
    previous = "2026-10-20"
    older = sqlite3.connect(desk / "x" / (previous + ".db"))
    older.execute(schema)
    for i in range(20):
        older.execute("INSERT INTO posts(event,tweet_id,received_at,handle,payload_json) VALUES('tweet',?,?,'kol',?)",
                      ("old-%d" % i, F.T - 86400 + i, json.dumps({"text": "OLD_%02d" % i})))
    older.commit()
    older.close()
    _, db = F._scout_db(tmp_path, pool=[("kol", "S", "2026-10-19T00:00:00+00:00")])
    seen = []
    try:
        def call(system, user):
            seen.append(user)
            return {"text": "news", "model_echo": F.SC.TYPE_MODEL}
        assert F.SC.label_types(db, call, desk / "x", F.SC.days_back(F.DAY, 2)) == 1
        assert len(seen) == 1
        assert "LATEST_POST_MARKER" in seen[0], seen[0]
        assert "OLD_00" not in seen[0] and all("OLD_%02d" % i in seen[0] for i in range(1, 20))
    finally:
        db.close()
