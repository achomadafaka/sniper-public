# Desk: feeds and LLM seats (Lambda slice)

Status: draft for crew brainstorm, 2026-10-21. No code, no `.env` change, no money action.
Order: operator 2026-10-21 via Alpha (brain decision a10fde26 plus the desk order): a fully
automated, LLM-controlled trading desk. Every increment ships live with a small clip, kill
knob ON, a daily loss halt and a pre-registered 30-close read. Shadow control arms are allowed.
The LLM is never on the fill path (CLAUDE.md rule 5.2). Engines fill. LLM seats choose the
coin, the policy and the dial at event boundaries.

This slice covers three things: (1) the feed inventory, (2) the seats, model tiers and cost,
with the JUDGE contract, and (3) the prompt-injection boundary. Section 6 is the rule-5
edge-case map.

## 1. Feed inventory

"On" means a process or thread delivered data at 2026-10-21 ~08:20Z. Evidence is the runner
heartbeat (`data/memecoin_runner.heartbeat`), `ps`, `crontab -l`, and the max row times in each
store. Latency numbers come from earlier brain reads, as cited. A number marked
"unmeasured" has no read behind it.

### 1a. Live today (memecoin funded stack)

| Feed | Code | Latency | Coverage | Cost | What a seat gets |
|---|---|---|---|---|---|
| J7 relay | `sniper/feeds/j7_feed.py` | Carries J7 transit. `created_at` runs 1.7-5 s before chain time, so never diff host and corrected stamps (brain 2026-10-16). | 83 tracked wallets (49 + 24 pump on accounts). Last 32 h: Solana 530 buys / 207 mints; 4663 65/28; 56 65/30; 8453 4/3; chain 1 4/2. | Free; tokens pasted by the operator. | Armed-wallet buy/sell with ticker, size and mcap. **Not a wallet signal source** for routing (operator 2026-10-02). |
| Shyft gRPC processed (Yellowstone) | `sniper/feeds/yellowstone/`, `memecoin_shyft_signal.py`, `memecoin_shyft_confirmed.py` | Slot level; slot lane decide-to-fill 452 ms on the first S+1 fill (2026-09-26) | 48 owners subscribed (slot_lane heartbeat) | Shyft Build, $200/month | Owner transactions at processed commitment. Trap: an arm/disarm blinds both regions for about 5 s (d22b2724). |
| RabbitStream shreds | `memecoin_rabbit_signal.py`, `memecoin_rabbit_*_amm.py` | Leads processed gRPC by 5.3 ms p50 / 12.5 ms p90 (44 buys, 2026-10-15) | Pump curve, plus DFlow/Jupiter/OKX routes it can decode | Included in Shyft Build | A pre-execution, owner-signed buy. It proves the signer only, never landing. |
| EVM on-chain wallet watchers | `scripts/research/onchain_evm_wallet_watch.py` (5 processes), `memecoin_evm_onchain_watch.py` | 4663 feed lag 121/220/283 ms min/p50/p90; Base preconf via Flashblocks | 4663, 8453, 56, 1, 5042 armed addresses. Chain 1 Relay buys log as `no_buy` (trap 0836398e). | Alchemy app key (monthly cap hit 2026-10-15), Chainstack ~49 RPS | Source swaps with pool identity |
| 4663 sequencer feed shadow | `scripts/memecoin_rh_feed_shadow_loop.py` | See row above | All 4663 sequencer txs | Free | Pre-receipt view of the 4663 tape |
| On-chain volume feed | `scripts/memecoin_onchain_volume_worker.py` 4663 / 8453 | feed_age 0.02 s / 0.76 s at read. Resets in 24 h: 20 on 4663, 36 on 8453. | Every pool log on those chains | RPC | Per-pool volume windows. A reset window is unknown, not zero. |
| BSC V2 live marks | `sniper/services/memecoin_bsc_v2_live.py` | Unmeasured | 56 V2 volume, V2/V3 funded pool marks | publicnode WS, free | Marks and volume on 56 |
| Solana pull tape | `scripts/memecoin_sol_tape_worker.py` | Unmeasured | Funded and watched mints | RPC | Trades per mint |
| Jupiter quotes (keyed) | runner `jupiter_load` | Unmeasured here | 464k quotes since start, 0 rate-limited | Jupiter key | Exact route price |
| DexScreener | runner | n/a | 278 calls since start | Free | **Banned for fill questions** (operator 2026-09-30). Seats may read it for display context only. |

### 1b. Dormant: code exists, nothing delivering

