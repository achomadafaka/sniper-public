"""Desk cohort U runners ranker (pre-registration 2026-10-21-desk-u-ranker-preregistration.md §2-§3)."""
import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_ranker as R
from sniper.services import memecoin_desk_universe as U
from sniper.services.memecoin_desk_safety import curve_address

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ranker_worker", ROOT / "scripts" / "memecoin_desk_ranker_worker.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)
_uspec = importlib.util.spec_from_file_location("universe_worker", ROOT / "scripts" / "memecoin_desk_universe_worker.py")
UW = importlib.util.module_from_spec(_uspec)
_uspec.loader.exec_module(UW)

T0 = 1792587600.0                       # 2026-10-21T13:00:00Z, a minute boundary
DAY = "2026-10-21"
VQ, VT = 30_000_000_000, 1_000_000_000_000_000      # price 3e-8 SOL/token


def _mint(i):
    from solders.pubkey import Pubkey
    return str(Pubkey(bytes([i + 1]) * 32))


def _emission(i, at, mcap=40_000.0):
    mint = _mint(i)
    sig = "sig%d" % i
    F = U.Field
    snap = U.build_snapshot(emission_at=at, fields={
        "mcap_usd": F(mcap, "pumpfun_grpc:curve_reserves", at, 7, "complete")})
    return {"schema": "desk-candidate/v1", "producer": "U", "network_id": "solana", "mint": mint,
            "pool": curve_address(mint), "band_entry_slot": 7, "trigger_signature": sig, "trigger_ordinal": 0,
            "ticket_id": U.ticket_id("solana", mint, 7, sig, 0), "gate_state": None,
            "emitted_at": datetime.fromtimestamp(at, timezone.utc).isoformat(), "snapshot": snap}


class Tape:
    def __init__(self, base):
        self.base = Path(base)
        self.files = UW.DayFiles(self.base)
        self.n = 0
        (self.base / "candidate_emissions").mkdir(parents=True, exist_ok=True)
        self.files.conn("gaps", T0)            # an empty gap file exists, as on the host
        self.files.commit()

    def emit(self, row):
        with open(self.base / "candidate_emissions" / (DAY + ".jsonl"), "a") as fh:
            fh.write(json.dumps(row) + "\n")

    def swap(self, mint, at, *, buy=True, sol=0.1, price_x=1.0, actor=None, sig=None, real_token=5):
        self.n += 1
        self.files.conn("swaps", at).execute(
            "INSERT INTO swaps(slot, signature, ordinal, mint, curve, is_buy, quote_atomic, token_atomic, actor, "
            "virtual_quote, virtual_token, block_time, received_at, real_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.n, sig or "s%d" % self.n, 0, mint, None, int(buy), int(sol * 1e9), 1, actor or "a%d" % self.n,
             int(VQ * price_x), VT, int(at), at, real_token))
        self.files.commit()

    def gap(self, at):
        self.files.conn("gaps", at).execute("INSERT INTO gaps VALUES(?,?,?)", (at, "ring_overflow", self.n))
        self.files.commit()

    def heartbeat(self, at):                   # keeps the tape head fresh at `at`
        self.swap("HEARTBEAT", at, sol=0.0)


def _candidate(tape, i, at, **kw):
    row = _emission(i, at, **kw)
    tape.emit(row)
    tape.swap(row["mint"], at, sig=row["trigger_signature"])         # the trigger swap
    return row


def _rows(base, rank_at):
    conn = sqlite3.connect(str(Path(base) / "ranker" / (DAY + ".db")))
    try:
        return {r[0]: r[1:] for r in conn.execute(
            "SELECT opportunity_id, rank, completeness, shortlisted FROM rank_rows WHERE rank_at=?", (rank_at,))}
    finally:
        conn.close()


def _cand(base, oid):
    conn = sqlite3.connect(str(Path(base) / "ranker" / "candidates.db"))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM candidates WHERE opportunity_id=?", (oid,)).fetchone() or {})
    finally:
        conn.close()


def test_every_in_band_candidate_gets_a_row_and_only_the_top_30_are_shortlisted(tmp_path):
    tape = Tape(tmp_path)
    rank_at = T0 + 120
    rows = [_candidate(tape, i, T0 + 10) for i in range(32)]
    for i, row in enumerate(rows):                    # candidate i gets i buyers in the window
        for k in range(i):
            tape.swap(row["mint"], rank_at - 30, actor="b%d-%d" % (i, k), sol=0.01)
    tape.heartbeat(rank_at - 1)
    stats = W.run_cycle(tmp_path, rank_at)
    got = _rows(tmp_path, rank_at)
    assert set(got) == {r["ticket_id"] for r in rows}
    assert stats["n_shortlisted"] == 30 and stats["n_unscored"] == 0
    ranks = {oid: v[0] for oid, v in got.items()}
    assert sorted(ranks.values()) == list(range(1, 33))
    cut = {oid for oid, v in got.items() if not v[2]}
    assert cut == {rows[0]["ticket_id"], rows[1]["ticket_id"]}        # fewest buyers
    assert _cand(tmp_path, rows[31]["ticket_id"])["first_shortlisted_at"] == rank_at
    assert _cand(tmp_path, rows[0]["ticket_id"])["first_shortlisted_at"] is None


