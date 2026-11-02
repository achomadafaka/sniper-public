import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sniper.services import memecoin_desk_intake as intake
import memecoin_desk_dexscreener as producer


def _source(tmp_path, polls):
    store = producer.Store(tmp_path / "dex", retention_days=0)
    (tmp_path / "dex").mkdir()
    for at, payloads in polls:
        def get(url, payloads=payloads):
            ep = url[len(producer.BASE):]
            return 200, payloads.get(ep, []), None
        producer.poll_pass(store, get=get, clock=lambda at=at: at)
    store.close()
    return tmp_path / "dex" / ("%s.db" % producer.utc_day(polls[0][0]))


def _rows(conn):
    return conn.execute("SELECT filter, network_id, mint, provider_rank, raw_json, observed_at FROM intake_rows ORDER BY id").fetchall()


AT = 1793000000.0


def test_normalizes_once_with_labels_ranks_and_raw_metrics(tmp_path):
    src = _source(tmp_path, [(AT, {"/token-boosts/top/v1": [
        {"chainId": "solana", "tokenAddress": "AbC111", "totalAmount": 0},
        {"chainId": "base", "tokenAddress": "0xABCD", "amount": 500}],
        "/metas/trending/v1": [{"name": "cats"}]})])
    out = intake.open_day(tmp_path / "intake", "dexscreener", AT)
    assert intake.dexscreener_pass(src, out, now=AT + 1, offset_s=0.5) == {"items": 2, "malformed": 0, "source_errors": 0}
    rows = _rows(out)
    assert [r[:4] for r in rows] == [("boost_top", "solana", "AbC111", 1), ("boost_top", "8453", "0xabcd", 2)]
    assert json.loads(rows[0][4])["totalAmount"] == 0 and rows[0][5] == AT - 0.5   # provider 0 stays 0; corrected receipt
    assert not any("trending" in r[0] for r in rows)                           # metas are not candidates
    assert intake.dexscreener_pass(src, out, now=AT + 2, offset_s=0.5)["items"] == 0          # cursor: nothing twice
    assert len(_rows(out)) == 2


def test_malformed_items_are_counted_never_silent(tmp_path):
    src = _source(tmp_path, [(AT, {"/token-profiles/latest/v1": [{"chainId": "solana"}, {"chainId": "solana", "tokenAddress": "X9"}]})])
    out = intake.open_day(tmp_path / "intake", "dexscreener", AT)
    assert intake.dexscreener_pass(src, out, now=AT, offset_s=None) == {"items": 1, "malformed": 1, "source_errors": 0}
    assert out.execute("SELECT items, malformed FROM polls WHERE filter='profile'").fetchone() == (1, 1)


def test_missing_source_is_a_named_error_row(tmp_path):
    out = intake.open_day(tmp_path / "intake", "dexscreener", AT)
    got = intake.dexscreener_pass(tmp_path / "nope.db", out, now=AT, offset_s=None)
    assert got["error"] == "source_missing"
    assert out.execute("SELECT status, error FROM polls").fetchall() == [("error", "source_missing")]
    assert _rows(out) == []


def test_first_sighting_is_per_feed_never_moved_and_unknown_clock_is_null(tmp_path):
    src = _source(tmp_path, [(AT, {"/token-boosts/top/v1": [{"chainId": "solana", "tokenAddress": "M1"}]}),
                             (AT + 60, {"/ads/latest/v1": [{"chainId": "solana", "tokenAddress": "M1"}],
                                        "/token-profiles/latest/v1": [{"chainId": "zzchain", "tokenAddress": "Q"}]})])
    out = intake.open_day(tmp_path / "intake", "dexscreener", AT)
    intake.dexscreener_pass(src, out, now=AT + 61, offset_s=0.0)
    assert out.execute("SELECT feed, network_id, mint, filter, observed_at FROM fs.first_sightings").fetchall() == [
        ("dexscreener", "solana", "M1", "boost_top", AT)]                       # the ad re-sighting is a row, not t0
    assert out.execute("SELECT network_id, chain_raw FROM intake_rows WHERE mint='Q'").fetchone() == (None, "zzchain")
    out2 = intake.open_day(tmp_path / "intake2", "dexscreener", AT)
    intake.dexscreener_pass(src, out2, now=AT, offset_s=None)
    assert {r[5] for r in _rows(out2)} == {None}                                # unknown offset: NULL, never host time
    assert out2.execute("SELECT COUNT(*) FROM fs.first_sightings").fetchone()[0] == 0
    assert intake.clock_offset(tmp_path / "missing.json") is None


