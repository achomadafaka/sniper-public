# Armed wallets must be followed on Fomo

**Date:** 2026-07-16
**Status:** Operator-approved design
**Scope:** Memecoin wallet arming and Fomo follow reconciliation; no funded entry-path changes

## Problem

Sniper already polls every effective armed wallet in the dedicated follow-activity fast lane, but
arming a wallet does not create the actual Fomo follow relationship. Read-only production evidence
on 2026-07-16 found:

- 31 effective armed wallets (`real_approved=1`, `paused=0`, `excluded=0`);
- 29 total Fomo follows;
- only 11 armed wallets present in Fomo's `followingIds`;
- 20 armed wallets missing the Fomo follow relationship.

This loses Fomo's personalized feed and notification coverage even though Sniper's own recovery poll
still runs. The required invariant is one-way:

```text
effective armed wallet -> followed on Fomo
```

Disarming, pausing, excluding, or auto-demoting a wallet must never unfollow it. Following is a
coverage superset, not a funded-trading verdict.

## Verified external contract

The current Fomo web client uses:

- `GET /v2/users/current` for the operator's Fomo user ID;
- `GET /v2/users/current/followingIds` for the complete followed-ID set;
- `POST /follows` with JSON `{"user_id": <operator_id>, "following_id": <target_id>}`;
- `DELETE /follows` for unfollowing.

This project implements the first three calls. It must not call `DELETE /follows`.

## Decision

Use immediate enforcement plus periodic repair.

### Immediate arm path

For an operator request that would newly approve a wallet:

1. Evaluate the existing paper-copy promotion gate without mutating `real_approved`.
2. If the gate refuses, preserve the existing refusal/audit behavior and make no Fomo request.
3. If the gate passes or the operator explicitly forces it, check `followingIds`.
4. If already followed, continue without a follow mutation.
5. If missing, call `POST /follows` and require a successful Fomo response.
6. Only then write `real_approved=1` through the existing audited persistence helper.

If Fomo authentication, lookup, or follow fails, the new approval remains off and the API returns a
named `fomo_follow_failed` error. It also appends an approval-audit row with action
`approve_refused_fomo_follow`, the current qualification evidence, actor, force flag, and bounded
failure reason. Existing armed wallets are never automatically revoked because of a later Fomo
outage.

Revocation uses the current local path only and never calls Fomo.

### Periodic repair

Extend the existing roughly five-minute Fomo follow-sync pass. After a successful current-user and
following-graph read:

1. Snapshot the effective armed IDs before any inbound follow reconciliation can pause rows.
2. Follow every armed ID missing from Fomo, sequentially and with bounded work.
3. Add successful targets to the in-memory followed set.
4. Run the existing Fomo-to-Sniper add/unpause/pause reconciliation.
5. Never pause a still-effective armed wallet merely because it was absent from the pre-repair Fomo
   graph. A failed follow remains active locally, is reported, and is retried on the next pass.

This ordering prevents the existing inbound sync from pausing an armed `source='follow'` wallet
before the reverse repair can restore its Fomo follow.

The pass returns deterministic counts and failed IDs:

```python
{
    "armed": 31,
    "armed_already_followed": 11,
    "armed_followed": 20,
    "armed_follow_failed": 0,
    "armed_follow_failures": [],
    # existing added/unpaused/paused/followed/complete fields remain
}
```

Failure of one target must not stop later targets. No retry occurs inside the same pass; the next
scheduled pass is the retry boundary.

The periodic path reuses the complete followed-ID set already returned by the existing paginated
follow-graph fetch. It does not add another following-list request. An incomplete graph skips reverse
follow mutations as well as the existing pause pass. The immediate arm path uses
`/v2/users/current/followingIds` because it has no complete sync snapshot to reuse.

## Components

### Authenticated Fomo mutation seam

Add a generic authenticated JSON request helper beside `fomo_get()` that:

- preserves the existing browser headers and bearer token handling;
- JSON-encodes the request body;
- refreshes once on HTTP 401/403 using the existing serialized rotated-token store;
- returns the same `(HTTP status, JSON body, Date header)` shape as `fomo_get()`;
- makes no request from a funded entry, reservation, safety, quote, or exit path.

Small follow helpers expose current user ID, followed IDs, and one follow mutation. Tests inject all
network seams; no test calls Fomo.

### Promotion-gate preview

Extract the existing approval qualification calculation into a pure read helper. Both the existing
audited `set_wallet_real_approved()` path and the new arm service consume that same result so the UI
preflight cannot drift from the persisted gate. Refusals, forced approvals, copy-PnL thresholds, and
approval audit semantics remain unchanged.

### Armed-follow service

The service owns external follow orchestration. Persistence stays network-free. It supports one
target for the immediate arm path and the complete effective armed set for periodic repair.

## Error handling

- Missing operator Fomo user ID: fail the new arm with `fomo_follow_failed`; periodic pass reports
  `no_user` and changes no wallet state.
- Incomplete/failed following-ID read: do not guess; fail the new arm and skip periodic mutations.
- Follow response not successful: leave a new wallet unarmed; keep an existing wallet armed, report
  its ID, and retry next pass.
- A successful follow requires both HTTP success and Fomo JSON `statusCode=200`; malformed success
  payloads fail closed.
- Already followed: idempotent success without `POST /follows`.
- One target failure: continue the bounded periodic batch.
- Disarm/pause/exclude/demote: no Fomo mutation.
- Fomo follow notification preferences can still be disabled in Fomo itself. This project guarantees
  the follow relationship, not a separate per-user notification toggle.

## Safety and scope invariants

- No new request occurs in the entry hot path.
- No change to explicit chain identity, reservations, token safety, sizing, capital accounting,
  kill switch, duplicate-submit protection, exits, paper verdicts, or adaptive policy.
- Fomo failure cannot revoke an existing armed wallet or close a position.
- The service never unfollows any wallet.
- The existing fast-lane roster remains the effective armed set and is re-read every sweep.
- External follow repair is bounded to the current effective armed roster; no broad discovery or
  cohort following is introduced.

## Tests

TDD must prove:

1. Successful new arm follows first, then persists approval.
2. Existing Fomo follow avoids a duplicate POST.
3. Follow failure leaves a new wallet unarmed with a named error.
4. Promotion-gate refusal performs no Fomo request and preserves its existing audit.
5. Disarm leaves the Fomo follow untouched and performs no DELETE.
6. Periodic repair follows every missing effective armed wallet and reports exact counts.
7. One periodic follow failure does not stop later wallets and never revokes the failed wallet.
8. An armed `source='follow'` wallet missing upstream is repaired, not paused.
9. Paused, excluded, and unapproved wallets are not reverse-followed.
10. Authenticated POST refreshes once on 401/403 and preserves the rotated-token contract.
11. Existing follow-sync, fast-lane, approval, concurrent reservation, and runtime tests remain green.

## Deployment and live verification

Before deploy, verify zero pending funded reservations and a clean exact-SHA release. Deployment
must use `scripts/deploy_memecoin_runtime.sh`.

After the runner is healthy:

1. Run one explicit armed-follow reconciliation using the pinned release.
2. Re-read `/v2/users/current/followingIds`.
3. Compare it to the current effective armed set from SQLite.
4. Require `armed_missing_follow=0`; report any Fomo rejection by exact wallet ID.
5. Confirm one pinned runner, healthy heartbeat, zero reservations, unchanged open positions, and no
   new log errors.

The current 20-wallet drift is live evidence for this change, not a fixture benchmark. No historical
trade, position, fill, approval, or accounting rows are rewritten.
