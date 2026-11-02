# LLM trading desk — umbrella design (v1)

**Date:** 2026-10-21
**Status:** historical v1 design and measurements; see the dated 2026-10-27 successor note for the current intake/GPT slice
**Owner:** Alpha
**Inputs:**
- Beta, execution and seat plumbing: `docs/superpowers/specs/2026-10-21-desk-execution-plumbing-design.md` (`b09239742`, `beta/desk-execution-plumbing`)
- Lambda, feeds and seats: `docs/superpowers/specs/2026-10-21-desk-feeds-and-seats-design.md` (`1b51b4ec0`, `lambda/desk-feeds-seats`)
- Gamma, opportunity map and pre-registered read: `docs/audits/2026-10-21-desk-opportunity-map.md` (`ad55ad52c`)
- Reference designs and operator orders: local memory `project-ai-trading-office-examples-2026-10-21.md`; brain decisions `a10fde26`, `1672675e`

## 0. Answer first

1. The desk is a new entry origin, `signal_source='desk'`, that rides the existing funded buy path
   (`memecoin_manual_intents` → `make_real_buy_fn` → `real_entry_hook`). No new fill engine. The copy
   fill path (slot lane, EVM fast lanes) is never touched. On the desk lane the model IS the trader
   (operator 2026-10-21): it selects the coin, decides when to enter, sets target and stop values and
   monitors the position; the existing fast fill system executes what it decides. Desk edge is
   selection and timing, not a race, so 1–3 s of model latency on the desk's own decision path is
   accepted and measured. The copy lane keeps rule 2 untouched.
2. Operator order 2026-10-21: live with real money from increment one, small clip, kill knob ON at
   deploy, daily loss halt, pre-registered read. No paper-first gate. A shadow control arm costs nothing
   and is the comparison.
3. Our own ledger says where the first slice goes. The funded book has no per-trade edge overall
   (981 closes, −0.04 %/close equal-weight). The strongest cell at n > 100 is **Solana, 30–100k USD
   source-reported entry mcap** (+4.3 %/close, n = 206, +1,309 USD; Solana > 100k is also positive,
   +1.9 % on n = 185). That history belongs to armed-wallet copy entries with the funded exits; it
   says where to fish, not what unarmed-wallet candidates with model-chosen timing and rails will
   return. Launch sniping lost every time it was measured
   (radar v1–v3 −7.5 to −9.9 %/trade on 623; V6). HL majors show no mispricing above the 9 bps taker
   round trip and no cross-venue capture exists.
4. **Slice 1:** desk entries, 25 USD, Solana, source-reported 30–100k band. Two candidate cohorts,
   read separately: (a) in-band coins just bought by unarmed (watch-only) roster wallets; (c) in-band
   coins from the Pump.fun universe feed once live. JUDGE (Haiku 5.5) is the trader: ENTER / WAIT /
   SKIP, timing and rails. Shadow JUDGE and shadow random (coin and rails drawn at the same response
   time) are the comparison; the read tests the whole desk policy within the wallet-seeded universe,
   not coin selection alone. Read per Gamma §8/§9.
5. There is no live launch universe today. Pump tape dead since 2026-10-12, opportunity engine crons
   stopped 2026-08-21, Axiom dead since 09-16, X capture since 07-16, HL since 08-12, crypto5m capture
   since 07-27. Slice 2 revives a universe feed (Pump.fun program via our gRPC first, opportunity
   engine second). Slice 3 is HL cross-venue **capture**, not trading.

## 0b. Direction change, 2026-10-21 ~12:4xZ (operator, brain 9115c88c)

The desk is its own quant trader on the Pump.fun universe, deciding on what it sees and aiming to
beat people to the coin they later buy. It is not a tracked-wallet follower: roster-wallet buys are
one weak fact attached to a universe ticket, never the trigger or the cohort anchor. Cohort U is
primary; H is a fact feed. The two proposed age/chase hard-filter kills are dropped. Seats v2 with a
model per seat is `docs/superpowers/specs/2026-10-21-desk-quant-seats-v2-design.md` (Lambda
`e2a05a386`): code universe scanner and code-ranked runners table (GPT may annotate the top-30,
measured against the plain code ranking), local qwen3.5:9b as a bulk theme tagger on aggregates
only, Haiku narrative and news/trends researchers and JUDGE, GPT checker and batch post-mortem.
Binding constraint: ~8.6k in-band candidates/day must cut to a few hundred JUDGE calls/day by code
before any model call. Gamma pre-registers the ranker (fixed cut, full logging of kept and cut,
forward outcomes of a random sample of cut candidates) and the U arms (SR over the post-ranking set
and a ranking-free SR over all in-band candidates). Local model harness verdict (episode 546996b9):
qwen3.5:9b fails the JUDGE and CRAWLER gates; it serves aggregate tagging, brain-queue triage and
label capture only.

## 1. Scope lines (operator, verbatim intent)