| Feed | Code | Last data | Cost | What a seat would get | Restart blocker |
|---|---|---|---|---|---|
| Fomo activity WS | `sniper/feeds/fomo_feed.py` | heartbeat `status=disabled` | Free | Global buyer feed | Fomo account banned (2026-09-12) |
| Axiom capture | `sniper/feeds/axiom_browser.py`, `axiom_parse.py` | `data/axiom_raw.db` and heartbeat stop 2026-09-16 12:03Z (2,976 WS frames/60 s at the end) | Operator login, headless Chromium | Pulse / new-pair / trending frames: the richest launch-universe feed we have | The worker is not running although `AXIOM_CAPTURE_ENABLED=1`. Cause unread (probably a halt file). Cloudflare block risk. |
| X cashtag capture plus LLM annotation | `memecoin_x_api_capture.py`, `memecoin_x_llm_worker.py`, `sniper/analysis/memecoin_x/llm_contract.py` | `memecoin_x_capture_runs`: 31 runs, last 2026-07-16; 6 coin mentions; 0 LLM jobs ever | X API: plan and price unverified | Narrative and promotion intent per coin | No X key or LLM key in the runner environment. Coverage tiny. |
| Opportunity radar | `memecoin_entry_radar.py`, `memecoin_radar_triage.py`, `memecoin_opportunity*.db` | Opportunity DB written 2026-08-30 | RPC | Launch universe with venue facts | `MEMECOIN_V6_RADAR_CONSUMER_ENABLED=0` |
| Hyperliquid WS / poll / whale | `sniper/feeds/hyperliquid_ws.py`, `hyperliquid_feed.py`, `hl_whale.py`; `sniper/algos/hl_microstructure/` | HL DBs written 2026-08-12 | Free public API | Majors trades, book, funding, liquidations, whale positions: the input for the leveraged majors lane | `HL_REAL_ENABLED=0`, `HL_REAL_DRY_RUN=1`. Alt-short is invalidated (CLAUDE.md HL section). |
| Polymarket crypto5m stack | `sniper/algos/polymarket_crypto5m/` (`binance_feed`, `chainlink_rtds`, `capture`, `live_desk`) | `data/crypto5m_capture.sqlite` is 0 bytes (2026-07-27); pid 672150 not running | Free (Binance, Polymarket RTDS, CLOB WS) | Binance aggTrade + 1 s kline, Chainlink BTC/USD, CLOB book with gap detection | No process running. Maker edge was a vol-floor mirage (brain 27420f20). |
| Pump curve tape | `sniper/services/memecoin_pump_tape.py`; `data/pump_snipe_tape.sqlite` | `start_tape_thread` has no caller outside its module. `ps_tokens` last created 2026-10-12 09:24Z, `ps_trades` 0 rows ever; cron commented 'research disabled 2026-10-12' (Alpha/Gamma 2026-10-21) | Public mainnet-beta, free | `volume_facts(mint)` in memory | Not wired into the runner |
| Opportunity engine | `sniper/services/memecoin_opportunity_*` | `MEMECOIN_OPPORTUNITY_CAPTURE=0`, crons stopped 2026-08-21 ('radar resource isolation'); decoded ~40 Pump.fun launches/min on 2026-08-30 | RPC | The in-house launch-universe decoder | Operator `.env` yes |
| Polymarket trade tap / CLOB WS normalizer | `polymarket_trade_tap.py`, `clob_ws_capture.py` | n/a | Free | Polymarket last trades / books | Library code only |
| Goldsky fills | `goldsky_fills.py` | Operator-triggered, historical | Free | Polymarket wallet fill history | Not a live feed |
| News / odds / commodities / weather / tennis | `news.py` (RSS, 15 min), `odds.py`, `commodities.py` (yfinance, 30 min), `weather.py`, `tennis_scores.py` | Polymarket-research era | Free / free tiers | Slow macro context | Minutes-scale polling: useless for microtrading, maybe for the majors lane's event calendar |

### 1c. What the inventory says

- **No live launch universe today.** Every live feed starts from an armed address (J7, Shyft,
  Rabbit, EVM watchers). The pump tape, the opportunity engine, Axiom, Fomo and X are all off.
- Universe candidates, in order:
  1. a Shyft gRPC (Yellowstone) subscription on the Pump.fun program instead of the 48 owners:
     every launch at slot speed, on the plan we already pay for. It must be proven not to
     throttle the funded processed feed (one token serves both);
  2. a revived opportunity engine (~40 launches/min decoded on 2026-08-30).

  Both need an operator `.env` yes.
