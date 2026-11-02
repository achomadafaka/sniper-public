"""G2 readiness (Alpha GO, 2026-09-30; pre-registration a9691d8da).

R1: the heartbeat kept only its latest cumulative counters, so the shadow's busiest minute (the
"captured burst") could not be found afterwards. The heartbeat thread now appends one line per
completed UTC minute to a bounded JSONL series. Observer only: the receive loop still only bumps
counters, and the journal worker only assigns the block number it already parsed.

R2: no proven wallet trades on Ink, so the address registry's Ink owners give G2 no sample. An
OBSERVATION-ONLY panel (MEMECOIN_INK_OBSERVATION_PANEL) joins the canonical backfill's watched set
(ground truth for the stream), and is recorded in the journal as observation-only. It never reaches
the projection into the funded ledger: never projected, never armed, never a registry owner.
"""
import json
import queue
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.research import onchain_ink_watch as ink  # noqa: E402
from sniper.persistence import db_evm_stream as journal  # noqa: E402

A, B, C = ("0x" + c * 40 for c in "abc")


def _t(minute, second=0):
    return datetime(2026, 9, 30, 12, minute, second, tzinfo=timezone.utc)


# --- R1: the per-minute series ---

def test_one_line_per_completed_minute_with_the_frame_delta(tmp_path):
    path = tmp_path / "minutes.jsonl"
    s = ink.MinuteSeries(path, max_bytes=10**6)
    stats = {"frames": 100, "overflows": 0, "sessions": 1, "last_block": 500}
    s.observe(_t(0, 10), stats, qsize=3)             # the start minute: nothing completed yet
    stats.update(frames=160, last_block=560)
    s.observe(_t(0, 55), stats, qsize=4)
    stats.update(frames=250, overflows=1, sessions=2, last_block=620)
    s.observe(_t(1, 5), stats, qsize=9)              # minute 12:00 completed
    stats.update(frames=300, last_block=680)
    s.observe(_t(2, 0), stats, qsize=0)              # minute 12:01 completed
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [ln["minute"] for ln in lines] == ["2026-09-30T12:00Z", "2026-09-30T12:01Z"]
    # A minute's line carries the counters as last seen INSIDE that minute; frames after that beat
    # count in the next minute, so the attribution error is at most one beat (5 s live).
    assert lines[0]["partial"] is True and lines[1]["partial"] is False
    assert lines[0]["frames_delta"] == 60 and lines[0]["frames"] == 160
    assert lines[1]["frames_delta"] == 90 and lines[1]["frames"] == 250
    assert lines[1]["sessions"] == 2 and lines[1]["overflows"] == 1
    assert lines[1]["last_block"] == 620 and lines[1]["queue_depth"] == 9


def test_the_series_file_is_bounded(tmp_path):
    path = tmp_path / "minutes.jsonl"
    s = ink.MinuteSeries(path, max_bytes=600)
    stats = {"frames": 0, "overflows": 0, "sessions": 1, "last_block": 1}
    for minute in range(20):
        stats["frames"] += 10
        s.observe(_t(minute), stats, qsize=0)
    backup = path.with_name(path.name + ".1")
    assert path.stat().st_size <= 600 and backup.exists() and backup.stat().st_size <= 600
    assert not path.with_name(path.name + ".2").exists()


def test_an_unwritable_series_never_raises(tmp_path):
    s = ink.MinuteSeries(tmp_path / "missing_dir" / "minutes.jsonl", max_bytes=10**6)
    stats = {"frames": 1, "overflows": 0, "sessions": 1, "last_block": 1}
    s.observe(_t(0), stats, qsize=0)
    s.observe(_t(1), stats, qsize=0)                  # would write; the directory is absent


def test_the_heartbeat_feeds_the_series_every_beat(tmp_path):
    seen = []

    class Series:
        def observe(self, now, stats, qsize):
            seen.append((stats["frames"], qsize))
    stop = threading.Event()
    t = threading.Thread(target=ink.heartbeat_loop,
                         args=(stop, tmp_path / "beat", {"frames": 7, "sessions": 1}, {}),
                         kwargs={"period_s": 0.02, "series": Series(), "qsize_fn": lambda: 5})
    t.start()
    for _ in range(100):
        if seen:
            break
        stop.wait(0.02)
    stop.set()
    t.join(2)
    assert seen and seen[0] == (7, 5)