SOL = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def test_pumpfun_lists_are_rows_with_position_venue_and_chain(tmp_path):
    lists = {"live": [{"mint": "AAApump", "chain_id": SOL, "complete": False, "usd_market_cap": 0}],
             "active": [{"mint": "BBBpump", "chain_id": SOL, "complete": True}, {"mint": "CCC", "chain_id": "eip155:8453"}],
             "top_mcap": [{"chain_id": SOL}], "new": []}
    def get(url):
        name = next(k for k, v in intake.PUMPFUN_LISTS.items() if url.endswith(v))
        return 200, lists[name], None
    out = intake.open_day(tmp_path, "pumpfun", AT)
    stats, refused = intake.pumpfun_pass(out, get=get, clock=lambda: AT, offset_s=1.0)
    assert not refused and stats == {"items": 3, "malformed": 1, "errors": 0}
    rows = out.execute("SELECT filter, network_id, mint, venue, provider_rank, observed_at FROM intake_rows ORDER BY id").fetchall()
    assert rows == [("live", "solana", "AAApump", "pump_curve", 1, AT - 1), ("active", "solana", "BBBpump", "pumpswap", 1, AT - 1),
                    ("active", None, "CCC", None, 2, AT - 1)]
    assert json.loads(out.execute("SELECT raw_json FROM intake_rows WHERE mint='AAApump'").fetchone()[0])["usd_market_cap"] == 0
    assert out.execute("SELECT COUNT(*) FROM fs.first_sightings").fetchone()[0] == 2      # unmapped chain: no t0
    assert out.execute("SELECT filter, items FROM polls WHERE filter='new'").fetchone() == ("new", 0)  # a real empty list


def test_pumpfun_refusal_stops_the_pass_and_is_named(tmp_path):
    calls = []
    def get(url):
        calls.append(url)
        return (500, None, "http_500") if len(calls) == 1 else (403, None, "refused:challenge")
    out = intake.open_day(tmp_path, "pumpfun", AT)
    stats, refused = intake.pumpfun_pass(out, get=get, clock=lambda: AT, offset_s=None)
    assert refused and len(calls) == 2 and stats["errors"] == 2
    assert out.execute("SELECT filter, status, error FROM polls ORDER BY id").fetchall() == [
        ("live", "error", "http_500"), ("active", "error", "refused:challenge")]
    assert out.execute("SELECT COUNT(*) FROM intake_rows").fetchone()[0] == 0


def test_collector_runs_both_feeds_into_the_state_root(tmp_path):
    import memecoin_desk_intake as collector
    stats, refused = collector.run_once("pumpfun", tmp_path, clock=lambda: AT,
                                        get=lambda url: (200, [{"mint": "Zpump", "chain_id": SOL, "complete": False}], None))
    assert not refused and stats["items"] == 4                                     # one row per list
    db = tmp_path / "data" / "desk" / "intake" / "pumpfun" / ("%s.db" % intake.utc_day(AT))
    assert sqlite3.connect(db).execute("SELECT observed_at FROM intake_rows LIMIT 1").fetchone() == (None,)  # no offset file
    stats, _ = collector.run_once("dexscreener", tmp_path, clock=lambda: AT)
    assert stats["error"] == "source_missing"


def test_source_errors_and_empty_polls_are_mirrored_not_silent(tmp_path):
    store = producer.Store(tmp_path / "dex", retention_days=0)
    (tmp_path / "dex").mkdir()
    producer.poll_pass(store, get=lambda url: (200, [], None), clock=lambda: AT)              # every list empty
    producer.poll_pass(store, get=lambda url: (429, None, "http_429"), clock=lambda: AT + 60)  # 429 pauses all
    store.close()
    out = intake.open_day(tmp_path / "intake", "dexscreener", AT)
    got = intake.dexscreener_pass(tmp_path / "dex" / ("%s.db" % intake.utc_day(AT)), out, now=AT + 61, offset_s=0.0)
    assert got == {"items": 0, "malformed": 0, "source_errors": 1}
    polls = out.execute("SELECT filter, status, items, error FROM polls ORDER BY id").fetchall()
    assert ("boost_top", "ok", 0, None) in polls and ("boost_top", "error", None, "source:http_429") in polls