def test_a_tie_breaks_on_opportunity_id_bytes():
    f = {"net_sol": 0.0, "buyers": 0, "n_swaps": 0, "ret_ln": 0.0}
    ids = ["desk:U:%02d" % i for i in range(31, -1, -1)]
    rows = R.rank_cycle([{"opportunity_id": i, "completeness": "complete", "features": f} for i in ids])
    by = {r["opportunity_id"]: r for r in rows}
    assert [by["desk:U:%02d" % i]["rank"] for i in range(32)] == list(range(1, 33))
    assert not by["desk:U:30"]["shortlisted"] and by["desk:U:29"]["shortlisted"]


def test_a_swap_received_after_rank_at_never_moves_the_rank(tmp_path):
    tape = Tape(tmp_path)
    rank_at = T0 + 120
    a, b = _candidate(tape, 0, T0 + 10), _candidate(tape, 1, T0 + 10)
    tape.swap(a["mint"], rank_at - 5, sol=1.0)
    tape.heartbeat(rank_at - 1)
    tape.swap(b["mint"], rank_at + 0.5, sol=50.0)                    # known after the cycle start
    tape.emit(_emission(2, rank_at + 0.5))                            # an emission known after it
    W.run_cycle(tmp_path, rank_at)
    got = _rows(tmp_path, rank_at)
    assert got[a["ticket_id"]][0] == 1 and got[b["ticket_id"]][0] == 2
    assert _emission(2, rank_at + 0.5)["ticket_id"] not in got


def test_a_healthy_tape_with_no_swaps_is_a_known_zero(tmp_path):
    tape = Tape(tmp_path)
    rank_at = T0 + 600
    row = _candidate(tape, 0, T0 + 10)
    tape.heartbeat(rank_at - 1)
    W.run_cycle(tmp_path, rank_at)
    rank, completeness, _ = _rows(tmp_path, rank_at)[row["ticket_id"]]
    assert (rank, completeness) == (1, "complete")


@pytest.mark.parametrize("break_tape, want", [("gap", "tape_gap"), ("stale", "tape_stale")])
def test_an_unhealthy_tape_is_unscored_and_closes_nothing(tmp_path, break_tape, want):
    tape = Tape(tmp_path)
    rank_at = T0 + 120
    row = _candidate(tape, 0, T0 + 10, mcap=40_000.0)
    tape.swap(row["mint"], rank_at - 20, price_x=10.0)               # would read as out of band
    if break_tape == "gap":
        tape.heartbeat(rank_at - 1)
        tape.gap(rank_at - 30)
    stats = W.run_cycle(tmp_path, rank_at)
    assert _rows(tmp_path, rank_at)[row["ticket_id"]][:2] == (None, want)
    assert stats["tape"] == want and _cand(tmp_path, row["ticket_id"])["closed_at"] is None


def test_a_candidate_that_left_band_before_its_first_cycle_keeps_one_row_then_closes(tmp_path):
    tape = Tape(tmp_path)
    row = _candidate(tape, 0, T0 + 10, mcap=40_000.0)
    tape.swap(row["mint"], T0 + 50, price_x=3.0)                     # 120k: above the band
    tape.heartbeat(T0 + 59)
    W.run_cycle(tmp_path, T0 + 60)
    assert _rows(tmp_path, T0 + 60)[row["ticket_id"]] == (None, "left_band", 0)
    assert _cand(tmp_path, row["ticket_id"])["close_reason"] == "left_band"
    tape.swap(row["mint"], T0 + 100, price_x=1.0)                     # back in band, no new emission
    tape.heartbeat(T0 + 119)
    W.run_cycle(tmp_path, T0 + 120)
    assert row["ticket_id"] not in _rows(tmp_path, T0 + 120)          # closed is closed