def test_the_journal_worker_records_the_block_it_parsed(tmp_path):
    frames, stop, overflow = queue.Queue(), threading.Event(), threading.Event()
    stats = {}
    frames.put((1, json.dumps({"metadata": {"block_number": "0x1f4"}}), 1, "2026-09-30T12:00:00Z"))
    t = threading.Thread(target=ink.process_frames,
                         args=(frames, stop, overflow, str(tmp_path / "journal.db")),
                         kwargs={"stats": stats})
    t.start()
    for _ in range(250):                               # bounded: a dead worker must not hang
        if "last_block" in stats:
            break
        stop.wait(0.02)
    stop.set()
    t.join(5)
    assert stats.get("last_block") == 500


# --- R2: the observation-only panel ---

def _panel(tmp_path, owners):
    path = tmp_path / "panel.json"
    path.write_text(json.dumps({"label": "observation-only", "source": "census",
                                "frozen_at": "2026-09-30T12:00:00Z", "owners": owners}))
    return path


def test_a_panel_loads_as_lowercase_unique_addresses(tmp_path):
    panel = ink.load_panel(_panel(tmp_path, [A.upper().replace("0X", "0x"), A, B]))
    assert panel["owners"] == frozenset({A, B}) and panel["source"] == "census"


@pytest.mark.parametrize("owners", [[], ["0x123"], [A, "not-an-address"], "0x" + "a" * 40])
def test_a_malformed_panel_is_refused(tmp_path, owners):
    with pytest.raises(ValueError):
        ink.load_panel(_panel(tmp_path, owners))


def test_the_panel_is_labelled_observation_only_in_the_journal(tmp_path):
    with sqlite3.connect(tmp_path / "journal.db") as conn:
        journal.ensure_stream_tables(conn)
        journal.record_observation_panel(conn, frozenset({A, B}), source="census",
                                         frozen_at="2026-09-30T12:00:00Z")
        rows = conn.execute("SELECT address, label FROM ink_observation_panel ORDER BY address"
                            ).fetchall()
    assert rows == [(A, "observation_only_never_projected"), (B, "observation_only_never_projected")]


def test_backfill_reads_the_panel_and_projection_never_sees_it(monkeypatch):
    calls = {}
    monkeypatch.setattr(ink, "backfill_once",
                        lambda stream, rpc, *, watched: calls.setdefault("backfill", dict(watched)))
    monkeypatch.setattr(ink, "project_canonical_once",
                        lambda stream, main, rpc, *, watched, **kw: calls.setdefault(
                            "projection", dict(watched)) and 0)

    class Scope:
        def __enter__(self):
            return "main"

        def __exit__(self, *exc):
            return False
    registry = {A: ("user-a", "handle-a")}
    ink.projection_tick("stream", "rpc", Scope, registry=registry, panel=frozenset({B, C}),
                        quote_usd=2500.0, quote_age_s=1.0, decimals_fn=None, wake=None)
    assert set(calls["backfill"]) == {A, B, C}
    assert calls["projection"] == registry


def test_no_quote_price_still_advances_projection_without_the_funded_ledger(monkeypatch):
    """The cursor must keep moving (a later registry owner must not inherit a backlog), and a
    projection pass that writes nothing must not open sniper_tracker.db."""
    calls, opened = [], []
    monkeypatch.setattr(ink, "backfill_once", lambda *a, **k: calls.append("backfill"))
    monkeypatch.setattr(ink, "project_canonical_once", lambda *a, **k: calls.append("project"))
    ink.projection_tick("stream", "rpc", lambda: opened.append(1), registry={},
                        panel=frozenset({B}), quote_usd=None, quote_age_s=None,
                        decimals_fn=None, wake=None)
    assert calls == ["backfill", "project"] and opened == []


BLOCK_HASH, REORG_HASH = "0x" + "bb" * 32, "0x" + "dd" * 32
TXS = ("0x" + "a1" * 32, "0x" + "a2" * 32)


