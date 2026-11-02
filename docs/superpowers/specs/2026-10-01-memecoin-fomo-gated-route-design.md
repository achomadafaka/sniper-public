# Fomo route for launchpad-gated (co-sign hook) Solana coins (design)

**Author:** Beta · **Order:** the operator, relayed by Alpha 2026-10-01 ~18:00Z: revisit the Fomo route for
Meteora DBC coins whose transfer hook requires the platform co-sign. "We do not care about the tos."
Example: THEO, DegenCapitalLLC buy of $1.6k, refused "transfer hook 4Tabcoy1 requires the platform
co-sign". **Status:** NOT BUILDABLE under §0 (amendment A0), spec only. Phase 0 (the operator's capture) comes before any build, and
the operator approves the build.

**Amendment A0 (2026-10-01T18:18:24Z): Q0 ran. The route is NOT BUILDABLE under §0.** Alpha approved the two
unauthenticated calls (no account, session or money). Beta sent each one once, with the §0 truthful
headers: `User-Agent: sniper-fomo-trade/1`, no `Origin`, `Referer` or `privy-client`, Python stdlib TLS.

| call | status | edge | body |
|---|---|---|---|
| `POST https://auth.privy.io/api/v1/sessions` with `privy-app-id`, `privy-client-id` and `{"refresh_token":"invalid"}` | 403 | cloudflare, cf-ray `a43d783f0d88c8a4-ZRH`, JSON from the application | `{"error":"Must specify origin","code":"missing_origin"}` |
| `GET https://prod-api.fomo.family/`, no auth | 401 | cloudflare, cf-ray `a43d78402be5bc11-ZRH`, JSON from the application | `{"success":false,"message":"No authorization token provided",...}` |

- Neither Cloudflare edge refused the truthful client. The `fomo_feed.py:452` comment ("403 (error
  1010)") is stale: today the refusal is Privy's `missing_origin`, not 1010.
- Privy's application refuses any request with no `Origin`. It would accept only an origin on Fomo's
  Privy allow-list, i.e. Fomo's domains (an inference, not a measurement); sending one is forbidden by §0.
- Every route shape needs the Privy session refresh, because access tokens expire. So no route can be
  built inside the hard line. Per §1.2 the build stops here, and the operator skips the $5 capture.
- Everything below is kept as the record of the design.

**Inputs:**
- feasibility spec `docs/superpowers/specs/2026-09-29-memecoin-fomo-cosign-dbc-feasibility.md`
  (f1f73de69): the chain read of 365 co-signed swaps;
- `docs/audits/2026-09-27-fomo-authorized-execution-instructions.md`: the EYES receipt and the
  lifecycle rules;
- `docs/audits/2026-09-27-f352f952-dbc-route-gap.md`;
- decision 6cff1db6;
- memory `finding-dbc-lost-coins-are-launchpad-hook-gated` with its 09-29 correction.

**What changes from 6cff1db6.** That decision recorded the route as not built because Fomo's terms
bar automated execution through ordinary accounts. The operator now accepts that risk. The other
findings stand:
- Fomo's co-signer signs only messages Fomo builds;
- funds sit in Fomo's wallet;
- the account's credentials never go in `.fomo.env`.

## 0. The hard line

The route uses Fomo's own web API from our process, with the operator's own session, and says
truthfully what it is. It never evades bot detection. **If the flow needs any of the following, the
build stops and we report:**
- solving a CAPTCHA or a Cloudflare Turnstile, JS, or managed challenge, or replaying a
  `cf_clearance` cookie;
- a forged client identity: a browser `User-Agent`, an `Origin` or `Referer` naming fomo.family,
  `sec-ch-ua*` headers, a `privy-client` value naming Privy's browser SDK, TLS/JA3 impersonation
  (curl_cffi and similar), or a headless or automated browser;
- rotating IPs or accounts, or any other way around a rate limit or ban;
- typing, storing or reading the operator's password, email code or seed phrase.

**Alpha's ruling (2026-10-01). This is not an operator toggle.**
- The trade client sends truthful headers only: `User-Agent: sniper-fomo-trade/1`, no `Origin`, no
  `Referer`, no `privy-client`.
- If Fomo or Privy refuses a truthful client (403, an origin or CORS error, or a challenge), the route
  is blocked. We stop and report.