def test_graduation_closes_and_an_open_candidate_expires_at_max_life(tmp_path):
    tape = Tape(tmp_path)
    grad, old = _candidate(tape, 0, T0 + 10), _candidate(tape, 1, T0 + 10)
    tape.heartbeat(T0 + 59)
    W.run_cycle(tmp_path, T0 + 60)
    tape.swap(grad["mint"], T0 + 90, real_token=0)
    tape.heartbeat(T0 + 119)
    W.run_cycle(tmp_path, T0 + 120)
    assert _cand(tmp_path, grad["ticket_id"])["close_reason"] == "left_band"
    end = T0 + 10 + R.MAX_LIFE_S + 60
    tape.heartbeat(end - 1)
    W.run_cycle(tmp_path, R.cycle_start(end))
    assert _cand(tmp_path, old["ticket_id"])["close_reason"] == "max_life"


def test_a_superseded_emission_never_seen_still_gets_its_row(tmp_path):
    tape = Tape(tmp_path)
    first = _candidate(tape, 0, T0 + 5)
    again = _emission(0, T0 + 70)                     # same mint re-entered band
    again["trigger_signature"] = "sig-again"
    again["ticket_id"] = U.ticket_id("solana", again["mint"], 7, "sig-again", 0)
    tape.emit(again)
    tape.swap(again["mint"], T0 + 70, sig="sig-again")
    tape.heartbeat(T0 + 119)
    W.run_cycle(tmp_path, T0 + 120)                    # the worker was down at T0 + 60
    got = _rows(tmp_path, T0 + 120)
    assert got[first["ticket_id"]] == (None, "left_band", 0) and got[again["ticket_id"]][0] == 1
    assert _cand(tmp_path, first["ticket_id"])["close_reason"] == "superseded"


def test_a_forged_emission_is_never_ranked(tmp_path):
    tape = Tape(tmp_path)
    forged = _emission(0, T0 + 10)
    forged["snapshot"]["fields"]["mcap_usd"]["value"] = 41_000.0       # changed without a rehash
    tape.emit(forged)
    tape.heartbeat(T0 + 59)
    stats = W.run_cycle(tmp_path, T0 + 60)
    assert stats["n_rows"] == 0 and stats["refused"] == {"emission_snapshot_hash": 1}


def test_the_sr_draws_are_the_registered_hashes_at_the_fixed_p():
    oid = "desk:U:474a6feb96df2f8df0eb8e24"
    want = int.from_bytes(hashlib.sha256(("desk-sr-u-all-v1|" + oid).encode()).digest(), "big") / 2 ** 256
    d = R.sr_draws(oid)
    assert d["u_sr_all"] == want and d["sr_all_draw"] == (want < 0.08)
    assert (R.SR_ALL_P, R.SR_POST_P) == (0.08, 0.25)          # amendment 3 (PR #524)
    ids = ["desk:U:%d" % i for i in range(20_000)]
    assert 1_450 <= sum(R.sr_draws(i)["sr_all_draw"] for i in ids) <= 1_750
    assert 4_700 <= sum(R.sr_draws(i)["sr_post_draw"] for i in ids) <= 5_300