def _journal(path, owner):
    """Two canonical observations from `owner` in block 7, in the order production writes them."""
    conn = sqlite3.connect(path)
    journal.ensure_stream_tables(conn)
    for n, tx in enumerate(TXS):
        key = f"57073:payload:{n}:{tx}"
        journal.insert_observation(conn, {
            "observation_key": key, "payload_id": "payload", "block_number": 7, "tx_hash": tx,
            "owner": owner, "state": "provisional",
            "receipt": {"transactionHash": tx, "status": "0x1", "logs": []}})
        journal.reconcile_canonical(conn, key, {"transactionHash": tx, "blockHash": BLOCK_HASH,
                                                "blockNumber": "0x7", "status": "0x1",
                                                "logs": []}, block_hash=BLOCK_HASH)
    journal.record_canonical_block(conn, 7, BLOCK_HASH)
    conn.commit()
    return conn


def _rpc(method, params):
    if method == "eth_getBlockByNumber":
        return {"number": "0x7", "hash": BLOCK_HASH, "timestamp": hex(1790769600)}
    return {"hash": params[0], "blockHash": BLOCK_HASH, "blockNumber": "0x7"}


def _tick(stream, scope, registry):
    ink.projection_tick(stream, _rpc, scope, registry=registry, panel=frozenset({B}),
                        quote_usd=2500.0, quote_age_s=1.0, decimals_fn=lambda m: 18, wake=None)


def test_a_pass_over_panel_changes_never_opens_the_funded_ledger(tmp_path):
    stream = _journal(tmp_path / "journal.db", B)
    opened = []
    scope = lambda: opened.append(1) or pytest.fail("opened sniper_tracker.db")
    _tick(stream, scope, {A: ("user-a", "handle-a")})
    assert journal.next_unprojected(stream) == []                    # the cursor still advanced
    journal.record_canonical_block(stream, 7, REORG_HASH)            # a reorg retracts both
    stream.commit()
    assert journal.next_unprojected(stream)
    _tick(stream, scope, {A: ("user-a", "handle-a")})
    assert opened == [] and journal.next_unprojected(stream) == []


def test_a_registry_owner_retraction_still_marks_its_detection(tmp_path):
    from sniper.persistence import db_memecoin as funded
    stream = _journal(tmp_path / "journal.db", B)
    main = sqlite3.connect(tmp_path / "main.db")
    funded.ensure_memecoin_tables(main)
    for n, tx in enumerate(TXS):
        key = f"57073:payload:{n}:{tx}"
        main.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, "
                     "network_id, mint, detected_at, created_at, source_observation_key) "
                     "VALUES (?,?,?,?,?,?,?,?)", (key, "u", B, "57073", "0xmint",
                                                   "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z", key))
    main.commit()

    class Scope:
        def __enter__(self):
            return main

        def __exit__(self, *exc):
            main.commit()
            return False
    _tick(stream, Scope, {B: ("u", "h")})
    journal.record_canonical_block(stream, 7, REORG_HASH)
    stream.commit()
    _tick(stream, Scope, {B: ("u", "h")})
    states = {r[0] for r in main.execute("SELECT source_observation_state FROM onchain_wallet_detections")}
    assert states == {"retracted"}


@pytest.mark.parametrize("registry, since, due", [
    ({}, 999.0, False),                        # nothing can be projected: no Binance call
    ({A: ("u", "h")}, 11.0, True),
    ({A: ("u", "h")}, 5.0, False),
])
def test_the_eth_price_is_read_only_when_a_registry_owner_can_use_it(registry, since, due):
    assert ink.price_due(registry, since_last_s=since) is due


# --- the shadow's ETH/USD never reaches the funded FMP key (Alpha, from Gamma's pre-read) ---

def test_the_shadow_price_is_binance_only_and_a_failure_is_unknown(monkeypatch):
    from sniper.execution import evm_wallet
    monkeypatch.setattr(evm_wallet, "asset_usd_price",
                        lambda *a, **k: pytest.fail("the shadow reached the FMP fallback path"))
    urls = []

    def failing(method, url, **kw):
        urls.append(url)
        return 500, None, None
    assert ink.binance_eth_usd(http=failing) is None
    assert urls == [ink.BINANCE_ETH_URL] and "binance.com" in urls[0]

    def ok(method, url, **kw):
        return 200, {"symbol": "ETHUSDT", "price": "2471.5"}, None
    assert ink.binance_eth_usd(http=ok) == 2471.5


@pytest.mark.parametrize("reply", [None, {}, {"price": "nan"}, {"price": "-1"}, {"price": "x"}])
def test_an_unusable_binance_reply_is_unknown(reply):
    assert ink.binance_eth_usd(http=lambda *a, **k: (200, reply, None)) is None


