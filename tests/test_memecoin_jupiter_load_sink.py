"""Durable Jupiter load buckets (spec 2026-08-30).

JUPITER_LOAD lives in process memory, so the runner's restarts erase the funded rate-limit
record. This sink persists per-minute buckets behind the same seams.

The defect this file exists to prevent: `_note_host_429` fires per HOST ATTEMPT inside the
failover loop, while `JUPITER_LOAD["rate_limited"]` fires once per QUOTE and only when every host
failed. Recording both at one seam would inflate the persisted rate by the failover fan-out, and
an inflated rate that looks authoritative is worse than no instrument.
"""
import pytest

from sniper.services import memecoin_jupiter_load_sink as sink


@pytest.fixture(autouse=True)
def _clean():
    sink.reset()
    yield
    sink.reset()


def test_host_attempt_and_quote_are_counted_separately():
    """One quote, two hosts refusing, then a terminal 429: pressure=2, lost decision=1."""
    sink.note("host_429", role="funded", host="api", now="2026-08-30T15:00:11Z")
    sink.note("host_429", role="funded", host="lite-api", now="2026-08-30T15:00:11Z")
    sink.note("rate_limited", role="funded", now="2026-08-30T15:00:11Z")
    b = sink.snapshot()
    assert b[("2026-08-30T15:00", "funded", "api")]["host_429"] == 1
    assert b[("2026-08-30T15:00", "funded", "lite-api")]["host_429"] == 1
    assert b[("2026-08-30T15:00", "funded", "_quote")]["rate_limited"] == 1
    assert sum(v["host_429"] for v in b.values()) == 2, "pressure must count host attempts"
    assert sum(v["rate_limited"] for v in b.values()) == 1, "lost decisions must count quotes"


def test_a_recovered_429_is_pressure_but_not_a_lost_decision():
    """429 on one host, success on the next: the quote was NOT lost."""
    sink.note("host_429", role="funded", host="api", now="2026-08-30T15:00:11Z")
    sink.note("quotes", role="funded", now="2026-08-30T15:00:11Z")
    b = sink.snapshot()
    assert sum(v["host_429"] for v in b.values()) == 1
    assert sum(v["rate_limited"] for v in b.values()) == 0
    assert sum(v["quotes"] for v in b.values()) == 1


def test_buckets_accumulate_within_a_minute_and_split_across_minutes():
    for _ in range(3):
        sink.note("quotes", role="funded", now="2026-08-30T15:00:59Z")
    sink.note("quotes", role="funded", now="2026-08-30T15:01:00Z")
    b = sink.snapshot()
    assert b[("2026-08-30T15:00", "funded", "_quote")]["quotes"] == 3
    assert b[("2026-08-30T15:01", "funded", "_quote")]["quotes"] == 1


def test_flush_writes_only_CLOSED_minutes():
    """A quote in flight across the boundary must not read as a discrepancy."""
    sink.note("quotes", role="funded", now="2026-08-30T15:00:10Z")
    sink.note("quotes", role="funded", now="2026-08-30T15:01:10Z")
    written = []
    n = sink.flush(None, write_fn=lambda conn, rows: written.extend(rows),
                   now="2026-08-30T15:01:30Z")
    assert n == 1, "only the 15:00 bucket is closed"
    assert written[0]["ts_minute"] == "2026-08-30T15:00"
    assert ("2026-08-30T15:01", "funded", "_quote") in sink.snapshot(), "open minute kept"


def test_auth_failure_is_never_folded_into_rate_limited():
    """A 429 says we are asking too much; a 401 says the key is revoked."""
    sink.note("auth_failed", role="funded", host="api", now="2026-08-30T15:00:11Z")
    b = sink.snapshot()
    assert b[("2026-08-30T15:00", "funded", "api")]["auth_failed"] == 1
    assert b[("2026-08-30T15:00", "funded", "api")]["rate_limited"] == 0


def test_note_never_raises_whatever_it_is_given():
    """A counter must never be able to refuse a funded quote."""
    for bad in (object(), None, "not_a_field"):
        sink.note(bad, role="funded", now="2026-08-30T15:00:11Z")
    sink.note("quotes", role=object(), host=object(), now=object())


def test_a_raising_write_fn_does_not_propagate_and_does_not_retry_forever():
    sink.note("quotes", role="funded", now="2026-08-30T15:00:10Z")
    def boom(conn, rows): raise RuntimeError("table gone")
    assert sink.flush(None, write_fn=boom, now="2026-08-30T15:02:00Z") == 0
    assert sink.COUNTS["errors"] == 1
    # the batch is DROPPED, not retried against a broken table forever
    assert sink.flush(None, write_fn=lambda c, r: None, now="2026-08-30T15:02:00Z") == 0


