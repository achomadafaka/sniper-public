# Desk news capture: design (Lambda, 2026-10-27)

Status: design + code for review (Zeta or Delta), then Alpha installs the cron. Research/paper only, 0 USD, no key. Purpose: the stored text input for the narrative guesser's
forward (B) read (`2026-10-27-desk-narrative-guesser-design.md`; Gamma measurement view 2026-10-27). X/J7 capture is a separate operator decision and is not part of this.

## 1. Reuse and deviation
`sniper/feeds/news.py` is the Polymarket-era strategy-runner adapter (writes `news_snapshots` in the main ledger, `feedparser.parse(url)` hides HTTP status, errors become `[]`).
Reused from it: `_canonicalize_url`, `_compute_article_hash`, and the source list, which is extended there (`DESK_NEWS_SOURCES`) and its dead Reuters default removed
(`feeds.reuters.com` does not resolve). NOT reused: the adapter class and its storage; the desk needs its own file, raw bytes, typed errors and per-source cadence, none of which fit a
feed adapter that swallows errors. The adapter keeps working unchanged apart from the dead URL.

## 2. Sources and cadence (all public, no auth, reachability tested 2026-10-27)
Al Jazeera RSS 15 min; Google Trends US daily RSS 15 min; Reddit r/memes and r/popular hot RSS 10 min each; Know Your Meme newsfeed 30 min; Hacker News RSS 15 min;
Wikipedia top pageviews (previous UTC day) every 6 h (items are unique per (date, article), so repeats store nothing). ~50 requests/h, <5 MB/day.

## 3. Storage: `data/desk/news/<day>.db` (UTC day of `received_at`)
- `polls(id, source, received_at, status, http_status, bytes, items_seen, new_items, error_class, error_detail)`. EVERY attempt writes a row. `status` is `ok` or `error`.
- `items(source, item_id, first_received_at, published_at, title, summary, raw_json)`: `UNIQUE(source, item_id)`, first sighting only, never updated; `raw_json` is the entry as parsed (all fields).
- `received_at` / `first_received_at` = corrected UTC (`sniper.util.clock.real_utc_now`). `published_at` is the publisher's string, descriptive only: a consumer may use an item at slot S only if `first_received_at <= S`.
- Absent vs zero vs unknown: `status='ok'` with `items_seen=0` is an empty feed; `status='error'` is UNKNOWN (typed `error_class`: `timeout`, `transport`, `http_status`, `empty_body`, `parse_error`). An error is never stored as an empty result and never retried inside the poll.
- Dedupe memory across restarts and day files: the poller loads the item ids of today's and yesterday's db before inserting.

## 4. Edge cases (CLAUDE.md rule 5)
1 Money path: none. 2 Fill path: none; separate process, nice 19 / ionice idle, one-shot per cron minute under flock. 3 Concurrency: single writer (flock), per-day db, no globals mutated.
4 See §3. 5 Failures: timeout 15 s, no retry, each source isolated (one failing source does not skip the others), typed row. 6 Units: epoch seconds; Wikipedia date is UTC.
7 Sample: a source outage is `missing` for that window, visible in `polls`, never a quiet period. 8 Rollback: remove the cron; data is append-only, nothing reads it yet.

## 5. Run
`python3 scripts/memecoin_desk_news_poller.py [--state-dir data/desk] [--once]`: polls every source whose last attempt is older than its cadence, exits. Cron every minute.
Check left behind: `tests/test_desk_news_poller.py` (typed errors, no empty-on-error, first-sighting-only, restart dedupe, corrected clock).