- No knob, flag or env var re-enables forged headers. A test pins the trade client's header set.

**Allowed:** request fields that carry data, not client identity: the Privy app id and client id,
Fomo's own `x-*` data headers, and the session's bearer token.

**Known consequence: the route may be blocked before Phase 0.**
- The feed client's own comment says Privy's edge refuses a non-browser client:
  `sniper/feeds/fomo_feed.py:452`, "auth.privy.io is Cloudflare-fronted: browser headers required
  or 403 (error 1010)" (commit f12568fd2, 2026-07-05). Stale per A0: the refusal is now
  `missing_origin`.
- Access tokens expire, so every route shape needs the Privy session refresh. If that edge still
  refuses a truthful client, no route can be built.
- Q0 (§1.2) tests both edges with no session before the operator spends money on the capture.

**Out of scope, pre-existing.** The feed client (`sniper/feeds/fomo_feed.py:328-335`,
`BROWSER_HEADERS`, and the refresh headers at :452-458) already sends a forged Chrome User-Agent,
fomo.family `Origin`/`Referer` and a `privy-client` SDK value. This spec does not change it. The trade
client never imports `BROWSER_HEADERS` or copies those values.

## 1. Phase 0: the interface, from the operator's own capture (no build, no call from our side)

### 1.1 What the operator captures, and how

Use his own logged-in browser (Chrome or Chromium), account **ExactLeftBison** (Solana owner
`3h75aBTLbQYaJnSr7JztjYdWEke5thUSUXzqJjQpe7AM`). The Fomo wallet holds USDC (~$8 after EYES).

1. Open fomo.family, then open DevTools (F12) > Network.
   - Tick "Preserve log" and "Disable cache".
   - Clear the log, then reload the page with DevTools open. The page load captures the session and
     token calls.
2. Open a gated coin's page. Any Fomo DBC coin works; one whose refusal we logged (THEO, EYES) is
   best. **Buy $5 of it.** EYES was $2, so $5 gives the second point that tells a flat fee from a
   percentage. Wait until the UI shows the trade confirmed.
3. Wait about 30 s, then **sell the whole balance** of that coin. Wait for confirmation.
4. Without trading, open these two screens and look only. **Do not click export.**
   - the wallet's "Withdraw / Send" screen;
   - Settings, to see whether an "Export wallet / private key" option exists.
5. In the Network panel, right-click any row and choose **"Save all as HAR (sanitized)"**
   (Chrome 130+). This strips `Cookie`, `Set-Cookie` and `Authorization`.
   - Also note the clock time of the buy click and the sell click (local time is fine). They anchor
     the latency read.
6. Save the HAR as `~/.config/sniper-fomo-trade/capture-raw.har`. The directory is mode 700 and the
   file mode 600. It is outside the repo and never committed.
7. **No redaction tool ships (Appendix A).** The capture does not run while A0 stands. If the route
   ever reopens, an allowlist redactor is built and reviewed first, and Beta reads only its output.

What NOT to do:
- no password, email code or seed phrase goes anywhere;
- do not export the wallet key in Phase 0;
- do not log out of the session used for the capture. Logout revokes the refresh tokens of that
  session, which does not matter for the capture but matters for §2.

### 1.2 What the capture must answer

**Q0, before the capture: the two edges, with no session.** Beta sends two unauthenticated calls with
the §0 truthful headers. Each call runs once, after the operator approves the pair. No token, no
account and no session is involved.
- `POST https://auth.privy.io/api/v1/sessions` with the Privy app id and the body
  `{"refresh_token": "invalid"}`;
- `GET https://prod-api.fomo.family/` with no auth.

Read the result per edge:
- A JSON auth error from the application (400 or 401) means the edge accepts a truthful client.
  Phase 0 can go ahead.
- A 403 with a Cloudflare error page (1010 or 1020) or a challenge means the edge refuses it. The
  route is blocked under §0. We report, and the operator skips the capture.

The interface section of this spec is completed from the redacted HAR, as amendment A1, before any
build:

