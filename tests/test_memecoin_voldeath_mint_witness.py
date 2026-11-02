"""The mint-wide vol_death witness (docs/superpowers/specs/2026-09-28-memecoin-voldeath-mint-witness-design.md).

A fire needs the whole MINT quiet, not only the one pool we watch: in 4 of 21 funded Solana fires
since 2026-09-22 the coin kept trading on other pools. The fixture holds the REAL signature windows
around five fires (Gamma, d9d93786): four must veto (`mint_moving`), PHISH 66467 must still fire.
Every way the read can fail must still FIRE (fail-open to the exit, the operator's rule)."""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from sniper.services import memecoin_volume_death as V
from sniper.services.memecoin_solana_swap_marks import (
    SolanaSwapMarkCache, mint_window_verdict, own_mint_signatures, probe_mint_activity,
)

FIXTURE = Path(__file__).parent / "fixtures" / "memecoin" / "voldeath_mint_window_frames.json"
POSITIONS = json.loads(FIXTURE.read_text())["positions"]
EXIT_S = 30.0


def _rows(p):
    return sorted(p["signatures"], key=lambda r: (r["slot"], r.get("transactionIndex") or 0), reverse=True)


def _now(p):
    return datetime.fromisoformat(p["fired_at"]).timestamp()


@pytest.mark.parametrize("visible", ["all_frames", "confirmed_before_the_fire"])
@pytest.mark.parametrize("p", POSITIONS, ids=[str(p["position_id"]) for p in POSITIONS])
def test_the_real_windows(p, visible):
    rows = _rows(p)
    if visible == "confirmed_before_the_fire":
        # What a read AT the fire could have seen (Lambda's review): blockTime runs 0.2-1.3 s behind
        # our own submit stamp, so only rows strictly before the fire's second are certainly visible.
        rows = [r for r in rows if r.get("blockTime") is not None and r["blockTime"] < _now(p) - 1]
    verdict = mint_window_verdict(rows, now_ts=_now(p), exit_s=EXIT_S, own_sigs={p["our_sale_sig"]})
    assert ("mint_moving" if verdict[0] == "moving" else "fire") == p["expected"], verdict


def _row(sig, bt, err=None):
    return {"signature": sig, "slot": int(bt), "err": err, "blockTime": bt}


NOW = 1_790_000_100.4


def test_the_window_edges():
    v = lambda rows: mint_window_verdict(rows, now_ts=NOW, exit_s=EXIT_S, own_sigs=set())[0]
    assert v([_row("a", int(NOW))]) == "still"                     # the fire's own second: excluded
    assert v([_row("a", int(NOW) - 1)]) == "moving"                # one second before the fire
    assert v([_row("a", NOW - 28)]) == "moving"                    # the window's oldest edge
    assert v([_row("a", NOW - 29.5), _row("b", NOW - 40)]) == "still"   # before the window
    assert v([]) == "still"


def test_own_failed_and_undated_rows_do_not_count():
    rows = [_row("ours", NOW - 5), _row("failed", NOW - 6, err={"InstructionError": [0, "x"]}),
            {"signature": "undated", "slot": 1, "err": None, "blockTime": None}, _row("old", NOW - 90)]
    assert mint_window_verdict(rows, now_ts=NOW, exit_s=EXIT_S, own_sigs={"ours"})[0] == "still"
    moving = mint_window_verdict(rows + [_row("theirs", NOW - 3)], now_ts=NOW, exit_s=EXIT_S, own_sigs={"ours"})
    assert moving == ("moving", NOW - 3, 1, "theirs")


def test_a_full_page_that_never_reaches_back_past_the_window_is_unknown():
    rows = [_row(f"f{i}", NOW - 2 - i * 0.01, err={"x": 1}) for i in range(100)]
    assert mint_window_verdict(rows, now_ts=NOW, exit_s=EXIT_S, own_sigs=set())[0] == "unknown"
    assert mint_window_verdict(rows[:99], now_ts=NOW, exit_s=EXIT_S, own_sigs=set())[0] == "still"


