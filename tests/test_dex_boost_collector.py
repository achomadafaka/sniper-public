"""DexScreener boost collector (Gamma prereg 93b0da92c section 1 + Amendment 2 a7eb2e8b4): events from intake_rows,
seen-before from intake history and the raw store, executable round-trip quotes at fixed offsets, typed states."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_s = importlib.util.spec_from_file_location("dex_boost_collector", ROOT / "scripts/research/dex_boost_collector.py")
C = importlib.util.module_from_spec(_s)
_s.loader.exec_module(C)

T0 = 1_793_000_000.0                                    # corrected UTC of the first sighting
DAY = C.utc_day(T0)


def intake(d, rows, *, poll_at=T0, day=DAY):
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / ("%s.db" % day))
    c.execute("CREATE TABLE IF NOT EXISTS intake_rows(id INTEGER PRIMARY KEY, poll_id INTEGER, feed TEXT, filter TEXT, "
              "network_id TEXT, chain_raw TEXT, mint TEXT, venue TEXT, provider_rank INTEGER, rank_kind TEXT, raw_json TEXT, "
              "observed_at REAL, host_received_at REAL, clock_offset_s REAL, source_ref TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY, filter TEXT, received_at REAL, status TEXT)")
    for filt, mint, at in rows:
        c.execute("INSERT INTO intake_rows(feed, filter, network_id, mint, observed_at, host_received_at, clock_offset_s, "
                  "raw_json) VALUES('dexscreener', ?, 'solana', ?, ?, ?, 0.0, '{}')",
                  (filt, mint, at, None if at is None else at))
    c.execute("INSERT INTO polls(filter, received_at, status) VALUES('boost_latest', ?, 'ok')", (poll_at,))
    c.commit()
    c.close()


class World:
    """A collector in a temp world. Tests that jump hours between ticks model a collector that kept running, so
    downtime detection is off unless the test asks for it (down=True: the real 60 s rule)."""

    def __init__(self, tmp, monkeypatch, down=False):
        monkeypatch.setattr(C, "DOWN_AFTER_S", 60.0 if down else 1e12)
        self.now = T0
        self.jup_calls, self.ps_calls = [], []
        self.on_tape = set()
        self.jup_answer = None
        self.throttled = False
        self.tmp = tmp
        self.col = C.Collector(intake_dir=tmp / "intake", out_dir=tmp / "out", desk_dir=tmp / "desk",
                               clock_fn=lambda: (0.0, self.now), jupiter_fn=self.jup, ps_buy=self.psb, ps_sell=self.pss,
                               paper_throttled_fn=lambda: self.throttled, now_fn=lambda: self.now,
                               raw_dir=tmp / "raw")

    def psb(self, desk, mint, spend, at, *, evaluated_at, integrity, live_now=None, detail=None):
        self.ps_calls.append(("buy", mint, spend, at, integrity))
        if mint not in self.on_tape:
            return None, "no_swaps_before_cut"
        return {"tokens_atomic": str(spend * 7), "spend_lamports": str(spend)}, None

    def pss(self, desk, mint, qty, at, *, evaluated_at, integrity, live_now=None, detail=None):
        self.ps_calls.append(("sell", mint, qty, at, integrity))
        return {"lamports": str(qty // 6)}, None

    def jup(self, inp, out, amount):
        self.jup_calls.append((inp, out, amount))
        if self.jup_answer is not None:
            return self.jup_answer
        return {"inAmount": str(amount), "outAmount": str(amount * 3),
                "routePlan": [{"swapInfo": {"ammKey": "AMM1"}}]}, None

    def at(self, t):
        self.now = t
        intake(self.tmp / "intake", [], poll_at=t)               # the source stays alive
        return self.col.tick()

    def rows(self, sql, *a):
        path = self.tmp / "out" / ("%s.db" % DAY)
        if not path.exists():
            return []                                       # no store for that day was ever created
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, a)]
        finally:
            c.close()


@pytest.fixture
def w(tmp_path, monkeypatch):
    intake(tmp_path / "intake", [("boost_top", "OLD", T0 - 3600)], poll_at=T0 - 3600)
    world = World(tmp_path, monkeypatch)
    world.col.start()                                    # OLD is history: left-censored, never an event
    return world


def test_first_appearance_per_class_after_start_is_an_event(w):
    intake(w.tmp / "intake", [("boost_latest", "M1", T0), ("profile", "M1", T0), ("boost_top", "OLD", T0),
                              ("boost_top", "M1", T0 + 1), ("ad", "M2", T0)])
    w.at(T0 + 1)
    ev = w.rows("SELECT class, mint FROM events ORDER BY class")
    assert ev == [{"class": "BOOST", "mint": "M1"}, {"class": "PROFILE", "mint": "M1"}]


def test_the_raw_store_marks_mints_as_seen_before(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    c = sqlite3.connect(raw / "2026-10-22.db")
    c.execute("CREATE TABLE items(poll_id INTEGER, endpoint TEXT, received_at REAL, chain_id TEXT, token_address TEXT, "
              "payload_json TEXT, source TEXT)")
    c.execute("INSERT INTO items VALUES(1, '/token-boosts/top/v1', 1, 'solana', 'RAWSEEN', '{}', 's')")
    c.commit()
    c.close()
    intake(tmp_path / "intake", [], poll_at=T0)
    world = World(tmp_path, monkeypatch)
    world.col.start()
    intake(tmp_path / "intake", [("boost_latest", "RAWSEEN", T0), ("profile", "RAWSEEN", T0)])
    world.at(T0 + 1)
    assert world.rows("SELECT class FROM events") == [{"class": "PROFILE"}]      # seen as a BOOST before


def test_null_observed_at_is_unusable_and_counted(w):
    intake(w.tmp / "intake", [("boost_latest", "M9", None)])
    w.at(T0 + 1)
    assert w.rows("SELECT * FROM events") == []
    assert w.rows("SELECT n FROM counters WHERE name='unusable_clock'") == [{"n": 1}]


def test_every_return_is_priced_from_jupiter_and_the_tape_is_only_a_diagnostic(w):
    """Amendment 7: one fill model (jupiter_capture) for every event and offset; an on-tape pool also gets a labelled
    ps_tape cross-check at offsets <= +15 min, never a return."""
    w.on_tape.add("M1")
    intake(w.tmp / "intake", [("boost_latest", "M1", T0)])
    w.at(T0 + 1)
    w.at(T0 + 31)
    for h in C.HORIZONS:
        w.at(T0 + h)
    q = w.rows("SELECT offset_s, side, state, price_source, proceeds_lamports FROM quotes ORDER BY offset_s")
    assert [r["offset_s"] for r in q] == [0.0] + list(C.HORIZONS)
    assert {r["price_source"] for r in q} == {"jupiter_capture"} and all(r["state"] == "known" for r in q)
    assert all(int(r["proceeds_lamports"]) == C.CLIP_LAMPORTS * 3 * 3 for r in q[1:])   # sells the acquired quantity
    d = w.rows("SELECT offset_s, side, label, amount_out FROM diagnostics ORDER BY offset_s")
    assert [(r["offset_s"], r["side"], r["label"]) for r in d] == [(0.0, "buy", "ps_tape_crosscheck"),
                                                                  (300.0, "sell", "ps_tape_crosscheck"),
                                                                  (900.0, "sell", "ps_tape_crosscheck")]
    assert w.rows("SELECT price_source FROM events") == [{"price_source": "jupiter_capture"}]


def test_off_tape_coins_use_jupiter_capture_exact_quantity(w):
    intake(w.tmp / "intake", [("profile", "M2", T0)])
    w.at(T0 + 1)
    w.at(T0 + 31)
    assert w.jup_calls == [(C.WSOL, "M2", C.CLIP_LAMPORTS)]
    w.at(T0 + 300)
    assert w.jup_calls[-1] == ("M2", C.WSOL, C.CLIP_LAMPORTS * 3)            # sells exactly the acquired quantity
    assert {r["price_source"] for r in w.rows("SELECT price_source FROM quotes")} == {"jupiter_capture"}


def test_a_late_entry_censors_the_event_never_a_later_price(w):
    intake(w.tmp / "intake", [("boost_latest", "M1", T0)])
    w.at(T0 + 1)
    C.DOWN_AFTER_S = 60.0                                # the real rule for this test (monkeypatch restores it)
    w.at(T0 + 200)                                       # no tick for 199 s: the collector was down over the window
    q = w.rows("SELECT offset_s, state, reason FROM quotes ORDER BY offset_s")
    assert q[0] == {"offset_s": 0.0, "state": "censored", "reason": "collector_down"}
    assert all(r["state"] == "censored" and r["reason"] == "collector_down" for r in q[1:]) and len(q) == 6
    assert w.jup_calls == [] and w.ps_calls == []


@pytest.mark.parametrize("answer,state", [((None, "rate_limited"), "rate_limited"), ((None, "route_absent"), "route_absent"),
                                          ((None, "provider_error"), "provider_error")])
def test_failed_quotes_are_typed_rows_never_missing_or_zero(w, answer, state):
    intake(w.tmp / "intake", [("profile", "M3", T0)])
    w.at(T0 + 1)
    w.jup_answer = answer
    w.at(T0 + 31)
    q = w.rows("SELECT offset_s, state, amount_out FROM quotes ORDER BY offset_s")
    assert q[0]["state"] == state and q[0]["amount_out"] is None and len(q) == 6


def test_a_throttled_paper_arm_makes_the_collector_back_off_first(w):
    intake(w.tmp / "intake", [("profile", "M4", T0)])
    w.at(T0 + 1)
    w.throttled = True
    w.at(T0 + 31)
    assert w.jup_calls == []
    assert w.rows("SELECT state, reason FROM quotes WHERE offset_s=0") == [{"state": "source_paused",
                                                                           "reason": "paper_arm_throttled"}]
    w.throttled = False
    intake(w.tmp / "intake", [("profile", "M5", T0 + 40)])
    w.at(T0 + 41)
    w.at(T0 + 71)                                          # the pause ended: its interval is a recorded gap
    gaps = w.rows("SELECT source, from_at, to_at, reason FROM gaps WHERE source='jupiter_capture'")
    assert gaps == [{"source": "jupiter_capture", "from_at": T0 + 31, "to_at": T0 + 71, "reason": "paper_arm_throttled"}]


def test_an_explicit_bucket_holds_0_2_per_second_and_halves_after_a_429(w):
    b = C.Bucket(0.2)
    assert b.take(0.0) and not b.take(1.0) and b.take(5.0)
    b.cut(5.0)
    assert not b.take(10.0) and b.take(15.1)                                  # half rate: one per 10 s


def test_a_dead_intake_source_writes_a_gap_and_reads_no_events(w):
    intake(w.tmp / "intake", [("boost_latest", "M5", T0)], poll_at=T0 - 1000)
    w.now = T0 + 1
    out = w.col.tick()                                   # last ok poll 1001 s ago
    assert out["intake_alive"] is False and w.rows("SELECT * FROM events") == []
    w.at(T0 + 2)
    gaps = w.rows("SELECT source, from_at, to_at, reason FROM gaps")
    assert gaps == [{"source": "intake", "from_at": T0 + 1, "to_at": T0 + 2, "reason": "source_dead"}]


def test_jupiter_capture_accepts_only_an_exact_quantity_answer(monkeypatch):
    class R:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["key"] = req.full_url, req.headers.get("X-api-key")
        return R(seen["body"])
    monkeypatch.setattr(C.urllib.request, "urlopen", opener)
    seen["body"] = {"inAmount": "100", "outAmount": "300"}
    assert C.jupiter_capture("A", "B", 100, key="K")[0]["outAmount"] == "300"
    assert seen["url"].startswith("https://api.jup.ag/swap/v1/quote?") and seen["key"] == "K"
    seen["body"] = {"inAmount": "99", "outAmount": "300"}                      # not the asked quantity
    assert C.jupiter_capture("A", "B", 100, key="K") == (None, "provider_error")
    assert C.jupiter_capture("A", "B", 100, key="") == (None, "provider_error")


def test_the_collector_never_touches_the_funded_seam_or_keyless_lite():
    """Amendment 4 cond. 2, by AST: no funded seam, runner or lite-api import, name or string outside the docstring."""
    import ast
    tree = ast.parse((ROOT / "scripts/research/dex_boost_collector.py").read_text())
    doc = ast.get_docstring(tree, clean=False)
    banned_mod = ("solana_real_seams", "memecoin_real_runner", "memecoin_quotes", "sniper.execution", "memecoin_live_marks")
    banned = ("_quote_with_fallback", "_jup_call", "lite-api", "MEMECOIN_JUPITER_API_KEY")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
            assert not any(b in m for m in mods for b in banned_mod), mods
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != doc:
            assert not any(b in node.value for b in banned), node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            assert (node.id if isinstance(node, ast.Name) else node.attr) not in banned


def test_the_key_identity_guard_compares_fingerprints_and_refuses_unknowns(tmp_path):
    """Alpha's design: host_salt (32 B, 0600) + funded_key.fp (hex sha256(salt || funded)); the collector never reads
    the funded key. Missing or malformed reference refuses, equal refuses, different passes."""
    import hashlib
    import os
    env = "cap"                                              # the capture key value, read from --capture-env
    with pytest.raises(SystemExit, match="reference missing"):
        C.key_identity_guard(tmp_path, env)
    salt = os.urandom(32)
    (tmp_path / "host_salt").write_bytes(salt)
    os.chmod(tmp_path / "host_salt", 0o600)
    with pytest.raises(SystemExit, match="reference missing"):
        C.key_identity_guard(tmp_path, env)                 # no fp file yet
    (tmp_path / "funded_key.fp").write_text(hashlib.sha256(salt + b"fund").hexdigest())
    assert 0 <= C.key_identity_guard(tmp_path, env) < 1      # different key: passes, returns the fp age in days
    (tmp_path / "funded_key.fp").write_text(hashlib.sha256(salt + b"cap").hexdigest())
    with pytest.raises(SystemExit, match="IS the funded key"):
        C.key_identity_guard(tmp_path, env)
    (tmp_path / "funded_key.fp").write_text("not-hex")
    with pytest.raises(SystemExit, match="malformed"):
        C.key_identity_guard(tmp_path, env)
    os.chmod(tmp_path / "host_salt", 0o644)
    with pytest.raises(SystemExit, match="group/others"):
        C.key_identity_guard(tmp_path, env)
    with pytest.raises(SystemExit, match="capture key missing"):
        C.key_identity_guard(tmp_path, "")

def test_the_tape_diagnostic_goes_through_the_ledger_seam_on_host_time(w):
    """Amendment 4 cond. 6: one clock sample converts corrected UTC to host for the tape cross-check."""
    w.on_tape.add("M7")
    w.col.clock_fn = lambda: (100.0, w.now - 10)          # host = corrected + 100, a fresh sample
    intake(w.tmp / "intake", [("boost_latest", "M7", T0)])
    w.at(T0 + 1 + 100)                                     # host clock
    w.at(T0 + 31 + 100)
    assert w.ps_calls[-1][3] == T0 + 31 + 100.0             # corrected entry time + 100 = the host cut


def test_a_failing_tape_diagnostic_never_touches_the_study_quote(w):
    w.on_tape.add("M8")
    intake(w.tmp / "intake", [("boost_latest", "M8", T0)])
    w.at(T0 + 1)
    w.col.ps_buy = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tape broke"))
    w.at(T0 + 31)
    assert w.rows("SELECT state FROM quotes WHERE offset_s=0") == [{"state": "known"}]
    assert w.rows("SELECT reason FROM diagnostics") == [{"reason": "diagnostic_error:RuntimeError"}]


# ---- Delta HOLD 08ac5165e

def test_a_response_after_the_window_is_late_not_known(w):
    intake(w.tmp / "intake", [("profile", "M6", T0)])
    w.at(T0 + 1)
    real = w.jup

    def slow(inp, out, amount):
        w.now += 10                                         # the request at +85 s answers at +95 s
        return real(inp, out, amount)
    w.col.jupiter_fn = slow
    w.at(T0 + 85)
    q = w.rows("SELECT offset_s, state, reason, taken_at, response_at FROM quotes ORDER BY offset_s")
    assert q[0]["state"] == "late" and q[0]["reason"] == "response_late"
    assert q[0]["taken_at"] == T0 + 85 and q[0]["response_at"] == T0 + 95
    assert all(r["state"] == "censored" and r["reason"] == "entry_late" for r in q[1:])


def test_a_restart_after_midnight_reopens_yesterdays_pending_quotes(tmp_path, monkeypatch):
    t0 = 1_793_059_200.0 - 120                              # 2 min before a UTC midnight
    assert C.utc_day(t0) != C.utc_day(t0 + 300)
    intake(tmp_path / "intake", [], poll_at=t0 - 10, day=C.utc_day(t0))
    first = World(tmp_path, monkeypatch)
    first.now = t0 - 5
    first.col.start()
    intake(tmp_path / "intake", [("profile", "M7", t0)], poll_at=t0, day=C.utc_day(t0))
    first.at(t0 + 1)
    first.at(t0 + 31)                                       # entry known, +300 s sell pending in yesterday's store
    second = World(tmp_path, monkeypatch)                   # a fresh process after midnight
    second.now = t0 + 200
    intake(tmp_path / "intake", [], poll_at=t0 + 300, day=C.utc_day(t0 + 300))
    second.at(t0 + 300)
    c = sqlite3.connect(tmp_path / "out" / ("%s.db" % C.utc_day(t0)))
    assert c.execute("SELECT state FROM quotes WHERE offset_s=300").fetchone() == ("known",)


def test_a_restart_drains_every_day_store_with_pending_rows(tmp_path, monkeypatch):
    """Amendment 8: an event 3 days back with a +24 h sell already done still has nothing pending, but an older store
    with a pending row is reopened on start (not only the last three UTC days)."""
    intake(tmp_path / "intake", [], poll_at=T0)
    first = World(tmp_path, monkeypatch, down=True)
    first.col.start()
    intake(tmp_path / "intake", [("profile", "M8", T0)])
    first.at(T0 + 1)
    first.at(T0 + 31)
    second = World(tmp_path, monkeypatch, down=True)
    second.now = T0 + 5 * 86400                              # a long outage
    second.col.resume()
    assert DAY in second.col.days


def test_offsets_due_during_downtime_are_censored_collector_down(tmp_path, monkeypatch):
    intake(tmp_path / "intake", [], poll_at=T0)
    first = World(tmp_path, monkeypatch, down=True)
    first.col.start()
    intake(tmp_path / "intake", [("profile", "M9", T0)])
    first.at(T0 + 1)
    first.at(T0 + 31)                                        # entry known; +300 s due later
    second = World(tmp_path, monkeypatch, down=True)
    second.now = T0 + 2000                                   # the process was down from T0+31 to T0+2000
    second.col.resume()
    second.at(T0 + 2000)
    q = second.rows("SELECT offset_s, state, reason FROM quotes WHERE offset_s IN (300, 900)")
    assert q == [{"offset_s": 300.0, "state": "censored", "reason": "collector_down"},
                 {"offset_s": 900.0, "state": "censored", "reason": "collector_down"}]
    g = second.rows("SELECT source, from_at, to_at FROM gaps WHERE source='collector'")
    assert g == [{"source": "collector", "from_at": T0 + 31, "to_at": T0 + 2000}]


def test_a_paced_miss_while_running_stays_late_not_collector_down(w):
    intake(w.tmp / "intake", [("profile", "M10", T0)])
    w.at(T0 + 1)
    w.col.glob = C.Bucket(0.0, burst=0.0)                   # paced out for the whole window, ticks keep running
    for t in range(31, 100, 20):
        w.at(T0 + t)
    assert w.rows("SELECT state, reason FROM quotes WHERE offset_s=0") == [{"state": "late", "reason": "window_passed"}]


def test_every_response_time_is_corrected_utc(w):
    """Delta 57d415215: known rows stored the host response time; every outcome stores corrected UTC."""
    w.col.clock_fn = lambda: (100.0, w.now - 10)
    intake(w.tmp / "intake", [("profile", "M11", T0)])
    w.at(T0 + 1 + 100)
    real = w.jup

    def slow(inp, out, amount):
        w.now += 2
        return real(inp, out, amount)
    w.col.jupiter_fn = slow
    w.at(T0 + 31 + 100)
    r = w.rows("SELECT state, taken_at, response_at FROM quotes WHERE offset_s=0")[0]
    assert r == {"state": "known", "taken_at": T0 + 31, "response_at": T0 + 33}


def test_an_offset_due_during_downtime_is_censored_even_if_the_restart_is_inside_its_window(tmp_path, monkeypatch):
    intake(tmp_path / "intake", [], poll_at=T0)
    first = World(tmp_path, monkeypatch, down=True)
    first.col.start()
    intake(tmp_path / "intake", [("profile", "M12", T0)])
    first.at(T0 + 1)
    first.at(T0 + 31)
    second = World(tmp_path, monkeypatch, down=True)
    second.now = T0 + 310                                    # down 31..310; +300 s fell due in the outage
    second.col.resume()
    second.at(T0 + 320)                                      # Gamma Am9 case: a quote at +320, inside 270..330
    assert second.rows("SELECT state, reason FROM quotes WHERE offset_s=300") == [{"state": "censored",
                                                                                  "reason": "collector_down"}]


def test_entry_due_during_outage_is_not_first_seen_time(tmp_path, monkeypatch):
    """Delta a4087d3b7: the entry is due at first_seen + 30 s; an outage 1..70 swallows it even though first_seen + 0
    lies before the outage and a +70 quote would still be inside the +30..+90 window."""
    intake(tmp_path / "intake", [], poll_at=T0)
    first = World(tmp_path, monkeypatch, down=True)
    first.col.start()
    intake(tmp_path / "intake", [("profile", "M13", T0)])
    first.at(T0 + 1)
    second = World(tmp_path, monkeypatch, down=True)
    second.now = T0 + 70
    second.col.resume()                                      # outage 1..70 recorded
    second.at(T0 + 70)
    assert second.rows("SELECT state, reason FROM quotes WHERE offset_s=0") == [{"state": "censored",
                                                                                "reason": "collector_down"}]
    assert second.jup_calls == []



def test_reads_are_indexed_and_finished_days_keep_no_handle(tmp_path, monkeypatch):
    """Delta resource gate a4087d3b7: the daily count and the due page use indexes; a finished day is closed."""
    intake(tmp_path / "intake", [], poll_at=T0)
    w = World(tmp_path, monkeypatch)
    w.col.start()
    intake(tmp_path / "intake", [("profile", "M14", T0)])
    w.at(T0 + 1)
    conn = w.col.day(DAY)
    plans = [" ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + q, a)) for q, a in (
        ("SELECT COUNT(*) FROM quotes WHERE taken_at >= ?", (0.0,)),
        ("SELECT p.event_id FROM pending p JOIN events e USING(event_id) WHERE p.due_at <= ? ORDER BY p.due_at LIMIT 5",
         (0.0,)))]
    assert all("SCAN quotes" not in p for p in plans[:1]) and "pending_due" in plans[1]
    done = World(tmp_path, monkeypatch)
    c = done.col.day("2020-01-01")                          # an old store with nothing pending
    c.commit()
    done.col.resume()
    assert "2020-01-01" not in done.col.days and DAY in done.col.days



def test_the_key_guard_refuses_a_missing_or_short_salt_and_an_unreadable_reference(tmp_path):
    """Gamma: missing salt, wrong-length salt and an unreadable reference file each refuse."""
    import hashlib
    import os
    env = "cap"
    (tmp_path / "funded_key.fp").write_text(hashlib.sha256(b"x" * 32 + b"fund").hexdigest())
    with pytest.raises(SystemExit, match="reference missing"):
        C.key_identity_guard(tmp_path, env)                 # fp present, salt missing
    (tmp_path / "host_salt").write_bytes(os.urandom(16))
    os.chmod(tmp_path / "host_salt", 0o600)
    with pytest.raises(SystemExit, match="malformed"):
        C.key_identity_guard(tmp_path, env)                 # 16-byte salt
    (tmp_path / "host_salt").write_bytes(b"x" * 32)
    os.chmod(tmp_path / "funded_key.fp", 0o000)
    try:
        if os.access(tmp_path / "funded_key.fp", os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        with pytest.raises(SystemExit, match="unreadable"):
            C.key_identity_guard(tmp_path, env)
    finally:
        os.chmod(tmp_path / "funded_key.fp", 0o600)



def test_the_process_never_holds_the_funded_key(tmp_path, monkeypatch):
    """Delta 86c5c0f87 / Alpha: no .env loader; the capture key only from --capture-env; a secret-like variable name in
    the environment refuses start before any file or network access."""
    import ast
    src = (ROOT / "scripts/research/dex_boost_collector.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
            assert not any("envfiles" in m for m in mods), mods
        if isinstance(node, ast.Name):
            assert node.id != "load_env_files"
        if isinstance(node, ast.Attribute) and node.attr == "get":
            assert not (isinstance(node.value, ast.Attribute) and node.value.attr == "environ"), "reads env values"
    for name in ("MEMECOIN_" + "JUPITER_API_KEY", "MEMECOIN_CAPTURE_JUPITER_API_KEY", "TELEGRAM_BOT_TOKEN"):
        with pytest.raises(SystemExit, match="secret-like"):
            C.funded_env_refusal({"PATH": "/bin", name: "x"})
    assert C.funded_env_refusal({"PATH": "/bin", "HOME": "/h", "SNIPER_STATE_ROOT": "/s"})
    monkeypatch.setenv("MEMECOIN_" + "JUPITER_API_KEY", "inherited")
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    with pytest.raises(SystemExit, match="secret-like"):
        C.main([])


def test_the_capture_env_file_holds_exactly_the_capture_key(tmp_path):
    import os
    f = tmp_path / "capture.env"
    with pytest.raises(SystemExit, match="missing or unreadable"):
        C.read_capture_env(f)
    for body, why in (("MEMECOIN_CAPTURE_JUPITER_API_KEY=abc\nOTHER_KEY=x\n", "exactly one"),
                      ("MEMECOIN_" + "JUPITER_API_KEY=abc\n", "exactly one"),
                      ("MEMECOIN_CAPTURE_JUPITER_API_KEY=\n", "empty")):
        f.write_text(body)
        os.chmod(f, 0o600)
        with pytest.raises(SystemExit, match=why):
            C.read_capture_env(f)
    f.write_text("# capture only\nMEMECOIN_CAPTURE_JUPITER_API_KEY=\"abc\"\n")
    os.chmod(f, 0o600)
    assert C.read_capture_env(f) == "abc"
    os.chmod(f, 0o640)
    with pytest.raises(SystemExit, match="group/others"):
        C.read_capture_env(f)


def test_a_tape_diagnostic_keeps_the_fee_basis_label():
    import sqlite3
    from types import SimpleNamespace
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE diagnostics(event_id,offset_s,side,label,taken_at,amount_in,amount_out,reason)")
    fake = SimpleNamespace(ps_buy=object(), ps_sell=object(), _ps=lambda *a: (dict(tokens_atomic="5", proof=dict(fee_basis="newest_nonzero")), None))
    C.Collector._diagnostic(fake, conn, "e", 0.0, "buy", "M", 1, 1.0, None)
    assert conn.execute("SELECT label,reason FROM diagnostics").fetchone() == ("ps_tape_crosscheck", "fee_basis:newest_nonzero")