- Fully automated, LLM-controlled, exploits anything our feeds hand us.
- Microtrading only on very small or very active markets with strong direction.
- A leveraged BTC/majors lane for mispricings is in scope.
- Reuse `sniper/algos/polymarket_crypto5m/` for real-time feed ingestion and computation.
- 1-second candles.
- Agile: each increment live with real money, kill knob, read, then the next.

Standing rules that bind every increment: no LLM call, lock or retry on the fill path (CLAUDE.md
rule 2); fix flaws, never switch a feature off as the fix; the rule-5 edge-case map before code;
funded copy stays copy-only with no coin filters (operator 2026-08-23); DexScreener never answers a
fill or price question.

## 2. Architecture: engine trades, seats govern

| Seat | Who | When it acts | Output |
|---|---|---|---|
| HARD FILTER | code, cheapest first: chain allowlisted, mint identity, age, mcap band, liquidity, route exists, tape alive, token safety, own exposure | every candidate | kill reason or pass |
| CHIEF | code router | every survivor | which lane owns it: copy, desk-micro, majors |
| CRAWLER | Haiku 5.5, no tools, cached per coin | coin enters the watch set, then at most every N min | enum `{promotion, warning, analysis, unknown}` from X / J7 metadata / news |
| JUDGE (VET + QUANT dial) | Haiku 5.5, effort low, one call per candidate | once per surviving candidate; on the desk lane synchronously before the fill (1–3 s accepted, p50/p90 recorded) | `{key, verdict ENTER/SKIP/WAIT, entry: now or a code-checkable condition on 1 s bars, rails: tp/sl/trail values inside operator bounds, reason_code}` |
| SIZE | code: `MEMECOIN_DESK_CLIP_USD × clip_scale`, desk concurrency, desk latch | after ADMIT | intent or SKIP |
| FILLS | existing funded buy path via `memecoin_manual_intents` | intent claimed | terminal state `filled / refused / expired / unresolved` |
| RISK | engine on ticks enforces the rails the seat set; the seat re-reads at boundaries (fill, rail hit, 1 s structure break, timecap − N) and may tighten rails or `EXIT`; LP-pull and source-sell protections stay deterministic and cannot be postponed by a seat | boundaries | `EXIT`, `TIGHTEN(rails)` or `HOLD`; never adds size, never widens a stop |
| POST-MORTEM | Sonnet 5.5, batch, daily | offline | graded closes vs verdicts, proposals as text to the crew; never writes config |
| CHECKER | GPT via `codex exec`, diagnostic | fixed random sample of JUDGE accepts and rejects, blinded to the verdict, same frozen inputs | concrete defect enum: wrong asset, unsupported claim, stale evidence, contradiction. Disagreement is recorded with outcomes, never a veto in v1 |

Communication is one append-only table, `desk_tickets`, in `data/memecoin_desk.db` (not the ledger).
Each seat appends one row; the next seat reads the chain. A ticket carries opportunity id,
chain/mint/pool, parent revision, evidence time, expiry, and policy/model/prompt versions; duplicate
consumers converge on the existing reservation identity (reuse the frozen-request checks in
`sniper/services/memecoin_x_llm_queue.py:114`). A decision is published with an expiry BEFORE the
setup; the engine reads it locally; a late answer cannot authorize a retrospective entry. Brain
retrieval for a seat is frozen as of the decision, and a prior model opinion is provenance, never
independent evidence. Durable findings go to the brain through
the write gate. No message bus.

Event boundaries where a seat may act (Lambda §2): pre-arm hot set; post-fill dial within T seconds;
micro-momentum admission on 1 s bars (the trigger itself is code); majors regime at minute cadence.

## 3. Seat contract (Beta §2, Lambda §2.3)

- Input `desk-seat-in/v1`: ticket, stage, coin `{network_id, mint, pool}`, `asof {slot, block_ts,
  read_at}`, numeric/enum facts, `inputs.missing` listing every unmeasured field. A seat never fetches.
- Output `desk-seat-out/v1`: verdict from the stage's allowed set, `reason`, optional `dial`.
  Structured output (json_schema) where the provider supports it; the same strict code validator
  either way.
- Validation in code after the model. Literal null, empty, unparseable, schema mismatch, out-of-enum,
  mismatched key echo → SKIP `seat_null` / `seat_malformed`. Timeout, HTTP error, 429, refusal
  → SKIP `seat_call_failed:<kind>`, no retry inside the ticket. Dial out of range → SKIP
  `dial_out_of_range`, never clamped. BUY from a non-decision seat → SKIP `buy_out_of_stage`.
  **No failure maps to ADMIT.** Each cause has its own counter.
- Never re-run until BUY, enforced by schema: `UNIQUE(ticket_id, stage)`,
  `UNIQUE(network_id, mint, seat, inputs_hash)`, plus `MEMECOIN_DESK_SEAT_COOLDOWN_S` per
  `(network_id, mint, seat)`.