def test_first_sighting_is_feed_wide_across_days(tmp_path):
    for at in (AT, AT + 86400):
        (tmp_path / str(at)).mkdir()
        src = _source(tmp_path / str(at), [(at, {"/token-boosts/top/v1": [{"chainId": "solana", "tokenAddress": "M1"}]})])
        out = intake.open_day(tmp_path / "intake", "dexscreener", at)
        intake.dexscreener_pass(src, out, now=at, offset_s=0.0)
    assert out.execute("SELECT observed_at FROM fs.first_sightings").fetchall() == [(AT,)]     # day 2 is not a new t0


def test_a_non_finite_or_bool_offset_is_unknown(tmp_path):
    for v in ("NaN", "true", '"3"'):
        f = tmp_path / ("o%d.json" % len(v))
        f.write_text('{"offset_s": %s}' % v)
        assert intake.clock_offset(f) is None
    (tmp_path / "ok.json").write_text('{"offset_s": 0.25}')
    assert intake.clock_offset(tmp_path / "ok.json") == 0.25


def test_a_429_with_a_stalled_body_is_still_a_refusal_poll(tmp_path, monkeypatch):
    import io
    import urllib.error
    import urllib.request

    class Stall(io.RawIOBase):
        def read(self, *a):
            raise TimeoutError("body stalled")

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, Stall())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert intake.http_get("https://x.test/a") == (429, None, "refused:http_429")
    out = intake.open_day(tmp_path, "pumpfun", AT)
    stats, refused = intake.pumpfun_pass(out, clock=lambda: AT, offset_s=0.0)
    assert refused and out.execute("SELECT filter, status, error FROM polls").fetchall() == [("live", "error", "refused:http_429")]


def test_a_truncated_200_body_is_a_transport_poll(tmp_path, monkeypatch):
    import http.client
    import urllib.request

    class Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, *a):
            raise http.client.IncompleteRead(b"partial", 10)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp())
    assert intake.http_get("https://x.test/a") == (None, None, "transport")
    out = intake.open_day(tmp_path, "pumpfun", AT)
    stats, refused = intake.pumpfun_pass(out, clock=lambda: AT, offset_s=0.0)
    assert not refused and stats["errors"] == len(intake.PUMPFUN_LISTS)
    assert {r for r in out.execute("SELECT status, error FROM polls")} == {("error", "transport")}


def test_any_fetch_exception_is_a_typed_poll_row(tmp_path):
    def get(url):
        raise RuntimeError("anything")
    out = intake.open_day(tmp_path, "pumpfun", AT)
    stats, refused = intake.pumpfun_pass(out, get=get, clock=lambda: AT, offset_s=0.0)
    assert not refused and stats["errors"] == len(intake.PUMPFUN_LISTS)
    assert {r for r in out.execute("SELECT status, error FROM polls")} == {("error", "exception:RuntimeError")}


def test_prune_keeps_first_sightings_whole_and_archives_polls(tmp_path):
    old, new = AT - 40 * 86400, AT
    for at in (old, new):
        out = intake.open_day(tmp_path, "pumpfun", at)
        intake.record_poll(out, "pumpfun", "live", at, [{"network_id": "solana", "chain_raw": SOL, "mint": "M%d" % at,
                                                          "venue": "pump_curve", "raw": {"x": 0}}], offset_s=0.0)
        intake.record_poll(out, "pumpfun", "new", at, [], offset_s=0.0, error="transport")
        out.close()
    assert intake.prune(tmp_path / "pumpfun", now=AT, keep_days=30) == [intake.utc_day(old)]
    assert not (tmp_path / "pumpfun" / ("%s.db" % intake.utc_day(old))).exists()
    assert (tmp_path / "pumpfun" / ("%s.db" % intake.utc_day(new))).exists()
    fs = sqlite3.connect(tmp_path / "pumpfun" / "first_sightings.db")
    assert fs.execute("SELECT filter, venue, provider_rank, rank_kind, raw_json FROM first_sightings WHERE mint=?",
                      ("M%d" % old,)).fetchone() == ("live", "pump_curve", 1, "list_position", '{"x": 0}')
    assert sorted(fs.execute("SELECT filter, status, error FROM polls_archive")) == [
        ("live", "ok", None), ("new", "error", "transport")]