def test_a_busy_mint_is_still_seen_behind_twenty_newer_rows():
    newer = [_row(f"n{i}", NOW - 0.5) for i in range(40)]          # inside the fire's last second
    assert mint_window_verdict(newer + [_row("t", NOW - 5)], now_ts=NOW, exit_s=EXIT_S,
                               own_sigs=set())[0] == "moving"


def test_malformed_answers_are_unknown():
    for bad in (None, {"rows": []}, [{"slot": 1}], [{"signature": "s", "blockTime": "x"}]):
        assert mint_window_verdict(bad, now_ts=NOW, exit_s=EXIT_S, own_sigs=set())[0] == "unknown"


def test_every_read_failure_is_unknown_and_bounded():
    def raises(*_a):
        raise RuntimeError("HTTP 429 max usage reached")

    def sleeps(*_a):
        time.sleep(2.0)
        return []

    args = dict(now_ts=NOW, exit_s=EXIT_S, own_sigs=set())
    assert probe_mint_activity("M", rpc_call=raises, **args)[0] == "unknown"
    t = time.monotonic()
    assert probe_mint_activity("M", rpc_call=sleeps, deadline_s=0.2, **args) == ("unknown", "timeout", 0, None)
    assert time.monotonic() - t < 1.0                               # the deadline bounds the wait
    assert probe_mint_activity("M", rpc_call=lambda *_a: [_row("x", NOW - 4)], **args)[0] == "moving"


def _await(cache, **args):
    deadline, got = time.monotonic() + 3, "pending"
    while got == "pending" and time.monotonic() < deadline:
        got = cache.onchain_witness(**args)
        time.sleep(0.01)
    return got


ARGS = dict(position_id=7, mint="M", window_start_iso="2026-09-28T12:00:00+00:00",
            now_iso="2026-09-28T12:00:30+00:00", current_pool_key="Pool", exit_s=EXIT_S, veto_max_s=120.0)
NO_OWN = lambda _mint: set()                                           # noqa: E731


def test_the_async_witness_vetoes_on_the_mint_and_caches_the_veto():
    now_ts = datetime.fromisoformat(ARGS["now_iso"]).timestamp()
    calls = []

    def mint_probe(mint, now_ts, exit_s, own_sigs):
        calls.append(now_ts)
        return ("moving", int(now_ts) - 3, 2, "SIG")
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_a: "still", mint_probe=mint_probe, own_sigs_fn=NO_OWN)
    assert _await(cache, **ARGS) == "moving:mint"
    time.sleep(3.2)                                                  # the vault verdict goes stale
    assert _await(cache, **ARGS) == "moving:mint"                    # re-probed: veto still cached
    assert len(calls) == 1                                           # one mint read, not one per tick
    assert cache.mint_note(7).startswith("mint=moving") and "sig=SIG" in cache.mint_note(7)


@pytest.mark.parametrize("mint_probe", [
    lambda *_a, **_k: ("unknown", "timeout", 0, None),
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("raised inside the pool")),
], ids=["unknown", "raises"])
def test_a_failed_mint_read_still_fires(mint_probe):
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_a: "still", mint_probe=mint_probe, own_sigs_fn=NO_OWN)
    assert _await(cache, **ARGS) == "still"
    assert cache.mint_note(7).startswith("mint=unknown")


def test_without_exit_s_there_is_no_mint_read():
    cache = SolanaSwapMarkCache(onchain_probe=lambda *_a: "still",
                                mint_probe=lambda *_a, **_k: pytest.fail("the mint was read"))
    assert _await(cache, **dict(ARGS, exit_s=None)) == "still"


