# LI.FI funded quote-capacity reserve for chain 4663

Date: 2026-08-30
Status: approved specification; implementation not approved
Scope: LI.FI quote use on the funded Robinhood Chain path

Related specification: `2026-08-30-memecoin-lifi-funded-route-safety-design.md`

## 1. Decision

The public LI.FI quota is not a funded automation resource.

Public access can support read-only probes and one explicit manual $5 canary round trip. It cannot support automated funded entries or exits.

Automated funded LI.FI use requires an authenticated server-side key. The observed authenticated quota must also cover the complete exit reserve.

LI.FI entry requests cannot use the exit reserve. An insufficient or unknown reserve blocks only LI.FI entries.

Existing direct-router entry and exit paths remain independent. This design does not reduce the exit quote cadence or any safety gate.

## 2. Capacity fact

LI.FI documents these current `/quote` limits:

- unauthenticated: 75 requests per two hours;
- authenticated default: 100 requests per minute;
- quote endpoints use a two-hour rolling window;
- an authenticated default therefore permits 12,000 requests per two hours.

Source: [LI.FI rate limits](https://docs.li.fi/api-reference/rate-limits), read on 2026-08-30.

The current funded contract permits two Robinhood positions. The funded EVM preview path runs once per one-second fastlane tick.

```text
rail_reserve = 2 positions * ceil(7200 seconds / 1 second) = 14,400
action_reserve = 2 positions * 2 exit actions * 3 slippage rungs = 12
exit_reserve = 14,400 + 12 = 14,412
```

The 60-second exact-quote heartbeat does not bound LI.FI demand. It applies outside the one-second funded preview path.

The authenticated default of 12,000 cannot cover two LI.FI-funded positions. It fails before the four requests required for a new entry.

The authenticated default can cover one LI.FI-funded position. Two positions require a higher observed LI.FI quota.

## 3. Modes

### 3.1 Public mode

Public mode permits only these actions:

1. A read-only route probe can request a quote.
2. An operator can request one manual $5 canary round trip.

```text
canary_request_cost =
    4 entry requests
    + 3 exit slippage rungs
    = 7 requests
```

Public mode prohibits automated funded entry and exit use. A scheduler, signal handler, or unattended runner cannot select it.

The canary reserve check must use this rule:

```text
effective_remaining - in_flight_requests >= canary_request_cost
```

The check must leave seven requests after all in-flight requests. A failed check blocks the canary before any transaction action.

### 3.2 Authenticated funded mode

Authenticated funded mode requires all these conditions:

1. The key stays in the server environment.
2. The LI.FI key-test endpoint accepts the key.
3. A quote response includes valid limit, remaining, and reset headers.
4. The observed capacity covers the exit reserve.
5. The entry request leaves the full exit reserve available.

The presence of a key is not sufficient. The observed response defines the effective quota.

The funded runner must have a dedicated key. A web route, probe worker, research worker, or manual script cannot share its quota.

## 4. Exit reserve

The implementation must derive the reserve from the active funded contract.

```text
rail_reserve =
    max_lifi_funded_positions
    * ceil(provider_window_seconds / fastlane_tick_seconds)

action_reserve =
    max_lifi_funded_positions
    * max_exit_actions_per_position
    * slippage_rungs_per_action

exit_reserve = rail_reserve + action_reserve

entry_request_cost = 4
```

The current binding values are:

- `provider_window_seconds = 7200`;
- `fastlane_tick_seconds = 1`;
- `max_exit_actions_per_position = 2`;
- `slippage_rungs_per_action = 3`;
- `entry_request_cost = 4`.

The two exit actions are one partial exit and one terminal exit. The three slippage rungs are the active `3,10,25` ladder.

The four entry requests are the buy quote, initial sell check, post-approval buy refresh, and final sell check.

The final sell check must use the refreshed buy quote's exact `toAmountMin` as its input.

The LI.FI path must budget all four requests. An early failure can use fewer requests, but admission still reserves four.

For two LI.FI positions, `exit_reserve` is 14,412. Entry admission therefore requires at least 14,416 available requests.

The 14,416 example assumes zero in-flight requests. Each in-flight request increases the required remaining count by one.

For one LI.FI position, `exit_reserve` is 7,206. Entry admission therefore requires at least 7,210 available requests.

The implementation must not use an arbitrary reserve percentage. It must not use the current open-position count as the maximum.

The provider response headers are authoritative:

- `ratelimit-limit` gives the effective window capacity;
- `ratelimit-remaining` gives the available capacity;
- `ratelimit-reset` gives the provider reset time.

A configured value or documented value cannot override a lower observed value.

The LI.FI-funded position cap is one under the authenticated default quota. A higher cap requires enough observed capacity for the complete formula.

Direct-route Robinhood positions do not use this LI.FI position cap. They remain subject to the existing chain and book caps.

Quota headers are current for one exit quote period. An authentication failure or 429 makes them stale immediately.

After a cold start, LI.FI entries remain blocked until the runner observes current headers. If a LI.FI position is open, the first quote must be an exit quote.

With no LI.FI position open, one bounded authentication probe can establish the initial headers. This probe cannot sign or broadcast a transaction.

## 5. Admission rule

A LI.FI entry can start only when this condition is true:

```text
authenticated
and headers_are_current
and effective_limit >= exit_reserve + entry_request_cost
and effective_remaining - in_flight_requests - entry_request_cost >= exit_reserve
```

One shared atomic quota guard must cover all LI.FI quote-related endpoints. An exit request has priority over every entry request.

If the check fails, the system must refuse the LI.FI entry before approval, signing, or broadcast. The refusal must name `lifi_exit_quota_unavailable`.

The system can continue with an existing direct route. It must not convert a LI.FI quota failure into a general route failure.

## 6. Runtime failure rules

An exit quote can use reserved capacity. An entry quote cannot use reserved capacity.

An authentication failure or HTTP 429 must disable new LI.FI entries immediately. The system must report one bounded operator alert.

The system must not retry an entry quote with exit-reserve capacity. Existing exit retry limits remain unchanged.

The system must not use public access as an automated fallback after an authenticated failure.

If the key fails while a LI.FI-dependent position is open, the system must keep direct exit routes active. It must also report the LI.FI exit fault.

The system must never reduce the one-second fastlane or 60-second heartbeat to fit a provider limit.

## 7. Secret handling

The backend sends the key in the `x-lifi-api-key` header. The web client never receives the key.

The system must not write the key to logs, API payloads, database rows, traces, or status output.

Source: [LI.FI authentication](https://docs.li.fi/api-reference/authentication), read on 2026-08-30.

## 8. Operator status

The runner must persist one atomic state snapshot with a corrected timestamp and runner heartbeat.

The funded status surface must show these non-secret fields:

- LI.FI mode: `public_probe`, `authenticated_funded`, or `disabled`;
- authentication state;
- effective limit;
- remaining requests;
- reset seconds;
- calculated exit reserve;
- calculated LI.FI-funded position cap;
- LI.FI entry admission state;
- all active disable reasons.

The status surface must identify stale state and stale or missing headers. It must not show a healthy state from configuration alone.

## 9. Required tests

1. The current two-position contract calculates a 14,400-request rail reserve.
2. Two exit actions across three rungs add 12 requests.
3. The complete two-position exit reserve is 14,412 requests.
4. The fixed entry request cost is four requests.
5. One-position admission refuses 7,209 and permits 7,210 available requests.
6. Two-position admission refuses 14,415 and permits 14,416 available requests.
7. Public mode refuses an automated funded LI.FI entry.
8. The manual $5 canary cost is seven requests.
9. Public mode refuses a canary at six available requests.
10. Public mode permits a canary at seven available requests.
11. Missing authentication headers refuse a funded LI.FI entry.
12. A lower observed limit overrides a higher configured or documented limit.
13. An entry that would consume the exit reserve is refused.
14. A concurrent exit wins the shared quota guard before an entry.
15. An exit can use reserved capacity.
16. A 429 disables new LI.FI entries without disabling direct routes.
17. The API, logs, and status payload do not expose the key.
18. Each quota boundary test repeats with in-flight requests.
19. The status surface marks a stale snapshot and shows all active disable reasons.

## 10. Activation gates

This specification does not approve implementation, deployment, or funded activation.

Implementation requires a red-green focused test cycle for each admission and priority rule. The existing funded safety tests must remain green.

Before funded activation, a server-side key must return valid authenticated quota headers. The observed remaining capacity must pass the reserve rule.

The authenticated default quota permits at most one LI.FI-funded position. Two positions require an effective limit of at least 14,416 requests.

The remaining count must also pass the admission formula. The in-flight request count must stay inside that check.

An operator must complete the manual $5 canary round trip. The standard entry, fill, position, exit, and accounting records must agree.

The canary must bind to the exact release SHA, manifest hash, custody wallet, and chain 4663.

The canary does not enable automation. An operator must separately activate its exact release SHA and pin-set hash.

Automated LI.FI entry activation requires separate approval. The pinned runtime deployment contract remains unchanged.