- The majors lane has no live feed. HL and Binance both need a restart; both are public and
  free.
- JUDGE volume today: upper bound ~500 calls/day from `memecoin_events` (Gamma, 2026-10-21).
  Universe volume is unknown until candidate 1 or 2 runs.

### 1d. Universe transport: Shyft gRPC on the Pump.fun program feeding the opportunity engine

Operator ruling 2026-10-21 (via Alpha): BOTH. gRPC is the transport, and the opportunity
engine's `OpportunitySourceEnvelope` path is the decoder and store. It runs in its own
CPU-bounded process, never inside the funded runner.

**Subscription.** One `SubscribeRequest` on its own connection. The same request shape is
already used on the funded stream for migrations (`memecoin_shyft_signal.py:59-71`).

```python
pb.SubscribeRequest(
    transactions={
        "pump": pb.SubscribeRequestFilterTransactions(
            vote=False, failed=False,
            account_include=["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]),   # create, buy, sell, complete, migrate
        # optional second label, post-graduation trades:
        # "pumpswap": ...(account_include=["pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"])
    },
    blocks_meta={"meta": pb.SubscribeRequestFilterBlocksMeta()},   # block_time + blockhash per slot
    commitment=pb.CommitmentLevel.CONFIRMED,
)
```

- `account_include` on the program id matches every transaction that loads the program,
  router CPIs included (Jupiter, OKX, DFlow, bots), so create, buy and sell are all covered.
  No `account_required` is needed.
- `failed=False` drops the ~89% failed bot spam (pump tape measurement, 2026-09-26) at the
  server, not in our CPU.
- Use CONFIRMED and set `confirmation=CONFIRMED`. The universe engine is not a race lane.
  PROCESSED would need TENTATIVE rows, ORPHANED handling and slot-status tracking, for a gain
  the JUDGE (seconds) cannot use.

**Expected load.** None of this is measured on gRPC yet; it is inferred from the
2026-09-26 public logsSubscribe measurement: ~220 frames/s at processed, 89% failed, so about
24 successful pump transactions/s. Creates at 22-45 launches/min are 0.4-0.75/s; trades are
the rest. Each gRPC update carries the full transaction plus meta, roughly 2-4 KB, so about
50-100 KB/s, plus about 2.5 blocks_meta/s.

- That is light for one Python process. The CPU cost is protobuf decode and the Borsh event
  decode per program-data line.
- The first hour must log actual msg/s, bytes/s, decode ms p50/p90 and backlog before it is
  trusted.

**Throttle test 2026-10-21** (operator yes via Alpha). One extra gRPC connection to grpc.ams:
hour A ran slots only from 09:13:05Z, then hour B ran slots plus the Pump.fun filter. The
metric is funded head arrival (heartbeat ts minus `head_age_s`) minus the probe's first sighting
of the same slot, per feed. Stop rule: B rolling 5-min p90 > max(1.2 x A p90, A p90 + 100 ms).

**Caveat:** hour A is dirty. The operator's Ollama update and a 6.6 GB model pull ran inside
it. Hour A is therefore an upper bound on the quiet baseline, not the baseline. If B is within
noise of A, the result reads "no visible effect under a dirty A", not PASS. Results are
appended when B closes.

**Shared token risk.** One Shyft token serves the funded processed feed. This stream must be
shown not to throttle it: watch funded slot_lane recv-S and reconnects for 1 h with the
universe stream on, against a matched hour off. If it does throttle, it needs a second token
(a cost question for the operator).

**Envelope mapping** (`sniper/services/memecoin_opportunity_sources.py:79`):

| Envelope field | Source on the gRPC update |
|---|---|
| `source_key` | `"opportunity:pumpfun_grpc"` |
| `source_provider` | `"shyft_grpc"` |
| `source_cursor` | `f"{slot}:{signature}:{event_index}"` (same shape as bags recovery) |
| `venue_id` | `VenueId.PUMPFUN_SOLANA` |
| `program_or_contract_id` | `PUMPFUN_PROGRAM` |
| `transaction_id` | base58 of `transaction.signature` (bytes) |
| `block_or_slot` | `update.transaction.slot` |
| `transaction_index` | `transaction.index` (in-block position; logsSubscribe never had it) |
| `event_index` | Same indexing as `memecoin_opportunity_live`: the log-line index for `Program data:` lines, `(outer << 16) \| inner` for anchor CPI events (`e445a52e51cb9a1d` prefix in inner instructions). Both come from `meta.log_messages` / `meta.inner_instructions`. |
| `block_hash` | `blocks_meta.blockhash` joined by slot. None if the meta has not arrived; never invented. |
| `chain_timestamp` | `blocks_meta.block_time` joined by slot. Until it lands, hold the row (bounded buffer) or fall back to `received_at` with a flag, as `_chain_iso` does. The fallback rows are labelled. |
| `received_at` | Corrected UTC at protobuf receipt |
| `confirmation` | `ConfirmationState.CONFIRMED` |
| `payload` | `{"log_line": "Program data: ...", "actor_id": <fee payer>}`: the input `decode_pumpfun_event` already takes |
| `token_hint` | The mint from the decoded event, or `""` |