# ---------------------------------------------------------------- the decision
OPEN, NOWISO, COHORT = "2026-08-29T12:00:00+00:00", "2026-08-29T12:01:01+00:00", "2026-01-01T00:00:00+00:00"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE memecoin_position_marks (id INTEGER PRIMARY KEY, chain_id TEXT,
      mint TEXT, sweep_ts TEXT, mid REAL, executable_exit_value REAL, liquidity REAL, mcap REAL,
      route_quote_id TEXT, source TEXT, quality TEXT)""")
    c.execute("CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, tx_sig TEXT)")
    c.execute("INSERT INTO memecoin_fills (position_id, tx_sig) VALUES (7, 'our-entry-sig')")
    return c


def _decide(c, cache, **kw):
    pos = {"id": 7, "opened_at": OPEN, "mint": "M", "network_id": "solana", "quote_route_target": None}
    return V.armed_exit_decision(c, pos, now_iso=NOWISO, held_s=61.0, exit_s=60.0, min_opened_at=COHORT,
                                 stream_ok=False, require_onchain=True, reserve_witness_cache=cache,
                                 exit_quote_evidence={"route_plan": [{"ammKey": "Pool", "inputMint": "M"}]}, **kw)


def test_the_decision_names_mint_moving_and_passes_exit_s_and_the_cap(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_MINT_WITNESS", True, raising=False)
    seen = {}
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **kw: (seen.update(kw), "moving:mint")[1]
    assert _decide(conn, cache)[0:3:2] == (False, "mint_moving")
    assert seen["exit_s"] == 60.0 and seen["veto_max_s"] == cfg.MEMECOIN_VOLDEATH_MINT_VETO_MAX_S
    assert "mint_moving" in V.POST_THRESHOLD_REASONS and "mint_moving" in V.HELD_WITNESS_REASONS


def test_the_knob_off_is_todays_decision(conn, monkeypatch):
    from sniper.settings import memecoin as cfg
    seen = {}
    cache = SolanaSwapMarkCache()
    cache.onchain_witness = lambda **kw: (seen.update(kw), "still")[1]
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_MINT_WITNESS", False, raising=False)
    assert _decide(conn, cache) == (True, 60.0, "measured_onchain_still")
    assert seen["exit_s"] is None


def test_the_audit_note_rides_only_on_the_decisions_the_mint_witness_ended():
    cache = SolanaSwapMarkCache()
    cache._mint_notes[7] = "mint=unknown:timeout"
    assert V.mint_audit_note(cache, 7, "measured_onchain_still") == " mint=unknown:timeout"
    assert V.mint_audit_note(cache, 7, "mint_moving") == " mint=unknown:timeout"
    assert V.mint_audit_note(cache, 7, "route_changed") == ""
    assert V.mint_audit_note(None, 7, "measured_state_still") == ""
    assert V.mint_audit_note(cache, 8, "measured_state_still") == ""


def test_the_runner_writes_the_note_into_the_vol_death_audit_detail():
    src = (Path(__file__).resolve().parents[1] / "sniper/services/memecoin_real_runner.py").read_text()
    assert 'detail="%s silence=%.1fs%s" % (_vd_why, _vd_silence or -1.0, mint_audit_note(' in src


def test_the_knob_is_off_by_default():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_VOLDEATH_MINT_WITNESS is False          # activated by an explicit .env line
    assert cfg.MEMECOIN_VOLDEATH_MINT_VETO_MAX_S == 120.0


def test_a_mint_trading_every_20s_never_holds_past_the_cap():
    # Lambda F1: any mint tx every <= 27 s would hold the rail until max_hold.
    reads = []

    def mint_probe(mint, now_ts, exit_s, own_sigs):
        reads.append(now_ts)
        return ("moving", now_ts - 1, 1, "S%d" % len(reads))
    cache = SolanaSwapMarkCache(mint_probe=mint_probe, own_sigs_fn=NO_OWN)
    t0 = 1_790_000_000.0
    got = [cache._mint_check(7, "M", t0 + i * 20, EXIT_S, veto_max_s=120.0) for i in range(7)]
    assert got[:6] == ["moving:mint"] * 6 and got[6] == "still"            # 0..100 s held, 120 s fires
    assert cache.mint_note(7).startswith("mint=unknown:veto_cap")
    assert cache._mint_check(7, "M", t0 + 140, EXIT_S, veto_max_s=120.0) == "moving:mint"   # new episode


def test_our_own_signatures_on_the_mint_cover_every_position_and_the_in_flight_ones():
    # Lambda F2: fills of EVERY funded position on the mint, de-risk and close sigs, pending entries.
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, mint TEXT, execution_mode TEXT, "
              "derisk_sig TEXT, close_sig TEXT)")
    c.execute("CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, tx_sig TEXT)")
    c.execute("CREATE TABLE memecoin_pending_entries (id INTEGER PRIMARY KEY, mint TEXT, tx_sig TEXT, "
              "cleanup_tx_sig TEXT)")
    c.executemany("INSERT INTO memecoin_positions VALUES (?,?,?,?,?)", [
        (1, "M", "real", "derisk1", None), (2, "M", "real", None, "close2"), (3, "M", "paper", "px", "py"),
        (4, "OTHER", "real", "o1", "o2")])
    c.executemany("INSERT INTO memecoin_fills (position_id, tx_sig) VALUES (?,?)",
                  [(1, "fill1"), (2, "fill2"), (3, "paperfill"), (4, "otherfill"), (1, None)])
    c.executemany("INSERT INTO memecoin_pending_entries (mint, tx_sig, cleanup_tx_sig) VALUES (?,?,?)",
                  [("M", "pend1", "clean1"), ("OTHER", "pend2", None)])
    assert own_mint_signatures("M", conn=c) == {"fill1", "fill2", "derisk1", "close2", "pend1", "clean1"}


def test_an_unreadable_own_signature_list_is_unknown_and_fires():
    def boom(_mint):
        raise RuntimeError("ledger locked")
    cache = SolanaSwapMarkCache(mint_probe=lambda *_a, **_k: pytest.fail("read without own sigs"),
                                own_sigs_fn=boom)
    assert cache._mint_check(7, "M", 1_790_000_000.0, EXIT_S, 120.0) == "still"
    assert cache.mint_note(7).startswith("mint=unknown:own_sigs")


def test_two_reads_in_flight_refuse_a_third_and_release_their_slots():
    # Lambda F3: no backlog that bursts into the key when it recovers.
    import threading

    def slow(*_a):
        time.sleep(0.6)
        return []
    args = dict(now_ts=NOW, exit_s=EXIT_S, own_sigs=set(), deadline_s=0.05)
    threads = [threading.Thread(target=probe_mint_activity, args=("M",), kwargs=dict(rpc_call=slow, **args))
               for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert probe_mint_activity("M", rpc_call=lambda *_a: [], **args) == ("unknown", "busy", 0, None)
    time.sleep(0.8)
    assert probe_mint_activity("M", rpc_call=lambda *_a: [], **args)[0] == "still"


def test_the_held_line_logs_once_per_veto_episode():
    cache = SolanaSwapMarkCache(own_sigs_fn=NO_OWN,
                                mint_probe=lambda mint, now_ts, exit_s, own_sigs: ("moving", now_ts - 1, 1, "S"))
    assert V.held_log_due(cache, 7, "mint_moving") is True
    assert V.held_log_due(cache, 7, "mint_moving") is False
    assert V.held_log_due(cache, 7, "state_moving") is True                 # other reasons unchanged
    cache._mint_probe = lambda *_a, **_k: ("still", None, 0, None)
    cache._mint_check(7, "M", 1_790_000_500.0, EXIT_S, 120.0)             # the episode ends
    assert V.held_log_due(cache, 7, "mint_moving") is True


def test_the_cached_veto_expires_by_each_callers_own_exit_s():
    # Lambda F7: the cache keeps the newest time per mint; each caller computes its own expiry.
    reads = []

    def mint_probe(mint, now_ts, exit_s, own_sigs):
        reads.append(now_ts)
        return ("moving", now_ts - 1, 1, "S")
    cache = SolanaSwapMarkCache(mint_probe=mint_probe, own_sigs_fn=NO_OWN)
    t0 = 1_790_000_000.0
    assert cache._mint_check(7, "M", t0, 30.0, 600.0) == "moving:mint"        # newest = t0 - 1
    assert cache._mint_check(8, "M", t0 + 40, 60.0, 600.0) == "moving:mint"   # 60 s window: still cached
    assert len(reads) == 1
    assert cache._mint_check(9, "M", t0 + 40, 30.0, 600.0) == "moving:mint"   # 30 s window: expired, re-read
    assert len(reads) == 2