def test_an_old_schema_first_sightings_is_migrated_and_backfilled(tmp_path):
    """Delta 601c619d1: a store made by 7ccd63482 (7-column first_sightings) keeps collecting after the upgrade, its
    t0 gets the self-contained fields from its own day, and prune never drops a day with an unfilled t0."""
    d = tmp_path / "pumpfun"
    d.mkdir()
    day = sqlite3.connect(d / ("%s.db" % intake.utc_day(AT - 40 * 86400)))
    day.executescript(intake.DDL)
    day.execute("INSERT INTO intake_rows (id, poll_id, feed, filter, network_id, chain_raw, mint, venue, provider_rank, "
                "rank_kind, raw_json, observed_at, host_received_at, clock_offset_s, source_ref) VALUES "
                "(1,1,'pumpfun','live','solana',?,'OLD','pump_curve',3,'list_position','{}',?,?,0,'x')",
                (SOL, AT - 40 * 86400, AT - 40 * 86400))
    day.commit()
    day.close()
    fs = sqlite3.connect(d / "first_sightings.db")
    fs.execute("CREATE TABLE first_sightings (feed TEXT NOT NULL, network_id TEXT NOT NULL, mint TEXT NOT NULL, "
               "day TEXT NOT NULL, row_id INTEGER NOT NULL, filter TEXT NOT NULL, observed_at REAL NOT NULL, "
               "PRIMARY KEY (feed, network_id, mint))")
    fs.execute("INSERT INTO first_sightings VALUES ('pumpfun','solana','OLD',?,1,'live',?)",
               (intake.utc_day(AT - 40 * 86400), AT - 40 * 86400))
    fs.execute("INSERT INTO first_sightings VALUES ('pumpfun','solana','GONE','2000-01-01',9,'live',1.0)")
    fs.commit()
    fs.close()
    out = intake.open_day(tmp_path, "pumpfun", AT)                  # today: old-schema store, new code
    intake.record_poll(out, "pumpfun", "live", AT, [{"network_id": "solana", "chain_raw": SOL, "mint": "NEW",
                                                     "venue": None, "raw": {"a": 1}}], offset_s=0.0)
    out.close()
    assert intake.prune(d, now=AT, keep_days=30) == [intake.utc_day(AT - 40 * 86400)]
    fs = sqlite3.connect(d / "first_sightings.db")
    assert fs.execute("SELECT provider_rank, venue, raw_json FROM first_sightings WHERE mint='OLD'").fetchone() == (3, "pump_curve", "{}")
    assert fs.execute("SELECT raw_json FROM first_sightings WHERE mint='NEW'").fetchone() == ('{"a": 1}',)
    assert fs.execute("SELECT raw_json FROM first_sightings WHERE mint='GONE'").fetchone() == (None,)   # kept, unfilled


def test_dexscreener_rows_written_before_midnight_are_normalized_after_it_once(tmp_path):
    """Beta: run_once read only today's producer file, so rows after the last pre-midnight pass were never read."""
    import memecoin_desk_intake as collector
    day1 = 1793059199.0                                          # 2026-10-26 23:59:59 UTC
    dex = tmp_path / "data" / "desk" / "dexscreener"
    dex.mkdir(parents=True)
    store = producer.Store(dex, retention_days=0)
    producer.poll_pass(store, get=lambda url: (200, [{"chainId": "solana", "tokenAddress": "LATE"}]
                                               if url.endswith("/token-boosts/top/v1") else [], None),
                       clock=lambda: day1)
    store.close()
    for now in (day1 + 2, day1 + 62):                            # 00:00:01 and a minute later
        stats, _ = collector.run_once("dexscreener", tmp_path, clock=lambda now=now: now)
    assert "error" in stats                                      # today's producer file does not exist yet
    db = tmp_path / "data" / "desk" / "intake" / "dexscreener" / ("%s.db" % intake.utc_day(day1))
    assert sqlite3.connect(db).execute("SELECT mint FROM intake_rows").fetchall() == [("LATE",)]   # once