**Edge cases.**

- **Reconnect gap.** `from_slot` exists in the vendored proto; whether Shyft serves replay is
  unverified. Every disconnect writes an `op_data_gaps` row with its slot span. Gap windows
  are unknown, never zero activity.
- **Dedupe.** `raw_event_id` fingerprints include `source_key`, so this source never dedupes
  against old logsSubscribe rows. Joins across sources use `(transaction_id, event_index)`.
- **Decoder drift.** The pumpfun decoder is version-pinned (`pump-public-idl-0.1.0-2026-07-26`).
  An unknown discriminator returns `blocked(UNKNOWN_VERSION)`, counted, never dropped silently.
- **Rollback.** Kill the process. The funded runner does not depend on it.

## 2. Seats

Both reference designs agree on one rule: code does almost everything, and the model reads
only what code cannot. The mapping below follows 0xboan's funnel (13 code kills, 1 model read,
cheapest first, so the bill scales with survivors) inside A_Pirate_I's seat order.

| Seat (A_Pirate_I) | Ours | Who | When it acts |
|---|---|---|---|
| Hard filter | Code kill checks, cheapest first: chain armed, mint identity, age, mcap band, liquidity, route exists, tape alive, safety, own exposure | Code | Every candidate |
| CHIEF | Event router: which lane owns the candidate (copy, micro-momentum, majors) | Code | Every survivor |
| CRAWLER | Narrative read of X / J7 metadata / news for one coin, as a frozen snapshot | **Haiku 5.5**, offline from the decision, cached per coin | When a coin enters the watch set, then at most every N min |
| VET | The one model read: the JUDGE (2.3) | **Haiku 5.5, effort low** | Once per surviving candidate per event boundary |
| QUANT dial | Pick an exit/sizing policy id from a fixed menu | JUDGE output field, not a separate call | Same call |
| SIZE | Clip from bankroll, caps and regime | Code (existing funded sizing) | Engine |
| FILLS | Existing funded engines (slot lane, EVM lanes) | Code | Engine |
| RISK | Daily loss halt, kill knob per seat, global exposure | Code | Engine and supervisor |
| (post-mortem) | Grade closed trades against the verdict; propose menu changes to the crew | **Sonnet 5.5**, Batch API (50% off), daily | Offline. Never writes config. |

Where a seat acts (operator ruling 2026-10-21, brain 61859b83):

1. **DESK lane: the JUDGE is the trader.** It runs synchronously before the desk fill, with a
   5 s wall deadline (operator), and p50/p90 is recorded per call. Every desk bet is placed
   by the model; no answer means no desk entry. After ENTER, the fill path itself is code
   only: no model call between the order decision and the signed send.
2. **COPY lane: no LLM** (operator ruling 2026-10-21). No hot set, no pre-arm, no verdict read.
   The copy engine is unchanged.
3. **Majors lane.** The JUDGE picks regime and policy at a slow cadence (minutes); fills are
   code. (2.3 contract to be adapted when that lane is scoped.)

### 2.1 Model tiers and the stale registry

`sniper/settings/models.py` defaults are behind:

- `HAIKU` = `claude-haiku-4-5-20251001` ($1 / $5 per MTok);
- `SONNET` = `claude-sonnet-5` ($2 / $10).

The current models are `claude-haiku-5-5` ($0.10 / $0.50, up to 100K prompt) and
`claude-sonnet-5-5` ($2 / $10). The Haiku default is 10x overpriced for desk work. The desk
should pin its own tier constants (`DESK_JUDGE`, `DESK_CRAWLER`, `DESK_REVIEW`) in that file,
each env-overridable. It must not flip `SNIPER_HAIKU`, which would move every other caller.

The runner environment has no `ANTHROPIC_API_KEY`, `XAI_API_KEY` or `OPENAI_API_KEY`. So
`memecoin_radar_triage.py` has always run "unconfigured". Section 2.1b covers the operator's choice: the subscription via the Agent SDK instead of an API key.

