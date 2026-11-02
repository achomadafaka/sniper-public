# Memecoin Thesis Capture + Telegram Front-Run Latency Instrument — Design

**Date:** 2026-07-08
**Status:** approved (operator green-lit all design leans 2026-07-08)
**Scope:** two additive, read-mostly features on the memecoin copy-mirror subsystem — (1) capture the traders' posted theses that already flow through the fomo `/feed` we poll, (2) a Telegram alert that fires the instant we detect a curated large buy, instrumented to test whether we beat fomo's own push notification.

---

## 0. Motivation & the one constraint that shapes everything

The fomo `/feed` we already poll every ~500ms carries `thesis_created` items (~9 per poll) that we currently discard in `normalize_feed_item` (it returns `None` for any non-buy/sell type). Each thesis carries the trader's reasoning **plus** their live position context. Live-verified schema (9 items, 2026-07-08):

- **Attribution is clean.** Top-level `userId` and `tokenAddress` are present and `raw.userId == body.userId == large_buy.userId` (same-trader cross-check). A thesis maps to a curated `source_user_id` with no guessing.
- **Stable dedup keys:** top-level `id` (feed-item id) + `body.commentId`. Top-level `swapId`/`tradeId` link a thesis to the exact trade.
- **PnL is in percent, not fraction** (`-29.38`, `-49.44`, `10.10`).

**The constraint (empirical):** in the live sample, theses skew *underwater cope*, not conviction — 3 of 4 non-neutral were negative (CryptoGodJohn MANIFEST **−49.4% / −$111k** "round-trip 6 figures to make 7 figures"; gaylord WARD −29.4%; 0xVantaa −11.3%; only hihi33 +10.1% was green, a taunt). **A thesis is not a buy signal — it is frequently a bagholder rationalizing a loss.** Therefore:

> Thesis capture is **display / context only**. It MUST NOT mutate any copy decision, gate, verdict, position, or sizing. The informative unit is *text + their live PnL + entry timing together*, never text alone. We surface objective facts (their unrealized PnL bucket) — **no fabricated sentiment/NLP.**

This mirrors the existing analyzer doctrine (display-only surfaces never touch `overall_verdict`/`gate_result`/copyability).

The Telegram instrument tests the subsystem's founding premise — "front-run the notification herd" — head-on: does our poll-based detection reach the operator's phone *before fomo's own push*?

---

## 1. Phase 1 — Thesis capture

### 1.1 Data model: `memecoin_theses`

New table, created idempotently in `ensure_memecoin_tables` (CREATE IF NOT EXISTS + additive `ALTER` migration pattern already used in the file).

| column | source | notes |
|---|---|---|
| `id` | autoincrement PK | |
| `fomo_thesis_id TEXT UNIQUE` | top-level `id` (else synth) | dedup key; INSERT OR IGNORE |
| `comment_id TEXT` | `body.commentId` | edit-tracking / secondary key |
| `source_user_id TEXT` | top-level `userId` (else `body.userId`) | == curated id |
| `handle TEXT` | `body.userHandle` / `displayName` | |
| `mint TEXT` | top-level `tokenAddress` (else `body.tokenAddress`) | |
| `ticker TEXT` | `body.ticker` | |
| `comment TEXT` | `body.comment` | the thesis text; may embed a URL |
| `has_link INTEGER` | derived | 1 if comment matches an `https?://` URL |
| `their_pnl_pct REAL` | `body.percentageUnrealizedPnl` | **percent**; their unrealized PnL at post time |
| `their_realized_pnl_pct REAL` | `body.percentageRealizedPnl` | |
| `their_notional_usd REAL` | `body.positionNotionalUsd` | THEIR size — never leaks into our sizing |
| `their_unrealized_pnl_usd REAL` | `body.unrealizedPnlUsd` | |
| `market_cap REAL` | `body.marketCap` | |
| `fdv REAL` | `body.fdv` | |
| `price REAL` | `body.price` | |
| `tag TEXT` | `body.tag` | e.g. "Top Trader"; often None — store raw, assume nothing |
| `network_id TEXT` | top-level `networkId` | multichain |
| `swap_id TEXT` | top-level `swapId` | link to the exact trade (may be None) |
| `trade_id TEXT` | top-level `tradeId` | |
| `created_at TEXT` | top-level `createdAt` | **fomo clock** (host runs +7h) |
| `received_at TEXT` | corrected real-UTC receipt time | |
| `lag_s REAL` | `server_epoch - created` | age when we saw it; -1.0 if unknown |
| `signal_source TEXT DEFAULT 'global_feed'` | | all captures are global feed |