| # | question | where it shows in the HAR |
|---|---|---|
| Q1 | The buy and sell endpoints: path, method, request body (mint, side, amount, amount unit, slippage, client order id?) and response body | `prod-api.fomo.family` XHR rows at the click times |
| Q2 | **Who signs the user side.** (A) Fomo's backend executes: one POST returns a signature or order id, and no Privy signing call appears. (B) The backend returns a message the client signs through Privy's wallet API: rows to `auth.privy.io` under `/api/v1/wallets/.../rpc`. (C) Signing happens only inside the Privy iframe: `postMessage` with no HTTP signing row. | the rows between the click and the confirmation |
| Q3 | Auth: access-token type and lifetime (the redaction keeps each JWT's `iat`/`exp`/`iss`/`aud` claims and drops the token), refresh endpoint, refresh-token rotation, any fomo-app-scoped token | `auth.privy.io/api/v1/sessions` rows, response bodies |
| Q4 | The fee breakdown at $5 against EYES at $2: total debit, net swap input, fee lines | the quote response before the buy, and the receipt |
| Q5 | Order status, and recovery after a lost response: an order id, a status endpoint, a websocket event | XHR polling rows and `_webSocketMessages` |
| Q6 | Idempotency: does the request carry a client-generated id the server deduplicates on? | the request body and headers |
| Q7 | The withdraw endpoint shape, and whether key export exists | the step 4 screens |
| Q8 | **The truthful-client test with a session.** Beta replays ONE harmless read the app made (a quote or balance GET, never a trade) once, with the §0 truthful headers (no `Origin`, no `Referer`). It uses the route's own §2 session, never the capture session or the feed account. Only after the operator approves that one call. | 200 versus 403, an origin error or a challenge |

**Decision after Phase 0:**
- **Interface (A), server executes:** buildable. Our adapter posts the order and reconciles by chain
  signature.
- **Interface (B), client signs through Privy's HTTP wallet API:** buildable. Our adapter asks Privy
  to sign with the session. Fomo's co-signature is then added server-side or already present.
- **Interface (C), iframe-only signing:** not buildable inside the hard line (it needs browser
  automation). Stop and report.
- **Q0 or Q8 refused** (403, an origin error or a challenge for a truthful client, from Fomo or
  Privy): not buildable. Stop and report.

## 2. Credentials (never `.fomo.env`)

- One directory, `~/.config/sniper-fomo-trade/` (mode 700), holding:
  - `credentials.json` (mode 600): access token, refresh token, Privy client-analytics id, the
    expected Solana owner `3h75aBTL...`;
  - `tokens.json` (mode 600): the rotated tokens.
- None of these are environment variables. `load_env_files` never sees them, and no other process can
  read them through `.fomo.env`.
- **The operator puts them there, never us.** Use a fresh session the trade route owns alone:
  - log in to Fomo in a NEW private window;
  - copy `privy:token`, `privy:refresh_token` and the CA id from that window's localStorage into
    `scripts/memecoin_fomo_trade_credentials.py`. It prompts with hidden input, refuses a path inside
    the repo, writes with mode 600 and prints only lengths;
  - close the window WITHOUT logging out.

  Privy rotates the refresh token on every refresh and a spent token is dead, so a session that both
  the browser and the runner refresh kills itself.
- **The adapter refuses to start** when:
  - either file is wider than 600, or the directory is wider than 700;
  - the path resolves inside the repo or the state root;
  - the session's Solana owner differs from `3h75aBTL...`;
  - the Privy account id equals the one in `.fomo.env` (a feed account must never trade).
- **Endpoint allowlist.** The adapter's HTTP client has a hard-coded list: the buy, sell, status,
  quote and refresh paths found in Phase 0. Any other path raises.
  - The trade account never calls `/feed`, `/v2/users/*`, follow endpoints, the leaderboard or the
    websocket. That traffic is what got three accounts banned (feasibility spec §4).
  - A test pins the allowlist.
- **Refresh is single-flight.** One lock; the rotated token is written atomically (temp file, then
  rename) BEFORE the new access token is used. A second process refuses to load the session while a
  lock file names a live pid.
- Tokens are never logged. Every log line goes through a redactor; a test greps the adapter's log
  output for JWT shapes.

## 3. The money path (CLAUDE.md rule 5, in order)

### 3.1 Money path

Yes: entry, exit, sizing and signing. Every item below applies, and the operator approves before the
build.

**Scope:**
- ONLY Solana mints whose hook program is classified `fomo_cosign_buy_gate` (§5);
- ONLY automatic entries the ordinary path already admitted, i.e. the same chase, volume, safety and
  exposure gates that ran before;
- the source must be armed on solana, as today.

**Sizing:**
- a FIXED clip, `MEMECOIN_FOMO_ROUTE_CLIP_USD` (code default 0 = route off; proposed 25);
- the regime risk scale only scales it down;
- spendable is the Fomo wallet's own USDC (chain read of `3h75aBTL...`'s USDC token account), never
  the main wallet's.

