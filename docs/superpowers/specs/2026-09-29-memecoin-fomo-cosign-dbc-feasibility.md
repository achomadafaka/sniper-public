# Fomo co-sign-gated DBC coins: can a dedicated Fomo account buy them? (FEASIBILITY, no build)

Date 2026-09-29 (08:40Z). Operator order via Alpha: a NEW Fomo account, used ONLY to buy the
co-sign-gated Meteora DBC coins. Not signals, not follows. This is the feasibility read; nothing
was built and no call was made with the account. Gamma prices the 7 origins in parallel; the build
decision needs both.

## Verdict

- **(a) Fomo co-signs a transaction for our own funded keypair: not possible without Fomo's
  cooperation.** The co-signature is Fomo's executor AgmLJBMD signing the whole message, and only
  Fomo's backend holds that key. Every co-signed trade seen was built by Fomo's router for the
  account's own wallet. No evidence either way that Fomo will co-sign for an external wallet.
- **(b) Trade from the account's Fomo embedded wallet through Fomo's API: technically the only
  route, with three blockers.**
  1. The trading endpoints are not in this repo. It holds read endpoints only.
  2. The funds and coins sit in the Fomo wallet (custody).
  3. Fomo's published terms prohibit automated trade execution through ordinary accounts
     (`docs/audits/2026-09-27-fomo-authorized-execution-instructions.md`, citing
     https://fomo.family/terms).
- **(c) Not possible** is the outcome if Fomo will not give supported API access. The 09-27 audit
  already recommended asking Fomo for a partner interface; nothing since changes that.

## (1) The chain read

**Method.** Research Helius key via `funded_exit_truth.rpc`, gated on flat Solana (0 open, 0
reserved). For each gated mint, the first 100 successful transactions
(`getTransactionsForAddress`, ascending), then every DBC `Swap2WithTransferHook` among them
(discriminator b75d992818e6c297). The mints:
- ZrueWB1Y (hook eyesEswW);
- AzmuURyd and CpzF978U (hook eyesfsib);
- 74cmFdL5 (hook ESKuS8vD).

**Samples.** 10 buys and 10 sells on ZrueWB1Y, and 3 + 3 on each other mint. All are co-signed.
Buys: tQnwkE6R, 23NP3AR4, 3tpu8aDQ, 4P87mxHL, 4m5b8L4k, 5oLTJjy9, 4kb4tM5i, 5y3vCEns, 5b6Ggf8n,
7fWWz7J8. Sells: 3ELr58eg, 2TMtdnEA, 3Htr2Nu7, 5dWMsfuk, 3H8pRtne, 4NMwZKu3, 3piGBjDD, 4bkbbPq8,
3H8pgRHC, 5fFZNtQy.

<!-- schema:external=fomo_cosign_counts -->
| mint | hook | buy co-signed | sell co-signed | not co-signed |
|---|---|---|---|---|
| ZrueWB1Y | eyesEswW | 72, all log "Fomo verification passed" | 10, no Fomo log | 12 "other" by A4ycsCc2 via eyesbEx9 (the burn-engine buyback) |
| AzmuURyd | eyesfsib | 74, all logged | 21, no log | 1 buy + 4 other by DF8qw7Ej (the launch: mint keypair co-signs) |
| CpzF978U | eyesfsib | 72, all logged | 22, no log | 1 buy + 5 other by GJdzvx1v (the launch) |
| 74cmFdL5 | ESKuS8vD | 73, no Fomo log | 21 | 4 buys + 1 sell signed by 2xLAdy5X + 5bgQnNjC, direct DBC call + Memo |

**Signers and fee payer.**
- Every co-signed trade has exactly two signers. AgmLJBMD is the fee payer, and it also pays the
  new token account's rent.
- The user's wallet is the other signer. Router ProVF4pM is the outer program.
- Its instruction is 50 bytes, discriminator aa2955b184501f35, with 36 accounts. That is
  identical on all 365 co-signed swaps.

**Who receives the coins.**
- On every buy, the token account that received coins belongs to the user signer. On every sell,
  the user signer's account paid them.
- None of the 20 sampled users is among our 49 roster Solana addresses.
- The operator's 09-27 manual buy fits the same pattern: owner 3h75aBTL (the account
  ExactLeftBison's wallet) signed, and AgmLJBMD paid.
- The chain cannot tell a Privy embedded wallet from an external wallet. Treat "embedded" as
  unproven, likely.

**Instruction layout.** Router → DBC `Swap2WithTransferHook`, 20 accounts, with AgmLJBMD among
them (passed on to the hook as a remaining account). The instructions sysvar is present in every
sample. No Ed25519 or secp256k1 verify instruction appears in any sample.

**What the hook checks (eyesEswW / eyesfsib).**
- The two executables are the same size (100,045 bytes), so they are the same program deployed
  twice.
- Their strings include:
  - "FOMO_REQUIRED: Buying from this curve requires Fomo. Open this token in Fomo to buy.";
  - "Fomo verification passed";
  - "burn engine buyback: allowed" and "zero-amount curve transfer: allowed";
  - "creator", "registered mint", "NOT_LAUNCH_CONFIG" and "NOT_ENGINE_CONFIG".
- No nonce, expiry, deadline or payload strings, and no signature-verify instruction.
- So the gate is a signer check: the registered verifier (AgmLJBMD) must sign. No Fomo-issued
  payload was found. The instructions sysvar is present, so introspection of the router
  instruction cannot be ruled out. This does not change the outcome: AgmLJBMD's signature covers
  the whole message, so only a Fomo-built message can carry it.
- **Buys only.** The string says "Buying", and no sell logs "Fomo verification passed".

**ESKuS8vD (74cmFdL5) is a different hook.** It is 111,669 bytes with no Fomo strings, and its
refusal text is Portuguese ("a chave verificadora da plataforma ..."). It accepted AgmLJBMD, and
it also accepted 4 buys signed by 2xLAdy5X + 5bgQnNjC through a direct DBC call. It is another
launchpad's verifier set that happens to include Fomo's key. Classify it separately.

## (2) The routes

**(a) AgmLJBMD co-signs a message for our funded keypair.**
- Needs a Fomo endpoint that builds, or co-signs, a transaction for a wallet that is not the
  account's own. None is observed, and none is in the repo.
- Custody would stay ours, which makes it the preferred route if Fomo offers it. That is question
  1 of the 09-27 audit.

**(b) The account's embedded wallet trades through Fomo's API.** The app's flow is: its backend
builds the router message with AgmLJBMD as fee payer, the user wallet signs, AgmLJBMD co-signs,
and the transaction is submitted. Automating it needs:
- the account's Privy session;
- the trading endpoints — not in `sniper/feeds/fomo_feed.py` or `fomo_account.py`, which hold
  only `/feed`, `/v2/users/*`, `/v2/leaderboard`, the `/ws` activity socket and Privy
  `/api/v1/sessions`;
- either Fomo's server executing the order, or our process holding the embedded wallet's signing
  key (Privy key export).

What a session needs, per `FomoAuth`: an access token, a refresh token and a Privy client-analytics
id (FOMO_ACCESS_TOKEN / FOMO_REFRESH_TOKEN / FOMO_CA_ID), the browser headers the WAF demands, and
the single-use refresh-token rotation. Custody risk: funds and coins live in Fomo's wallet.
Withdrawal is Fomo's send function, or key export.

**(c)** is the default until Fomo answers.

## (3) The exit

- **Before migration**, through the same route: Fomo sold for all 74 observed users.
- **Without Fomo, likely yes but unproven.** The hook gates buys only (see above). Our own DBC
  `swap2_with_transfer_hook` sell, signed only by the coin's owner, should pass. That needs:
  - the owner's key (route (a), or an exported embedded-wallet key in route (b));
  - a LiteSVM proof on the eyes executable, as `output/research/up-hook-20260928/audit.md` did for
    UP.

  No successful sell without AgmLJBMD was seen on an eyes pool: every user sold through Fomo's
  router.
- **After migration** the hook is revoked and an ordinary route (DAMM v2 via Jupiter) sells.
  Confirm on one migrated eyes coin before relying on it.
- **Ban during a position.** Route (b) without an exported key has no exit until the ban lifts.
  That is the custody risk in its sharpest form.

## (4) Ban risk

The three prior bans (`scripts/memecoin_fomo_account_swap.sh`, memory):

<!-- schema:external=fomo_bans -->
| account | refused | trigger |
|---|---|---|
| cmlflprko | 2026-07-31 | REST `/feed` poll every 1.5 s = 57,600 req/day against the app's 5,760 (10x) |
| cmsd52yly | 2026-09-06 19:31Z | a 0.25 s pacing floor = sustained 4 req/s (~345,600/day); WS closed 13 times with 1008 policy violation, then 403 |
| cmr6rem8o | 2026-09-09 14:38Z | 403 with a live JWT; pacing was already 0; the WS had reconnected 9 times with zero payloads. Cause not established |

- All three were feed and automation accounts. None traded, so none was banned for trading.
- ~7 buys in 16 days is human-scale trade volume. But the footprint that got accounts banned was
  request volume, not trades. And the terms bar automated execution whatever the volume.
- Low volume lowers the signature that has banned us. It does not make the use permitted.
  Whether to run it is the operator's call. This spec does not design around Fomo's enforcement.
- **Hard requirement if (b) goes ahead: the trading account makes no call except its own trades
  and its auth refresh.**
  - `load_env_files` loads `.fomo.env` into every process (runner, web, scripts). `FomoAuth`
    reads FOMO_* from it, and its token store `.fomo_tokens.json` is shared.
  - Writing the new account into `.fomo.env` would hand it to the feed, profile and follow-repair
    code. The follow repair is exactly the bulk-follow bot signature that got accounts banned.
  - Its credentials need their own names and their own token store.
  - Never run `memecoin_fomo_account_swap.sh` for it: that script swaps the feed account.

## (5) Rule 5 edge cases and latency

**Money path.** Yes: entry, exit, signing and custody. Every item below is mandatory before any
build.

**Speed.** Measured 2026-09-29, 12 unauthenticated requests to `prod-api.fomo.family`, no account:

| stage | p50 | p90 |
|---|---|---|
| TCP connect | 7 ms | 11 ms |
| TLS done | 20 ms | 30 ms |
| first byte (cold) | 30 ms | 46 ms |

- That is the network floor. The trade call's server time (build, Privy sign, co-sign, submit) is
  **unmeasured**. Measuring it needs a call with the account, which this order excludes.
- Ordinary coins must see zero added calls: classify the hook from the mint account already read
  at safety time.
- Gated coins go to their own worker thread. Today they cannot fill at all, so nothing already
  fillable slows down.

**Concurrency and global state.**
- `fomo_feed._TOKEN_STORE` is a module global fixed at import, and `_rate_guard` (with its
  ban-guard latch) is process-global.
- A second account in the same process would share the store file and the ban latch. One
  account's ban would silence the other, and a refresh would overwrite the other's token.
- It needs a per-instance store path and its own guard.

**Absent, zero and unknown.**
- A Fomo timeout after submit is `submitted_unknown` until the chain answers.
- An unreadable Fomo-wallet balance is unknown, never 0.
- The fee schedule is unknown: the 09-27 receipt was 2.00 debited, 1.05 swapped and 0.95 charged.
  One receipt is not a schedule.

**Dependency failures.** Fomo down or 403 mid-position means no route-(b) exit; see (3). Two more
known failure modes:
- the Privy refresh deadlock (memory 2026-08-03);
- the WAF answers 403 or 430 without browser headers (430 is what the unauthenticated probe got).

**Units and identity.**
- The input is USDC (the test), not SOL.
- Total debit is not the net swap input.
- Positions sit in a different wallet from the funded keypair. Every reservation, fill, balance
  read and exit must bind to (chain, wallet, route).
- These are Token-2022 coins, so decimals come from the mint.

**The sample.**
- 7 co-sign origins since 09-13 (Gamma), priced from the source's outcome, not our fills.
- The fee and the route's delay subtract from every one of them.
- ESKuS8vD origins are not Fomo-only.

**Rollback.** Turning the knob off stops entries only. Existing positions in the Fomo wallet must
stay reconciled and exitable, and the withdrawal path must be named before the first buy.

## What would change the verdict

1. Fomo offers a supported co-sign or partner API. Route (a) is preferred if it can sign for our
   wallet.
2. A LiteSVM proof that an eyes-hook sell passes without AgmLJBMD. That would make the exit
   independent of Fomo for whoever holds the owner's key.
3. Gamma's price of the 7 origins, net of the Fomo fee and route delay, clears the cost of a
   custodial wallet.

The chain facts do not settle any of these.