- Desk JUDGE schema (playbook `desk-judge-v1`, Lambda `12d746287`): `{key, verdict ENTER|WAIT|SKIP,
  cond (WAIT only){metric enum, op, value, window_s, expiry_s ≤ bound}, rails{tp, sl, trail},
  reason_code}`. ENTER = now. WAIT = a code-checkable 1 s-bar condition that fires without a model
  re-call (rails and clip frozen at the decision); unmet at expiry = `entry_expired`; an unknown bar
  never satisfies a cond. Rails outside bounds = SKIP `rails_out_of_bounds`. Draft bounds tp 15–150,
  sl 10–40, trail 8–30 %; `MEMECOIN_DESK_RAIL_BOUNDS` not yet in `.env`, operator to set. Prompt
  ≈ 2k tokens, cacheable; one line of minified JSON out.
  `stale_on_enter`: price moved more than a bounded % since the fact snapshot voids the verdict.
  Hard wall deadline = `judge_timeout`, late answer discarded. Measured on the subscription
  (Lambda, n = 20/10): one CLI spawn per call, JSON as text, wall p50 3.9 / p90 4.4 s, API 3.0 / 3.6 s;
  structured-output tool turn adds ~1 s; thinking off gains nothing; a long-lived `ClaudeSDKClient`
  bleeds context across calls (1.4k → 60k input tokens over 50 calls, half with no output) and is
  unusable. Deadline set to 5 s (operator 2026-10-21). Compact schema run (n = 20): wall p50 4.60 /
  p90 5.13 s, API 3.69 / 4.29 s; output p50 590 tokens of which ~60 are JSON and the rest thinking,
  which the CLI path cannot disable; prompt cache already hits (4,157 cached / 4 uncached input);
  spawn ≈ 0.9 s. Injected ticker: 20/20 SKIP `suspicious_text` in 2.8 s. Expect 10–15 %
  `judge_timeout` at 5 s; the only lever left is a direct Messages API key with thinking disabled
  (unmeasured). Haiku 4.5 on the subscription hung past 400 s, dropped.
- One coin per call. `memecoin_radar_triage.py` batches 15 coins per prompt; one injected ticker
  sways 14 verdicts. Not reused.
- Two-hop isolation: raw text (ticker, name, URI, X text, handles) reaches only the CRAWLER, which
  emits enums. The JUDGE sees numbers and the CRAWLER's enums. Mint, pool, chain, size, slippage and
  recipient come from code's candidate record only. Every hard gate re-runs in the engine; no verdict
  raises a cap, clip, ceiling or slippage. On the desk lane the verdict carries the entry decision and
  rail values; code bounds-checks them against operator limits and rejects (SKIP `dial_out_of_range`)
  rather than clamping.
- Persist every call: input hash, model, effort, latency, tokens, verdict, failure cause, SKIPs
  included. The read needs that denominator.

## 4. Execution plumbing (Beta)

- Same funded wallet as copy for slice 1 (operator 2026-10-21); a separate desk wallet is deferred.
- Origin `signal_source='desk'`, `origin_event_id='desk:<ticket_id>'`; `strategy` stays `MEMECOIN_REAL_POLICY` (the exit sweep selects by it; desk is identified by `signal_source`, Beta step 1). Not
  `'manual'`: manual skips concurrency and quiet hours (`memecoin_real_leg.py:283`,
  `memecoin_evm_leg.py:810`) and already runs −217.90 USD over 237 closes.
- `is_manual_entry` → `bypasses_selection(ev)` true for `{manual, desk}`. Capital and capacity gates
  compare `== "manual"` literally, so desk stays capped with no edit; pin with a test.
- New analytics bucket `desk`; never folds into copy or manual.
- `override=True` and token-safety-warning bypass refused for desk.
- Two additive columns on `memecoin_manual_intents`: `signal_source` (default `'manual'`),
  `desk_ticket_id`. `drain` passes `signal_source` to `buy_fn`.
- `ManualIntentWorker` cloned from `ManualExitWorker`: today's claim lag p50 3.54 s / p90 11.93 s
  (n = 445) waits for the main-loop pass and shares `max_per_pass=2` with operator buys. Operator
  intents first. Measure claim lag before and after.
- Knobs in `sniper/settings/memecoin.py`: `MEMECOIN_DESK_ENABLED` (1), `MEMECOIN_DESK_CHAINS`
  (`solana`), `MEMECOIN_DESK_CLIP_USD` (25), `MEMECOIN_DESK_MAX_CONCURRENT` (5, inside the global cap raised to 11), `MEMECOIN_DESK_DAILY_LOSS_USD` (200, write-once latch in `memecoin_desk_loss_trips` joined by every full-close writer,
  below the book latch), `MEMECOIN_DESK_SEAT_COOLDOWN_S` (900).
- Crowded Solana fee and BSC gas bid are 8–12 % of a 25 USD clip: off for desk entries.
- The per-coin one-position guard must refuse a duplicate when `source_user_id` is NULL (SQLite
  treats NULLs as distinct in the open-unique index). Test required.
