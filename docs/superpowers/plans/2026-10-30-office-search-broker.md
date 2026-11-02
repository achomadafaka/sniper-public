# Office shared keyless search broker

Owner Epsilon; reviewer Zeta; Alpha merges and activates. No funded or environment changes.

1. RED offline SQLite/process witnesses: exact query/empty/age, five-process singleflight, shared admission, UTC rollover, failure typing, retention.
2. Host-owned SQLite beside Office state. Cache success 5 minutes, typed failures 5 seconds; lease 120 seconds with token fencing, followers wait at most 2 seconds. Retain at most 1,000 answers and 7 days, prune on access and hourly. No network under a transaction.
3. Shared policy `config/office/search.json`, daily_budget 30. Proxy reserves each provider HTTP attempt, including fallback, shell and browse; cache hits cost zero. This conservative unit permits at most 30 new queries, potentially fewer. Failed and ambiguous sends are consumed. No paid overflow or keys.
4. Five-second pacing per provider family across processes. Proxy denies rather than sleeps; funded denylist precedes admission. Missing/bad config or store fails closed. Provider domain list covers all registered engines; arbitrary search relays are outside scope.
5. Wire tool, proxy construction, config and hourly retention. Preserve typed unknown/challenge/rate-limit/timeout vs explicit empty. Return fetched_at/age_s/cached.
6. Focused offline checks, schema checks, exact-SHA Zeta review. No runtime acceptance claimed. Rollback retains the guard or stops Office search.

Cache key preserves exact query bytes, including case and whitespace; cached data is hostile. State files remain host-only. Admission is per UTC day; backward clock fails closed. No changes reach funded filling, sizing, signing, providers or services.