### 2.1b Subscription path (operator 2026-10-21: use the subscription via the Agent SDK)

The operator chose the Claude subscription through `claude_agent_sdk` (0.2.118), as
`bot/relay.py` does, over an API key. Probe 2026-10-21 ~09:0xZ, with a stripped seat:
`tools=[]`, `setting_sources=[]`, `mcp_servers={}`, `strict_mcp_config=True`, a custom
`system_prompt`, and `output_format` json_schema.

- The SDK spawns the bundled CLI. That CLI's stored OAuth is **expired**: every call returned
  "Failed to authenticate: OAuth session expired and could not be refreshed" in ~0.7 s. So
  the Telegram relay cannot answer either. The operator must log in again. A long-lived token
  for unattended use (`claude setup-token` -> `CLAUDE_CODE_OAUTH_TOKEN`) probably suits seats
  better; verify before relying on it.
- The failure comes back as a `ResultMessage` with `subtype='success'` and `is_error=True`.
  The seat must check `is_error`. The subtype alone reads a dead login as success, which is
  the swallowed-error class in rule 5.5.
- `output_format` adds one tool (`StructuredOutput`), so `max_turns` must be at least 2.
- The seat config must stay stripped. The relay's `_base_opts` loads every setting source,
  skills, MCP, hooks (brain RAG) and the Claude Code preset prompt. That is slow, token-heavy,
  and gives a seat that reads untrusted text a full tool set, which breaks section 3 rule 1.
- Subscription limits are shared with the crew sessions on the same account. Crew work can
  starve seats, and seats can starve the crew. `rate_limited` is its own SKIP cause, and seat
  calls need a daily cap.
- Per-call latency (CLI spawn plus model) is unmeasured until the login works. It decides
  whether a seat runs one process per call or keeps one `ClaudeSDKClient` session open.
- Whether automated trading seats fit the subscription's usage terms is unverified. That is
  an operator check.

### 2.1c GPT subscription via Codex CLI (operator 2026-10-21: two subscriptions, low-cost models)

Probe 2026-10-21 ~08:35Z: `codex exec --ephemeral --skip-git-repo-check -s read-only -m <model>
-c model_reasoning_effort=low --output-schema schema.json -o last.txt` with stdin closed.
codex-cli 0.154.0, logged in with ChatGPT.

| Model | Wall time | Tokens/call | Verdict on the injected ticker |
|---|---|---|---|
| gpt-5.6-luna | 5.7 s, 6.2 s | 22,648 | SKIP, schema-valid |
| gpt-5.6-terra | 8.0 s | 24,154 | SKIP |
| gpt-6-astra | 7.4 s | 24,957 | SKIP |

- On a ChatGPT login, only the models in `~/.codex/models_cache.json` work. The config default
  `gpt-6.1-sol`, and `gpt-5-mini`, `gpt-5.1-codex-mini`, `gpt-5.4-mini`, `gpt-5.5-mini`, all
  fail with 400 "not supported when using Codex with a ChatGPT account".
- **Without stdin closed, `codex exec` waits on stdin forever** (the first probe hung 120 s).
- About 22k tokens per one-word verdict: the Codex agent prompt plus the user config's MCP
  servers, plugins and hooks. That uses up subscription quota fast. A seat must strip them
  with `-c` overrides or a `-p` profile. **Never** use a second `CODEX_HOME` holding a copied
  `auth.json`: the rotating refresh token would then be shared, the J7 lesson.
- `-s read-only` still lets the model read the disk, `.env` included, and it has a shell
  tool. A seat that reads untrusted text needs the shell disabled. Until that is shown,
  Codex seats get only code-built fact sheets, no raw X/J7 text (section 3 rule 1).
- 6-8 s per call (process spawn plus agent loop) fits only the slow boundaries: pre-arm hot
  set, CRAWLER, majors regime. It does not fit the post-fill dial window unless T ≥ 10 s.

**Two-provider split.** Claude seats and GPT seats run as separate workers with separate
quotas. The JUDGE can be run on both for one read (agreement rate, SKIP-cause mix), then use
one per lane, or both with ADMIT requiring agreement (narrow-only, section 2.3). Each
provider is its own failure domain: one provider down means its seats SKIP and page; the
other keeps running.

### 2.2 Cost per day

Token assumptions: per call, ~1,000 cached system tokens, ~1,200 fact-sheet tokens, ~400
thinking tokens at effort low, ~80 output tokens. Caching only pays above the model's
minimum prefix, so check `usage.cache_read_input_tokens` at the first live call. Prices are
the first-party API rates (claude-api skill, cached 2026-10-06).

