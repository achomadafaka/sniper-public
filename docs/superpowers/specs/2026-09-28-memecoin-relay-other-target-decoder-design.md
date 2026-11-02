# Relay envelope buys through a swap target other than the adapter (design + rule 5)

Status: SPEC. Operator order (direct, 2026-09-28 ~12:55Z): build the other_target decoder now.
Alpha: cover the three census shapes, direction from the trader's own token delta, real frames as
fixtures. Author: Lambda.

## What the measurement says

Every J7 4663 fill since 09-21 is a Relay envelope (51 of 51; `scripts/research/evm_relay_shape_census.py`).
The envelope is always three calls: approve(payment, spender), a swap call, and the router's cleanup.

| swap call target | selector | fills | watched trader |
|---|---|---:|---:|
| Relay adapter 0x039ec98a… (decoded today) | 998b5942 | 47 | 33 |
| 0x0005ea38… (0x Settler) | bb66eb41 | 2 | 2 |
| Kyber router 0x6131b5fa…, called directly | e21fd0e9 | 1 | 1 |
| 0x00000000… (0x AllowanceHolder) | 2213bc0b | 1 | 0 |

The watcher refuses the other three as `unsupported_relayer_swap`. Among the 15 watcher misses since
06:37Z, 2 are this shape (change 06:46:05, letsgopenguin 07:56:21). The traced line is the target
check, `swap[0] != RELAY_ADAPTER_4663`, at memecoin_evm_onchain_watch.py:190.

### Why the adapter rule cannot simply accept any target

In the adapter shape, the tokens pass THROUGH the call target:

    payer -> ROUTER -> ADAPTER -> ...pool... -> ADAPTER -> ROUTER -> wallet

The decoder requires those four legs by address. In real other-target frames they do not exist:

- **Kyber direct** (4663, DexGemsReal 0xc1f711f9): the router pays a Kyber EXECUTOR (0x8f10b468),
  and the coin comes back to the router from that executor. The Kyber router itself never holds
  either token.
- **Settler** (8453, prevaile 0x32afe8e3): the router pays the Settler, and the coin comes to the
  router straight from the pool (0x8d6ad994).
- **AllowanceHolder** (56, 1hundredx 0x896b4dd1): the router pays the pool, and the coin comes to
  the router from another pool.

## Design

For `any_route` (the watcher's decode) only, a swap call to a target other than the adapter is
accepted when every one of the following holds. Everything the adapter shape already checks stays
checked: the proxy, the Permit2 envelope, three calls, no call value or failure flag, the payer is
not the wallet, the cleanup to the wallet alone with the same metadata, both FundsMovement events,
and receipt/tx identity.

1. **The approval names the target.** The approve call's spender equals the swap call's target,
   and the allowance equals the paid amount. This already holds for the adapter.
2. **Direction from the trader's OWN delta**, never from the largest mover:
   - the wallet's net change in `mint` is +amount_out;
   - amount_out is exactly the ROUTER -> wallet leg of `mint`, and is at least the cleanup
     minimum;
   - the wallet moves no payment token at all.
3. **The router is a pass-through:**
   - payer -> ROUTER is exactly `paid`, and the router's net payment is 0 (it paid out exactly
     `paid`);
   - the `mint` legs INTO the router sum to exactly amount_out, and the router's net `mint` is 0.
4. **Other gainers of `mint`:** the existing fee/tax rule, seeded from where the coin entered the
   router rather than from the call target. Each extra gainer must have been paid from the
   pool-side flow. For the adapter the seed is the adapter itself, so today's shape is unchanged.

Proof version `relay-envelope-other-target-v1`. It carries `swap_target` and `swap_selector` as
evidence. Like the envelope version, it does not pin the Kyber code: trust comes from the token
legs and the Relay proxy/router code, which is pinned and warm.

The address verifier (`any_route=False`) keeps the Kyber-only proof it was calibrated on.

## Rule 5

1. **Money path.** Yes, one step removed: a detection wakes the on-chain lane, which runs the
   same funded entry hook and every gate. A wrong proof would enter a coin the wallet did not buy.
   That is why direction comes from the wallet's own delta, and why the router must net to zero in
   both tokens.
2. **Speed.** A pure decode over logs already fetched. No new read.
3. **Concurrency.** Pure function. No state.
4. **Absent vs zero vs unknown.**
   - A leg that is absent is not a zero: a missing router leg refuses.
   - An unknown target is never trusted by address; only the flows decide.
5. **Failure modes.**
   - The decoder returns None and names the step: `unsupported_relayer_swap` stays the reason
     for a failed shape.
   - A frame with two coins, or a wallet that also spends payment, refuses.
6. **Units and identity.**
   - Raw atomic amounts compare exactly; no USD enters the proof.
   - Addresses are lowercased.
   - Direction is from the wallet, not the biggest transfer (Gamma, trade_of).
7. **Sample.** Target: 4 of 51 J7 4663 fills and 2 of 15 watcher misses become decodable. After
   deploy, count watcher no_buy lines with decode=unsupported_relayer_swap on watched buys.
8. **Rollback.** Revert the commit. The adapter path is byte-identical.

## Check left behind

- Real frames as fixtures:
  - the fixture's 4663 Kyber-direct, 56 AllowanceHolder and 8453 Settler buys;
  - six 4663 frames from publicnode: the census's four other-target fills and the two misses.
- Tests:
  - each decodes to the wallet with amount_out equal to the router -> wallet leg;
  - every adapter frame in the fixture keeps its current proof;
  - fabricated negatives refuse: the approval to a different spender, the wallet also paying
    payment, and a router that keeps part of the coin.
- A mutant that restores the adapter-only target check fails the other-target cases.