- `unresolved` counts as open desk exposure until reconcile settles it.
- Desk and copy share the final chain/mint reservation guard, unresolved entries included. Beta
  verified the gap is wider than reservations: `idx_mc_pending_coin_uniq_v2` covers `status='reserved'`
  only, `_entry_pending` is keyed by uid, the open-position unique index includes uid and strategy, so
  copy and desk can double a coin today. v1 adds one origin-blind `(network_id, lower(mint))` guard
  inside the reservation transaction: reserved, unresolved attempt, or open real position all refuse,
  both ways. **Operator call:** may copy add to a coin the desk holds, or vice versa (v1 refuses both).
- The initial desk policy (clip, rails, expiry) is frozen BEFORE submission and carried through
  reservation, fill and recovery. Today `apply_rails` runs after the fill (`memecoin_manual_intents.py:361`),
  and an arm failure leaves the intent terminally `filled` with no rails; for desk that is a refusal,
  not a logged exception. `_EXIT_OVERRIDE_PRIORITY` (`db_memecoin.py:3379`) omits `desk`; define its
  precedence and authority; TIGHTEN preserves omitted fields and compares the complete effective
  policy atomically; required `sl_pct` rejects null, 0, NaN, ±inf, negative and out-of-bounds. Beta
  R1/R2 (uncommitted on top of `825640b84`): rails written into the intent before enqueue; if the
  after-fill arm fails the intent resolves `refused desk_rails_not_armed` + a deterministic close
  intent + a page, never `filled` unarmed; `desk` sits directly after `operator` in
  `_EXIT_OVERRIDE_PRIORITY`; cohort overlay writers skip desk positions; an active operator row means
  the desk records but does not write. Producer H = `observation_owners()` minus the armed set, writes
  only `memecoin_desk.db`, never `memecoin_events`, `memecoin_entry_attempts`,
  `onchain_wallet_detections` or any approval.
- Cohort (a) plumbing: today's confirmed scanner (`scripts/research/onchain_wallet_watch.py:666`)
  takes `armed_verified_owners()` only. Watch-only owners come from
  `wallet_observation.observation_owners()` (`sniper/services/wallet_observation.py:305`); reuse the
  receipt verification for them without widening funded copy authority.
- Rug and honeypot defense (Beta §6, verified 2026-10-21): the funded copy lane has NO deterministic
  LP-pull exit today (`classify_lp_removal` is called only by `memecoin_exit_brain.py` and the V6
  engine; `MEMECOIN_ENGINE_AUTHORITATIVE_CHAINS` is empty in `.env`, fails OFF; a funded LP pull exits
  only via rails, b_stop, vol_death or catastrophe 80). Cluster admission is OFF
  (`MEMECOIN_REAL_CLUSTER_GATE_ENABLED` defaults 0, not in `.env`, worker not running). The holder
  scan (`solana_holder_scan.holder_concentration`) has no funded writer. Desk pre-entry kills, cheapest
  first, unknown = KILL with its failure kind: authority/fee/delegate/DBC hook plus any Token-2022
  transfer hook not on an operator allowlist; serial-creator prior; NEW holder writer (top-10 % net of
  LP vault, curve account and burn; creator %); cluster when re-armed; NEW exact-route sell probe of
  the acquired quantity; EVM `token_safety_ok_evm` for later chains. In-position typed events:
  `lp_reserve_drop`, `authority_change`, `creator_or_top_holder_sell`, `sell_buy_flip`,
  `maker_collapse`, `sellability_fail`, `roster_buy_after_entry`; three code exits the seat cannot
  postpone: LP catastrophic, freeze authority set, K consecutive sellability failures; a blind detector
  emits `monitor_blind`, never silence. Thresholds unset until Gamma's pre-rug read; rows carry
  `detector_version`. **Operator call:** add the LP-catastrophic code exit to the copy lane too.
- Two traps in `apply_rails` (`memecoin_manual_intents.py:291`, verified 2026-10-21): (a) custom mode
  writes 0 = OFF for every unnamed percentage rail, so `sl_pct` is a REQUIRED rail on a desk answer
  or a take-profit-only verdict opens a position with no stop; (b) it writes `source='operator'`,
  `actor='operator'`, and operator rows may loosen; desk rows need `source='desk'`,
  `actor='desk:<ticket>'` or the tighten-only rule binds to nothing. The catastrophe floor is outside
  `RAIL_FIELDS` (frozen contract, `real_runner:457`) and stays deterministic; pinned by test.
- Boundary seat answers HOLD / TIGHTEN / EXIT. TIGHTEN is tighten-only, checked against the active
  desk row in the same transaction as the write; one loosened or dropped rail rejects the whole
  answer. A failed boundary call = HOLD on the armed rails, never EXIT. WAIT fires without a model
  re-call; rails and clip frozen at the decision. New knobs: `MEMECOIN_DESK_WAIT_MAX_S`,
  `MEMECOIN_DESK_ENTER_TTL_S`, `MEMECOIN_DESK_BOUNDARY_MAX_S`, `MEMECOIN_DESK_RAIL_BOUNDS`.

## 5. Candles on the slot clock

- `memecoin_candles.py:52` buckets `memecoin_position_marks.sweep_ts` (held coins only), not
  `pool_ticks`. The Solana tape's `observed_at` is a batch clock: one second holds 2,580 rows over
  511 slots.
