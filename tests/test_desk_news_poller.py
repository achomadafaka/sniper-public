"""Desk news capture (spec 2026-10-27-desk-news-capture-design.md)."""
import json
import sqlite3
import urllib.error

import pytest

from sniper.services import memecoin_desk_news as N

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Cat wins</title><link>https://x.test/a?utm_source=z</link><guid>g1</guid><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate><description>d</description></item>
<item><title>Dog wins</title><link>https://x.test/b</link><guid>g2</guid></item></channel></rss>"""
EMPTY = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title></channel></rss>"""
T0 = 1_793_000_000.0
SRC = (("s1", "rss", "https://x.test/feed", 600),)


def poller(tmp_path, body=RSS, t=None, exc=None):
    clock = {"t": T0 if t is None else t}

    def fetcher(url):
        if exc:
            raise exc
        return 200, body
    return N.Poller(tmp_path, clock=lambda: clock["t"], fetcher=fetcher, sources=SRC, pause_s=0), clock


def rows(tmp_path, sql, day=None):
    out = []
    for f in sorted(str(p) for p in (tmp_path / "news").iterdir() if p.suffix == ".db" and p.name != "seen.db"):
        out += sqlite3.connect(f).execute(sql).fetchall()
    return out


def test_first_sighting_only_and_a_restart_does_not_duplicate(tmp_path):
    p, clock = poller(tmp_path)
    assert p.run_once()[0]["new_items"] == 2
    clock["t"] += 700
    assert p.run_once()[0]["new_items"] == 0
    p2, _ = poller(tmp_path, t=clock["t"] + 700)                     # fresh process: dedupe comes from the db, not memory
    assert p2.run_once()[0]["new_items"] == 0
    assert len(rows(tmp_path, "select * from items")) == 2 and len(rows(tmp_path, "select * from polls")) == 3


def test_cadence_gates_the_poll(tmp_path):
    p, clock = poller(tmp_path)
    assert len(p.run_once()) == 1
    clock["t"] += 100
    assert p.run_once() == []


@pytest.mark.parametrize("exc,klass", [(urllib.error.HTTPError("u", 503, "busy", {}, None), "http_status"), (TimeoutError("t"), None), (N.NewsError("empty_body", "x"), "empty_body"),
                                       (N.NewsError("transport", "dns"), "transport")])
def test_an_error_is_a_typed_row_never_an_empty_result(tmp_path, exc, klass):
    def fetcher(url):
        raise N.NewsError("timeout", exc) if isinstance(exc, TimeoutError) else (N.NewsError("http_status", exc.reason, exc.code) if isinstance(exc, urllib.error.HTTPError) else exc)
    p = N.Poller(tmp_path, clock=lambda: T0, fetcher=fetcher, sources=SRC, pause_s=0)
    r = p.run_once()[0]
    assert r["status"] == "error" and r["items_seen"] is None and r["new_items"] is None
    row = rows(tmp_path, "select status, error_class, items_seen from polls")[0]
    assert row[0] == "error" and row[1] == (klass or "timeout") and row[2] is None
    assert rows(tmp_path, "select * from items") == []


def test_an_empty_but_valid_feed_is_ok_with_zero_items_not_an_error(tmp_path):
    p, _ = poller(tmp_path, body=EMPTY)
    r = p.run_once()[0]
    assert r["status"] == "ok" and r["items_seen"] == 0


def test_garbage_bytes_are_a_parse_error(tmp_path):
    p, _ = poller(tmp_path, body=b"<html>not a feed")
    r = p.run_once()[0]
    assert r["status"] == "error" and r["error_class"] == "parse_error"


def test_raw_entry_and_corrected_receive_time_are_stored(tmp_path):
    p, _ = poller(tmp_path)
    p.run_once()
    t, raw = rows(tmp_path, "select first_received_at, raw_json from items where item_id='g1'")[0]
    assert t == T0 and json.loads(raw)["title"] == "Cat wins"


def test_wikipedia_top_is_one_item_per_date_and_article(tmp_path):
    body = json.dumps({"items": [{"year": "2026", "month": "10", "day": "26", "articles": [{"article": "Cat_Fancy", "views": 9, "rank": 1}, {"article": "Dog", "views": 5, "rank": 2}]}]}).encode()
    p = N.Poller(tmp_path, clock=lambda: T0, fetcher=lambda u: (200, body), sources=(("wikipedia_top", "wiki_top", "https://w.test/{date}", 60),), pause_s=0)
    assert p.run_once()[0]["new_items"] == 2
    assert [r[0] for r in rows(tmp_path, "select item_id from items order by item_id")] == ["2026-10-26:Cat_Fancy", "2026-10-26:Dog"]
    assert N.PARSERS["wiki_top"]


def test_no_source_in_the_default_list_is_the_dead_reuters_feed():
    from sniper.feeds import news
    assert not [u for s in news.DESK_NEWS_SOURCES for u in [s[2]] if "reuters" in u] and not [u for u in news.DEFAULT_RSS_FEEDS if "feeds.reuters.com" in u]


def test_entries_that_share_one_link_are_distinct_items(tmp_path):
    shared = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>alpha</title><link>https://t.test/rss</link></item><item><title>beta</title><link>https://t.test/rss</link></item></channel></rss>"""
    p, _ = poller(tmp_path, body=shared)
    assert p.run_once()[0]["new_items"] == 2                         # Google Trends: every entry carries the same link; 9 of 10 were lost before


