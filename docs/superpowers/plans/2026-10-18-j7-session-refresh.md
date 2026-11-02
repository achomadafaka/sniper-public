# J7 Session Refresh Implementation Plan

> **For agentic workers:** Use the existing isolated worktree. Implement the tasks below with focused RED/GREEN checks and independent review.

**Goal:** Renew the bot-owned J7 session before access expiry, with no funded-runner restart during renewal.

**Architecture:** A daemon owned by each refresh-enabled J7 feed exchanges the private `__Host-j7_rt` cookie at J7's existing HTTPS auth endpoint. It durably replaces access and refresh credentials together in `.j7.env`, then reconnects only that account's existing feed. Socket receipt clocks, queues, wallet authority and all trading gates remain unchanged.

**Tech Stack:** Python standard library, existing python-socketio, pytest with `-n 2`.

## Interfaces and scope

- `sniper/feeds/j7_auth.py`: `J7Session(path, key, token)`, `refresh_if_due(now=None, force=False) -> str | None`, and `status() -> dict` containing only safe metadata. `enabled` requires the matching `J7_REFRESH_TOKEN[_2]` credential. Refresh starts one hour before expiry, has a 10-second HTTP timeout and bounded retries; revocation needs manual login.
- `sniper/feeds/j7_feed.py`: optional per-feed session, isolated daemon renewal, pending-token handoff after the old socket stops, callable reconnect auth, and credential fingerprint in the locked status snapshot. Clear both roster snapshots on handoff; retain signal queues and receipt clocks.
- `scripts/memecoin_runner_loop.py`: persist fingerprint from the same roster snapshot; obtain token expiries dynamically. No refresh I/O on the cycle or entry path.
- `sniper/services/memecoin_alerts.py`: suppress routine long-lead warnings for automatic accounts; retain warnings when renewal fails to extend an access token near expiry, manual-account warnings and DOWN alerts.
- Private state only: account 2 owns the copied refresh chain; remove its refresh cookie from Chrome only after durable local storage is verified. Account 1 stays manual. No secrets in code, fixtures, logs or status.

## Tasks

- [x] Add failing auth tests: due time, cookie exchange/rotation, same user/session and later expiry, no redirects, timeout/429/rejection/malformed response, atomic `0600` persistence, unrelated-setting preservation, cross-process ownership and persistence retry without reusing a spent refresh token.
- [x] Implement the bounded auth owner; run its focused tests RED then GREEN.
- [x] Add failing feed/runner/alert regressions; implement socket-only handoff, immutable roster identity snapshots, dynamic expiry and automatic-account alerts.
- [x] Run the affected J7 suites with two workers and an independent security/concurrency review. Fix findings with focused regression proof; final integration/schema gate below.
- [ ] Integrate against current main, run the guarded pinned-release validation/cutover with backups deferred, and verify fresh runtime auth status, cookie renewal, native readiness and unchanged authority.

## Review focus

- An old roster must never acquire the fingerprint of a new credential.
- A blocked auth request or file lock must not block observation/drain/native execution.
- New refresh secrets must survive before reconnect; a failed disk write must not retry a consumed refresh credential.
- Wrong-account/session or stale responses never replace working credentials.
- Browser and bot must not independently consume the same rotating refresh chain.

## Evidence and limits

- Auth: 45 focused tests passed after failing coverage for rotating-cookie loss on a truncated response body. HTTP 200 and the strictly validated cookie pair are authoritative; the redundant JSON body is not read.
- Feed: 99 focused tests passed, including failing-then-passing delayed-callback, single-account heartbeat, and disconnected reconnect-owner regressions. Each credential owns a separate Socket.IO client; `shutdown()` ends its reconnect owner before activation.
- Combined affected gate passed 218 tests before the final reconnect regression; rerun after integration.
- Real bot-owned refresh on 2026-10-18 returned HTTP 200, retained user/session identity, extended expiry, and saved both credentials together with mode `0600`. The new access token passed the authenticated Fomo-list API (HTTP 200). Account 1 was unchanged. Chrome's refresh cookie was removed after durable handoff with operator approval.
- Bounded local observe/drain benchmark, 1,000 samples per case: normal p50/p90 21.35/22.04 microseconds; auth request blocked p50/p90 22.16/23.13 microseconds. This measures local ingress, not exchange or order latency.
- Revocation, a server-side session limit, or losing the process during an unsuccessful disk write may require login. No funded trade was submitted to test renewal.
