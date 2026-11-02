# Memecoin real-money deployment — wallet allowlist, 13% fraction sizing, accounting (2026-07-10)

Operator-approved design (this session). Scope: gate real entries to a per-wallet allowlist,
resize clips to 13% of per-venue equity, add an unquotable-24h zombie guard and a pre-registered
auto-demote rule, enrich Telegram alerts (watch everything, trade few), add a manual real-buy
path, and give the cockpit truthful accounting (cash / deployed / unrealized / realized / equity).

**Non-goals:** no change to paper arms or the pre-registered A/B; no new exit policy (mirror +
SL−50% + derisk@+100% + no TP + no timecap stay exactly as configured); no BSC/Robinhood arming;
no conviction sizing (still refuted).

## 1. Per-wallet real allowlist

- `memecoin_wallets.real_approved INTEGER DEFAULT 0` (idempotent ALTER in
  `ensure_memecoin_tables`, same pattern as `excluded`).
- Helpers: `real_approved_user_ids(conn)`, `set_wallet_real_approved(conn, uid, approved)`.
- Gate: in `make_real_hook`'s hook (memecoin_real_runner.py) — **buys only**. Not approved →
  `{"status": "not_approved"}`, no reservation, no executor. Sells always pass (mirror-exit must
  work for any real position, including manual entries on watch-only wallets).
- Paper copy + Telegram alerts unchanged for the full curated roster.
- Seed (operator rule "all wallets above +$5 paper PnL", 2026-07-10 snapshot, 12 wallets):
  PrimeT, zinceth, capodon, theveeman, Thokani, bruhcolli, WileyRaeDermott, goyim, careful,
  chimpfone, insentos, jester.

## 2. Sizing — 13% of per-venue equity

- New env: `MEMECOIN_REAL_SIZING=fraction`, `MEMECOIN_BANKROLL_FRACTION=0.13`.
- `real_trade_size(usdc_balance, open_notional_usd)` gains the `fraction` mode:
  `clip = round(max(MEMECOIN_MIN_TRADE_USD, equity * fraction), 2)`.
- **Per-venue equity**: venue free USDC + venue deployed cost basis (confirmed open + reserved
  on that venue). Both risk-context builders pass network-scoped snapshot/pending into the
  sizing input; the concurrency / exposure gate checks stay GLOBAL (unchanged design).
- `MEMECOIN_MAX_CONCURRENT=7` (7 × 13% ≈ 91% max deployed; 8 would always bounce the last).
- Reservation==spend invariant machinery untouched (ctx computes clip; executor spends
  `decision.approved_size_usdc`).
- Known bind: if a venue's equity < floor/0.13 (≈$231), the $30 floor makes clips exceed 13%.

## 3. Exits — operator spec + zombie guard

- Live config already matches the operator spec: mirror-on-sell always on,
  `MEMECOIN_REAL_STOP_PCT=50`, `MEMECOIN_REAL_DERISK_PCT=100`, `MEMECOIN_REAL_TP_PCT=0`,
  `MEMECOIN_REAL_MAX_HOLD_S=0`. No change.
- New rail: **unquotable-24h auto-exit** (`MEMECOIN_REAL_UNQUOTABLE_EXIT_S=86400`, 0=off).
  In `real_exit_sweep`: each sweep updates `last_quote_price/ts` for real positions that DID
  quote; a position whose last successful quote (or open, if never quoted) is older than the
  threshold gets an on-chain exit attempt (reason `unquotable`). Sell-ladder failure → the
  existing stuck-bag path. A quoted position is NEVER touched by this rail — it is not a timecap.

## 4. Auto-demote guardrail (pre-registered 2026-07-10)

- After every real close: if the wallet's cumulative real realized PnL ≤
  `-MEMECOIN_REAL_DEMOTE_LOSS_USD` (default 100) OR its last
  `MEMECOIN_REAL_DEMOTE_CONSEC` (default 4) real closes are all losses →
  `real_approved=0` + Telegram alert (`REAL-DEMOTE`). Operator re-approval is manual.
- Rationale: allowlist selection rides on 1–15 paper closes/wallet — noise. The demote rule is
  registered before go-live so it can't be post-hoc rationalized away.

## 5. Telegram — watch everything, trade few

- Front-run alert (`build_alert_message`) gains: the wallet's paper record
  (`paper +$461/14 closes`) and a `REAL-ARMED` vs `watch-only` tag. Lookup is one local SQL on
  the copy path (no HTTP); thesis lines unchanged.
- `alert_entry` / `alert_exit` gain ticker + handle (+ PnL% on exit) so "what has entered" is
  readable on the phone (currently mint-only).

