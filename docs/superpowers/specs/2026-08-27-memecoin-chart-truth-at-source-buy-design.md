# Chart Truth At The Source Buy

**Status:** Design for build, 2026-08-27. The operator approved the scope. Beta builds it AFTER the
refusal-capture lane. Alpha installs the cron.

## The defect

No record holds the chart price at the moment the copied trader bought. Three fields look like that
record. None is.

1. `memecoin_entry_attempts.source_price` is the trader's own FILL. The fill carries his market
   impact. `sniper/services/memecoin_signal_copy_quotes.py` states this in its first paragraph.
2. `memecoin_signal_outcomes_v2.ref_price` is a COPY of `their_price`. The ratio is 1.000 at p10, at
   the median, and at p90 over 5,000 rows. It is not an independent observation.
3. `memecoin_events.wouldbe_price` is another route quote. It covers 1.1% of 123,054 events. Its
   median against `their_price` is 1.365.

`memecoin_entry_attempts.display_price` is a chart price, but the runner reads it at OUR decision.
The feed delivers an event 15 s to 30 s after the trader fills. A price read at the decision is
therefore too late by that interval.

**The consequence.** The funded chase caps compare our exact quote to the trader's fill. That ratio
divides a route price by a route price. The 2026-08-27 entry-screen review reported
`display_price / source_price` as "the coin's move" and found it below 1.0x at every market-cap
band. That reading overstates. The ratio cannot separate a coin that FELL from a trader who PAID UP.

## What this job makes possible, and what it does not

The job records one chart price per source buy. That price sits in the same space as
`display_price`, so two quantities become measurable for the first time:

- **The trader's own route premium** — his fill over the chart at his minute.
- **The coin's true move** — the chart at our decision over the chart at his minute.

**The job grades forward only.** It cannot recover a price that nobody stored. The 60 refusals in
the `coin already ran N% past their entry` family carry zero quote prices today, and they stay
ungradeable permanently. The operator accepted this. Do not write a plan that promises to recover
them.

**A historical backfill is out of scope and must not be attempted.** One GeckoTerminal page returns
1,000 minute candles, which is 16.7 hours, not the full history. Six weeks needs about 60 pages for
each of 1,641 pools. That is about 98,000 requests, or about 55 hours of continuous polling.

## The source

GeckoTerminal minute OHLCV.

**The existing client does not cover this yet, and the build must not assume it does.**
`sniper/execution/geckoterminal_quote.py` holds the HTTP plumbing, the browser user-agent GT
requires, the TTL cache and the rate guard — all reusable. It does NOT hold the OHLCV endpoint, and
its network map is `GT_NETWORK = {"4663": "robinhood"}`, with a comment stating that only
live-verified slugs belong in it because a guessed slug would price a coin against the wrong chain.

Two additions are therefore in scope for the build:
1. Add the `solana` slug to `GT_NETWORK`. The 2026-08-27 probe IS the live verification that comment
   asks for: `networks/solana/pools/{pool}/ohlcv/minute` returned HTTP 200 in 0.43 s with 1,000
   candles.
2. Add the OHLCV call. Every other chain stays unmapped until someone verifies it the same way.

```text
GET https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/minute?limit=1000
```

The endpoint needs no key. It returns candles in reverse time order. A probe on 2026-08-27 returned
HTTP 200 in 0.43 s with 1,000 candles across 17.3 hours.

## The dedup key carries no measurement

**The key is `(network_id, pool_address, buy_minute)`.** Every part is a stable identifier. The
price is the MEASUREMENT and it must never enter the key.

This rule comes from the 2026-08-26 watch-sweep defect. That sweep keyed its repage window on a
whole message, every message embedded a drifting number, and one continuous problem paged the
operator every 15 minutes. A value that changes cannot be an identity.

The practical effect here: a later fetch of the same minute may return a revised candle. A revision
UPDATES the measurement columns on the existing row. It never inserts a second row.

