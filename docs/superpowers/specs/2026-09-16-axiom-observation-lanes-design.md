# Axiom observation lanes — design

Date: 2026-09-16. Owner: Codex. Status: operator scope amended on 2026-09-16.

## 1. Goal

Read Axiom data for source comparison, existing Conviction and Gold research, account discovery, and verified identity links.
The operator explicitly rejected a new paper arm. J7 remains the funded signal source.
Operator account actions remain explicit. A verified address link does not grant funded authority.

| Slice | Output | Consumer |
|---|---|---|
| S1 Trader directory | X handle -> wallet address per chain, with PnL, trades, volume | address verifier -> `memecoin_wallet_addresses` -> on-chain watcher |
| S2 KOL and trader scan | ranked traders and social callouts | wallet discovery (research) |
| S3 Research intake | Axiom discovery, KOL, live trades, and curated account lists | existing Conviction and Gold research; operator account review |
| S4 Wallet trade events | buys and sells for verified addresses, on one local clock | J7 comparison (coverage, arrival) |

## 2. Non-goals

- No funded authority. No slice can open, close or size a real position. The funded gate keeps
  refusing any signal whose `signal_source` is not `follow_activity`, `onchain_watch` or `manual`.
- No writes to Axiom. The adapter never clicks buy, sell, deposit, add wallet or any setting.
- No credentials in the repo, in `.env`, or in this chat. The operator logs in once inside a
  browser window; the session lives in a 0700 profile directory outside git.
- No custom HTTP client against `api*.axiom.trade`. Cloudflare Turnstile gates login and TLS
  fingerprinting blocks non-browser clients (Beta, 2026-09-16, from the maintained public SDK).

## 3. Why a real browser, not the internal API

Beta mapped the public reverse-engineered SDKs. Facts that decide the transport:

1. Auth is cookie-based (`auth-access-token`, `auth-refresh-token`), minted by email + password +
   emailed OTP behind a Cloudflare Turnstile CAPTCHA. A bare HTTP client is blocked by TLS
   fingerprinting; the maintained SDK drives a real Chrome to get in.
2. The surfaces we want most (cross-chain trader leaderboard with resolved addresses, KOL feed,
   discovery catalogue) appear in NO public SDK. Only the app renders them.
3. The one documented event path (WebSocket room `v:<wallet>`) has a public history of "responds
   nothing" and endpoint version churn (`-v2` -> `-v3`, `-v3` -> `-v4`).

A Playwright persistent Chromium profile solves all three: it is Chrome, so Cloudflare admits it;
it renders every surface; and it exposes the app's own WebSocket frames and REST responses
through `page.on("websocket")` and `page.on("response")` without us naming an endpoint. The repo
already uses this pattern for X fixtures (`scripts/capture_memecoin_x_fixtures.py`).

## 4. Components

### 4.1 `scripts/axiom_session.py login`
Opens Chromium with the profile outside the repository (mode 0700). The operator
logs in by hand (password, OTP, Turnstile). The script waits for Enter and closes. Nothing is
typed by the script.

### 4.2 `sniper/feeds/axiom_browser.py` — the capture worker
One process, `flock` on `data/axiom_capture.lock`. Headless Chromium on the same profile. It opens
the pages a slice needs and records:

- every WebSocket frame received (`page.on("websocket")` -> `framereceived`), and
- every JSON response from `*.axiom.trade` (`page.on("response")`),

into `axiom_raw_messages`. Each row carries `received_mono_ns` from `time.monotonic_ns()` and
`received_at` from `real_utc_now_iso()`. The monotonic stamp is the only clock used for arrival
comparison. Axiom's own timestamps are stored as data and never used as a clock
([[finding-fomo-createdat-precedes-chain-confirmation-2026-09-02]]).

Page schedule (knobs, `.env`):

| Knob | Default | Meaning |
|---|---|---|
| `AXIOM_CAPTURE_ENABLED` | 0 | master switch |
| `AXIOM_PAGE_INTERVAL_S` | 600 | how often the directory / KOL / discovery pages reload |
| `--pages` | `trackers,discover` | capture pages; this does not prove wallet subscriptions |

Push data (WebSocket) costs no requests; only page reloads do. The interval knob is the operator's
speed control. The operator chose "full speed"; the default starts at 600 s and the knob is theirs.

An API 403, a Turnstile page, a login page, or three consecutive empty loads sets `data/axiom_capture.halted` with the reason and pages the operator through the existing
Telegram alert path. The worker preserves the halt across restarts. The operator uses `reset-halt` after session repair.
An apex document 403 pauses page reloads; an apex XHR 403 stops capture.