Indexes: `(source_user_id, created_at)`, `(mint, received_at)`.

No `stance` column — stance is computed at read time from `their_pnl_pct` so the classification can evolve without a migration or stale rows (one-source-of-truth: DB stores raw facts).

No `curated` flag now — capture is curated-only (§1.4). When the sampled-non-curated lane is added later, add the flag then.

### 1.2 Normalization: `normalize_thesis_item`

New pure function + dataclass in `sniper/feeds/fomo_feed.py` (alongside `normalize_feed_item`):

```
@dataclass
class ThesisEvent:
    fomo_thesis_id: str; comment_id: str
    source_user_id: str; handle: str
    mint: str; ticker: str
    comment: str; has_link: bool
    their_pnl_pct: float | None; their_realized_pnl_pct: float | None
    their_notional_usd: float | None; their_unrealized_pnl_usd: float | None
    market_cap: float | None; fdv: float | None; price: float | None
    tag: str; network_id: str; swap_id: str; trade_id: str
    created_at: str; received_at: str; lag_s: float
    signal_source: str = "global_feed"

def normalize_thesis_item(raw, received_iso, server_epoch) -> Optional[ThesisEvent]
```

Rules:
- Return `None` unless `raw["type"] == "thesis_created"`.
- Require `source_user_id` and `mint`; else `None`.
- **Skip fully-empty:** if `comment.strip()` is empty → `None` (a bare position-share with no text is noise). A comment that is *only* a URL is kept (`has_link=1`).
- `has_link` = regex `https?://` present in comment.
- Dedup id = top-level `id`; if absent, `"synth-" + sha1(userId|mint|commentId|createdAt)[:16]` (same idiom as trade dedup).
- `lag_s` computed from `server_epoch` and `createdAt` exactly like `normalize_feed_item` (-1.0 if unknown).
- PnL/number fields via the existing `_num` helper but preserving `None` when the key is absent (distinguish unknown from 0.0 — use a `_numN` that returns `None` on missing rather than `0.0`, since "their PnL is unknown" ≠ "their PnL is 0%").

### 1.3 Persistence: `db_memecoin`

- `record_thesis(conn, th_dict) -> bool` — `INSERT OR IGNORE` on `fomo_thesis_id`; returns True iff new (create_signal_outcome idiom). Keep-first on re-delivery/edit.
- `list_theses(conn, uid=None, mint=None, limit=50) -> [dict]` — newest-first, optional filters.
- `latest_thesis_by_trader(conn, uids) -> {uid: dict}` — one query, latest thesis per uid (for decorating the Traders table without N+1).
- `token_thesis_counts(conn, mints, within_hours=24) -> {mint: count}` — confluence: how many curated theses on each mint in the window (batch).

### 1.4 Runner hook: `run_once`

After the existing `events = [...]` build (line ~146), parse theses from the same `raw_items`:

```
if MEMECOIN_THESIS_ENABLED:
    for th in (normalize_thesis_item(r, recv_iso, server_epoch) for r in raw_items):
        if th and th.source_user_id in curated:      # curated-only
            try:
                m.record_thesis(conn, th.__dict__)
            except Exception:
                pass                                   # best-effort; never break copy path
```

Placed after `curated` is computed. Best-effort (wrapped), gated by `MEMECOIN_THESIS_ENABLED` (default on — capture is read-only and low-risk). Does not touch the copy/exit path.

### 1.5 Stance classifier

New pure module `sniper/analysis/memecoin_thesis.py`:

```
def thesis_stance(their_pnl_pct) -> str:
    # underwater | flat | winning | unknown  — objective PnL bucket, NOT sentiment
    if their_pnl_pct is None: return "unknown"
    if their_pnl_pct <= UNDERWATER_PCT: return "underwater"   # default -10
    if their_pnl_pct >= WINNING_PCT:    return "winning"      # default +20
    return "flat"
```

This is the honest "cope-vs-conviction" badge: it reports *where their position is*, letting the operator read the text against reality (the memekingdom/NYAN insight — "buy more" while −49% reads differently than while +331%). No claim to parse tone.

Thresholds from `web_settings`/`settings.memecoin` (`MEMECOIN_THESIS_UNDERWATER_PCT`, `MEMECOIN_THESIS_WINNING_PCT`).

### 1.6 API (`api_memecoin.py`)

- `GET /api/memecoin/theses?uid=&mint=&limit=` — recent curated theses (optional filters), each decorated with `stance`.
- `GET /api/memecoin/traders/<uid>/theses` — convenience alias for `?uid=`.
- Decorate the combined/gated trader rows (`_decorate_rows` or `list_curated_with_stats`) with `latest_thesis` (text-truncated) + `stance` + `thesis_age` (corrected clock) so the Traders table shows the last thing each trader said and where that position stands.
- Confluence: include `token_thesis_count` when a `mint` filter is used, so the UI can badge "N curated traders posted on this token."