| Seat | Model | Calls/day (assumed) | $/call | $/day |
|---|---|---|---|---|
| JUDGE on roster-trigger (H) candidates | Haiku 5.5 | ≤500 (Gamma upper bound from `memecoin_events`) | ~$0.0004 | ~$0.20 |
| JUDGE on universe survivors | Haiku 5.5 | ~2,000 (unmeasured; no live universe feed) | ~$0.0004 | ~$0.80 |
| CRAWLER | Haiku 5.5 | ~500 coins x 4 refresh | ~$0.0006 | ~$1.20 |
| JUDGE escalation (only if Haiku disagreement is shown) | Sonnet 5.5 | ≤100 | ~$0.008 | ≤$0.80 |
| Post-mortem | Sonnet 5.5 Batch | 1 run, ~200k tokens in | — | ~$0.30 |

Total: about $3/day at these volumes, and it scales linearly with survivors. The same load
on Haiku 4.5 would be about $25/day; all-Sonnet about $50/day. Cost is not the constraint.
Seat latency is (2.4). X API cost is unverified and is the one feed that could dominate.

### 2.3 JUDGE contract (DESK lane)

- **One call per candidate. Never batch coins in one call.** `memecoin_radar_triage` sends 15
  coins in one prompt, so one injected ticker can sway the other 14 verdicts.
- **Output is structured output (`output_config.format` json_schema), and nothing else:**

  ```json
  {"k": "<echo of opportunity_id>", "v": "E" | "W" | "S",
   "e": null | [metric, ">=", value, window_s, expiry_s],
   "r": null | [tp_pct, sl_pct, trail_pct | null],
   "c": <reason code int>}
  ```

  This is the compact contract measured in 2.4; the playbook (current `desk-judge-v6`) is the authority
  for enums and ranges. The CLI path returns text, and a strict code validator parses it.
- **Identity.** `k` echoes an immutable `opportunity_id`, never `chain:mint`. A coin can be a
  candidate many times (several roster buys, a universe launch and a later roster buy), and
  each candidacy is its own row.
  - `opportunity_id` = content hash of (trigger kind, chain, mint, trigger transaction
    signature, slot, event ordinal).
  - `cohort_id` names the frozen policy (playbook prompt_version + rail bounds + gates).
  - Tickets, verdicts, fills and the 30-close read key on these two ids. A mismatched echo is
    SKIP `key_mismatch`.
- **Entry condition (W only).** One condition, frozen at the verdict. Code evaluates it on 1 s
  bars. The grammar is closed: `bu`/`tr` with `>=`, window 1-5 s, expiry 10-30 s (playbook desk-judge-v6).
  - Unmet at expiry: SKIP `entry_expired`.
  - A bar that is unknown because of a tape gap never satisfies the condition.
  - **No re-judge:** the candidate is never sent back to the model. A later trigger on the
    same coin is a new opportunity_id.
- **Rails** are checked against operator bounds (min/max for tp, sl and trail). **Out of range
  rejects the whole verdict as SKIP (`rails_out_of_bounds`); code never clamps.** Rails pick
  among live funded exit policies (operator ruling: v1 has no new exit mechanics).
- **NULL kills.** Code checks every required fact before the call; a missing fact is SKIP
  with no model call. A model `null`, a refusal (`stop_reason == "refusal"`), a timeout past
  the deadline, malformed JSON, a key echo that does not match, an out-of-enum value, or a
  `cond` outside the grammar is SKIP. Each cause gets its own counter. **No failure ever maps
  to ENTER.**
- **Facts in, not text.** The fact sheet has numbers, enums and booleans only.
  - Untrusted strings (ticker, name, symbol, URI, description, X/J7 text, handles) go to the
    CRAWLER only.
  - The JUDGE sees the CRAWLER's validated enums: `crawler_intent` plus the boolean
    `crawler_flags` {copycat_ticker, bought_hype_pattern, suspicious_text, rug_history_claim}.
  - **No raw string field exists on the JUDGE sheet**, and code builds the sheet from a typed
    schema that has no free-text slot.
  - The v1-v3 benches put `ticker` on the JUDGE sheet; that was a boundary violation, removed
    in v4.
- **The verdict can only narrow.** ENTER lets the candidate reach the existing hard gates,
  which code re-runs at send time. No verdict raises a cap, a clip, a ceiling or a slippage
  limit.
