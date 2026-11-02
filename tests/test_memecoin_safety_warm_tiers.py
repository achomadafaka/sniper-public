"""The shared safety warm set (lambda + Beta, Alpha GO 2026-10-14): the tiers, the armed helpers and
one stats pacer for every lane's warmer in the runner process."""
import sqlite3

from sniper.services import memecoin_rh_fast_decide as fast
from sniper.services import memecoin_rh_fast_lane as lane


def test_tiers_order_signals_then_the_window_then_activity(monkeypatch):
    monkeypatch.setattr(fast, "lane_route", lambda tape, mint, now: {"kind": "v4_native", "pool": "p", "key": {}})

    class Tape:
        def busiest(self, now):
            return ["0xbusy", "0xrecent"]
    got = {e["mint"]: e["priority"] for e in fast.make_candidates(
        Tape(), 0.0, signal_mints={"0xsig"}, armed_mints={"0xhour"}, k=5,
        recent_mints={"0xrecent", "0xhour"})}
    assert got == {"0xsig": 0, "0xhour": 0, "0xrecent": 1, "0xbusy": 11}


def _ledger(path):
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    db.add_wallet(conn, "uid", "Degen")
    db.set_wallet_real_approved(conn, "uid", True, chains=["4663"], force=True)
    db.add_wallet(conn, "off", "Unarmed")
    return conn


def test_the_armed_helpers_read_decided_and_sold_mints_for_one_chain(tmp_path):
    from sniper.persistence import db_memecoin as m
    path = str(tmp_path / "ledger.db")
    conn = _ledger(path)
    for attempt, chain, mint in (("a", "4663", "0xAA"), ("b", "56", "0xbb")):
        conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, "
                     "decision_started_at, completed_at, terminal_status, terminal_reason) "
                     "VALUES (?, ?, ?, ?, ?, 'refused', 'test')",
                     (attempt, chain, mint, "2026-10-14T10:00:00+00:00", "2026-10-14T10:00:01+00:00"))
    for uid, mint, side in (("uid", "0xCC", "sell"), ("uid", "0xdd", "buy"), ("off", "0xee", "sell")):
        conn.execute("INSERT INTO memecoin_events (source_user_id, mint, side, created_at, network_id) "
                     "VALUES (?, ?, ?, '2026-10-14T10:00:00+00:00', '4663')", (uid, mint, side))
    conn.commit()
    since = "2026-10-14T09:00:00+00:00"
    assert m.armed_bought_mints(path, "4663", since) == {"0xaa"}
    assert m.armed_sold_mints(path, "4663", since) == {"0xcc"}          # armed wallets' sells only
    assert m.armed_bought_mints(path, "4663", "2026-10-14T11:00:00+00:00") == set()


def test_an_unreadable_ledger_raises_so_the_caller_keeps_its_last_set(tmp_path):
    import pytest
    from sniper.persistence import db_memecoin as m
    with pytest.raises(sqlite3.OperationalError):
        m.armed_bought_mints(str(tmp_path / "missing.db"), "4663", "2026-10-14T00:00:00+00:00")


def test_one_stats_pacer_serves_every_warmer_in_the_process():
    clock = [100.0]
    pacer = lane.WarmStatsPacer(gap_s=1.0, clock=lambda: clock[0])
    assert pacer.take() and not pacer.take()
    clock[0] += 1.0
    assert pacer.take()
    assert isinstance(lane.WARM_STATS, lane.WarmStatsPacer)


def test_lookups_count_hits_and_misses_by_tier():
    cache = lane.SafetyCache(lambda m, usd=None: (True, "r"), ttl_s=60, usd=50)
    cache.note_candidates([{"mint": "0xa", "priority": 0}, {"mint": "0xb", "priority": 12}], now=1000.0)
    cache._verdicts["0xa"] = (True, "r", 990.0, 0.0)
    assert cache.reason("0xa", now=1000.0) is None
    assert cache.reason("0xb", now=1000.0) == "no_safety_verdict"
    assert cache.reason("0xz", now=1000.0) == "no_safety_verdict"
    assert dict(cache.lookups) == {"0:hit": 1, "activity:miss": 1, "other:miss": 1}
    assert cache.hit_age_s["0"] == [10.0, 1]


def test_the_decide_keeps_its_last_warm_set_when_the_ledger_is_unreadable(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    path = str(tmp_path / "ledger.db")
    conn = _ledger(path)
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, decision_started_at, "
                 "completed_at, terminal_status, terminal_reason) VALUES ('a', '4663', '0xAA', "
                 "'2026-10-14T10:00:00+00:00', '2026-10-14T10:00:01+00:00', 'refused', 'test')")
    conn.commit()
    from datetime import datetime
    now_s = datetime.fromisoformat("2026-10-14T10:30:00+00:00").timestamp()
    warm = {"hour": set(), "recent": set()}
    assert fast.refresh_warm_set(warm, path, now_s) and warm == {"hour": {"0xaa"}, "recent": {"0xaa"}}

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "armed_sold_mints", locked)
    assert not fast.refresh_warm_set(warm, path, now_s + 7200)
    assert warm == {"hour": {"0xaa"}, "recent": {"0xaa"}}            # the last good read stands