### 4.3 Parsers (pure functions, tested on saved fixtures)
- `parse_trader_directory(dom_or_json) -> [{x_handle, display, address, chain, pnl_24h, trades, volume, followers}]`
- `parse_trader_positions(...) -> [{address, mint, bought_usd, sold_usd, remaining_usd, opened_at}]`
- `parse_discovery(...) -> [{chain, mint, pair, mcap, liquidity, volume_m5, age_s, filters}]`
- `parse_wallet_event(frame) -> {address, chain, mint, side, amount_usd, tx_sig, axiom_ts}`

Each parser refuses a shape it does not recognise (returns `[]` and logs the first 200 bytes) so a
UI change produces "no data", never wrong data
([[feedback-an-absent-read-must-not-answer-the-question-2026-09-08]]).

### 4.4 Tables (SQLite, `sniper_tracker.db`, created by `ensure_axiom_tables`)

<!-- schema:external=axiom_raw_messages -->
<!-- schema:external=axiom_trader_directory -->
<!-- schema:external=axiom_wallet_events -->
<!-- schema:external=axiom_discovery_tokens -->
<!-- schema:external=axiom_kol_callouts -->

| Table | Key columns |
|---|---|
| `axiom_raw_messages` | id, received_mono_ns, received_at, transport (ws/rest), source (url or room), page, payload (TEXT), byte_len |
| `axiom_trader_directory` | x_handle, display, address, chain, pnl_24h, trades, volume, followers, seen_at; UNIQUE(x_handle, chain, address) |
| `axiom_wallet_events` | id, received_mono_ns, received_at, address, chain, mint, side, amount_usd, tx_sig, axiom_ts, raw_id |
| `axiom_discovery_tokens` | id, seen_at, chain, mint, pair, mcap, liquidity, volume_m5, age_s, filter_set |
| `axiom_kol_callouts` | id, seen_at, kol_handle, mint, chain, called_mcap, text, raw_id |

Raw messages use `data/axiom_raw.db`. Retention limits age and retained payload bytes to 2 GB.
SQLite reuses free pages; the worker does not shrink the file with VACUUM. Parsed tables retain observations.
The capture excludes `bundle-key-and-wallets*` before it reads or stores the response body.

### 4.5 Slice logic

**S1 address verification.** Handle matches and explicit research links produce candidates.
The verifier uses independent `follow_activity` events and supported transaction evidence.
Three distinct token purchases and no contradiction can confirm a candidate.
A negative verdict requires complete coverage and an exact control transaction.
Unsupported payment paths remain unknown. The verifier requires `wallet-paid-v2` proof details.
Old seed rows do not satisfy this proof. Seeds cannot overwrite a current proof.
Shared addresses cannot replace independent Fomo ownership. An empty proof refresh clears the watcher address list.
The bounded verifier CLI separates read-only inspection, public RPC reads, and proof writes.

**S2 KOL and trader scan.** Directory rows and callouts are research tables. No automatic arming.
Roster changes stay operator decisions on funded results
([[feedback-roster-changes-use-funded-results-only-2026-09-11]]).

**S3 existing research and account review.** Fresh, parser-v2 Solana discovery identities enter the existing scanner for Conviction and Gold.
The adapter compares receipt times with corrected UTC. The existing scanner retains its lifecycle clock, rules, Golden roster, and paper arms.
The adapter does not use Axiom native values as USD scores. It does not infer token birth from the receipt time.

The Axiom panel exposes the captured directory, named tracked-wallet identities, KOL calls, trade observations, and token discovery.
The operator can add, remove, or replace local research accounts and request an identity check against an existing tracked account.
A valid manual address has origin `manual`. A manual address does not establish an account handle or ownership proof.
The operator applies changes to the Axiom account manually. These actions do not grant funded authority.
The panel accepts `axiom_confirmed` only with proof version `wallet-paid-v2`; it labels older confirmations for a new check.

Current capture limits:

- `smart-money-traders` and named identity objects in `tracked-wallets-v4` supply the normalized account directory.
- `top-traders-v6`, `transactions-feed-trader-info-v3`, and `transactions-from-traders-v4` remain raw-only. Their positional fields lack a verified decoder.
- The wallet WebSocket decoder remains unavailable. REST wallet transactions have history provenance; they do not establish live arrival speed.
- The worker reports subscription coverage as unknown. Aggregate WebSocket traffic does not prove wallet coverage.
- KOL source IDs and source times define event identity when available. Other rows identify unique payloads, not unique calls.
- Solana market cap and liquidity retain SOL units. EVM REST values retain USD units. Unknown volume windows do not populate `volume_m5`.

