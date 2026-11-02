# Memecoin CLAWD Entry Reliability Design

**Date:** 2026-08-12
**Status:** Operator approved

## Goal

Reduce armed-wallet receipt delay and remove one false Solana quote refusal.

Arm `runitbackghost` for funded Solana entry after the pinned deployment passes all gates.

The market-cap limit does not change.

## Evidence

The Fomo global feed did not store two `runitbackghost` CLAWD buys.

The follow sync failed on the paginated follow endpoint during the CLAWD event window.

The follow lane received two CLAWD buys from `0xsneakky` after 104 seconds and 83 seconds.

Jupiter returned `priceImpactPct=100%` for two CLAWD routes. No other retained attempt has this exact value.

The later CLAWD signal failed the `3x` first-seen chase limit.

## Design

### Follow graph fallback

The follow sync first reads the current paginated follow graph.

If that read fails, it reads `/v2/users/current/followingIds`.

A complete ID list supplies the repair set. The sync does not pause wallets from this fallback.

The service does not call an unfollow endpoint.

### Fast follow receipt

The fast lane uses four bounded fetch workers for the armed roster.

The main follow thread processes completed responses and owns all SQLite work.

The workers make read requests only. They do not use a database connection.

The service records `lag_s` from the response receipt time and the source event time.

The service keeps the current deduplication, age, authority, and reservation rules.

### Fresh follow chase rule

A `follow_activity` buy is fresh when its measured receipt lag is from 0 through 60 seconds.

For a fresh buy, the runner compares the current price with the source event price.

The fresh event-price limit is 40 percent.

The runner does not apply the first-seen ratio to a fresh follow buy.

All other buys keep the configured first-seen ratio.

Missing or invalid receipt lag keeps the first-seen ratio.

### Solana impact sentinel

The normal Jupiter impact limit remains unchanged.

An impact result below 99.9 percent follows the existing limit.

An impact result at or above 99.9 percent is an untrusted sentinel.

The executor then checks the existing exact planned-quantity sell-back quote.

The executor proceeds only when the sell-back cost is at most twice the entry impact limit.

A missing or costly sell-back quote refuses entry before signing.

No later safety, identity, reservation, signing, submission, or confirmation rule changes.

## Funded authority

After deployment, the operator arm flow adds `runitbackghost` on Solana with `force=1`.

The arm flow must follow the wallet on Fomo before it writes funded authority.

The deployment must have zero unresolved reservations before activation.

## Tests

Focused tests cover the fallback, bounded fetches, receipt lag, chase selection, and impact sentinel.

The test run must not include the full memecoin suite.

## Deployment

Merge the reviewed commit to local `main`.

Use `scripts/deploy_memecoin_runtime.sh` with the merged commit.

Verify the release link, one runner process, heartbeat, authority, logs, and zero unresolved reservations.

Do not claim a latency improvement until a new organic follow event supplies receipt evidence.
