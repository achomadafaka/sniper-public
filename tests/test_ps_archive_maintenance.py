"""PumpSwap archive growth control: strip payload_b64 from closed days, compress closed days, read compressed days."""
import importlib.util
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_ps_archive as A

ROOT = Path(__file__).resolve().parents[1]
_s = importlib.util.spec_from_file_location("ps_archive_maintenance", ROOT / "scripts/ps_archive_maintenance.py")
S = importlib.util.module_from_spec(_s)
_s.loader.exec_module(S)


def day_file(d, day, *, closed=True, n=50):
    """An archive day in the worker's archive layout (swaps_ps + ps_keep), rows with a raw payload_b64."""
    path = d / ("keep-%s.db" % day)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE swaps_ps(pool TEXT, slot INTEGER, signature TEXT, ordinal INTEGER, received_at REAL, "
              "payload_json TEXT, payload_b64 TEXT)")
    c.execute("CREATE UNIQUE INDEX swaps_ps_sig_ord ON swaps_ps(signature, ordinal)")
    c.execute("CREATE TABLE ps_keep(pool TEXT PRIMARY KEY, keep_from REAL, keep_until REAL, signature TEXT, slot INTEGER)")
    c.executemany("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?)",
                  [("P", i, "s%d" % i, 0, 1000.0 + i, '{"ix_name":"buy"}', "x" * 600) for i in range(n)])
    c.execute("INSERT INTO ps_keep VALUES('P', 1000, 1660, 's', 1)")
    c.commit()
    c.close()
    if closed:
        os.chmod(path, 0o444)
    return path


def rows(path, cols="pool, slot, signature, ordinal, received_at, payload_json"):
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return sorted(c.execute("SELECT %s FROM swaps_ps" % cols).fetchall()), c.execute("SELECT * FROM ps_keep").fetchall()
    finally:
        c.close()


def test_only_closed_days_are_candidates(tmp_path):
    a = day_file(tmp_path, "2026-10-26")
    day_file(tmp_path, "2026-10-28", closed=False)                            # the worker's current day
    (tmp_path / "keep-2026-10-27.new.db").write_bytes(b"")                    # a build in progress is no day
    assert S.closed_days(tmp_path) == [a]


def test_strip_replaces_a_closed_day_with_its_verified_b64_free_copy(tmp_path):
    path = day_file(tmp_path, "2026-10-26")
    before = rows(path)
    S.strip_day(path, log=lambda *a: None)
    assert rows(path) == before                                                # every other column and row equal
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM swaps_ps WHERE payload_b64 IS NOT NULL").fetchone()[0] == 0
    c.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444 and S.stripped(path)
    assert not (tmp_path / "keep-2026-10-26.new.db").exists()


def test_a_failed_strip_keeps_the_original(tmp_path, monkeypatch):
    path = day_file(tmp_path, "2026-10-26")
    real = S._converter

    def tampered():
        M = real()
        inner = M.convert_drop_b64
        M.convert_drop_b64 = lambda base, **kw: inner(base, _tamper=lambda c: c.execute(
            "DELETE FROM main.swaps_ps WHERE slot=3"), **kw)
        return M
    monkeypatch.setattr(S, "_converter", tampered)
    with pytest.raises(SystemExit, match="verification FAILED"):
        S.strip_day(path, log=lambda *a: None)
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    assert c.execute("SELECT COUNT(*) FROM swaps_ps WHERE payload_b64 IS NOT NULL").fetchone()[0] == 50
    c.close()


def test_a_held_day_is_not_touched(tmp_path, monkeypatch):
    path = day_file(tmp_path, "2026-10-26")
    real = S._converter

    def held():
        M = real()
        M.holders = lambda p: [4242]
        return M
    monkeypatch.setattr(S, "_converter", held)
    with pytest.raises(SystemExit, match="open in pids"):
        S.strip_day(path, log=lambda *a: None)


def test_compress_then_read_the_day_back_equal(tmp_path):
    path = day_file(tmp_path, "2026-10-26")
    want = rows(path, "*")
    S.compress_day(path, log=lambda *a: None)
    assert not path.exists() and (tmp_path / "keep-2026-10-26.db.zst").exists()
    with A.open_day(tmp_path, "2026-10-26", scratch_dir=tmp_path / "scratch") as c:
        got = (sorted(c.execute("SELECT * FROM swaps_ps").fetchall()), c.execute("SELECT * FROM ps_keep").fetchall())
    assert got == want
    assert list((tmp_path / "scratch").iterdir()) == []                        # the on-demand copy is gone


def test_a_bad_compressed_copy_keeps_the_original(tmp_path, monkeypatch):
    path = day_file(tmp_path, "2026-10-26")
    monkeypatch.setattr(S, "_sha256_zst", lambda p: "0" * 64)
    with pytest.raises(SystemExit, match="does not decompress equal"):
        S.compress_day(path, log=lambda *a: None)
    assert path.exists() and not list(tmp_path.glob("*.zst*"))


def test_open_day_reads_a_plain_day_and_refuses_a_missing_one(tmp_path):
    day_file(tmp_path, "2026-10-26")
    with A.open_day(tmp_path, "2026-10-26") as c:
        assert c.execute("SELECT COUNT(*) FROM swaps_ps").fetchone()[0] == 50
    with pytest.raises(FileNotFoundError):
        with A.open_day(tmp_path, "2026-10-01"):
            pass


def tape(d, ends=()):
    """A live tape with ps_keep windows ending at `ends` (epoch seconds)."""
    path = d / "tape.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE IF NOT EXISTS ps_keep(pool TEXT PRIMARY KEY, keep_from REAL, keep_until REAL, signature TEXT, slot INTEGER)")
    c.executemany("INSERT INTO ps_keep VALUES(?,?,?,?,?)", [("p%d" % i, t - 660, t, "s", 1) for i, t in enumerate(ends)])
    c.commit()
    c.close()
    return path