All comment text is passed through as raw data; **escaping happens in the UI layer** (§1.7).

### 1.7 UI (`static/app.js`, memecoin controller)

- Traders table: new **Thesis** cell — latest thesis (truncated ~60 chars) + a `stance` badge (underwater=red, winning=green, flat=grey, unknown=dim) + age via `_ageRealIso` (fomo-clock corrected). Empty when no thesis.
- Per-trader panel: a **Theses** list — each row = ticker · their PnL% at post (with stance colour) · notional · age · the comment. Links rendered as click-through (`target=_blank rel="noopener noreferrer"`), **never auto-fetched**.
- Token view: when expanding a position/token, show curated theses on that mint + the confluence count.
- **XSS:** every `comment`, `handle`, `ticker` rendered via the existing `_esc`/textContent path — thesis text is attacker-controlled free text; never `innerHTML` raw.
- **Injection boundary:** thesis text is data. It is never fed to an LLM or used as an instruction anywhere in the pipeline.

---

## 2. Phase 2 — Telegram front-run latency instrument

### 2.1 What "faster than fomo sends them" honestly means

Two readings; only one is a real, winnable test:

- **vs fomo `createdAt` (the trade timestamp):** we are *always* later by `lag_s` (~0.3–0.8s feed lag). Trivially no. Not the test.
- **vs fomo's push notification hitting the phone:** fomo push ≈ their queue + APNs/FCM delivery (typically 1–5s); ours ≈ poll interval (≤500ms–1.5s) + Telegram send (~0.3–0.8s). **Plausibly winnable — measurement decides.** This is the test.

The shared reference clock is **the phone**: the operator observes, on the same device, when our Telegram lands vs when fomo's push lands. We cannot read fomo's push-arrival time programmatically, so the win/loss verdict is a manual paired read over N events. We *can* log our own side precisely (detection lag, send latency) and stamp every message so the comparison is exact.

### 2.2 Data model: `memecoin_alert_latency`

| column | notes |
|---|---|
| `id` PK | |
| `fomo_event_id TEXT UNIQUE` | dedup — one alert per event |
| `source_user_id, handle, mint, ticker` | |
| `their_size_usd REAL, market_cap REAL` | |
| `fomo_created_at TEXT` | fomo `createdAt` (T0, fomo clock) |
| `detected_at TEXT` | **corrected real-UTC** at alert dispatch (same domain as `alert_sent_at`, so diffable) |
| `lag_s REAL` | feed lag: `server_epoch − created` (real-UTC), age when detected |
| `local_ms REAL` | our receipt→send-start gap (record_alert commit + thesis lookup + msg build) |
| `alert_sent_at TEXT` | corrected real-UTC wall-clock after send |
| `send_ok INTEGER` | Telegram API success |
| `send_ms REAL` | Telegram POST duration |
| `signal_source TEXT` | always `global_feed` (only fresh events alerted) |

**Clock discipline (review fix 2026-07-08):** `detected_at` and `alert_sent_at` are BOTH corrected real-UTC (`real_utc_now_iso()`), never the raw host clock (which runs ~+7h). Only `created_at`/`lag_s` reference fomo time, and `lag_s` is computed from fomo's own `Date` header (real-UTC), so every stored timestamp is on one real-UTC basis — diffing `alert_sent_at − detected_at` is honest.

### 2.3 Sender: `sniper/services/memecoin_telegram.py`

```
def send_telegram(text, *, token=None, chat_id=None, http=None, timeout=None) -> (ok: bool, ms: float)
```

- Token/chat from `MEMECOIN_TG_BOT_TOKEN`/`MEMECOIN_TG_CHAT_ID`, falling back to the shared `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.
- **Fail-closed:** missing token/chat → `(False, 0.0)`, log once, never raise.
- Tight timeout (`MEMECOIN_TG_TIMEOUT_S`, default 3s) so a Telegram stall can't hang the poll loop.
- `http` injectable (default `urllib`) → tests never hit the network.
- The bot token is a **credential**: it is set by the operator in the environment. Claude never handles or prints it.

### 2.4 Runner hook: `run_once`

Inside the existing `for ev in mine:` loop, on a **new** curated event (`insert_event` returned True), **before** the copy fan-out (the send latency is the thing under test, so fire ASAP; copy is not latency-sensitive in paper):

```
if (MEMECOIN_TG_ALERTS and ev.side == "buy"
        and (ev.their_size_usd or 0) >= MEMECOIN_TG_MIN_USD
        and alerts_this_poll < MEMECOIN_TG_MAX_PER_POLL):
    _maybe_alert(conn, ev, real_utc_now_iso())
    alerts_this_poll += 1