def test_a_second_ranker_exits_while_the_lock_is_held(tmp_path, monkeypatch, capsys):
    import fcntl
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    lock_dir = tmp_path / "data" / "desk" / "ranker"
    lock_dir.mkdir(parents=True)
    monkeypatch.setattr(W, "run_cycle", lambda *a, **k: pytest.fail("ranked while another ranker held the lock"))
    with open(lock_dir / "ranker.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert W.main(["--once"]) == 0
    assert "another ranker holds the lock" in capsys.readouterr().out


def _fake_proc(root, pid, argv, state_root=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if state_root is not None:
        (d / "environ").write_bytes(b"SNIPER_STATE_ROOT=" + str(state_root).encode() + b"\0")


def test_the_ranker_watchdog_starts_the_pinned_worker_and_stops_a_superseded_one(tmp_path):
    import os
    import shutil
    import subprocess
    import time
    rel = tmp_path / "releases" / "A" / "scripts"
    rel.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "memecoin_desk_ranker_watchdog.sh", rel)
    shutil.copytree(ROOT / "scripts" / "lib", rel / "lib")
    (rel / "memecoin_desk_ranker_worker.py").write_text("")
    (tmp_path / "current").symlink_to(tmp_path / "releases" / "A")
    fake_py = tmp_path / "python3"
    fake_py.write_text('#!/bin/sh\necho "$1 $2" > "%s"\n' % (tmp_path / "started"))
    fake_py.chmod(0o755)
    proc = tmp_path / "proc"
    proc.mkdir()
    child = subprocess.Popen(["sleep", "30"])          # its pid as the superseded entry: never signalled
    try:
        old = str(tmp_path / "releases" / "OLD" / "scripts" / "memecoin_desk_ranker_worker.py")
        _fake_proc(proc, child.pid, ["/usr/bin/python3", old], tmp_path / "state")
        _fake_proc(proc, 900002, ["vim", "memecoin_desk_ranker_worker.py"])
        out = subprocess.run(["bash", str(tmp_path / "current" / "scripts" / "memecoin_desk_ranker_watchdog.sh")],
                             env=dict(os.environ, SNIPER_STATE_ROOT=str(tmp_path / "state"),
                                      SNIPER_PYTHON=str(fake_py), DESK_RANKER_WATCHDOG_PROC_ROOT=str(proc)),
                             capture_output=True, text=True, timeout=30)
        for _ in range(50):
            if (tmp_path / "started").exists():
                break
            time.sleep(0.05)
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        assert child.returncode is None, "the watchdog signalled a pid from a fake process table"
    finally:
        child.kill()
        child.wait()
    pinned = str((tmp_path / "releases" / "A" / "scripts").resolve() / "memecoin_desk_ranker_worker.py")
    assert "stopping superseded desk ranker pid=%d" % child.pid in out.stderr and "900002" not in out.stderr
    assert (tmp_path / "started").read_text().split() == [pinned, "--loop"]


def test_the_kill_knob_stops_the_ranker(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RANKER_ENABLED", "0")
    monkeypatch.setattr(W, "run_cycle", lambda *a, **k: pytest.fail("ranked with the kill knob at 0"))
    assert W.main(["--once"]) == 0 and "MEMECOIN_DESK_RANKER_ENABLED=0" in capsys.readouterr().out


# ---------------------------------------------------------------- Codex's witnesses on 7b777bd22

def test_a_swap_received_later_never_changes_a_past_cycles_tape_health(tmp_path):
    tape = Tape(tmp_path)
    rank_at = T0 + 120
    tape.swap("HEAD", rank_at - 1)
    conn = tape.files.conn("swaps", rank_at)
    conn.execute("UPDATE swaps SET slot=100")
    tape.files.commit()
    before = W.tape_health([conn], [], rank_at)
    assert before == ("ok", 1.0)
    tape.swap("FUTURE", rank_at + 1)                           # received after rank_at, a far later slot
    conn.execute("UPDATE swaps SET slot=501 WHERE mint='FUTURE'")
    tape.files.commit()
    assert W.tape_health([conn], [], rank_at) == before


def test_a_crash_between_the_rows_and_the_registry_never_loses_a_first_cycle_row(tmp_path, monkeypatch):
    tape = Tape(tmp_path)
    cand = _candidate(tape, 0, T0 + 10)
    tape.swap(cand["mint"], T0 + 50, price_x=3.0)                # left band before its first cycle
    tape.heartbeat(T0 + 59)
    rank_at, oid = T0 + 60, cand["ticket_id"]
    real_rw = W.rw

    class Crash(RuntimeError):
        pass

    class CrashingCandidates:                                   # dies inside the registry write
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE candidates SET closed_at"):
                raise Crash("inside the registry write")
            return self.conn.execute(sql, *args)

    def crashing_rw(path, ddl):
        conn = real_rw(path, ddl)
        return CrashingCandidates(conn) if path.name == "candidates.db" else conn
    monkeypatch.setattr(W, "rw", crashing_rw)
    with pytest.raises(Crash):
        W.run_cycle(tmp_path, rank_at)
    assert _cand(tmp_path, oid).get("closed_at") is None         # never closed without its row
    assert _rows(tmp_path, rank_at)[oid] == (None, "left_band", 0)
    monkeypatch.setattr(W, "rw", real_rw)
    W.run_cycle(tmp_path, rank_at)                               # the restart replays the cycle
    assert _rows(tmp_path, rank_at)[oid] == (None, "left_band", 0)
    assert _cand(tmp_path, oid)["close_reason"] == "left_band"


def test_the_sr_rails_are_their_own_uniform_draws_on_the_frozen_menu():
    bounds = {"tp_pct": (15, 150), "sl_pct": (10, 40), "trail_pct": (8, 30)}
    oid = "desk:U:474a6feb96df2f8df0eb8e24"
    u = int.from_bytes(hashlib.sha256(("desk-sr-u-tp-v1|" + oid).encode()).digest(), "big") / 2 ** 256
    assert R.sr_rails(oid, bounds)["tp_pct"] == 15 + int(u * 136)
    draws = [R.sr_rails("desk:U:%d" % i, bounds) for i in range(6_000)]
    for rail, (lo, hi) in bounds.items():
        values = [d[rail] for d in draws]
        assert min(values) == lo and max(values) == hi                    # the whole menu, ends included
    sl = [d["sl_pct"] for d in draws]
    assert all(150 <= sl.count(v) <= 250 for v in range(10, 41))          # 31 values, ~194 each
    assert [d["tp_pct"] for d in draws] != [d["sl_pct"] for d in draws]  # not one shared u