def test_buffer_is_bounded():
    for i in range(sink.MAX_BUCKETS + 50):
        sink.note("quotes", role="r%d" % i, now="2026-08-30T15:00:10Z")
    assert len(sink.snapshot()) <= sink.MAX_BUCKETS
    assert sink.COUNTS["dropped_full"] > 0, "overflow must be counted, never silent"


def test_role_defaults_are_recorded_not_guessed():
    sink.note("quotes", role=None, now="2026-08-30T15:00:10Z")
    assert ("2026-08-30T15:00", "unknown", "_quote") in sink.snapshot()


# --- wiring: the real seams in solana_real_seams, not the sink in isolation -----------------

def test_real_seams_split_pressure_from_lost_decisions():
    """Drive the ACTUAL counter functions and check the persisted split.

    Two hosts refuse one quote, then the quote ends 429. Pressure must read 2 and the lost
    decision must read 1. If a future edit records rate_limited inside the per-host seam this
    goes red, which is the whole point of the file.
    """
    import sniper.execution.solana_real_seams as S
    S._note_host_429("https://api.jup.ag")
    S._note_host_429("https://lite-api.jup.ag")
    S._sink_note("rate_limited")
    b = sink.snapshot()
    assert sum(v["host_429"] for v in b.values()) == 2, "pressure lost the per-host fan-out"
    assert sum(v["rate_limited"] for v in b.values()) == 1, "lost decisions inflated by fan-out"
    hosts = {k[2] for k in b if b[k]["host_429"]}
    assert hosts == {"api", "lite-api"}, "host label must be the short label, never a URL"


def test_real_quote_seam_records_a_quote():
    import sniper.execution.solana_real_seams as S
    S.note_role_quote("funded")
    b = sink.snapshot()
    assert sum(v["quotes"] for v in b.values()) == 1
    assert any(k[1] == "funded" for k in b)


def test_auth_failure_seam_does_not_touch_rate_limited():
    import sniper.execution.solana_real_seams as S
    S._note_host_auth_failure("https://api.jup.ag", 401)
    b = sink.snapshot()
    assert sum(v["auth_failed"] for v in b.values()) == 1
    assert sum(v["rate_limited"] for v in b.values()) == 0
    assert sum(v["host_429"] for v in b.values()) == 0


def test_no_host_label_ever_carries_a_url():
    """_host_label exists so a base that ever gains a secret keeps it out of the table."""
    import sniper.execution.solana_real_seams as S
    S._note_host_429("https://api.jup.ag/v6/quote?key=SHOULD_NEVER_APPEAR")
    for (_, _, host) in sink.snapshot():
        assert "//" not in host and "key" not in host.lower(), host


def test_end_to_end_through_the_real_db_layer():
    """Sink -> record_jupiter_load -> table, including the additive upsert across a restart.

    The upsert must ADD rather than replace: a restart mid-minute produces a second partial
    bucket for the same key, and a replacing write would erase the first half — the exact data
    loss this table exists to prevent.
    """
    import sqlite3
    import sniper.persistence.db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)

    sink.note("host_429", role="funded", host="api", now="2026-08-30T15:00:11Z")
    sink.note("host_429", role="funded", host="api", now="2026-08-30T15:00:12Z")
    sink.note("rate_limited", role="funded", now="2026-08-30T15:00:12Z")
    assert sink.flush(conn, now="2026-08-30T15:02:00Z") == 2

    # second process, same minute -> must accumulate, not overwrite
    sink.reset()
    sink.note("host_429", role="funded", host="api", now="2026-08-30T15:00:40Z")
    assert sink.flush(conn, now="2026-08-30T15:02:00Z") == 1

    row = conn.execute("SELECT * FROM memecoin_jupiter_load WHERE host='api'").fetchone()
    assert row["host_429"] == 3, "upsert replaced instead of accumulating across the restart"
    q = conn.execute("SELECT SUM(rate_limited) s FROM memecoin_jupiter_load").fetchone()
    assert q["s"] == 1
    tot = conn.execute("SELECT SUM(host_429) h, SUM(rate_limited) r "
                       "FROM memecoin_jupiter_load").fetchone()
    assert tot["h"] >= tot["r"], "pressure must never read below lost decisions"