```

`_maybe_alert`:
- `m.record_alert(conn, ev_fields)` — INSERT OR IGNORE on `fomo_event_id`; skip if not new (belt-and-suspenders with the event dedup).
- Build message: ticker · handle · size · mcap · fomo `created_at` · `lag_s` · our send wall-clock · **thesis if one exists for (uid, mint)** (latest from `memecoin_theses`).
- `send_telegram(...)`, measure ms, `m.update_alert_sent(conn, fomo_event_id, sent_at, ok, ms)`.
- Best-effort; wrapped so a send failure never breaks copy. Only `global_feed` events reach here (run_once feed events are all `global_feed`; the late `follow_activity` path never alerts — a 4-min-old signal would be a meaningless "slow" data point).

Gated by `MEMECOIN_TG_ALERTS` (default **off** — external send, explicit operator opt-in). Per-poll cap (`MEMECOIN_TG_MAX_PER_POLL`, default 5) prevents a burst (47 buys seen in one live poll) from tripping Telegram 429; dropped alerts are logged.

### 2.5 Readout: `/api/memecoin/alert-latency`

`m.alert_latency_summary(conn)` → `{n, n_sent, n_sent_ok, median_lag_s, median_local_ms, median_send_ms, median_total_ms, send_ok_rate, recent}`. Surfaces OUR side as **three disjoint measured segments** — `lag_s` (fomo feed → our receipt), `local_ms` (receipt → send-start), `send_ms` (the Telegram POST). Their sum (`median_total_ms`) is our full instrumented latency; nothing here proves we beat fomo — that is the manual phone read (§2.6). Send-time metrics condition on `send_ok=1` so a timed-out send (~3s) never inflates them. `n` = logged intents; `n_sent`/`n_sent_ok` = sent / successfully-sent (the sample backing the send medians). The UI pill is labelled "🔬 our detect+send … (vs fomo push = phone read)" — it does NOT badge itself "front-run" (that would presuppose the win).

**Per-poll cap truncation is logged.** When a burst exceeds `MEMECOIN_TG_MAX_PER_POLL`, the excess curated-buy alerts are dropped **in feed order** and counted (`run_once` returns `alerts_dropped`; a `logger.warning` fires). This is visible truncation, not silent — a reader knows the accrued sample was cut and (since feed order may correlate with freshness) which direction the bias likely runs. Curated buys are rare, so the cap rarely triggers; if bursts of >cap curated buys/poll become common, revisit with lag-priority ordering.

### 2.6 The actual verdict is a pre-registered manual phone read (out of v1 automation)

The automated readout is one-sided by construction. To claim "we beat fomo's push," a **paired manual comparison** must be run and pre-registered BEFORE reading results:
- **N ≥ 20** curated large-buy events, each observed on the **same phone** (the shared reference clock).
- For each event, record both arrival wall-clock times on that phone: (a) our Telegram, (b) fomo's own push for the **same trade**.
- **Outcome per event:** win (ours ≥ 1s earlier), tie (within ±1s), loss (fomo earlier). Decide the sign test / median delta before looking.
- **Kill/greenlight:** report the paired median delta + win-rate with a CI; do not report a verdict off fewer than N paired events or off the automated one-sided numbers alone.

Until that runs, the instrument's honest claim is bounded to: "our detection+send median is X; whether that beats fomo's push is unmeasured."

---

## 3. Settings (new knobs, `settings/memecoin.py`)

```
MEMECOIN_THESIS_ENABLED        = env bool, default 1
MEMECOIN_THESIS_UNDERWATER_PCT = env float, default -10.0
MEMECOIN_THESIS_WINNING_PCT    = env float, default 20.0
MEMECOIN_TG_ALERTS             = env bool, default 0        # external send — opt-in
MEMECOIN_TG_BOT_TOKEN          = env str, fallback TELEGRAM_BOT_TOKEN
MEMECOIN_TG_CHAT_ID            = env str, fallback TELEGRAM_CHAT_ID
MEMECOIN_TG_MIN_USD            = env float, default 0.0
MEMECOIN_TG_MAX_PER_POLL       = env int, default 5
MEMECOIN_TG_TIMEOUT_S          = env float, default 3.0
```

`.env.example` updated with the new knobs (documented, no secrets).

---

## 4. Edge cases (the failure surface, enumerated)

**Thesis capture**
1. Non-curated flood (~9/poll, mostly unfollowed) → curated-only filter at write; ~500k/day avoided.
2. Blank / link-only → skip empty-comment; keep URL-only with `has_link`.
3. PnL scale (percent, verified) — never ×100; `their_notional_usd` is THEIR size, never our sizing input.
4. `tag` enum unknown → store raw.
5. A thesis ≠ a call — display-only; never mutates copy/gate/verdict/position.
6. Cope skew (the §0 finding) — always render text with PnL bucket + entry age.
7. Re-delivery every poll → UNIQUE on `fomo_thesis_id`, keep-first.
8. Edits (same commentId, new text) → keep-first (original conviction is the signal).
9. Backfill gap during hibernate → lost (rolling feed, no thesis pagination) — stated, not hidden.
10. Injection: thesis text → data only, HTML-escaped, never to an LLM.
11. Off-platform links → click-through only, never auto-fetched (ToS, rate-limits, untrusted).
12. +7h clock skew → store `created_at` raw (fomo), age via corrected clock.
13. Unicode/emoji/very-long comments → TEXT + UI truncation + escape.
14. Unknown-vs-zero PnL → `None` preserved (`_numN`), stance `unknown` not `underwater`.

**Telegram instrument**
15. Alert storm (47 buys/poll seen) → per-poll cap + curated-only; **excess counted (`alerts_dropped`) + `logger.warning`** so truncation is visible, not silent.
16. Duplicate alerts on re-delivery → UNIQUE `fomo_event_id`.
17. Missing bot token/chat → fail-closed, log once, no crash.
18. Telegram stall → tight timeout, best-effort, can't hang the loop.
19. follow_activity events (≤15min stale) → never alerted (invalid for a latency claim).
20. Clock domains: `created_at`/`lag_s` reference fomo real-UTC; `detected_at` AND `alert_sent_at` are BOTH corrected real-UTC — never the raw host clock, never mixed. `local_ms`/`send_ms` are perf_counter durations (clock-agnostic).
21. Send-median poisoning by timeouts → `median_send_ms` conditions on `send_ok=1` (a 3s timeout can't inflate it); `send_ok_rate` reported separately.
22. Unmeasured pipeline gap → `local_ms` measures receipt→send-start so the three segments sum to a real total (`median_total_ms`); no hidden dark latency.
21. Send blocking copy → alert fires first with a 3s cap; copy (paper) tolerates the small delay; a send failure is swallowed.
22. Credential handling → operator sets the token in env; Claude never handles it.

---

## 5. Best-case upside (how to get the most out of it)
1. **The "why" behind a copy** — curated `large_buy` + thesis on the same mint within a short window = conviction entry with a reason; `swap_id`/`trade_id` makes the link exact.
2. **Objective cope-vs-conviction badge** — `stance × entry age`; surfaces bagholding vs adding-while-up.
3. **Cross-trader confluence** — ≥2 curated theses on the same mint in a window = independent conviction (a real signal), via `token_thesis_counts`.
4. **Discovery seeding (later)** — non-curated theses preceding runs → funnel candidates (sampled-non-curated lane, future).
5. **Alert enrichment** — the Telegram message carries the thesis, tying the two features together.
6. **Latency evidence** — accrued `alert_latency_summary` proves (or kills) the front-run premise the whole subsystem rests on.

---

## 6. Testing strategy
- `normalize_thesis_item`: type filter, attribution, skip-empty, link-only kept, synth-id, PnL None-vs-0, lag.
- `record_thesis`/`list_theses`/`latest_thesis_by_trader`/`token_thesis_counts`: dedup, filters, batch, ordering.
- `thesis_stance`: bucket boundaries + None→unknown.
- `run_once` thesis capture: curated-only, gated off = no-op, best-effort swallow, no copy-path interference.
- `send_telegram`: env resolution + fallback, fail-closed on missing config, ms measured, injected http, timeout path.
- `record_alert`/`update_alert_sent`/`alert_latency_summary`: dedup, update, summary math.
- `run_once` alert hook: gated off = no send, per-poll cap, only curated buys, dedup, thesis attached, send failure swallowed, follow_activity never alerts.
- API: theses endpoints (filters, stance decoration, escape-safe payload), alert-latency summary.
- One end-to-end test from a **real** `thesis_created` JSON fixture (per project rule: at least one test from a real payload, not a stub).

---

## 7. Explicit non-goals (v1)
- No sentiment/NLP on thesis text.
- No thesis-driven copy trigger, sizing, or gate.
- No non-curated thesis capture (later lane).
- No programmatic capture of fomo push-arrival time (manual phone read).
- No auto-fetching of thesis links.