- **Staleness.** The fact sheet carries its snapshot time and price. At ENTER, code re-reads
  the price. If it moved more than a bounded % since the snapshot (operator knob), the
  verdict is void, recorded as SKIP `stale_on_enter`. The up to 5 s the model takes is
  market time.
- **Persist every call:** input hash (`canonical_input_hash`), model, effort, wall ms, API ms,
  tokens, verdict, entry, rails, and failure cause, including SKIPs. The 30-close read needs
  that denominator.

### 2.4 Latency budget

- **DESK JUDGE.** The hard deadline is 5 s wall clock (operator, was 3 s); past it, the call
  is SKIP `judge_timeout`. A late answer is rejected by its opportunity_id: it is logged as
  late, never acted on, and it never re-opens the candidate. Record p50/p90 of wall ms and API ms per
  model.
- **Measured 2026-10-21, Haiku 5.5 effort low on the subscription, one CLI spawn per call,
  desk-judge-v1 with compact output.**
  - Clean decision, n=20: wall 4.60 / 5.13 s p50/p90, API 3.69 / 4.29 s, output 590 / 676
    tokens.
  - Injected ticker (refused), n=20: wall 2.80 / 3.27 s.
  - About 90% of output tokens are thinking. On the CLI path, thinking cannot be switched off:
    `thinking={'type':'disabled'}` and `max_thinking_tokens=0` are ignored (591 and 616 tokens).
  - The prompt cache hits on its own (cache_read 4,157 tokens per call).
  - The deadline is now 5 s (operator), and p90 sits at that edge.
  - Only a direct API key with thinking disabled would remove those tokens; unmeasured.
- **A long-lived `ClaudeSDKClient` is rejected.** A new `session_id` per call does not isolate
  context: input grew from 1,434 to 57-68k tokens over 50 calls, and 25 of 50 calls returned no
  structured output. Coins bleeding into each other's context is an injection path. One spawn
  per call only.
- **GPT via `codex exec`** measured 5.7-8.0 s per call (2.1c). That is over the deadline, so
  GPT is CHECKER / post-mortem only, never the DESK JUDGE (Alpha, umbrella spec).
- **COPY lane.** No LLM, so no added cost.

### 2.4b Candidate snapshot producer (required before build)

`memecoin_entry_features` cannot feed the JUDGE.
- `scripts/memecoin_entry_capture_loop.py` reads *terminal* entry attempts after the ledger
  commits them. Its Solana T1 worker runs only while the funded book is flat.
- Its rows are post-terminal, so they are not causal facts at decision time, for either H
  (roster) or U (universe) candidates. Verified 2026-10-21 against the loop's docstring and
  `start_t1_worker`; Codex audit.
- The 37/329 flow rows in Gamma's labeled set are pre-entry windows computed afterwards. They
  are fine as labels, and never a live input.

A separate producer builds one immutable snapshot per opportunity_id at decision time. Each
field carries `{value, source, known_at, slot, completeness}`:
- `source`: the exact feed or table;
- `known_at`: when *we* knew it;
- `slot`: the chain position the value reflects;
- `completeness`: `complete`, `partial`, `missing` or `window_not_elapsed`.

Rules:
- The JUDGE sheet is rendered from the snapshot. The snapshot is persisted with the ticket.
- Flow fields come from the gRPC universe tape (§1d), never from a post-terminal table.
- **H evidence.** For a roster trigger the snapshot keeps the confirmed signer, payment and
  acquisition evidence the copy lane already proves (signature, slot, signer = roster wallet,
  payment basis, acquired quantity). That evidence is a fact for the JUDGE only. It grants no
  funded-copy authority to an unarmed wallet and does not change copy-lane arming.

### 2.5 What to keep from `memecoin_radar_triage.py`

Keep:

- the injectable `llm_fn` (tests without network);
- `TriageUnconfigured` vs `TriageProviderError`, because an absent key is not a broken provider;
- the validator: known keys only, enum only, and `(network_id, mint)` matched to code's own
  sheet, so the model never names what the engine acts on;
- persisting verdicts with a model tag.

Drop:

- the urllib OpenAI-compatible shim and the `grok-2-latest` / `gpt-4o-mini` defaults (use the
  Anthropic SDK with the desk tier constants; keep `llm_fn` so a Grok seat stays pluggable);
- the 15-coin batch;
- the 45 s timeout (the desk seat budget is seconds);
- `armed_handles` in the prompt (untrusted text: send the count only);
- the free-text `reason` (replace it with `reason_code`).

Also keep `memecoin_x_llm_worker` / `llm_contract` for the CRAWLER. It already has frozen
snapshots, input hashes, job states, a three-attempt cap, and a prohibited-words filter
(buy/sell/size/safe...). It has never run.

