# Spec — negRisk CONVERSION accounting for the winning-wallet analyzer

**Date:** 2026-06-15
**Status:** IMPLEMENTED + validated 2026-06-15. `winning_wallet_thesis/conversions.py` (parser + cached on-chain fetcher), episode `convert_in/out_shares+cost` in types.py, time-ordered fold in position_builder.py, wired into enrich_ledger. Reconciliation vs Polymarket closed-realized: 0x5bffcf +39.8%→+1.7%; directional wallets 0x20ad75 −2.8% / 0x95074f +2.0% unchanged. Open §5 decisions resolved during build (cost-basis transfer via running per-share cost; collateral captured as reduced YES cost). Known limitation: per-population directional/structural split distorted on conversion-heavy MM wallets (total correct + conserved); MM out of scope for selection edge.
**Parent:** winning-wallet thesis extractor (`sniper/analysis/winning_wallet_thesis/`). Follows the activity-fill-dedup fix (`00d77df`, branch `fix/activity-fill-dedup`), which corrected directional wallets but left MM/negRisk-heavy wallets unreconciled.

## 1. Problem

The ledger normalizer (`ledger.py` `_TYPE_KIND`) has no `CONVERSION` entry, so negRisk `convertPositions` activity is **silently dropped**. Immaterial for directional wallets; material for market-maker / arb wallets.

Trust-check after the dedup fix (analyzer realized vs Polymarket closed-position realized):
- 0x20ad75 (directional): −2.8% ✓
- 0x95074f (directional): +2.0% ✓
- **0x5bffcf (market maker): +39.8% ✗** — TRADE 183k, MERGE $66.6M, **CONVERSION $19.5M / 327 rows**, SPLIT $19M, REDEEM $37M.

Goal: account for negRisk conversions so MM/conversion-heavy wallets reconcile. Non-goal: changing directional-wallet behavior (must stay byte-stable).

## 2. Verified mechanics (authoritative — Polymarket/neg-risk-ctf-adapter `NegRiskAdapter.sol`)

For an event with `n` questions, `convertPositions(_marketId, _indexSet, _amount)`:
- **Set bits** in `_indexSet` (count `m`) = the NO tokens **burned**, `_amount` of each (sent to a burn address).
- **Unset bits** (`n − m`) = outcomes whose **YES tokens are minted** to the user, `_amount` each.
- **Collateral returned** = `(m − 1) × amountOut`, where `amountOut = _amount − fee`, `fee = _amount × feeBips / FEE_DENOMINATOR`.
- Value-conserving under "exactly one outcome resolves YES": `Σ_{set} NO_i = (m−1) + Σ_{unset} YES_j`. (Confirmed by value algebra and the contract source.)

## 3. Data sourcing (feasibility confirmed 2026-06-15)

The Data-API CONVERSION row carries only the **event** conditionId + a single `size`(==`usdcSize`); it lacks `indexSet`, `n`, `amount`, and the sub-market mapping. Full per-leg accounting therefore needs two extra sources, both reachable:

1. **On-chain tx receipt** (per conversion). We already have each conversion's `transactionHash` from the activity feed. `POLYGON_RPC_URL` (Alchemy) is configured. `eth_getTransactionReceipt` → ERC-1155 `TransferBatch`/`TransferSingle` logs give the exact position token ids burned (NO) and minted (YES) and amounts; the ERC-20 USDC `Transfer` gives the collateral. This avoids decoding `indexSet` math — the transfers ARE the per-leg truth. (Verified: receipt fetch returns logs incl. 6× `TransferBatch` + 4× ERC-20 `Transfer` for the Fed-December conversion.)
2. **Gamma event structure** (per event): `gamma-api.polymarket.com/events?slug=<eventSlug>` → `negRisk`, `markets[]` with each sub-market `conditionId` + `groupItemTitle`, ordered by outcome index. Maps event → sub-market episodes and resolves position-token id → (conditionId, outcome).

