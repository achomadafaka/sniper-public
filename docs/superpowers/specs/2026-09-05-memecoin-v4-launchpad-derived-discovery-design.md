# Memecoin V4 launchpad-derived pool discovery (chain 4663) — design

Date: 2026-09-05. Owner: Alpha (sniper-fc). Status: approved by the operator's 2026-09-05 mandate
("fill all signals that are good"); builder sniper-f0.

## 2026-10-11 amendment: bounded preparation

Current derivation confirms both counter-to-token and token-to-counter probes before storing a key; the one-probe description below is historical. Robinhood background preparation now warms the existing complete discovery union before an entry, under its existing research budget. It retains discovery TTLs, every fresh actual-clip buy/reverse and funded safety check, and uses invocation-local cancellation plus a final publication fence. Cold entry discovery remains unchanged. See `2026-10-11-rh-entry-speed-design.md`; preparation success is not funded signal-to-fill proof.

## Problem

At the first feed signal a launchpad coin's pool is 0-30 seconds old. Measured 2026-09-05 on
chain 4663: CHILL Initialize -> first signal 27 s, NOTFLIX 225 s, YIELDPAD -12 s, n00b -6 s (the
wallet bought before the Initialize block was visible). The chain's log index surfaces Initialize
38-61 s after the block, and only one endpoint serves range `eth_getLogs` at all (QuickNode's plan
caps it at 5 blocks and returns 413; publicnode returns 403). The public indexers list the coin's
fee-trap pools first and its real pool minutes later. So log-based and indexer-based discovery
both miss the first signal by construction, and every requote inside the 90-second window lands
before the index catches up. This week: `discovery_rpc_failed` 7, `poolkey_read_failed` 5,
`rate_limit` 6, v4 `rpc_transport` 26, and 10 of 28 `no_route` coins had their real pool stored
by us only after the refusal (KPOP +2707%, CHILL +176%, FRIES +57% within the hour).

## Outcome

For a coin created by a registered launchpad, the runner derives the real pool key from the
launchpad contract with one `eth_call`, computes the pool id as `keccak(abi.encode(PoolKey))`,
confirms it with one V4Quoter `eth_call`, and hands it to the quoter as a candidate. No logs, no
indexer, any endpoint in the pool. A pool that is not yet initialised at the signal returns
`PoolNotInitialized` (`0x486aa307`), which the entry path already treats as transient and
requotes.

## Evidence

- Launchpad `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e` (sends the pool-creation transaction,
  selector `0x2f53ef2f`): `eth_call` with selector `0x3cf28b5a` and the token address as the
  single 32-byte argument returns 15 words: word 0 = the coin, word 4 = the counter currency
  (`0x0…0` = native ETH), word 6 = fee (0), word 7 = tick spacing (200). Selector `0x6651812c`
  with no argument returns the hook `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044`.
- Reproduced the stored key for YIELDPAD (native), PH (LLY), NOTFLIX, n00b. FRIES and KPOP return
  word 0 != coin: they belong to another launchpad and are out of scope for v1.
- All 3,579 stored pools of this hook share fee 0 / tick spacing 200; `pool_id ==
  keccak(abi.encode((currency0, currency1, fee, tickSpacing, hooks)))` verified on 3 of 3.
- This launchpad's hook family is 28% of all fee-0 pools we have keyed and 26 of the 35
  foreign-paired route refusals this week.

## Design

### Registry (settings)

`MEMECOIN_EVM_V4_LAUNCHPADS_<chain>`: comma list of entries
`<launchpad>:<info_selector>:<coin_word>:<counter_word>:<fee_word>:<ts_word>:<hook_selector>`.
Default for 4663: `0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e:3cf28b5a:0:4:6:7:6651812c`.
An entry with a malformed field is skipped with a warning at import. Empty list = feature off.

### Derivation (new module `sniper/execution/evm_v4_launchpad.py`)

`derived_pool_keys(chain, mint, rpc_url, *, http=None) -> list[(pool_id, key)]`:

1. For each registry entry: `eth_call(launchpad, info_selector + pad32(mint))`. Skip when the
   result is shorter than the largest word index, or when `word[coin_word] != mint` (the coin is
   not this launchpad's). Read `counter = word[counter_word]`, `fee = word[fee_word]`,
   `ts = word[ts_word]`.
2. Hook: `eth_call(launchpad, hook_selector)`; cache per launchpad for the process lifetime.
3. `currency0, currency1 = sorted((counter, mint))` by address value; `key = {currency0,
   currency1, fee, tick_spacing, hooks}`; `pool_id = keccak(abi.encode(...))`.
4. Confirm with `_quote_single(chain, key, zero_for_one, probe_amount, rpc_url)` where the probe
   sells the counter for the coin at `MEMECOIN_EVM_V4_LAUNCHPAD_PROBE_ATOMIC` (default 10^15 for
   native, 10^18 otherwise). A quote proves the pool; `PoolNotInitialized` marks the key
   `pending` and still returns it (the caller's transient-miss requote covers it).
5. Store confirmed keys through the existing `_store_pool_key` so every later read is free.

All calls go through the existing `_rpc` read layer (hedged, quarantine-aware). Never a log read.

### Integration (evm_v4_quote.py)

In `_pools_for_mint`, before the indexer union: `derived = derived_pool_keys(...)`; add each
`pool_id` to `merged` with reserve 0.0 and prime `_key_cache` with its key. A derived, confirmed
key counts as a known sane ETH/WETH-or-foreign pool for the trap-only scan rule. Derived pools rank
first (`_fee_rank` 0 with a known sane fee). The rest of discovery is unchanged.

### Reasons

`_mark_blind` is untouched. Derivation failures are logged once per (launchpad, mint) at DEBUG and
noted in the rung reason channel as `launchpad_not_ours`, `launchpad_read_failed`,
`launchpad_pool_pending`.

## Testing

Unit (`tests/test_memecoin_v4_launchpad_discovery.py`):

1. The struct decoder reads coin, counter, fee, tick spacing by configured word index.
2. A struct whose coin word is not the mint is skipped.
3. The pool id equals `keccak(abi.encode(PoolKey))` for a fixed key (golden value from the ledger:
   CHILL's stored pool).
4. A confirmed key is stored and primes the key cache; `_pools_for_mint` lists it first.
5. `PoolNotInitialized` returns the key as pending and does not store it.
6. A malformed registry entry is skipped; an empty registry is inert.
7. No test makes a network call.

Canary (read-only, publicnode): for 20 hook-`0xe5e702` coins with a stored key, derive and
compare: expected 20 of 20 matches on the pool id.

## Rollout

Ship with the registry default populated; the feature is one `eth_call` per fresh coin per
launchpad and is safe to enable on deploy. Report the first fill whose route came from a derived
key.