## 6. Manual real buy (operator path for watch-only signals)

- `POST /api/memecoin/real/buy {mint, source_user_id?, ticker?}` — full gate chain: kill switch →
  risk gate/reservation (same clip sizing) → token safety → executor. Position carries the
  source trader's uid so their later sell mirror-closes it. Venue-routed by mint prefix
  (base58 → Solana, 0x → allowlisted EVM chain).
- UI button on activity/candidate rows + a mint field in the Real-Money cockpit.

## 7. Accounting (cockpit truth)

- `GET /api/memecoin/real/accounting` per venue + combined:
  - **cash**: live USDC (+ native gas balance, displayed separately),
  - **deployed**: cost basis of open real positions + reserved pending,
  - **unrealized**: Σ(mark − entry) × open qty from one batched DexScreener call
    (null-marked positions reported as `unquotable_n`, excluded from the sum, not zeroed),
  - **realized**: cumulative + today (real-UTC day),
  - **equity**: cash + deployed + unrealized,
  - **clip_next**: the fraction-mode clip the next entry would get,
  - slots used / max.
- Cockpit panel renders this; envelope line shows live clip, fraction, allowlist count.

## 8. Ops facts (2026-07-10)

- Balances at design time: Solana $389.93 USDC + 0.0526 SOL; Base $284.39 USDC + 0.0055 ETH.
  Base ETH supports only ~3 concurrent full-book exits — top up to ~0.02 ETH for full weight.
- Kill switch is TRIPPED and stays tripped through deploy; clearing it is the operator's
  explicit go-live action.
- Real entries fire only on fresh global-feed events (≤120s); follow-activity stays paper-only.
- Paper evidence base: 302 closes, +$659 gross modeled at $100 clips; per-wallet N 1–15.
  $1–5k-sized source buys outperformed ≥$5k buys (+$849/121 vs −$190/179) — no extra size floor.

## Addendum — pre-launch review fixes (2026-07-10, same day)

An independent adversarial review + stop-out forensics landed after the build. Fixes (ad36197):

1. **P0 — paper/real index collision.** `idx_mc_pos_open_uniq` was `(uid, mint, strategy)
   WHERE is_open=1` without `execution_mode`. run_once opens the paper alpha_b arm BEFORE the
   real hook fires on the same event, so the FIRST real fill would have raised IntegrityError
   after the on-chain swap landed (orphaned funds), and the pre-index dedup sweep would retire
   the real row as a "dup". Rebuilt as `idx_mc_pos_open_uniq_v2` with
   `COALESCE(execution_mode,'paper')` in the key; dedup mode-scoped; regression tests.
2. **Stop debounce.** Forensics on the 4 paper −50% stop-outs: all four reverted, 2 were single
   bad DexScreener ticks (other traders were buying 3–4× the "stop price" within seconds). In
   real money that market-sells a healthy coin into a wick. `MEMECOIN_REAL_STOP_CONFIRM_SWEEPS=2`
   — the stop must hold across 2 consecutive ~15s sweeps; a quoted non-stop sweep resets the
   counter (`stop_hits` column). TP is deliberately NOT debounced.
3. **Take-profit ON.** Counterfactual sweep over all 83 alpha_b closes (`max_gain_pct`-based,
   90% slippage haircut): full TP in the +40–80% band dominates (actual +$58 → +$278 at +40,
   +$432 at the overfit +80 peak); full TP beats derisk-and-ride at every level (the riding
   remainder round-trips into stops). Config: `MEMECOIN_REAL_TP_PCT=40`,
   `MEMECOIN_REAL_DERISK_PCT=0` (inert below TP anyway). Caveats: paper data, n=83, fast
   spikes may gap through the fill.
4. **EVM balance re-read guards.** Bounded re-reads (3×, 1s) before: gone-close on a zero token
   read (erc20_balance fails closed to 0.0 — one bad RPC read booked −100% on a live position),
   entry anomaly on a lagged delta, and zero `received` after a confirmed sell. Mirrors the
   Solana pattern adopted after the real $5 anomaly incident.
5. **Atomic runner state.** `runner_alive` now rides in `/real/status`; the arm pill treats
   unknown as amber, never LIVE.

## Verification

- Focused suites: test_memecoin_sizing, test_memecoin_real_rails_v2, test_memecoin_real_seams,
  test_memecoin_real_alerts + new tests (allowlist gate, fraction sizing, zombie rail, demote,
  accounting endpoint). `python3 scripts/validate_schema_refs.py`. brain_index regenerate
  (schema change). Runner restart → heartbeat fresh → `/real/status` + `/real/accounting` sane →
  STOP and wait for operator kill-switch clear.