Cache discipline mirrors `load_activity`: cache receipts + event structure per wallet/event under `runs/wallet_anatomy/`; transient RPC/HTTP failures raise and are NOT cached as empty.

<!-- schema:external=conversion_receipts -->

## 4. Accounting design (per conversion, attributed to sub-market episodes)

Synthesize ledger legs from the receipt transfers, folded by `position_builder` like SPLIT/MERGE re-accounting:

- **YES minted** (`amount` on each unset-bit sub-market): add `amount` shares to that sub-market's YES episode with an allocated cost basis (new `episode.convert_in_shares` / `convert_in_cost`).
- **NO burned** (`amount` on each set-bit sub-market): remove `amount` shares from that sub-market's NO episode (new `episode.convert_out_shares`); these are the holdings spent into the conversion.
- **Collateral** (`(m−1)×amountOut`): cash IN to the wallet total, attributed structurally (not a directional bet).
- **Selection population unchanged:** conversion legs are structural, NOT `is_directional` — they must not enter `resolved_directional()` (selection-edge calibration). They enter `resolved_with_pnl()` (wallet total) only, like split/merge.
- **Reconciliation guard:** extend the inventory reconciliation residual to include conversion cash, expected ≈ 0 once attributed.

## 5. Open accounting decisions (lock during build, with fixture evidence)

1. **Cost-basis allocation for minted YES legs** — analogous to the SPLIT pair-allocation debate in `2026-05-31-analyzer-residual-work-design.md` §3.1. The conversion's economic cost is the NO holdings burned + (−collateral). Allocate across the (n−m) minted YES legs by the wallet's revealed price where available, else by market price at conversion ts, scaled so the allocation sums to the actual net cash. MUST sum to actual cash (no per-leg VWAP synthesis that breaks the sum).
2. **NO-burn vs prior NO cost basis** — burning NO that was previously bought should release that NO episode's cost basis into the conversion, not double-count.
3. **Fee handling** — `_amount − amountOut` fee is a real cost; book it.
4. **Proxy-wallet addressing** — the receipt's token transfers are to/from the Polymarket proxy wallet; confirm the proxy↔EOA mapping so legs attribute to the analyzed wallet.

## 6. Validation targets

- 0x5bffcf full realized (`resolved_with_pnl`) reconciles to Polymarket within an MM-appropriate tolerance (note Polymarket's own profile-P/L $2.36M vs closed-realized $3.52M disagree by $1.16M — MM PnL is intrinsically ambiguous; pick the closed-realized reference and document the residual).
- Directional wallets (0x20ad75, 0x95074f) realized totals **byte-stable** (no conversion legs → no change).
- New fixture: a real conversion receipt (saved JSON) + the Gamma event structure, asserting per-leg attribution and value conservation `(m−1)·amount == collateral`.

## 7. Phasing (TDD)

- **Step 0 — fixture + mechanics verification:** parse the Fed-December conversion receipt end-to-end, save as a fixture, confirm the §2 formula on real transfers. Audit doc with PROCEED/BLOCKED verdict (mirror the Goldsky gate).
- **Step 1 — receipt + event-structure fetchers** (`sniper/feeds/` or `winning_wallet_thesis/`), cached, injectable (no network in CI).
- **Step 2 — conversion → synthetic-leg builder** (pure, tested against the fixture).
- **Step 3 — fold legs into `position_builder` / `PositionEpisode`**; reconciliation guard.
- **Step 4 — reconcile 0x5bffcf; assert directional wallets byte-stable; re-run conversion-heavy wallets in the batch.**

## 8. Honesty interim (until landed)

Conversion-heavy / MM wallets stay **KNOWN-UNRECONCILED**: surface the conversion volume as a coverage limit / data-gap; do not present their realized PnL as trustworthy and do not advance them into WIW. The dedup fix is validated for directional wallets only.

## Sources
- NegRiskAdapter docs + contract: https://github.com/Polymarket/neg-risk-ctf-adapter (docs/NegRiskAdapter.md, src/NegRiskAdapter.sol)
