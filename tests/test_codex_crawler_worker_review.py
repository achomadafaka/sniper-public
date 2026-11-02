import importlib.util
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

ARCHIVE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ARCHIVE))
spec = importlib.util.spec_from_file_location("crawler_fixtures", ARCHIVE / "tests/test_memecoin_desk_crawler.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
W, C = F.W, F.C


def test_failed_input_is_retried_at_next_refresh_without_a_new_post(tmp_path):
    desk = F.world(tmp_path, metas=[(F.MINT, None, F.T)])
    db, calls = C.connect(tmp_path / "crawls.db"), []

    def bad_model(_system, user):
        calls.append(user)
        return {"text": F.answer(intent="moon")}

    first = F.run(desk, db, calls, F.T + 10, model=bad_model)
    assert first["failed"] == 1
    F.run(desk, db, calls, F.T + 10 + W.REFRESH_S)
    assert len(calls) == 2, {"calls": len(calls), "watch": db.execute("SELECT * FROM watch").fetchall()}


class InjectedCrash(BaseException):
    pass


def test_interrupted_attempt_retains_the_per_mint_refresh_cooldown(tmp_path):
    desk = F.world(tmp_path, metas=[(F.MINT, None, F.T)])
    db, calls = C.connect(tmp_path / "crawls.db"), []

    def crash_after_possible_model_call(_system, user):
        calls.append(user)
        raise InjectedCrash("process interrupted after charged call starts")

    with pytest.raises(InjectedCrash):
        F.run(desk, db, calls, F.T + 10, model=crash_after_possible_model_call)
    observer = sqlite3.connect(tmp_path / "crawls.db")
    assert observer.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    observer.close()
    F.run(desk, db, calls, F.T + 40)
    assert len(calls) == 1, {"calls": len(calls), "attempts": db.execute("SELECT * FROM attempts").fetchall()}


def test_rolling_hour_cap_uses_each_actual_call_start(tmp_path):
    other_mint = F.U.b58encode(bytes(range(32)))
    desk = F.world(tmp_path, metas=[(F.MINT, None, F.T), (other_mint, None, F.T)])
    db = C.connect(tmp_path / "crawls.db")
    starts = []
    class Clock:
        t, delay = F.T + 10, 10.0
        def __call__(self):
            return self.t
    clock = Clock()

    def model(_system, user):
        starts.append(clock.t)
        clock.t += clock.delay
        mint = user.splitlines()[0].removeprefix("mint: ")
        return {"text": F.answer(mint=mint)}

    W.crawl_pass(db, x_dir=desk / "x", desk_dir=desk, call_model=model,
                 per_hour=2, per_day=100, now=clock.t, clock=clock)
    assert len(starts) == 2
    F.add(desk, metas=[(F.MINT, None, F.T + 3605), (other_mint, None, F.T + 3605)])
    clock.t, clock.delay = F.T + 3611, 1.0
    W.crawl_pass(db, x_dir=desk / "x", desk_dir=desk, call_model=model,
                 per_hour=2, per_day=100, now=clock.t, clock=clock)
    max_in_hour = max(sum(end - 3600 < start <= end for start in starts) for end in starts)
    assert max_in_hour <= 2, {"relative_call_starts": [t - F.T for t in starts], "max_in_hour": max_in_hour}


def test_quiet_watch_reenters_on_new_input(tmp_path):
    desk = F.world(tmp_path, metas=[(F.MINT, None, F.T)])
    db, calls = C.connect(tmp_path / "crawls.db"), []
    assert F.run(desk, db, calls, F.T + 10)["crawled"] == 1
    quiet_at = F.T + C.FRESH_S + 1
    assert F.run(desk, db, calls, quiet_at)["left"] == 1
    F.add(desk, metas=[(F.MINT, None, quiet_at + 1)])
    result = F.run(desk, db, calls, quiet_at + 2)
    assert result["ingested"] == 1
    assert result["crawled"] == 1, db.execute("SELECT * FROM watch").fetchall()


def test_cap_rows_are_deduplicated_until_new_input(tmp_path):
    desk = F.world(tmp_path, metas=[(F.MINT, None, F.T)])
    db, calls = C.connect(tmp_path / "crawls.db"), []
    assert F.run(desk, db, calls, F.T + 10, per_hour=0)["cap_skipped"] == 1
    assert F.run(desk, db, calls, F.T + 20, per_hour=0)["cap_skipped"] == 0
    F.add(desk, metas=[(F.MINT, None, F.T + 30)])
    assert F.run(desk, db, calls, F.T + 40, per_hour=0)["cap_skipped"] == 1
    assert len(db.execute("SELECT id FROM crawls WHERE call_error='cap'").fetchall()) == 2
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert calls == []


@pytest.mark.parametrize("same_release", [False, True], ids=["other-release", "same-release"])
def test_watchdog_never_treats_another_state_root_as_this_root(tmp_path, same_release):
    # Never execute a watchdog matching the real collector basename. Only this test's sleepers match.
    name = "crawler_review_%s.py" % uuid.uuid4().hex
    release, old = tmp_path / "release", tmp_path / "old"
    root_a, root_b = tmp_path / "state-a", tmp_path / "state-b"
    for d in (release / "scripts", old / "scripts", root_a, root_b):
        d.mkdir(parents=True)
    sleeper = "import time\nwhile True:\n    time.sleep(1)\n"
    for directory in (release, old):
        (directory / "scripts" / name).write_text(sleeper)
    shutil.copytree(ARCHIVE / "scripts/lib", release / "scripts/lib")
    watchdog = release / "scripts" / "watchdog.sh"
    watchdog.write_text((ARCHIVE / "scripts/memecoin_desk_crawler_watchdog.sh").read_text()
                        .replace("memecoin_desk_crawler.py", name))
    foreign_script = (release if same_release else old) / "scripts" / name
    foreign = subprocess.Popen([sys.executable, str(foreign_script)],
                               env={**os.environ, "SNIPER_STATE_ROOT": str(root_b)})
    created_pid = None
    try:
        result = subprocess.run(["bash", str(watchdog)],
                                env={**os.environ, "SNIPER_STATE_ROOT": str(root_a), "SNIPER_PYTHON": sys.executable},
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        pid_file = root_a / "data/desk/crawler.pid"
        if pid_file.exists():
            created_pid = int(pid_file.read_text())
        assert foreign.poll() is None, {"foreign_returncode": foreign.poll(), "stderr": result.stderr}
        assert created_pid is not None, "another root's collector suppressed this root's startup"
    finally:
        foreign.kill()
        foreign.wait()
        if created_pid is not None:
            try:
                os.kill(created_pid, 9)
            except ProcessLookupError:
                pass



@pytest.mark.parametrize("pending_input", [False, True], ids=["already-covered", "arrived-during-call"])
def test_legacy_successful_watch_does_not_recrawl_without_new_input(tmp_path, pending_input):
    metas = [(F.MINT, None, F.T)]
    if pending_input:
        metas.append((F.MINT, None, F.T + 11))
    desk = F.world(tmp_path, metas=metas)
    db_path = tmp_path / "crawls.db"
    legacy = sqlite3.connect(db_path)
    old_ddl = C.DDL.replace(", last_ok_at REAL", "")
    assert "last_ok_at" not in old_ddl
    legacy.executescript(old_ddl)
    last_input = F.T + 11 if pending_input else F.T
    legacy.execute("INSERT INTO watch(mint,first_at,last_input_at,last_crawl_at) VALUES(?,?,?,?)",
                   (F.MINT, F.T, last_input, F.T + 10))
    legacy.execute("INSERT INTO inputs VALUES('meta',?,?,?,?)", (F.DAY, 1, F.MINT, F.T))
    if pending_input:
        legacy.execute("INSERT INTO inputs VALUES('meta',?,?,?,?)", (F.DAY, 2, F.MINT, F.T + 11))
    legacy.execute("INSERT INTO cursors VALUES('meta',?,?)", (F.DAY, len(metas)))
    legacy.execute("INSERT INTO attempts(mint,at) VALUES(?,?)", (F.MINT, F.T + 10))
    legacy.execute("INSERT INTO crawls(network_id,mint,known_at,input_hash,inputs_json,intent,flags_json,model,prompt_version) "
                   "VALUES('solana',?,?,?,?,?,?,?,?)",
                   (F.MINT, F.T + 12, "legacy-success", json.dumps([["meta", F.DAY, 1]]),
                    "promotion", json.dumps(F.FLAGS), C.MODEL, C.CRAWLER_VERSION))
    legacy.commit()
    legacy.close()
    db = C.connect(db_path)
    assert "last_ok_at" in {row[1] for row in db.execute("PRAGMA table_info(watch)")}
    assert db.execute("SELECT last_input_at,last_crawl_at FROM watch").fetchone() == (last_input, F.T + 10)
    calls = []
    result = F.run(desk, db, calls, F.T + 700)
    assert result["ingested"] == 0
    assert len(calls) == int(pending_input), {
        "new_calls": len(calls), "pending_input": pending_input, "watch": db.execute("SELECT * FROM watch").fetchall()}