**Total custody cap:**
- `MEMECOIN_FOMO_ROUTE_MAX_WALLET_USD` (proposed 75): the operator funds the Fomo wallet with at most
  this;
- above the cap, a GAS-LOW-style page asks him to sweep the excess. We never move funds out
  automatically.
- The cap bounds what a ban can freeze (§3.5).

**Concurrency:**
- `MEMECOIN_FOMO_ROUTE_MAX_CONCURRENT` (proposed 2) open or reserved Fomo-route positions;
- the global funded concurrency and exposure caps still count them;
- one in-flight order per mint, held until resolved.

**Fees (Q4):**
- EYES: a $2 debit was a $1.05 swap plus $0.95 in fees. If the fee is flat, that is 3.8% of a $25 clip
  per leg, about 7.6% a round trip, before slippage.
- Phase 0 decides. If the round-trip fee exceeds the median gated-coin return Gamma priced for the 7
  origins, the spec says so and the operator decides with that number in front of him.
- Every fill stores total debit, net swap input, each fee line and the received amount separately.
  `entry_notional` is the TOTAL debit, so r is net of the platform fee.

### 3.2 Speed on the fill path

**The ordinary path gets no added work:**
- the hook class comes from the mint account `token_safety` already reads, plus a dict lookup on the
  hook program id (§5);
- no Fomo call, no new RPC read, no new lock;
- measured before and after on the pinned release: J7-loop and Solana entry p50/p90.

**The gated path:**
- runs on its OWN worker thread, the pattern of the chain-1 worker;
- the J7 loop hands it a reserved entry and moves on, so a slow Fomo answer never holds another
  coin's entry or any exit;
- the token refresh runs off the entry path, ahead of expiry; an entry never waits on a refresh.

**Latency, measured, never estimated:**
- Phase 0 gives the app's own click-to-confirmed time;
- the canary measures signal to Fomo request to confirmed;
- the comparison is our Solana lanes: slot lane 6 ms decide to send, on-chain lane 723 ms. The
  route is expected to be slower, because Fomo builds, co-signs and submits. A trial or grade on this
  route states that bias beside r.

**Deadlines:** finite, from measured behavior. One order call with no automatic resend of a buy
(§3.5); one status poll cadence.

### 3.3 Concurrency and global state

- **Refresh is single-flight** with atomic token rotation (§2). The capture-then-restore pattern is
  banned: the adapter holds no module-level mutable session. The session object is owned by the
  worker thread.