## 3. Prompt-injection boundary

Untrusted inputs: X posts, J7 ticker/name/handle, token metadata (name, symbol, URI), news,
Axiom frames. The rules:

1. **Seats that read untrusted text have no tools and no write path.** Their only output is a
   schema-validated enum record.
2. **The engine executes nothing from a seat except a typed verdict from a fixed enum,**
   mapped through a code table to existing parameters. Mint, pool, chain, size, slippage and
   recipient always come from code's own candidate record, never from model output. A
   mismatched key echo is SKIP.
3. **Untrusted text enters only as quoted data** in the user turn, never in `system`. It is
   length-capped and NFKC-normalized. The cap also limits Unicode-trick payloads.
4. **Two-hop isolation.** Raw text goes to the CRAWLER, which outputs enums. The JUDGE sees
   only the CRAWLER's enums and numbers. An injection must survive two schema walls to reach
   a decision, and even then it can only ADMIT into the existing hard gates.
5. **No seat output changes config, knobs, rosters or caps.** Post-mortem proposals go to the
   crew as text, and the operator decides.

## 4. Pre-registered reads (per increment)

- At 30 closes per lane, compare the LLM-gated arm with the code-only shadow arm on the same
  candidate stream: full-book PnL, fill rate, and SKIP-cause mix.
- Kill the seat if the gated arm's mean per-close PnL is below the shadow's, or if failure
  SKIPs exceed 20% of calls (a broken seat looks like a picky seat).
- The daily loss halt is per lane, in code, and independent of any seat.

## 5. Out of scope here

- Execution engines (FILLS), sizing math, and the risk supervisor: other slices.
- Restarting dormant feeds: each restart is its own increment with its own read.

## 6. Rule-5 edge-case map

1. **Money path: yes.** Seats choose coin admission and the exit dial, so every item below is
   mandatory. This map goes to the operator before any build.
2. **Speed on the fill path.** DESK lane: the JUDGE (hard 5 s deadline) sits before the
   desk decision by operator ruling 61859b83. After ENTER, nothing model-side is on the send
   path, and `stale_on_enter` voids a verdict the market has moved past. COPY lane: no LLM,
   unchanged. No answer means no desk entry (operator).
3. **Concurrency and global state.** Seats run in their own worker (thread or process). One
   writer per verdict store, and the engine reads immutable snapshots. No module attribute,
   env var or shared connection is mutated. Funded tick observers keep
   `get_readonly_connection()`.
4. **Absent vs zero vs unknown.**
   - No verdict = not judged (lane default), not SKIP-by-model; they are counted apart.
   - A None fact is a NULL kill before the call, never sent as 0.
   - A tape window across an outage is None, and the JUDGE gets `unknown`, not 0 volume.
   - A model `null` is SKIP with cause `model_null`.
5. **Dependency failures.** Each is a distinct SKIP cause, never an empty success: timeout,
   429, 5xx, refusal, malformed output, wrong key, unconfigured key, and daily budget
   exhausted (the seat stops and pages once, with persisted page state; trap b6c22073). A
   wrong but valid answer is caught only by the shadow-arm read (section 4). Provider outage
   means the lane runs at its no-verdict default and pages.
6. **Units and identity.**
   - `network_id` is a string chain key (`solana`, `4663`), not the EVM `chain_id` int.
   - EVM mints are compared lower-cased; Solana base58 is compared as is.
   - mcap/liquidity are USD floats; ages are seconds.
   - Verdict TTL is in seconds, on the corrected UTC clock, never host vs J7 `created_at`.
7. **The sample.** The denominator is every candidate that reached the JUDGE, including SKIPs
   and failures. Post-entry facts exclude our own fills (own buys move the coin). The
   shadow arm sees the same candidates, so the comparison is about the seat, not the stream.
8. **Rollback.** Each seat has a kill knob, ON at deploy. Off means the engine runs
   code-only exactly as today, and verdicts already stored expire by TTL. Half-applied
   (seat off, store warm): the engine ignores verdicts once the knob is off, checked at read
   time.

**Runnable check to leave behind (with the first build):** a test that feeds the JUDGE
validator out-of-enum values, a mismatched key, a refusal stop, `null`, a late answer for a
closed opportunity_id, and a W condition outside the grammar. It also asserts the JUDGE sheet
renderer rejects any raw string field. It asserts SKIP with the right cause every time, and that the
engine's acted-on mint equals code's candidate mint. Prove it can fail by mapping one failure
cause to ADMIT.