- v1: bucket on `block_no` (slot); time only from `block_times`, missing = `DATA_INSUFFICIENT`; desk
  watchlist on the paper vault stream. EVM candles out of v1 (Base 2 s, Ethereum 12 s cannot give 1 s
  bars; BSC 0.75 s can, later).
- The held-coin Solana tape (`data/memecoin_sol_tape.db`) does not calibrate to fills (Gamma
  `0ef4a5e04`: fill/tape median 1.91 on amm, 2.78 on curve; starts after entry in 16/17; blind ≥ 60 s
  before the exit in 12/17; `quote_side`/decimals null; none of our own sigs recorded). Desk 1 s bars
  must come from a stream with decimals and quote side known (the Pump.fun gRPC universe stream),
  not from this tape, until it is fixed.
- 1 s bars feed the micro-momentum trigger (code) and the post-fill RISK boundaries. Slots are not
  seconds: keep slot order, block time and receive time as three separate fields; a 1 s bucket may
  hold no fresh observation, and closing it adds up to one second of waiting.

## 6. Feeds and universe (Lambda §1, Gamma §1.7, §6)

Live today, all wallet-centric: J7 (83 wallets), Shyft gRPC (48 owners), Rabbit shreds, EVM
watchers on 5 chains, 4663 seq shadow, on-chain volume tape 4663/8453 (~1 h retention), Solana held-coin
tape. JUDGE upper bound from `memecoin_events`: 250–500 distinct coins/day, Solana falling 396 → 236/day,
no events on 10-17 (unexplained gap).

Dormant: pump tape (dead 10-12; 1.66 M launches, median 32,295/day ≈ 22/min, p90 44,926; `ps_trades`
0 rows ever; 72 % of launches from serial creators ≥ 5 launches, ≈ 9k/day ≈ 6/min after that cut; no
holder/LP/depth fields), opportunity engine (`MEMECOIN_OPPORTUNITY_CAPTURE=0`, crons stopped 08-21 for
resource isolation, ~41 launches/min decoded on 08-30, 4663 poll failing on batch getLogs), Axiom
(CDP scrape, one day captured 09-16), X capture (07-16, 0 LLM jobs ever), HL (08-12), crypto5m
Binance/Chainlink capture (07-27, DB 0 bytes).

Universe plan: (a) Pump.fun program subscription on our existing Shyft gRPC / Yellowstone, every launch
at slot speed, free (Lambda §1d, `23601ceca`: `transactions{pump: vote=False, failed=False,
account_include=[6EF8…F6P]}` + `blocks_meta{}`, CONFIRMED, own connection, same shape as the funded
migration filter in `memecoin_shyft_signal.py:59-71`; ≈ 24 successful tx/s, creates 0.4–0.75/s,
50–100 KB/s inferred not measured; envelope cursor `slot:sig:event_index`, `transaction_index` from
gRPC; open: Shyft `from_slot` replay unverified, and a 1 h shared-token throttle test on funded
recv-S before it goes live); (b) opportunity engine revived in its own CPU-bounded process as decoder and
point-in-time store; (c) DexScreener `token-profiles/latest` and boosts as a lead source only;
(d) Axiom KOL callouts as CRAWLER context only if the browser session is revived; GMGN has no API.
None of (a)–(d) is in slice 1.

Model access: operator chose two subscriptions, Claude via the Agent SDK and GPT via `codex exec`, not API keys. Lambda's probe: `codex exec` on gpt-5.6-luna answers a one-word schema-valid verdict in 5.7–6.2 s at 22.6k tokens and SKIPped an injected ticker; `*-mini` and the default gpt-6.1-sol return 400 on a ChatGPT login; `codex exec` hangs unless stdin is closed; its read-only sandbox can read `.env`, so Codex seats get code-built facts only. At 6 s and 22k tokens the GPT seat is CHECKER / POST-MORTEM, not JUDGE. The SDK's
OAuth is expired on this host (fails in 0.7 s, "OAuth session expired and could not be refreshed");
the Telegram relay is down for the same reason. Trap: a dead login returns `ResultMessage`
subtype `success` with `is_error=True`; seat code treats it as SKIP and pages. Seat options stripped
(no tools, settings, MCP, hooks). Quota is shared with the crew sessions; API key at ≈ 3 USD/day is
the fallback. `models.py` HAIKU default is claude-haiku-4-5 (1/5 USD per M); desk uses desk-only tier
constants pointing at claude-haiku-5-5 (0.10/0.50), no global flip.

## 7. Slice 1: Solana 30–100k desk, live

