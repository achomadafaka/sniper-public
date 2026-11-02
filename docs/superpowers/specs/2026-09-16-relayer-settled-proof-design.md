> **SUPERSEDED 2026-09-16 by operator decision.** Watcher eligibility on chain 4663 now requires
> `relay-kyber-linked-v1` (Codex, `docs/audits/2026-09-16-relayer-linked-purchase-proof.md`), which links the
> exact payment to the swap that delivered the coin. A `relayer-settled-v1` row does NOT qualify; a test
> pins that. This document is kept for its evidence: the R3 failure and the minority-recipient finding.

# relayer-settled-v1 — an address proof for delegated execution

Date: 2026-09-16. Owner: Gamma. Status: operator-approved extension, rules corrected by measurement.

## 1. Why a second proof class exists

`wallet-paid-v2` proves a purchase by showing the wallet PAID: a settlement asset leaves the wallet
toward the counterparty that delivered the coin. On chain 4663 no wallet can ever show that. The
execution model is delegated: a relayer signs, a pooled account settles, and the wallet only
receives the coin. `wallet_paid_token_flows` counts only legs where the wallet is a party, so
`spent` is 0 on every real buy and the verdict is never `bought`. The rule is unsatisfiable there
by construction, not by accident, and the Robinhood on-chain lane stays dark until a second class
exists.

`wallet-paid-v2` remains the standard wherever it can be satisfied. This class is accepted ONLY for
chains listed in `RELAYER_SETTLED_CHAINS`, each entry justified by a measurement like the one below.
Solana keeps its own path and is untouched.

## 2. The measurement this class is built on

Transaction `0xb1be90f6…` (CryoPalmar, chain 4663, 56 logs, status `0x1`), read 2026-09-16:

- type `0x2`, **no `authorizationList`** — nothing in the transaction is signed by the wallet. The
  `eip7702_delegated_eoa` label describes the address's code, not this execution.
- `tx.from` is relayer `0xd460b418…`; `tx.to` is router `0xccc88a9d…`; `value` 0.
- Net token flows (three tokens):
  - mint `0x066669ec…`: exactly two non-zero nets — `0x85451d2f…` −Q and **the wallet +Q**. Every
    intermediary nets zero.
  - stable `0x5fc5360d…`: `0xf70da978…` −598918556, two pools positive.
  - WETH `0x0bd7d308…`: two pools negative, the zero address positive.
- The wallet appears in exactly ONE leg: it receives the mint. No settlement asset touches it.
- The router calldata (selector `0x0a2b8f36`, 15,620 bytes) names **the wallet twice** as
  left-padded address words, beside the payer `0xf70da978…` and the settlement token.

## 3. Correction to the approved rule set

The approved R3 read "the same tx carries a related NET payment leg attributable to the
beneficiary's swap". **It is not satisfiable.** The address that delivers the coin
(`0x85451d2f…`) pays nothing, and the address that pays (`0xf70da978…`) never touches the wallet
or the coin. Any rule that ties the payment to the beneficiary through token flows alone would
reject every genuine 4663 buy, exactly as `wallet-paid-v2` does.

R3 is replaced by a STRONGER and satisfiable fact: **the transaction was built naming this wallet**.

## 4. The rules

All facts come from one receipt, one transaction body, and the anchored window already built.

| # | Requirement |
|---|---|
| R1 | The wallet's NET balance of the mint over the transaction is > 0. |
| R2 | The mint has exactly one NET source address (intermediaries net zero and do not count). |
| R3' | The wallet's address appears in the transaction calldata as a left-padded address word: the call was constructed for it. |
| R4 | The wallet is the ONLY address with a net-positive balance of the mint: no distribution, no airdrop. |
| R5 | A settlement asset moves in the transaction — some party nets out a token that is not the mint. |
| R6 | `receipt.status == 0x1`. |
| R7 | Our own entry transaction for that event lies in the same log window (the anchored control). |
| R8 | At least 3 DISTINCT mints satisfy R1-R7 with zero contradictions, and an address claimed by two named traders is revoked. |

## 5. What this class does NOT prove

- **C1.** No `authorizationList`: nothing cryptographically binds the wallet to the execution.
- **C2.** The wallet never pays. "The wallet paid" is unprovable on this chain.
- **C3.** `tx.from` is a relayer and `tx.to` is a router; neither identifies the trader.
- **C4.** Calldata naming proves the call was BUILT for the wallet, not that the wallet authorized
  it. A relayer can name any address, including to gift it.

The claim is therefore: *the wallet was the sole named beneficiary of a settled swap, on three or
more distinct coins, each at a moment we independently copied that trader*. Nobody may read a
`relayer-settled-v1` row as "the wallet paid".

## 6. What it refuses

1. A plain transfer in: one token moves, no settlement leg (R5), and usually no router calldata.
2. A distribution or airdrop: more than one net-positive recipient (R4).
3. A coin arriving from several sources (R2).
4. A failed transaction (R6).
5. An unanchored coincidence (R7) and a single lucky match (R8 needs three distinct coins).
6. A shared executor serving two traders (R8).

Residual hole, stated: a gift deliberately routed through a router that also settles an unrelated
swap, naming the wallet, three times, on three coins, at three moments we independently copied that
trader. Implausible, not impossible.