def test_the_projection_loop_never_names_a_funded_price_seam():
    code = ink._projection_loop.__code__
    names = set(code.co_names) | set(code.co_varnames)
    assert not names & {"quote_usd_price", "native_usd_price", "asset_usd_price", "eth_usd_price"}
    assert "binance_eth_usd" in names


# --- Gamma review of ae120ee62 (2026-09-30): LOW 1-3 ---

def test_a_minute_after_a_heartbeat_stall_is_partial_and_carries_its_span(tmp_path):
    """LOW2: a stalled heartbeat must not hand a multi-minute frame delta to one clean minute, or
    C8's busiest minute can be a stall. Every line carries span_s, the seconds its delta covers."""
    path = tmp_path / "minutes.jsonl"
    s = ink.MinuteSeries(path, max_bytes=10**6)
    stats = {"frames": 100, "overflows": 0, "sessions": 1, "last_block": 500}
    s.observe(_t(0, 10), stats, qsize=0)
    stats.update(frames=160)
    s.observe(_t(0, 55), stats, qsize=0)
    stats.update(frames=4960)
    s.observe(_t(6, 5), stats, qsize=0)              # 12:00 completes; 12:01..12:05 had no beat
    stats.update(frames=4990)
    s.observe(_t(6, 55), stats, qsize=0)
    stats.update(frames=5000)
    s.observe(_t(7, 0), stats, qsize=0)              # 12:06 completes
    stats.update(frames=5060)
    s.observe(_t(7, 55), stats, qsize=0)
    s.observe(_t(8, 0), stats, qsize=0)              # 12:07 completes, adjacent to 12:06
    lines = {ln["minute"][-6:]: ln for ln in map(json.loads, path.read_text().splitlines())}
    assert lines["12:00Z"]["span_s"] == 45.0
    stalled = lines["12:06Z"]
    assert stalled["partial"] is True and stalled["frames_delta"] == 4830
    assert stalled["span_s"] == 360.0                 # 12:00:55 -> 12:06:55
    after = lines["12:07Z"]
    assert after["partial"] is False and after["frames_delta"] == 70 and after["span_s"] == 60.0


def test_no_quote_price_holds_a_registry_observation_without_any_read(tmp_path):
    """LOW3: without ETH/USD a registry owner's observation cannot be valued; the pass must stop
    BEFORE its block and tx reads, or a Binance outage becomes 2 research RPC/s."""
    stream = _journal(tmp_path / "journal.db", A)
    reads = []

    def rpc(method, params):
        reads.append(method)
        return _rpc(method, params)
    main = type("Main", (), {"__getattr__": lambda self, n: pytest.fail("opened the funded ledger")})()
    pending = len(journal.next_unprojected(stream))
    ink.project_canonical_once(stream, main, rpc, watched={A: ("user-a", "handle-a")},
                               quote_usd=None, decimals_fn=lambda m: 18,
                               detected_at="2026-09-30T12:00:00Z")
    assert reads == []
    assert pending and len(journal.next_unprojected(stream)) == pending   # held, not skipped


@pytest.mark.parametrize("owners, refused", [(None, True), ([], True), (["0xnope"], True),
                                             ([A], False)])
def test_a_bad_panel_refuses_to_start_and_a_good_one_reaches_run(tmp_path, monkeypatch, owners,
                                                                  refused):
    """LOW1: a set but unreadable or malformed panel must stop the observer before run()."""
    path = tmp_path / "panel.json"
    if owners is not None:
        _panel(tmp_path, owners)
    started = []
    monkeypatch.setattr(ink, "run", lambda **kw: started.append(kw["panel"]))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))          # no .env there
    monkeypatch.setenv("MEMECOIN_INK_OBSERVATION_PANEL", str(path))
    monkeypatch.setattr(sys, "argv", ["onchain_ink_watch.py"])
    if refused:
        with pytest.raises(SystemExit, match="refusing Ink observation panel"):
            ink.main()
        assert started == []
    else:
        ink.main()
        assert started and started[0]["owners"] == frozenset({A})


def test_the_panel_freeze_selftest_runs_under_pytest():
    """Gamma INFO (2026-09-30): the freeze's guards were covered only by its --selftest."""
    from scripts.research import ink_g2_panel_freeze
    ink_g2_panel_freeze.selftest()