- **The reservation** goes through `reserve_entry`'s existing BEGIN IMMEDIATE. The route is frozen
  into the funded contract's `source` block as `{"execution_route": "fomo_gated", "wallet":
  "3h75..."}`, the same tolerant block as the trial marker, so a rollback can still read it. Exits
  read the route from the contract and never guess it.
- **The exit sweep** sends a Fomo-route sell to the same worker. It never calls Fomo inline.

### 3.4 Absent, zero and unknown

**The Fomo wallet balance:**
- read from chain (owner `3h75...`), never from Fomo's UI or API;
- an unreadable balance refuses the entry and never reads as 0;
- a 0 balance refuses with "fomo wallet empty".

**Order outcome:**
- no response, a timeout, or a 5xx after the request was sent is `submitted_unknown`;
- the reservation is HELD, and is resolved only by chain evidence: the owner wallet's signatures since
  the request, a transaction touching the mint, and that transaction's token balance delta;
- the fill booked is the chain delta, never the API's claimed amount.

**Fees:** a fee line Fomo does not return is unknown, not 0. The chain delta of the owner's USDC is
the total debit.

### 3.5 Failure modes of every dependency

- **Access-token expiry:** refresh ahead of expiry. A failed refresh stops the route (no new entries)
  and pages. Exits keep trying through the API with the last good session, then stop.
- **Refresh rotation conflict** (spent token): the session is dead. The route stops and pages. The
  operator puts in a fresh session (§2). No automatic re-login exists, by design.
- **401/403 on a trade path:**
  - stop the route at once and page "FOMO ROUTE REFUSED (possible ban)";
  - no retry loop;
  - open positions go to the ban plan below.
- **429:** stop new entries for the `Retry-After` time (or 60 s). Never retry a BUY; a SELL retries
  once after the wait.
- **Ban during a position.** This is the custody risk in its sharpest form:
  - funds and coins are in Fomo's wallet;
  - the eyes hooks gate BUYS only ("Fomo verification passed" on 218 of 218 buys and 0 of 53 sells),
    so a sell signed only by the owner should pass the hook;
  - but signing as the owner needs the embedded wallet's private key, and Phase 0 Q7 says whether
    export exists;
  - **the operator decides before go-live:** export the key into a mode-600 file as ban insurance
    (custody returns to us; an own-built DBC sell then needs its LiteSVM proof, as UP had), or accept
    that a ban can freeze up to `MEMECOIN_FOMO_ROUTE_MAX_WALLET_USD` plus open positions until Fomo
    releases them;
  - after migration the hook is revoked and a Jupiter sell works for the owner, still with the key.
- **Wrong answer:**
  - interface (B): the message we are asked to sign is checked before signing: the owner, the mint,
    the input amount within the clip plus quoted fees, the recipient token account owned by `3h75...`,
    the router `proVF4pM` and the DBC program, AgmLJBMD as fee payer, no approve, delegate or
    authority change, and a fresh blockhash;
  - interface (A): we sign nothing, so the check is the chain delta after the fact. Any mismatch pages
    and stops the route.
- **The program changes:** the eyes binary is hash-bound (§5). A changed executable hash demotes the
  hook to `unknown` and entries refuse until it is re-classified.

### 3.6 Units and identity

- **Amounts:** the order amount's unit (USD, USDC atomic, or token) and meaning (total debit or net
  input) come from Q1 and Q4. A test pins both. USDC has 6 decimals; the coin's decimals come from
  the mint read.
- **Identity:** mint plus network `solana`. The wallet is fixed per route. The trade account's Privy
  id is never the feed account's.
- **Times:** the source slot and the fill slot are kept separately; host monotonic time for latency.

### 3.7 The sample

- The route only buys coins our gates already admitted.
- Since 09-13: 7 co-sign-gated refusals (Gamma 6f882ffe6), about 1 a week, plus THEO today. The forward
  sample grows slowly.
- r is measured on a slower lane than the ordinary one, after a platform fee the ordinary path does
  not pay. That is our route's return, not the wallet's.
- Grade it by the operator's funded-results rule; never pool it with ordinary Solana closes.

### 3.8 Rollback

- `MEMECOIN_FOMO_ROUTE_CLIP_USD=0` (the code default) stops new route entries. Exits for held route
  positions keep running.
- Removing the credentials file stops all route calls. Held coins then wait for the operator, since a
  sell needs the session or the key.
- Half-applied (route on, credentials missing): every gated coin refuses with
  "fomo route not ready", as it refuses today. Nothing changes for other coins.

## 4. The exit path

- **Primary:** sell the WHOLE token balance of `3h75...` through Fomo's sell endpoint (interface A or
  B), from the worker thread.
  - The funded rails decide WHEN (TP, stop, trail, vol_death, mirror).
  - Price marks for those rails come from the DBC pool's own state (curve reserves). That is a chain
    read and never a Fomo call, because each Fomo quote is account traffic.
  - The executable whole-bag quote that the funded exit rules require is taken from Fomo's quote
    endpoint ONLY at a decision (one call), if Q1 shows such an endpoint. Otherwise from DBC curve math
    on the pool state, which needs the decoder promoted from research with tests.
- **Sells and the hook:** the eyes binary gates buys only (above). ESKuS8vD (another launchpad's
  verifier set) had 21 sells, all co-signed; whether its sells need the co-sign is unknown. A coin on
  a hook class whose sell gate is unproven refuses ENTRY (a buy we cannot prove we can sell), until a
  LiteSVM proof or an observed owner-only sell settles it.
- **After migration** (DAMM v2), Fomo still sells for the account. A Jupiter sell needs the key.
- **Residuals:** a dust balance after a sell is booked as dust (existing rule). An unsellable bag uses
  the write-off script. The operator runs `--confirm`.

## 5. Detection: hook classification (the 09-27 audit's §2 refactor)

- `token_safety_verdict` keeps reading the mint once. `_launchpad_hook_program` returns the program id
  as today; a new pure `hook_class(program_id)` looks it up in a table:
  - `fomo_cosign_buy_gate`: the eyes binary (eyesEswW, eyesfsib, and any program with the same
    executable hash);
  - `size_cap`: UP's Ft8R4BsJ, with its caps;
  - `other_launchpad_verifier`: ESKuS8vD;
  - `unknown`: anything else, including THEO's 4Tabcoy1 until it is classified.
- **The table is built off the entry path:**
  - a background job hashes each hook program's executable (the ProgramData account) once and on
    upgrade;
  - the entry path only reads the dict;
  - an unhashed program is `unknown` and refuses with today's label, while the job hashes it for the
    next coin.
- **Verdict per class:**
  - `fomo_cosign_buy_gate` with the route ready (credentials valid, wallet funded, no route stop):
    the verdict passes TO THE ROUTE, and every other check still runs (freeze and mint authority,
    permanent delegate, transfer fee). An exception must not return before them, as the 09-27 audit
    requires.
  - Route not ready: refused "fomo route not ready".
  - `unknown`: "unsupported hook (unclassified)". Never "requires the platform co-sign", which the
    09-29 correction showed is not implied by the DBC authority.
- **Speed:** the dict lookup adds ~1 µs to the ordinary path. A test pins that ordinary (null-hook or
  no-hook) mints make zero Fomo calls and zero new reads.
- **Retry lane:** a gated coin whose route is ready never enters the Jupiter/Raydium retry lane. Its
  refusal stays terminal when the route is not ready, as today.

**Before the build:** THEO's hook 4Tabcoy1 is hashed and classified (one research read). If it is
not the eyes binary, it is `unknown`, and the example coin would not be bought by this route.

## 6. Build plan (after Phase 0, amendment A1, and the operator's go)

1. **Phase 0:** Q0 first (§1.2; a refusal ends the route here). Then the operator captures (§1.1);
   Beta writes amendment A1 (the interface) from the
   redacted HAR; Gamma reviews; the operator decides on the fee number and the key export.
2. **TDD:**
   - credential loader refusals;
   - endpoint allowlist;
   - the truthful header set (§0): no `Origin`, `Referer`, `privy-client` or browser `User-Agent`;
   - single-flight refresh with atomic rotation;
   - the hook-class table and the verdict split;
   - the worker with reservation, unknown outcome and chain reconciliation;
   - the message check (if interface B);
   - exits through the route;
   - the fee split;
   - the zero-Fomo-call pin on ordinary mints.
3. **Measurement:** ordinary-path p50/p90 before and after (no regression). The route's latency comes
   from the canary.
4. **Canary:** the operator's explicit go, at most 3 buys at $25 with full sells. A restart is injected
   mid-order once. Read: fills, unknowns resolved, fee per leg, latency, and that no non-allowlisted
   call left the process.
5. Gamma reviews each step; Alpha merges and deploys; activation is `.env` plus a restart, by the
   operator.

## Appendix A: redaction (nothing ships)

The inline script of c17fe231a leaked (Gamma MEDIUM). Its replacement, a denylist redactor in
82eea57f3 (`scripts/research/redact_har.py`), also leaked: Gamma's synthetic cases
(`~/.cache/gamma/fomo_adv.py`) got 18 of 18 secrets through, and the script still wrote the file
every time. The fail-closed check only re-checks values that the name and shape classifier already
found, so a classifier miss leaks silently. Examples:
- concatenated or plural names (`sessionid`, `authtoken`, `apiKeys`, `tokens`);
- an SSE or `text/plain` body;
- a secret used as a JSON key;
- names outside the lists (`recoveryPhrase`, `pwd`);
- a binary websocket frame;
- a URL-encoded copy of the value under an innocuous name.

The route is NOT BUILDABLE (A0), so the script and its test were dropped from the record (a522ba4c5).
No credential tool ships.

If the route ever reopens, the redactor must be an allowlist:
- it keeps values only for listed keys (mint, amounts, signature, status), and every other string
  becomes a marker;
- a post-scan refuses to write when any token-alphabet string of 16 or more characters survives
  outside that list.

It is reviewed against Gamma's cases before the operator runs it.