def test_main_is_a_dry_run_by_default(tmp_path, capsys):
    path = day_file(tmp_path, "2026-10-26")
    assert S.main(["strip-b64", "--archive-dir", str(tmp_path)]) == 0
    assert "dry run: 1 closed day" in capsys.readouterr().out
    assert not S.stripped(path)


def test_compress_skips_days_younger_than_the_min_age(tmp_path, capsys):
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date()
    old = day_file(tmp_path, (today - dt.timedelta(days=3)).isoformat())
    young = day_file(tmp_path, (today - dt.timedelta(days=1)).isoformat())
    assert S.main(["compress", "--archive-dir", str(tmp_path), "--tape", str(tape(tmp_path)), "--confirm"]) == 0
    assert not old.exists() and young.exists()



# ---- Delta HOLD eb96e1f7e: a sealed day never gets a plain twin; the reader refuses ambiguity

def test_a_late_plain_day_cannot_hide_compressed_history(tmp_path):
    path = day_file(tmp_path, "2026-10-26", n=2)
    S.compress_day(path, log=lambda *a: None)
    day_file(tmp_path, "2026-10-26", closed=False, n=1)                       # a late window recreated the plain day
    with pytest.raises(ValueError, match="archive_day_ambiguous"):
        with A.open_day(tmp_path, "2026-10-26"):
            pass


def test_the_worker_refuses_to_open_a_plain_day_beside_its_compressed_one(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ps_arch_ret", ROOT / "tests/test_memecoin_desk_pumpswap_retention.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    w = R.worker(tmp_path / "desk", monkeypatch)
    packed = Path(str(w.ps_archive_path("2026-10-26")) + ".zst")
    packed.parent.mkdir(parents=True)
    packed.write_bytes(b"x")
    with pytest.raises(OSError, match="archive_day_sealed"):
        w._ps_archive("2026-10-26")
    assert not w.ps_archive_path("2026-10-26").exists()


def test_a_day_with_a_pending_keep_window_is_not_sealed(tmp_path, capsys):
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).date()
    old_day = (today - dt.timedelta(days=4)).isoformat()
    open_day = (today - dt.timedelta(days=3)).isoformat()
    a, b = day_file(tmp_path, old_day), day_file(tmp_path, open_day)
    t = dt.datetime.fromisoformat(open_day + "T12:00:00+00:00").timestamp()
    assert S.main(["compress", "--archive-dir", str(tmp_path), "--tape", str(tape(tmp_path, [t])), "--confirm"]) == 0
    assert not a.exists() and b.exists()                                       # the day a window still targets stays


def test_an_unreadable_tape_seals_nothing(tmp_path, capsys):
    import datetime as dt
    old = day_file(tmp_path, (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=5)).isoformat())
    assert S.main(["compress", "--archive-dir", str(tmp_path), "--tape", str(tmp_path / "missing.db"), "--confirm"]) == 0
    assert old.exists() and "nothing compressed" in capsys.readouterr().out


def test_nested_reads_get_their_own_scratch_copies(tmp_path):
    path = day_file(tmp_path, "2026-10-26")
    S.compress_day(path, log=lambda *a: None)
    scratch = tmp_path / "scratch"
    with A.open_day(tmp_path, "2026-10-26", scratch_dir=scratch) as a:
        with A.open_day(tmp_path, "2026-10-26", scratch_dir=scratch) as b:
            assert len(list(scratch.iterdir())) == 2
            assert b.execute("SELECT COUNT(*) FROM swaps_ps").fetchone()[0] == 50
        assert a.execute("SELECT COUNT(*) FROM swaps_ps").fetchone()[0] == 50
    assert list(scratch.iterdir()) == []


# ---- Delta HOLD 55f40fd15: producer and compressor share a per-day seal lock

def _worker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("ps_arch_ret2", ROOT / "tests/test_memecoin_desk_pumpswap_retention.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    (tmp_path / "desk").mkdir(exist_ok=True)
    w = R.worker(tmp_path / "desk", monkeypatch)
    w.files.conn("swaps_ps", 1000.0)
    return w


def test_compression_cannot_publish_between_producer_check_and_connect(tmp_path, monkeypatch):
    """Delta's witness, adopted: compress_day fired inside the producer's connect is refused by the seal lock."""
    w = _worker(tmp_path, monkeypatch)
    day = "2026-10-01"
    path = w.ps_archive_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    day_file(path.parent, day, n=2)
    real, fired = sqlite3.connect, []

    def connect(name, *a, **kw):
        if str(name) == str(path) and not fired:
            fired.append(1)
            with pytest.raises(BlockingIOError):
                S.compress_day(path, log=lambda *a: None)
        return real(name, *a, **kw)
    monkeypatch.setattr(sqlite3, "connect", connect)
    w._ps_archive(day)
    assert fired and path.exists() and not Path(str(path) + ".zst").exists()


def test_a_day_the_compressor_holds_is_a_busy_pass_for_the_producer(tmp_path, monkeypatch):
    import fcntl
    w = _worker(tmp_path, monkeypatch)
    path = w.ps_archive_path("2026-10-02")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(sqlite3.OperationalError, match="busy"):          # archive_keep_ps counts this as busy
            w._ps_archive("2026-10-02")
    finally:
        os.close(fd)
    assert not path.exists()


@pytest.mark.parametrize("end", [None, float("nan"), float("inf")])
def test_a_malformed_window_end_seals_nothing(tmp_path, end):
    p = tape(tmp_path)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO ps_keep VALUES('bad', 0, ?, 's', 1)", (end,))
    c.commit()
    c.close()
    assert S.pending_window_days(p) is None