<!-- schema:external=memecoin_chart_truth -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_chart_truth (
    network_id     TEXT NOT NULL,
    pool_address   TEXT NOT NULL,
    buy_minute     TEXT NOT NULL,      -- ISO minute, UTC, built in Python
    open_price     REAL,
    high_price     REAL,
    low_price      REAL,
    close_price    REAL,
    volume_usd     REAL,
    price_space    TEXT NOT NULL,      -- always 'chart'; never compare to a route price unlabelled
    minute_range   REAL,               -- high_price / low_price, the resolution of this row
    fetched_at     TEXT NOT NULL,
    revision_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (network_id, pool_address, buy_minute)
);
```

`price_space` is a constant column on purpose. A reader must never compare this price to a route
price without the label. The 2026-08-27 review made exactly that error one level up.

**`buy_minute` comes from the EVENT, never from the clock.** Derive it from the source buy
timestamp on `memecoin_events`, truncated to the minute. An earlier draft of this paragraph said to
build it "from `real_utc_now_iso()`", which is the FETCH time and would have keyed every row to the
minute the job ran instead of the minute the trader bought. That is the whole measurement, inverted
by one sentence.

FORMAT the minute in Python. Do not use SQLite `datetime('now')` for any part of it. That function
renders a space separator, and `' '` sorts before `'T'`, which silently widened a "24 hour" window
to about 40 hours on 2026-08-23.

## The shared-IP yield mechanism

GeckoTerminal limits by IP. It issues no key. **The funded mark lane shares that IP.** It wrote
103,037 marks in the three days to 2026-08-23 and none since 2026-08-23T13:18. The budget is free
today. The funded lane may reclaim it at any time.

This is the Jupiter precedent. On 2026-08-21 a research retry loop burned about 7,000 requests of a
funded quota in 4 hours, because the research process assumed spare capacity that the funded process
owned. On 2026-08-26 the same shape appeared again: quotes run 162 Jupiter to 1 RPC, Jupiter has no
key, it limits by IP, and funded shares it.

The job must therefore obey four rules.

1. Read the count of funded GeckoTerminal marks in the last 5 minutes before each cycle.
2. Skip the whole cycle when that count is above `MEMECOIN_CHART_TRUTH_YIELD_MARKS` (default 50).
3. Sleep at least `MEMECOIN_CHART_TRUTH_MIN_GAP_MS` (default 2500) between requests. That holds the
   job under 24 requests per minute against a 30 per minute budget.
4. Stop the cycle on the first HTTP 429. Do not retry inside the cycle. Wait for the next hour.

A skipped cycle is a normal outcome. Log it with a count. Never let a skip become a retry loop.

### A skip must DEFER a pool, never DROP it

**This rule exists because the obvious version of the yield gate censors on the wrong axis.** A busy
funded lane and a busy signal tape have the same driver. If a skipped cycle discards its pools, the
job loses exactly the busiest tape, and every later decomposition reads as if the market were
quieter than it was. Beta identified this shape on 2026-08-27 while rejecting an occupancy gate for
the refusal-capture lane, where the same bias would have dropped the refusals that study needs.

The chart-truth job escapes the bias for one reason: **the source is historical.** A GeckoTerminal
page reaches 16.7 hours back, so a pool skipped now is still readable later. Deferral is not
censoring. That property must be used deliberately, not assumed.

1. Keep a durable queue of pools that need a minute. A skipped cycle leaves its pools in the queue.
2. Drain the queue oldest-deadline-first, ahead of new pools.
3. Drop a pool ONLY when the job fetched it, or when its buy minute ages past the 16.7 hour page
   window.
4. Count every age-out drop and log it. An age-out is real data loss and must be visible, not
   silent.

The queue always clears with room to spare. The p90 DAY holds 194 distinct pools, and at the
2500 ms minimum gap the job clears 194 requests in about 8 minutes.

### The yield gate reads a window, never a counter

The gate counts funded GeckoTerminal marks written in the last 5 minutes. That is a windowed query
against a table. **Do not replace it with a cumulative process counter.** A cumulative counter
resets when the process restarts, so a single read reports hours-since-restart rather than a rate.
Beta hit exactly this in `data/jupiter_load_samples.jsonl` on 2026-08-27: the honest figure is 510
rate-limited quotes measured as per-session deltas, while a naive "samples where rate_limited > 0"
reads 308 of 1,148 and means nothing.

## Cost

Cost scales with distinct POOLS, not with events. Measured over 400 hours since 2026-08-01 across
8,736 snapshots that carry a pool address:

| unit | median | p90 | max |
|---|---|---|---|
| distinct pools per hour | 8 | 18 | 42 |
| distinct pools per day | 76 | 194 | 216 |

At the p90 hour the job issues 18 requests. That is about 0.6 requests per minute against a 30 per
minute budget, or about 2%.

## The resolution limit

A minute candle cannot resolve an effect smaller than the range inside that minute. One probe pool
gave these ranges over 1,000 candles:

| statistic | high / low |
|---|---|
| median | 1.025x |
| p75 | 1.057x |
| p90 | 1.124x |
| p99 | 1.309x |

Our own route premium, for scale, is 1.609x under $15k, 1.296x at $15k to $30k, and 1.027x above
$1M. The instrument therefore resolves the effect below $30k and fails above $1M, where the effect
equals the noise and does not matter.

**This measurement came from ONE pool and FOUR POOLS ALREADY REFUTE IT AS A GENERAL FIGURE.**
Sampled live on 2026-08-28 during the build:

| pool | candles | median h/l | p90 | max |
|---|---|---|---|---|
| 36Txyskr… | 180 | 1.135 | **186.3** | **4418.6** |
| 3kLwbmLv… | 304 | 1.069 | 1.348 | 2.1 |
| 3yHeJgw4… | 134 | 1.056 | 1.205 | 1.8 |
| 48M3tRdb… | 1000 | 1.010 | 1.016 | 1.1 |

Resolution is not a property of the instrument, it is a property of the POOL. Three of four are fine
and one cannot resolve anything at minute grain. A pooled median would hide that completely, which
is why the per-row `minute_range` column exists: **a consumer must censor on it per row, not trust a
cohort-level average.** The 30-pool confirmation still stands as the gate, and the early read says it
will come back "depends on the pool" rather than a single number. Any decomposition must state the
share of rows it discarded on `minute_range` and the threshold it used.

**The provider limit is also tighter than this spec assumed.** A 429 arrived after five requests
spaced 2,500 ms apart. The 30/min figure quoted above is the published ceiling, not the observed
one, so `MEMECOIN_CHART_TRUTH_MAX_POOLS` may need lowering once a full hour has run. The design
degrades correctly regardless — rule 4 stops the cycle, the queue defers, the next hour retries —
but nobody should read 60 pools/hour as a validated budget.

## Failure posture

- A missing pool address SKIPS the event. It never guesses a pool.
- A missing candle for the exact minute writes NOTHING. It does not substitute a neighbour minute.
  An absent row means "unknown", which is a truthful answer.
- An HTTP error SKIPS the pool and increments a counter. It never fails the job.
- The job is measurement only. It must not write to `memecoin_positions`, to
  `memecoin_entry_attempts`, or to any funded decision surface.

## The buy timestamp is not exact, so the job stores a window

**This was an open question. It is now measured, and it changes the design.**

The job needs the minute in which the trader bought. `memecoin_events.created_at` is the only
candidate. It is a feed-side timestamp, not a settled on-chain time, and `memecoin_events` carries
no transaction signature, so no exact time is available.

Measured against our own observation clock over the last 4,000 events:

| statistic | (their `created_at`) minus (our `signal_observed_at`) |
|---|---|
| p1 | -44.2 s |
| p10 | -26.8 s |
| median | -3.6 s |
| p90 | +20.1 s |
| p99 | +26.2 s |

**44% of buys carry a timestamp LATER than our own observation of them.** That is physically
impossible for a true fill time, so `created_at` is a separate clock domain with about a 27 second
spread. A simulation at that spread puts about 21% of buys in the WRONG minute.

**The design answer: store a three-minute window, not a point.** One page already returns 1,000
candles, so the neighbours cost no extra request. The grain of the table stays one row for one
minute. A consumer reads minute-1, minute, and minute+1, and reports the range across the three as
the uncertainty on the trader's chart price.

A single candle would state a false precision. A window states a bound, and a bound is a truthful
answer.

## Acceptance tests

1. The key rejects a duplicate. Two writes of the same `(network_id, pool_address, buy_minute)`
   leave one row and set `revision_count` to 1.
2. A revised candle updates the price columns and does not insert a second row.
3. The yield gate skips the cycle when the funded mark count is above the threshold.
4. The job stops the cycle on the first HTTP 429 and issues no further request that cycle.
5. A missing minute writes no row.
6. `price_space` equals `'chart'` on every row.
7. The ISO minute comes from Python and carries a `T` separator.
8. One fetch for one pool writes the nominal minute AND both neighbours when the page holds them.
9. A consumer that finds fewer than three minutes reports the bound over what exists, and never
   substitutes a missing neighbour.

## What must be proven before anyone leans on the output

1. Confirm the minute-range distribution across at least 30 pools. State the pool count.
2. Confirm that the three-minute window contains the true buy for a sample with an independent time
   source. No such source exists today, so this test stays OPEN and the bound stays an assumption.
3. Report the decomposition only for coins below $30k market cap. Above $1M the effect equals the
   noise.