def test_an_html_block_page_with_http_200_is_a_parse_error_not_an_empty_feed(tmp_path):
    p, _ = poller(tmp_path, body=b"<html><head><title>Access denied</title></head><body>blocked</body></html>")
    r = p.run_once()[0]
    assert r["status"] == "error" and r["error_class"] == "parse_error" and r["items_seen"] is None


def test_received_at_is_the_fetch_completion_time_not_the_start(tmp_path):
    clock = {"t": T0}

    def slow(url):
        clock["t"] += 15.0                                           # the fetch takes 15 s
        return 200, RSS
    p = N.Poller(tmp_path, clock=lambda: clock["t"], fetcher=slow, sources=SRC, pause_s=0)
    p.run_once()
    assert rows(tmp_path, "select first_received_at from items limit 1")[0][0] == T0 + 15.0
    assert rows(tmp_path, "select started_at, received_at from polls")[0] == (T0, T0 + 15.0)


def test_error_rows_are_stamped_at_completion_too(tmp_path):
    clock = {"t": T0}

    def slow_fail(url):
        clock["t"] += 15.0
        raise N.NewsError("timeout", "slow")
    N.Poller(tmp_path, clock=lambda: clock["t"], fetcher=slow_fail, sources=SRC, pause_s=0).run_once()
    assert rows(tmp_path, "select received_at from polls")[0][0] == T0 + 15.0


def test_item_identity_is_durable_across_days_not_a_two_day_window(tmp_path):
    p, clock = poller(tmp_path)
    p.run_once()
    clock["t"] = T0 + 3 * 86400.0                                    # the same guid reappears on day 3
    assert p.run_once()[0]["new_items"] == 0
    assert len(rows(tmp_path, "select * from items")) == 2


def test_seen_identity_is_rebuilt_from_existing_day_files(tmp_path):
    p, clock = poller(tmp_path)
    p.run_once()
    (tmp_path / "news" / "seen.db").unlink()
    clock["t"] = T0 + 5 * 86400.0
    assert p.run_once()[0]["new_items"] == 0


def test_a_malformed_but_partially_parsed_feed_is_kept_and_flagged(tmp_path):
    broken = RSS.replace(b"</channel></rss>", b"<item><title>x</item></channel></rss>")
    p, _ = poller(tmp_path, body=broken)
    r = p.run_once()[0]
    assert r["status"] == "ok" and r["new_items"] >= 2
    assert (rows(tmp_path, "select error_detail from polls")[0][0] or "").startswith("partial:")


def test_a_day_file_from_the_first_schema_is_migrated_without_touching_old_rows(tmp_path):
    day = N.datetime.fromtimestamp(T0, N.timezone.utc).date().isoformat()
    (tmp_path / "news").mkdir()
    old = sqlite3.connect(tmp_path / "news" / ("%s.db" % day))
    old.executescript("""create table polls(id integer primary key, source text not null, received_at real not null, status text not null check(status in ('ok','error')),
      http_status integer, bytes integer, items_seen integer, new_items integer, error_class text, error_detail text);
      create table items(source text not null, item_id text not null, first_received_at real not null, published_at text, title text, summary text, raw_json text not null, unique(source, item_id));
      insert into polls(source, received_at, status, items_seen, new_items) values('s0', 5.0, 'ok', 1, 1);""")
    old.commit()
    old.close()
    p, _ = poller(tmp_path)
    assert p.run_once()[0]["status"] == "ok"
    got = rows(tmp_path, "select source, received_at, started_at from polls order by id")
    assert got[0] == ("s0", 5.0, None) and got[1][0] == "s1" and got[1][2] == T0


def test_a_truncated_feed_with_zero_entries_is_a_parse_error_not_an_empty_feed(tmp_path):
    p, _ = poller(tmp_path, body=b'<rss version="2.0"><channel><title>T</title>')
    r = p.run_once()[0]
    assert r["status"] == "error" and r["error_class"] == "parse_error" and r["items_seen"] is None and r["new_items"] is None
    assert rows(tmp_path, "select status, items_seen from polls")[0] == ("error", None)
    p2, _ = poller(tmp_path / "ok", body=EMPTY)                      # a well-formed empty feed stays ok / 0
    assert p2.run_once()[0]["status"] == "ok" and p2.run_once() == []
