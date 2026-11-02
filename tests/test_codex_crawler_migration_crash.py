"""Real SQLite migration crash window; no service/model/provider calls."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_crawler as C

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
START = 1792583900.0


class ProcessDeath(BaseException):
    pass


def test_reconnect_recovers_last_ok_backfill_after_death_immediately_after_alter(tmp_path, monkeypatch):
    original_connect = sqlite3.connect
    path = tmp_path / "crawls.db"
    legacy = original_connect(path)
    legacy.executescript(C.DDL.replace(", last_ok_at REAL", ""))
    assert "last_ok_at" not in {r[1] for r in legacy.execute("PRAGMA table_info(watch)")}
    legacy.execute("INSERT INTO watch(mint,first_at,last_input_at,last_crawl_at) VALUES(?,?,?,?)",
                   (MINT, START - 100, START - 1, START))
    legacy.execute("INSERT INTO crawls(network_id,mint,known_at,input_hash,inputs_json,intent,flags_json,"
                   "call_error,prompt_version) VALUES('solana',?,?,'h','[]','analysis',?,NULL,?)",
                   (MINT, START + 1, json.dumps({k: False for k in C.FLAGS}), C.CRAWLER_VERSION))
    legacy.commit()
    legacy.close()
    crashed = []

    class CrashConnection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            result = self.inner.execute(sql, *args)
            if sql.startswith("ALTER TABLE watch ADD COLUMN last_ok_at"):
                # The actual DDL has run. Closing rolls back an active transaction, as process
                # death would; when there is none, the schema change is already durable.
                crashed.append({"in_transaction_after_alter": self.inner.in_transaction})
                self.inner.close()
                raise ProcessDeath()
            return result

    with monkeypatch.context() as patch:
        patch.setattr(C.sqlite3, "connect", lambda *a, **kw: CrashConnection(original_connect(*a, **kw)))
        with pytest.raises(ProcessDeath):
            C.connect(path)
    assert len(crashed) == 1
    inspect = original_connect(path)
    columns = {r[1] for r in inspect.execute("PRAGMA table_info(watch)")}
    crashed[0]["last_ok_column_survived_death"] = "last_ok_at" in columns
    if "last_ok_at" in columns:
        crashed[0]["last_ok_after_death"] = inspect.execute("SELECT last_ok_at FROM watch").fetchone()[0]
    assert inspect.execute("SELECT COUNT(*) FROM crawls WHERE call_error IS NULL").fetchone()[0] == 1
    inspect.close()
    print("MIGRATION_CRASH", json.dumps(crashed[0], sort_keys=True))

    recovered = C.connect(path)
    try:
        value = recovered.execute("SELECT last_ok_at FROM watch WHERE mint=?", (MINT,)).fetchone()[0]
        assert value == START, f"successful legacy coverage lost after migration crash: {value!r}"
    finally:
        recovered.close()