**S4 trade events beside J7.** Both sources retain local receipt clocks and host/boot identity.
Exact matches require chain, owner, token, side, and transaction identity.
Weak matches require a unique compatible pair; missing amounts form a separate group.
Latency uses monotonic clocks only when the host/boot identity matches.
REST rows remain history. WebSocket transport alone does not establish a live signal.
The current wallet parser does not prove a live envelope; those WebSocket observations remain unknown.
The report separates unmatched observations from provider misses.
Subscription intervals remain unavailable, so coverage and superiority verdicts remain unproven.
Seven continuous days remain a minimum evidence requirement, not automatic source promotion.

## 5. Error handling

| Event | Action |
|---|---|
| Profile missing or logged out | worker exits with `login_required`; operator runs `axiom_session.py login` |
| API 403 / Turnstile / empty loads | preserve halt file; send existing operator alert |
| Parser refuses a shape | row skipped, raw kept, counter `parse_refused` in heartbeat |
| Browser crash | worker stops; no restart automation is established by this change |
| Retained raw payload exceeds 2 GB | remove oldest rows; file pages remain reusable |

Heartbeat: `data/axiom_capture.heartbeat` with `ts`, `pages_open`, `ws_frames_60s`, `last_frame_at`,
`parse_refused`, `halted`.

## 6. Testing

- Parser tests on saved fixtures under `tests/fixtures/axiom/` (captured once from the operator's
  session; addresses in fixtures are real public data).
- Clock test: two frames 50 ms apart produce `received_mono_ns` 50 ms apart regardless of
  `axiom_ts`.
- Verifier tests: 3 matches -> confirmed; 1 contradiction -> REFUTED; 2 matches -> unverified.
- Halt test: a 403 response sets the halt file and stops the loop.
- Gate test: a signal with `signal_source='axiom'` is refused by the funded entry hook.

## 7. Risks, stated

- Account block. Axiom's automated-access terms are unread (their ToS page returns 403 to us). A
  block would also cost the operator the app by hand. Mitigation: push data over reloads, the
  interval knob, and fail-closed halts. Fomo banned this project once for app-layer access
  ([[finding-memecoin-fomo-account-ban-2026-08-03]]).
- UI drift. Parsers go to "no data" on a shape change; the heartbeat shows `parse_refused`.
- Chain coverage. Only Solana and BNB wallet tracking are confirmed in public sources. Robinhood
  4663 and Base are checked live in S1 and reported, never assumed.
- Memory. One headless Chromium, about 500 MB. The host has 18 GB free.

## 8. Build order

1. Session login script, capture worker skeleton, raw table, heartbeat, halt logic, gate test.
2. S1 directory parser + verifier + on-chain watcher list feed (Gamma verifies).
3. S2 KOL capture (same pages).
4. S4 wallet event capture + J7 matcher (needs S1 output).
5. S3 existing research intake and operator account review.

Each step deploys nothing to the funded runner. The capture worker is its own process.

## 9. Completion contract — operator amendment, September 16

No new paper arm is authorized or required.
Source comparison requires the same chain/account population, immutable local receipt clocks, and durable host/boot identity.
REST history and rows with unknown provenance remain untimed. Weak and exact matches have separate results.
Unmatched rows do not establish provider misses without a confirmed observation interval.
Capture records its run and parser version, uses one process lock, respects saved halt state, and bounds retained raw data.
Historical schema migration preserves first receipts and treats missing provenance as unknown.
Purchase proof requires actual acquired value and payment evidence. Unsupported relayer cases remain unknown.
The on-chain address list accepts a successful empty refresh after proof revocation.
J7 receipt persistence runs outside the funded loop and reports receipt loss explicitly.
The operator's six Axiom tracker additions are acknowledged; actual wallet push shapes still require captured evidence.

## 10. Current provider limits

`top-traders-v6`, `transactions-feed-trader-info-v3`, and `transactions-from-traders-v4` remain raw-only.
Their positional layouts lack a verified decoder contract.
The named identities within `tracked-wallets-v4` can extend the account directory.
The local research list does not change Axiom subscriptions. The operator applies provider account changes in Axiom.
KOL rows with no provider ID or time identify unique payloads, not a complete call count.
Solana discovery enters the existing Conviction and Gold watch queue. Existing evaluator rules remain authoritative.
No new paper arm or funded approval is part of this change.
