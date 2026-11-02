# Memecoin funded review corrections

**Date:** 2026-07-17
**Status:** approved for implementation
**Scope:** correctness fixes on the funded memecoin path; no paper-strategy mutation

## Evidence disposition

| # | Verdict | Evidence | Action |
|---|---|---|---|
| 1 | confirmed | entry/safety use V3 -> V2 -> V4; exit stops at V2 | make exit use the same bounded venue ladder |
| 2 | confirmed | non-curated events are inserted in discovery, but the later alert loop iterates `mine`, which excludes them | dispatch eligible discovery consensus alerts from the discovery path |
| 3 | confirmed | `first_seen_buy_price` compares raw `network_id`; other coin lookups use normalized identity | use `COIN_NETWORK_SQL` and `normalized_coin_network()` |
| 4 | confirmed | a mined min-out retry replays the identical WETH withdraw pre-transaction | remember confirmed unwraps inside the one executor call and do not repeat them |
| 5 | confirmed | `real_trail` accepts any positive executable preview after a display-price nomination | derive executable unit price from exact proceeds/open quantity and re-check the peak floor |
| 6 | confirmed | merely having an mcap override bypasses executable percent confirmation even when the percent rail fired | pass current mcap into confirmation and require the rail that actually crossed |
| 7 | confirmed in narrower form | `eth_send_raw` collapses explicit JSON-RPC rejection and transport ambiguity to `None` | release/retry only on positively classified rejection; preserve unknown/no-resubmit on ambiguous transport |
| 8 | confirmed | SQLite `COUNT` turns unparseable timestamp inputs/evidence into zero | return `None` when the current timestamp or relevant prior timestamp evidence is unparseable |
| 9a | confirmed | 1-2M `mid` positions use source `micro` and consume the nano/micro cap | scope the cap to `nano`/`micro`, excluding `mid` |
| 9b | intentional | the consensus gate spans 1-30M, matching the existing consensus-lane design and settings | no change |
| 10 | latent, confirmed | a trail-only operator row can inherit later nonzero default TP/de-risk rails | make a bare operator trail explicitly disable TP and de-risk; retain the configured catastrophe stop |
| 11 | confirmed by `eth_abi` | canonical V4 exact-in-single encoding starts with a tuple offset word; current payload does not decode | prepend `0x20` |
| 12 | confirmed by `eth_abi` | PermitSingle plus dynamic signature has a seven-word head; signature offset is `0xE0` | encode `0xE0` |
| 13 | confirmed | V4 transaction and permit deadlines call skewed host `time.time()` | use `real_utc_now()` and one injectable corrected epoch source |
| 14 | confirmed | uint128/uint160 values are written without range validation | fail closed before encoding out-of-range values |
| 15 | partly confirmed | final quote age already uses the final quote timestamp, but retry stage columns retain attempt 1 | retain append-only per-submit events and make top-level submit stages describe the final submit |

Read-only live evidence on 2026-07-17: V4 is enabled; four real positions are open (three on 4663);
there are no currently reserved entries and no open bare-trail operator override. The defects are
therefore live-capability risks, not a request to rewrite current positions or production data.

## Funded contracts

### Venue symmetry and V4 calldata

- An entry-routable token must be exit-routable through the same ordered V3 -> V2 -> V4 ladder.
- The ladder remains fail-closed. V4 is attempted only when the earlier configured rungs return no
  quote and V4 is enabled.
- V4 action inputs must byte-match canonical ABI encoding. All narrow integer fields are range
  checked before encoding.
- Transaction and Permit2 deadlines use the repository's corrected real-UTC epoch.
- A confirmed WETH unwrap is reused only inside the same bounded min-out retry. It is never cached
  across entries and adds no balance/network read.

### Broadcast truth

- Success or `already known` proceeds to receipt confirmation.
- A positively classified JSON-RPC rejection (`nonce too low`, underpriced, insufficient funds,
  invalid transaction, fee below base fee) means this signed hash was not accepted. An entry
  reservation is released with `broadcast_rejected`; an exit may continue its existing ladder.
- Transport failure or an unclassified RPC response remains ambiguous. Confirm the locally known
  hash and retain the existing unknown/no-resubmit behavior if no receipt appears.
- Pre-transactions follow the same rule: ambiguous send response is confirmed by its known hash;
  explicit rejection aborts without pretending submission.

### Exit confirmation

- Display prices only nominate price exits. Exact-balance executable proceeds authorize them.
- A trail re-check uses `expected_usd / open_qty` against the stored peak/trail floor.
- A TP/SL mcap target auto-confirms only when the supplied current mcap crosses that exact target.
  Otherwise the executable percent threshold must pass.
  **Superseded for custom cap targets and legacy full-sell cap bands, 2026-10-19:** a supplied market/display cap no longer proves a crossing. Use the exact actual-sell quote and existing position target supply, and require a sufficient signed minimum output for TP submissions. SL remains a trigger with possible fill slippage; other active exits retain authority. See the [dated exit-control amendment](2026-07-14-memecoin-per-position-exit-control-design.md#2026-10-19-amendment-exact-saves-and-executable-cap-targets).
- Existing stop debounce, kill switch, timecap, mirror guard, token safety, and slippage ladders stay
  unchanged.

### Coverage and identity

- Discovery consensus alerts use the existing predicate, dedup row, per-poll cap, and nonblocking
  sender. They do not enter funded execution.
- First-seen anchoring and consensus counting use `(normalized network_id, mint)`.
- Timestamp uncertainty returns `None`; the funded consensus gate already fails open only on `None`.
- The nano/micro cap protects only assumed/sub-$250K ladder positions. The separately measured 1-2M
  mid-band scalp does not consume that cap.

### Retry telemetry

- One entry reservation keeps one immutable terminal attempt row.
- Every EVM submission attempt appends quote/sign/submit/confirmation events in the same reconciled
  monotonic clock domain.
- The fixed stage columns describe the final executable submission; the JSON retains earlier failed
  submissions. Missing stages remain null.

## Explicit non-goals

- No wider slippage, weaker token safety, new retry class, extra submit, or additional hot-path
  network request.
- No change to funded sizing, wallet arming, market-cap bands, consensus range, paper exits, or
  adaptive-policy verdicts.
- No production DB rewrite and no guessed chain identity.

## Acceptance

1. Canonical ABI comparisons decode and match byte-for-byte.
2. A V4-only token is previewable and sellable through the funded exit quote seam.
3. A min-out retry executes one unwrap, two swaps at most, and records both submissions.
4. Explicit broadcast rejection releases the entry; ambiguous transport remains reserved.
5. Healthy executable proceeds suppress a phantom display-price trail/TP/SL exit.
6. Normalized first-seen and malformed-consensus tests pass.
7. Mid-band positions do not consume the nano/micro cap.
8. Full memecoin, concurrency, schema, compile, and runtime-deploy suites pass before deployment.

<!-- schema:external=memecoin_events -->
<!-- schema:external=memecoin_positions -->
<!-- schema:external=memecoin_pending_entries -->
<!-- schema:external=memecoin_entry_attempts -->
# 2026-09-11 final execution quote amendment

Every Solana and EVM `b_stop` execution checks its selected quote before each signature and before any EVM approval.
The check reuses the effective stop predicate, frozen contract, operator override, and actual quoted quantity.
A refused stop resets its debounce state and releases only an unsigned claim. Signed or uncertain submissions retain their claim.
The existing route series stores versioned quote evidence separately from its scalar price. Historical rows retain NULL evidence.
This amendment changes quote correctness and evidence. Stop thresholds and debounce counts remain unchanged.