- **Candidates (Gamma §7, `c698b0140`).** Armed-only in-band refusals are too thin: median 2/day
  (0–3/day since 10-07), so 30 closes would take weeks to months. Three sets, operator picks:
  (a) **unarmed roster coins in band**, ≈ 50/day (13–87), off the J7 hint stream (`hint_only` in
  `memecoin_events` since 10-12, opaque IDs, no source receipt) → needs the native-fallback confirmed
  scan before a desk entry; (b) armed in-band coins including runner-entered, ≈ 5–6/day, overlaps
  copy, so the desk would double a copy position; (c) universe feed (Pump.fun gRPC) survivors in
  band, count unknown until the feed is live. Operator 2026-10-21: (a) yes, and (c) must exist. The
  roster-wallet buy is a fact-sheet field for the JUDGE (wallet track record with us, a confirmation
  whose weight depends on the wallet), never a trigger by itself. With (c) the desk sees in-band coins
  before roster wallets buy; a roster buy AFTER our entry is a boundary event for the RISK seat. Safety refusals are never candidates. The desk re-runs every hard gate. Mcap caveat: no
  independent entry-time mcap is stored; `source_market_cap` is the feed-claimed value at the
  source's buy (`claimed_untrusted`), `entry_mcap` equals it on all 386 fills; decision-time drift is
  unmeasured. The band is therefore a source-reported cohort until a decision-time cap is measured.
  Throughput: 50 raw hints/day is not 50 eligible candidates/day; confirmation, identity coverage,
  safety, expiry and model abstention all cut it, so "140 closes in a month" is a hope until counted
  live, and 140 per arm gives about 5 points of precision, not strong power.
- **Decision.** JUDGE acts as the trader: ENTER now, WAIT for a code-checkable demand condition on
  1 s bars (expiring), or SKIP; sets tp/sl/trail values inside operator bounds. Measured facts that
  shape it (Gamma, pre-registered, cohort H): waiting does not buy cheaper and costs tail
  (`4cdac983e`); swaps in the source's pool in (0,5] s excluding the source and us ≤ 1 → SKIP, skips
  23 % of signals that neither run (12 % vs 38 % reach +20 %) nor dump (4 % vs 20 % hit −20 % first),
  AUC 0.708 vs 0.591 for momentum (`d0090988b`). That gate is CODE, evaluated at t = 5 s from a
  whole-pool stream only (null = no gate), run in parallel with the model call started at the signal;
  the fill needs model ENTER and gate pass, decision ready at max(5 s, model wall). Playbook
  `desk-judge-v4` (`603e5dcc1`). Emergency exits (LP pull, source sell) stay deterministic.
- **Money.** 25 USD flat clip (operator 2026-10-21). `MEMECOIN_DESK_MAX_CONCURRENT` and
  `MEMECOIN_DESK_DAILY_LOSS_USD` are operator knobs, retuned live; starting values 5 and 200 USD
  (operator 2026-10-21, written to `.env`). The halt is a trigger on new entries, not a loss ceiling: exits,
  reconciliation and signed or submitted claims always finish. `MEMECOIN_DESK_ENABLED=1` at deploy;
  kill = set it to 0.
