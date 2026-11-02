"""J7 cross-check: the liveness reference for the watcher's topic-only subscriptions (2026-09-29).

Spec 2026-09-29-memecoin-evm-watch-dead-subscription-design.md, Alpha GO (D2). The transfers and sells
filters are sparse by design, so their silence proves nothing; J7 reports the same wallets' trades about
a second after their blocks. On healthy connections the socket delivered 19 of 19 J7 watched buys (it led
J7 by p50 0.29 s, at worst 1.18 s behind); on the two dead ones, 0 of 14. A J7 event by an acked address
whose tx the socket did not deliver within the grace forces a reconnect, at most one per min gap, and the
reconnect backfills the lane window so the missed trade is still read.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import scripts.research.onchain_evm_wallet_watch as w  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_onchain_evm_watch_backfill import WALLET, _heads, _run, env  # noqa: E402,F401

TX = "0x" + "8b" * 32
OTHER = "0x" + "99" * 20


def _row(i, *, tx=TX, trader=WALLET, side="buy", at=1000.0, chain="4663", lane="j7_wallets", fid=None):
    from datetime import datetime, timedelta, timezone
    received = (datetime(2026, 9, 29, 5, 43, 10, tzinfo=timezone.utc)
                + timedelta(seconds=at - 1000.0)).isoformat()
    return (i, fid or "swap:raybot:%s:%s:0x%s" % (tx, trader, "cd" * 20), received, side, chain, lane)


def _ts(row):
    from datetime import datetime
    return datetime.fromisoformat(row[2]).timestamp()


def _xc(**kw):
    return w.J7CrossCheck("4663", grace_s=15.0, min_gap_s=300.0, **kw)


def test_an_undelivered_watched_buy_forces_a_reconnect_after_the_grace():
    xc, r = _xc(), _row(1)
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
    assert xc.due(_ts(r) + 14.9) == [] and not xc.force.is_set(), "inside the grace: no verdict"
    misses = xc.due(_ts(r) + 15.0)
    assert [m["tx"] for m in misses] == [TX] and xc.force.is_set()
    assert xc.stats["missed"] == 1 and xc.stats["forced"] == 1


def test_a_delivered_buy_is_checked_not_forced():
    xc, r = _xc(), _row(1)
    xc.delivered_tx(TX)                               # the socket led J7, as it does
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
    assert xc.due(_ts(r) + 20) == [] and not xc.force.is_set() and xc.stats["checked"] == 1


@pytest.mark.parametrize("kw,why", [
    ({"trader": OTHER}, "an address the socket never acked"),
    ({"side": "sell"}, "a sell while the sell subscription is not live"),
    ({"chain": "56"}, "another chain"),
    ({"lane": "fomo_ws"}, "another delivery lane"),
])
def test_events_the_socket_was_never_asked_for_are_ignored(kw, why):
    xc, r = _xc(), _row(1, **kw)
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
    assert xc.due(_ts(r) + 60) == [] and not xc.force.is_set(), why


def test_an_event_from_before_this_connection_is_ignored():
    xc, r = _xc(), _row(1)
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) + 1)
    assert xc.due(_ts(r) + 60) == [] and not xc.force.is_set()


def test_a_live_sell_subscription_is_checked_too():
    xc, r = _xc(), _row(1, side="sell")
    xc.ingest([r], watched={WALLET}, sells_live=True, connected_at=_ts(r) - 60)
    assert len(xc.due(_ts(r) + 15)) == 1 and xc.force.is_set()


def test_a_second_miss_inside_the_min_gap_is_counted_not_forced():
    xc = _xc()
    a, b = _row(1), _row(2, tx="0x" + "7c" * 32, at=1100.0)
    xc.ingest([a], watched={WALLET}, sells_live=False, connected_at=_ts(a) - 60)
    xc.due(_ts(a) + 15)
    xc.force.clear()                                  # the push thread took it
    xc.ingest([b], watched={WALLET}, sells_live=False, connected_at=_ts(a) - 60)
    assert len(xc.due(_ts(b) + 15)) == 1 and not xc.force.is_set()
    assert xc.stats["suppressed"] == 1 and xc.stats["forced"] == 1


def test_an_unparsable_event_is_counted_and_skipped():
    xc, r = _xc(), _row(1, fid="fomo:12345")
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=0.0)
    assert xc.stats["unparsable"] == 1 and xc.pending == {}


def test_a_failed_ledger_read_is_unknown_never_dead():
    xc = _xc()

    def boom(last_id):
        raise RuntimeError("database is locked")
    assert w.j7_xcheck_tick(xc, boom, watched={WALLET}, sells_live=False, connected_at=0.0,
                            now=2000.0) == []
    assert xc.stats["unknown"] == 1 and not xc.force.is_set()


def test_the_cursor_starts_at_the_newest_event_and_moves_forward():
    xc, seen = _xc(), []

    def read(last_id):
        seen.append(last_id)
        return [] if last_id is None else [_row(last_id + 1)]
    w.j7_xcheck_tick(xc, read, watched={WALLET}, sells_live=False, connected_at=0.0, now=0.0)
    xc.last_id = 41
    w.j7_xcheck_tick(xc, read, watched={WALLET}, sells_live=False, connected_at=0.0, now=0.0)
    assert seen == [None, 41] and xc.last_id == 42


# --- wiring: the watcher's socket feeds `delivered`, and a forced reconnect backfills the lane window ---

@pytest.fixture
def wired(monkeypatch, env):
    captured = {}
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_J7_XCHECK_GRACE_S", 15.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_J7_XCHECK_MIN_GAP_S", 300.0, raising=False)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS", "4663")
    monkeypatch.setattr(w, "_start_j7_xcheck", lambda xc, tick: captured.update(xc=xc, tick=tick))
    return captured


def _miss(captured):
    def step(state, kw):
        xc = captured["xc"]
        r = _row(1)
        xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
        xc.due(_ts(r) + 15)
        captured["forced"] = kw["force_reconnect"]()
        captured["forced_again"] = kw["force_reconnect"]()
        state.reconnects += 1                      # what the stream does when told to
    return step


def test_a_forced_reconnect_backfills_the_lane_window(monkeypatch, env, wired):
    _run(monkeypatch, env, [_heads(2_000, 20), _miss(wired), _heads(2_025, 20)])
    assert wired["forced"] is True and wired["forced_again"] is False, "one reconnect per verdict"
    # The lane window, exactly: 45 s at the chain's 1 block/s back from the first head after the
    # reconnect, newest page first. Not the 6-block blip (2020-2025), and not the page cap.
    assert env["pages"] == [(2_016, 2_025), (2_006, 2_015), (1_996, 2_005), (1_986, 1_995),
                            (1_980, 1_985)]
    beat = json.loads(env["hb"].read_text())
    assert beat["j7_xcheck"]["forced"] == 1


def test_a_socket_delivery_counts_and_a_backfilled_one_does_not(monkeypatch, env, wired):
    def deliver(state, kw):
        kw["on_transfer"]({"transactionHash": TX, "address": "0x" + "cd" * 20,
                           "topics": [w.TRANSFER_TOPIC, "0x" + "00" * 32, "0x" + "00" * 12 + WALLET[2:]]})
        wired["delivered"] = TX in wired["xc"].delivered
        other = "0x" + "5e" * 32
        kw["on_transfer"]({"transactionHash": other, "address": "0x" + "cd" * 20,
                           "topics": [w.TRANSFER_TOPIC, "0x" + "00" * 32, "0x" + "00" * 12 + WALLET[2:]]},
                          backfill=True)
        wired["backfilled_counted"] = other in wired["xc"].delivered
    _run(monkeypatch, env, [_heads(2_000, 20), deliver])
    assert wired["delivered"] is True
    assert wired["backfilled_counted"] is False, "a backfill finding the trade does not prove the socket"


def test_with_the_knob_empty_no_check_runs(monkeypatch, env, wired):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_J7_XCHECK_CHAINS", "")
    _run(monkeypatch, env, [_heads(2_000, 5)])
    assert "xc" not in wired and "j7_xcheck" not in json.loads(env["hb"].read_text())


def _two_lane_events(wired):
    """A forced reconnect at block 2025, then a roster change at 2050 (the lane window is 45 blocks)."""
    return [_heads(2_000, 20), _miss(wired), _heads(2_025, 20), _roster_change, _heads(2_050, 20)]


def _roster_change(state, kw):                    # a second lane event within the cooldown
    state.last_reconnect_cause = "armed_set"
    state.reconnects += 1
    state.reconnect_causes["armed_set"] = state.reconnect_causes.get("armed_set", 0) + 1


def test_a_forced_lane_read_that_lost_a_page_is_read_again_by_the_next_lane_event(monkeypatch, env, wired):
    """Gamma M1 (2026-09-29): the cooldown and `backfill_hi` were recorded when the lane read was
    DISPATCHED. A read whose page 429'd (the flap case the cooldown exists for) closed the window,
    and the next lane event within 60 s read only from backfill_hi+1, so the page holding the buy the
    J7 cross-check forced for was never read again. Only a COMPLETE read starts the cooldown now."""
    import test_onchain_evm_watch_backfill as bf
    real_getlogs = bf._getlogs
    failed = {"left": 1}

    def flaky_getlogs(env_, hits, **kw):
        base = real_getlogs(env_, hits, **kw)

        def post(url, method, params):
            if method == "eth_getLogs":
                lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
                if lo <= 2_010 <= hi and failed["left"]:
                    failed["left"] -= 1
                    raise RuntimeError("HTTP 429")
            return base(url, method, params)
        return post
    monkeypatch.setattr(bf, "_getlogs", flaky_getlogs)
    _run(monkeypatch, env, _two_lane_events(wired), hits=[2_010])
    assert failed["left"] == 0, "the forced read did lose the page"
    rows = [p for row in env["inserts"] for p in row if isinstance(p, str) and p.startswith("4663:")]
    assert any(bf.TX in r for r in rows), "the next lane event read the lost page again"
    assert not any("lane-window backfill clipped" in line for line in env["lines"])


@pytest.mark.parametrize("max_calls,block_ts,why", [
    (3, True, "the page cap stopped the read short of the window's bottom"),
    (45, False, "a log was skipped: its block time was unavailable"),
])
def test_every_incomplete_lane_read_leaves_the_cooldown_open(monkeypatch, env, wired, max_calls, block_ts, why):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_BACKFILL_MAX_CALLS", max_calls, raising=False)
    _run(monkeypatch, env, _two_lane_events(wired), hits=[2_010], block_ts=block_ts)
    assert not any("lane-window backfill clipped" in line for line in env["lines"]), why


def test_a_complete_lane_read_closes_the_cooldown(monkeypatch, env, wired):
    """The control: the same two lane events with a complete first read clip the second."""
    _run(monkeypatch, env, _two_lane_events(wired), hits=[2_010])
    assert any("lane-window backfill clipped" in line for line in env["lines"])


def test_an_armed_set_resubscribe_followed_by_a_silent_reconnect_still_reads_the_lane_window(
        monkeypatch, env, wired):
    """56, 2026-09-29 08:06:35Z: the armed-set resubscribe got a socket that said nothing for 15 s,
    and a socket_silent reconnect followed BEFORE the first head. The gap check read only the last
    cause, socket_silent, so the lane window the armed-set resubscribe owed was never read (a
    63-block blip instead of ~100). The armed-set count is sticky across any number of reconnects."""
    def silent_reconnect(state, kw):
        state.last_reconnect_cause = "socket_silent"
        state.reconnects += 1
        state.reconnect_causes["socket_silent"] = state.reconnect_causes.get("socket_silent", 0) + 1

    _run(monkeypatch, env, [_heads(2_000, 20), _roster_change, silent_reconnect, _heads(2_040, 5)])
    # the lane window back from the first head after both reconnects (2040), 45 blocks at 1 block/s
    assert min(p[0] for p in env["pages"]) == 2_040 - 45, env["pages"]




def test_a_later_plain_reconnect_after_an_armed_set_one_reads_only_its_blip(monkeypatch, env, wired):
    """The armed-set count is consumed once: a socket error after it is an ordinary blip."""
    def socket_error(state, kw):
        state.last_reconnect_cause = "socket_error"
        state.reconnects += 1
        state.reconnect_causes["socket_error"] = state.reconnect_causes.get("socket_error", 0) + 1
    _run(monkeypatch, env, [_heads(2_000, 20), _roster_change, _heads(2_025, 20), socket_error,
                            _heads(2_050, 5)])
    assert not any("lane-window backfill clipped" in line for line in env["lines"])
    assert env["pages"][:5] == [(2_016, 2_025), (2_006, 2_015), (1_996, 2_005), (1_986, 1_995),
                                (1_980, 1_985)], "the armed-set lane window"
    assert env["pages"][5:] == [(2_045, 2_050)], "the socket error's blip only"


def test_a_tx_the_watcher_already_detected_is_delivered_not_a_miss():
    """D2 read 2026-09-30: J7 reported a 56 buy (tx 0x74d0031f44) 32 min after the watcher had
    detected and acted on it; the in-memory delivered set had rolled over, so the cross-check called
    a miss and forced a needless reconnect. A prior watcher detection of the same tx in the ledger
    now counts as delivered and never forces."""
    xc, r = _xc(), _row(1)
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
    assert xc.due(_ts(r) + 20, known_fn=lambda tx: tx == TX) == [] and not xc.force.is_set()
    assert xc.stats["known_prior"] == 1 and xc.stats["missed"] == 0


def test_an_unknown_or_unreadable_prior_detection_still_misses():
    for known in (lambda tx: False, lambda tx: 1 / 0):
        xc, r = _xc(), _row(1)
        xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
        assert len(xc.due(_ts(r) + 20, known_fn=known)) == 1 and xc.force.is_set()


def test_the_prior_detection_lookup_matches_the_chain_and_the_tx(tmp_path):
    import sqlite3
    db = tmp_path / "l.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE onchain_wallet_detections (signature TEXT UNIQUE)")
    c.execute("INSERT INTO onchain_wallet_detections VALUES (?)", ("56:%s:0x%s" % (TX, "cd" * 20),))
    c.commit()
    assert w._detected_before(c, "56", TX) and w._detected_before(c, "56", TX.upper().replace("0X", "0x"))
    assert not w._detected_before(c, "4663", TX) and not w._detected_before(c, "56", "0x" + "11" * 32)
    c.execute("INSERT INTO onchain_wallet_detections VALUES (?)", ("5:%s:0xmint" % TX,))
    assert not w._detected_before(c, "5", "0x" + "8b" * 31)          # a shorter tx is not a prefix match
    sql = []
    c.set_trace_callback(sql.append)
    w._detected_before(c, "56", TX)
    c.set_trace_callback(None)
    plan = str(c.execute("EXPLAIN QUERY PLAN " + sql[-1]).fetchall())
    assert "SEARCH" in plan, plan                                      # the function's own query seeks


def test_a_known_prior_detection_prints_one_stamped_line(capsys):
    """Alpha 2026-09-30 (task 014e77c8): known_prior lived only in the process-local stats, so every
    watcher restart zeroed it and a J7-late event between two beat reads was lost. Each one now
    prints one UTC-stamped line with its tx, side and age, so the log keeps it across restarts."""
    xc, r = _xc(), _row(1)
    xc.ingest([r], watched={WALLET}, sells_live=False, connected_at=_ts(r) - 60)
    xc.due(_ts(r) + 20, known_fn=lambda tx: tx == TX)
    lines = [l for l in capsys.readouterr().out.splitlines() if "known_prior" in l]
    assert len(lines) == 1, lines
    assert TX[:12] in lines[0] and "side=buy" in lines[0] and "age_s=20.0" in lines[0] and "total=1" in lines[0]
    import re
    assert re.match(r"\d{4}-\d\d-\d\dT", lines[0]), lines[0]          # _say's UTC stamp leads the line