- **Read contract amendments (Gamma §13, `29adcd654`, after Codex's methodology pass).** Contrast
  bound = lower(SJ) − upper(SR); an upper cap exists only where the record's own rails cap it, else
  INCONCLUSIVE, no PASS; the floor includes all incurred costs. "560 per arm proves +5" withdrawn;
  planning example ≈ 382 per arm for alt +10 vs null +5 (α 0.10 one-sided, power 0.80, SD 46),
  before clustering. SR p_H = 0.10 fixed now, p_U pre-registered before U starts; domain-separated
  hashes per draw (select / delay / tp / sl / trail / derisk); candidate emission written before any
  arm decides, per-arm admission logged separately (`desk_candidate_emissions`,
  `desk_arm_admissions`). Crossed coin × wallet × day bootstrap with shared weights across arms;
  intervals "exploratory" until a coverage simulation passes. `response_at` immutable; `trigger_at`
  added; WAIT end states expired / triggered_refused / triggered_failed /
  triggered_submitted_unknown / triggered_filled, only `expired` is an abstention. Valuation quotes in
  a frozen [T, T+60 s] window; late or missing = censored.
- **Read contract, consolidated (Gamma opportunity map §12, `0130a8e30`).** SR is independent of
  JUDGE: every hard-filter-pass ticket is taken iff sha256(ticket_id)/2^256 < p (p frozen per cohort),
  own 5-slot / 200 USD book, random delay and rails from the menu, within gate_state. WAIT: the
  shadow-JUDGE clock is the actual trigger time; an expired WAIT is an abstention. Common cutoff T,
  whole-book marks at exact quotes, missing-value bounds (−100 % / best observed); PASS needs the
  lower-bound book. 140 per arm is precision, not proof (proving ≥ +5 pts needs ≈ 560 per arm).
  Cluster bootstrap by coin (wallet nested) and by day, wider interval reported. Reads 1 and 2 can
  only KILL or CONTINUE; PASS only at the final read. Feature cutoff = `decision_started_at`.
  `memecoin_entry_features` rows are decision-window facts (windows end at the decision, own sigs
  excluded) but are written post-terminal (T1 after 30 s on a flat book), so a live desk needs the
  §2.4b candidate snapshot producer for the same fields.
- **Three records (GPT review 2026-10-21, adopted).** FUNDED JUDGE: actual execution. SHADOW JUDGE:
  the same decisions under the shadow fill model. SHADOW RANDOM: one coin drawn uniformly from the
  same eligible set at the same timestamp by `sha256(attempt_id)`, same dial, its own two-position
  book, shadow fill model. Shadow JUDGE − shadow random = selection value. Funded JUDGE − shadow
  JUDGE = execution-model error. Eligibility is frozen before JUDGE; rejects, failures, expired
  decisions and capacity blocks are retained. The 30-close checkpoint includes every admitted
  position through the cutoff, open exposure and missing valuations included, and has a calendar
  deadline. Pre-register the minimum worthwhile improvement after execution, data and model costs;
  kill the hypothesis if the interval's upper bound cannot reach it.
- **Read (Gamma §8 amends §2, `e3ca8ba2a`; frozen before the first entry, sha in the brain).**
  Records F (funded JUDGE), SJ (shadow JUDGE, same picks one-to-one, shadow fill = exact-route buy at
  JUDGE response time + measured median entry latency, exact-route sells, modelled fees), SR (shadow
  random, own book and own halt, draws coin AND rails by `sha256(attempt_id)` at the same response
  timestamp from the eligible set as of then and uniformly over JUDGE's allowed rail bounds, so
  selection value = coin choice + rails choice; draws only with a free slot, skips logged). Per-call
  columns: sent/received, latency_ms, model id, tokens, billed cost, timeout/error; a JUDGE timeout or
  error = no entry, counted, no SR draw. Latency cancels out of selection value and is charged only in
  F's live net; SJ-at-candidate-time is a diagnostic, never a pass input (Gamma §9, `d2b9883d1`). Selection value =
  mean(SJ) − mean(SR), two-sample bootstrap. Execution error = median(F − SJ), paired. Metered model
  cost spread over F closes and subtracted from F and SJ. Minimum worthwhile improvement = +5 pts per
  close of selection value after fees and model cost (1.25 USD per close; the cell's own copy edge is
  +4.3 %). Pass: LB90 > 0, point ≥ +5, survives drop-best, F live net after fees and model > 0,
  execution error median ≥ −5. Power: SD ≈ 46 pts, so 30 per arm resolves ≈ 15 pts and the +5 MWI
  needs ≈ 140 closes per arm; 0–15 pts at read 1 = INCONCLUSIVE, continue. Calendar: read 1 at 30
  closes or day 14 (fewer closes = UNDERPOWERED, can kill, cannot pass); read 2 at 60 or day 35; hard
  stop day 56, knob off unless passed.
- **Kill.** Cumulative live net ≤ −200 USD per cohort (operator 2026-10-21, matches the 200 USD halt; worst realized before the kill acts ≈ 325 USD = 200 + 5 × 25 open); daily halt on 2 consecutive days; selection point ≤ 0 at
  read 1; execution error median < −10 at ≥ 15 closes (that kill names the fill path, not JUDGE); a
  JUDGE call found on the COPY-lane path, or a copy-lane entry p50 regression after the desk deploy
  (desk latency itself is not a kill); any desk entry priced or gated on a display price. A kill turns the knob off and writes a brain fact.
- **Expected cost of being wrong (Gamma §10, 25 USD × 5 open × 200 halt).** 30-close p5/p50/p95 = −75 / 0 / +72 USD whole book, −36 / +32 / +102 in the cell; the 200 USD halt fired on 0 of the last 30 replayed days (a 50 USD halt would have fired on 8); worst daily bound 325 USD.

## 8. Build order (each step its own branch, Gamma pass, knob ON at deploy)

1. Origin split: `bypasses_selection`, desk analytics bucket, two intent columns, duplicate-guard
   test, one hand-written desk intent end to end at the smallest clip.
2. `ManualIntentWorker`, claim lag measured before and after.
3. `desk_tickets` + seat validator + code-only funnel (hard filter → size) on the slice-1 candidate
   set; the shadow CONTROL arm starts here so its denominator predates the model. The desk hard
   filter owns market-data presence (the desk inherits manual's bypass of
   `_market_data_unavailable_reason`, the dead-coin gate and the volume floors, so no market data =
   KILL). The desk daily halt counts realized PnL only; open desk losses are not counted.
4. JUDGE seat on Haiku 5.5 via the subscription (after the operator's `claude login`); slice-1 read
   begins at the first desk fill.
5. Slot-clock 1 s candles for held pools, then the desk watchlist; micro-momentum trigger.
6. Universe feed (Pump.fun program gRPC; opportunity engine revived) → slice 2.
7. HL cross-venue 1 s capture (HL mark vs Binance) → majors lane spec only after deviations above
   9 bps that revert are observed. Known gaps before any binary fair value is trusted:
   `sniper/algos/polymarket_crypto5m/live_desk.py:335` takes its opening reference from Binance while
   Polymarket resolves on Chainlink TWAP at both boundaries; `sniper/hyperlive/hl_exchange.py:63` is
   isolated 1x; Polymarket applies market-specific taker delays, so the 452 ms Solana figure says
   nothing about competitiveness there. Funding carry is hourly, not a sub-second race.

## 9. Rule-5 edge-case map (merged)

1. **Money path: yes.** Seats decide admission and dial; every item below is mandatory and this map
   goes to the operator before any build.
2. **Fill-path speed.** The COPY fill path is untouched: no seat call, lock or retry between a source
   signal and our fill; the engine reads any copy-side verdict as a dict lookup, p50/p90 measured,
   target < 1 ms. The DESK lane has no source to race: the model call sits between candidate and
   desk fill by design (operator 2026-10-21), its p50/p90 is recorded per call, and the fill after the
   decision uses the existing fast path. Desk intents share no thread, lock or connection with the
   copy lanes; `ManualIntentWorker` claim lag measured before/after; a funded entry p50 regression on
   any chain with the desk running is a kill.
   **Not handled:** the no-verdict default per lane (copy = code-only as today, universe = SKIP) is
   an operator call.
3. **Concurrency and global state.** Seats run in their own worker with their own connection; one
   writer per verdict store; the engine reads immutable snapshots; no module attribute, env var or
   shared connection mutated; tick observers keep `get_readonly_connection()`.
4. **Absent vs zero vs unknown.** No verdict ≠ SKIP-by-model, counted apart. A None fact is a NULL
   kill before the call, never 0. A tape window across an outage is `unknown`, not 0 volume. Model
   null = SKIP `model_null`. `inputs.missing` lists every unmeasured field.
5. **Dependency failures.** Timeout, 429, 5xx, refusal, malformed, wrong key echo, unconfigured or
   expired login, daily budget exhausted: each a distinct SKIP cause, never an empty success; the
   seat pages once with persisted page state. Provider outage = lane runs at its no-verdict default
   and pages. Rugcheck-style sources down = SKIP that candidate, never loop.
6. **Units and identity.** `network_id` is the string chain key; EVM mints lower-cased, Solana base58
   as is; mcap and liquidity USD floats, ages seconds; verdict TTL seconds on the corrected UTC
   clock, never host vs J7 `created_at`. Solana `pool` = coin-vault identity, not a displayed market.
7. **The sample.** Denominator = every candidate that reached the JUDGE, SKIPs and failures included.
   Post-entry facts exclude our own fills. CONTROL sees the same candidates, so the read is about the
   seat, not the stream. Censored pairs (missing exact quote) are reported, not counted.
8. **Rollback.** Each seat and the desk have a kill knob, ON at deploy. Off = engine runs code-only
   exactly as today; stored verdicts expire by TTL; the engine checks the knob at read time, so a
   warm store with the knob off is ignored. Intents already claimed finish to a terminal state.

**Runnable check left behind with step 3:** a test feeding the seat validator out-of-enum values, a
mismatched key echo, a refusal stop, literal null, and a ticker string carrying an instruction; it
asserts SKIP with the right cause every time and that the acted-on mint equals code's candidate mint.
It must fail when one failure cause is mapped to ADMIT.

## 10. Operator rulings needed

1. Exit rails in v1: per ruling 61859b83 the JUDGE sets tp/sl/trail values inside operator bounds
   (`MEMECOIN_DESK_RAIL_BOUNDS`); the earlier "existing menu only" wording is superseded.
2. No-verdict default per lane: copy = today's code-only path, universe = SKIP (recommend yes).
3. `claude login` on this host, so the subscription path and the Telegram relay work again.
4. Universe feed: BOTH (operator 2026-10-21). Shyft gRPC on the Pump.fun program is the transport;
   the opportunity engine is the decoder and point-in-time store, fed through its transport-neutral
   `OpportunitySourceEnvelope` instead of its own WS. Runs as its own CPU-bounded process (the 08-21
   stop was host load). Needs `.env` lines.
5. Slice-1 numbers: 25 USD clip, 5 open, 200 USD desk halt, −200 USD cumulative kill, Solana only (operator 2026-10-21, in `.env`).

## 11. Current intake/GPT successor — 2026-10-27

For the new intake cohort, the current contracts are `2026-10-27-desk-intake-candidate-path-design.md`, playbook `desk-judge-v26`, and `docs/audits/2026-10-27-intake-desk-cohort-preregistration.md` through Amendment 4 (`84f35a5a496009116484937579f3dbce706bf126`). The operator's later paper-first intake order governs that cohort; this historical design's immediate-funded rollout wording grants no authority to activate it. Existing funded copy execution is unchanged.

The merged caller at `04e67e6e2928cf0844a734cc046bb874267ec0de` supports a new frozen `gpt-6-luna` / `codex_cli` / `low` subscription identity and strict seven-key v26 output. Historical Claude cohorts, original schemas and October 21 latency observations retain their original meaning and cannot describe GPT performance. A new model/provider is a new policy hash; no silent alias or automatic Opus fallback is permitted. Runtime activation, operator-only provisioning and `.env` approval remain separate from reviewed code. Narrative GPT remains default OFF under its own forward preregistration.
