# Knowledge Compilation — Design

> Durable synthesized markdown compiled from operational DB state. Not a notes app.

## Decisions locked (owner-approved)

1. **Scope v1:** `knowledge/strategies/{active,retired}/` + `knowledge/wallets/`. Nothing else.
2. **Writer model:** script-compiled from DB (#3). Daily cron + manual rebuild escape hatch.
3. **Retired gate:** only strategies with real runtime history — closed trades ≥ 1 OR `pnl_usdc != 0` OR non-empty `pause_reason`. Abandoned experiments get no page.
4. **Wallet scope:** `followed_wallets ∪ watchlist_wallets`, dedup by wallet, hard cap. Priority: followed first, then watchlist by `added_ts` DESC.
5. **Human header block:** every page preserves a `<!-- HUMAN:START -->...<!-- HUMAN:END -->` region across recompiles. Everything else is script-owned and rewritten on each run.
6. **Incidents:** hand-written only, not in this v1.
7. **Markets:** deferred until world-events lifecycle fix lands and stays healthy for a week.

## Non-goals

- Absorbing HANDOFF.md (lives outside repo at `~/.claude/projects/-home-sniper-sniper/memory/HANDOFF.md`, hardcoded in `scripts/update_handoff.py:24`). Stays as volatile scratch.
- Replacing `docs/`, `MEMORY.md`, Supabase memories, or `index/`. All keep current roles.
- Compiling pages for every `user_analysis` wallet. Curated only.
- Real-time refresh. Daily is fine — these are synthesized, not live.

## Runtime location

Compile runs on **VPS** against live `~/sniper/sniper_tracker.db` + Supabase. Local DB is stale (verified 2026-04-18: local has 1 `strategies` row, VPS per MEMORY.md has 35). Pages get committed back to repo; daily cron then pulls them into agent context naturally via git.

## File layout

```
knowledge/
  strategies/
    active/
      {id:03d}-{strategy_type}.md        e.g. 021-political_no_value_v1.md
    retired/
      {id:03d}-{strategy_type}.md        frozen — written once, never recompiled
  wallets/
    {wallet_address}.md                   e.g. 0x3d49...a8b2.md (full address)
  README.md                               humans-read-first explainer of the layer
```

`{id:03d}` zero-pads for alphabetic sort. Filename stability matters — label/name changes don't rename files.

## Data sources

### Strategy page inputs (VERIFY these tables/columns exist on VPS — local is stale):

| Source | Purpose | Key columns |
|--------|---------|-------------|
| `strategies` | runtime state | `id, wallet, label, strategy_type, status, pause_reason, paused_at, pnl_usdc, trades_triggered, instance_tag, lifecycle_stage, created_at, updated_at` |
| `strategy_research` (joined on `strategies.strategy_type = strategy_research.slug`) | research writeup | `hypothesis, edge_summary, entry_logic, exit_logic, risk_notes, evidence, sample_size` |
| `strategy_positions` (aggregated per `strategy_id`) | live performance | `state, realized_pnl, entry_price, opened_ts, closed_ts` |
| `paper_trades` (filter by `strategy_id` if column exists, else by wallet+condition linking) | paper perf | `entry_price, exit_price, roi, opened_at, closed_at, signal_insider_score` |

**Join risk:** assume `strategies.strategy_type` is not always a `strategy_research.slug` (research notes may not exist for every strategy). Missing research → page still compiles, research section shows `(no research entry yet)`.

### Wallet page inputs:

| Source | Purpose | Key columns |
|--------|---------|-------------|
| `followed_wallets` | membership + operator-provided context | `label, added_at, added_reason, status, total_positions, total_pnl_usdc, last_copied_at, strategy_json, notes` |
| `watchlist_wallets` | membership | `label, added_ts, last_synced_ts` |
| `user_analysis` (LEFT JOIN on `wallet`) | scoring | `insider_score, speculator_score, classification, tier, strategy_group, win_rate, pnl, closed_sample_size, roi_percent, qualifying_pnl` |
| `wallet_profiles` (LEFT JOIN on `wallet`) | Polymarket profile | `username, pseudonym, bio, profile_prediction_count, profile_joined_ts` |
| `followed_positions` (aggregated per `wallet`) | current open book | |

## Page templates

### `knowledge/strategies/active/{id}-{slug}.md`

```markdown
<!-- HUMAN:START -->
<!-- Operator-owned freeform notes. Preserved across recompiles.
     Good uses: current hypothesis, active tuning thoughts, open questions,
     what you're watching for, non-obvious reasons behind the setup. -->
<!-- HUMAN:END -->

# Strategy #{id} — {label}

**Type:** `{strategy_type}`
**Status:** {status}  •  **Lifecycle:** {lifecycle_stage}
**Wallet:** `{wallet}`  •  **Instance tag:** `{instance_tag}`
**Last compiled:** {iso_utc_now}

## Performance (live, from DB)

| Metric | Value |
|--------|-------|
| Paper trades triggered | {trades_triggered} |
| Realized PnL (USDC) | {pnl_usdc:+.2f} |
| Open positions | {open_position_count} |
| Closed positions | {closed_position_count} |
| Closed position PnL | {closed_pnl:+.2f} |
| Win rate (closed) | {win_rate:.1%} ({wins}/{closed_count}) |

## Hypothesis & edge
{hypothesis}

## Entry logic
{entry_logic}

## Exit logic
{exit_logic}

## Risk notes
{risk_notes}

## Evidence
{evidence}  (sample size: {sample_size})

## Recent activity
- Last 5 closed positions (from strategy_positions): {markdown_table}
- Created: {created_at}  •  Updated: {updated_at}

---
*Compiled automatically from `strategies`, `strategy_research`, `strategy_positions` tables. Do not hand-edit outside the HUMAN block — changes will be overwritten.*
```

### `knowledge/strategies/retired/{id}-{slug}.md`

Same template, but:
- Header shows `Retired on: {paused_at}`
- New section: `## Reason retired` (populated from `pause_reason`)
- New section: `## Final lessons` (HUMAN block is the *primary* content here, since the page is frozen)
- Banner at top: `> Frozen snapshot. Not recompiled. Update by hand if lessons evolve.`
- Written once; compile script checks `if file exists: skip`.

### `knowledge/wallets/{wallet}.md`

```markdown
<!-- HUMAN:START -->
<!-- Operator-owned notes. Why this wallet, what edge you suspect, things
     you've noticed that numbers can't show. -->
<!-- HUMAN:END -->

# `{wallet_short}` — {label_or_pseudonym}

**Source list:** {followed | watchlist | both}
**Added:** {added_at}  •  **Added reason:** {added_reason}
**Profile:** {username} @ Polymarket  •  joined {profile_joined_ts}
**Last compiled:** {iso_utc_now}

## Scoring (from user_analysis)
| Metric | Value |
|--------|-------|
| Classification | {classification} |
| Tier | {tier} |
| Insider score | {insider_score:.1f} |
| Speculator score | {speculator_score:.1f} |
| Win rate | {win_rate:.1%} ({wins}/{closed_sample_size}) |
| Strategy group | {strategy_group} |
| Lifetime PnL (Polymarket) | {pnl:+,.0f} |
| ROI | {roi_percent:+.1%} |
| Qualifying PnL | {qualifying_pnl:+,.0f} |

## Copy performance (from followed_wallets, if present)
| Metric | Value |
|--------|-------|
| Positions copied | {total_positions} |
| Copy PnL | {total_pnl_usdc:+.2f} |
| Last copied | {last_copied_at} |

## Profile
{bio}

## Current open book
{followed_positions_summary}

---
*Compiled from `followed_wallets`, `watchlist_wallets`, `user_analysis`, `wallet_profiles`. Do not hand-edit outside the HUMAN block.*
```

## Compile script

`scripts/compile_knowledge.py`

**CLI:**
```bash
python3 scripts/compile_knowledge.py --all                       # default, all pages
python3 scripts/compile_knowledge.py --strategies-only
python3 scripts/compile_knowledge.py --wallets-only
python3 scripts/compile_knowledge.py --target strategies/active/021-political_no_value_v1.md
python3 scripts/compile_knowledge.py --dry-run                   # print what would change
python3 scripts/compile_knowledge.py --commit                    # auto-commit regenerated pages
```

**Behavior:**
1. For each target page, read existing file (if any) and extract HUMAN block between markers.
2. Build new body from DB queries.
3. Splice HUMAN block back into new body.
4. Write atomically via `.tmp` → `os.replace`.
5. If file content byte-identical to previous, skip (don't touch mtime).
6. Retired pages: skip recompile if file exists.
7. `--commit`: stage + commit changed files with message `knowledge: daily recompile {date}`.

**HUMAN block preservation algorithm:**
- Regex match `<!--\s*HUMAN:START\s*-->(.*?)<!--\s*HUMAN:END\s*-->` with DOTALL.
- If markers missing from existing file: log warning, use empty block, don't overwrite.
- Unit-tested explicitly — that's the highest-risk operation (destroying operator notes is unacceptable).

**Schema tolerance:** all queries use `COALESCE`/`IFNULL` for missing columns (e.g. `lifecycle_stage` might not exist on older DBs). Missing-column errors log and skip the field, don't fail the page.

**Wallet cap logic:** `LIMIT 100`; if `followed_wallets` count < 100, fill remainder from watchlist ordered by `added_ts DESC`. Log when cap is hit.

## Cron deployment

New systemd user timer on VPS:

```
# ~/.config/systemd/user/sniper-knowledge-compile.service
[Unit]
Description=Compile Sniper knowledge pages from DB
Wants=sniper-web.service

[Service]
Type=oneshot
WorkingDirectory=/home/sniper/sniper
ExecStart=/home/sniper/sniper/.venv/bin/python3 scripts/compile_knowledge.py --all --commit
StandardOutput=append:/home/sniper/sniper/logs/knowledge_compile.log
StandardError=append:/home/sniper/sniper/logs/knowledge_compile.log

# ~/.config/systemd/user/sniper-knowledge-compile.timer
[Unit]
Description=Daily knowledge page compile

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Auto-commit risk: timer commits on main. Accepted — pages are purely generated, merge conflicts are impossible if script is deterministic. If there's contention, cron runs at 03:30 UTC (pick a quiet window).

## Failure modes to handle explicitly

| Failure | Response |
|---------|----------|
| DB locked during compile | Retry 3x with 5s backoff, then skip this cycle. WAL mode should prevent this. |
| Missing `strategy_research` entry | Compile page with `(no research entry yet)` stub, don't fail. |
| HUMAN markers missing from existing file | Warn, don't overwrite, skip this page. |
| Wallet has no `user_analysis` row | Compile page with scoring section marked "(not yet analyzed)". Don't skip. |
| New retired strategy detected (`status` flipped from active → stopped/paused with meaningful history) | On first detection, write retired page (copying any HUMAN block from the active page), then delete the active page. |
| Wallet removed from both lists | Compile script deletes the page (with git log preserving history). |

## Ship plan (ordered)

1. **Verify schema on VPS** — run `python3 scripts/brain_index.py schema strategies strategy_research strategy_positions followed_wallets watchlist_wallets user_analysis wallet_profiles` and confirm every column referenced in this doc exists. Patch column names if any diverged.
2. **Build `scripts/compile_knowledge.py`** with tests. TDD on the HUMAN block preservation (most-critical). Test file: `tests/test_compile_knowledge.py`.
3. **One-shot manual run on VPS:** `python3 scripts/compile_knowledge.py --dry-run --all` → review output. Then `--all` without `--commit` → hand-inspect first 5 strategy pages + 5 wallet pages. Then `--commit`.
4. **Add `knowledge/README.md`** — one-pager explaining: this tree is compiled, the HUMAN block is yours, retired pages are frozen, everything else gets rewritten daily.
5. **Install cron timer**, enable, verify first auto-run.
6. **Update CLAUDE.md** — point agents at `knowledge/` as the first place to look for strategy/wallet context.

## Verification commands after deploy

```bash
# Pages exist and parse
ls knowledge/strategies/active/ knowledge/strategies/retired/ knowledge/wallets/ | head

# HUMAN markers present in every compiled page
grep -L "HUMAN:START" knowledge/strategies/active/*.md knowledge/wallets/*.md

# Timer is armed
systemctl --user status sniper-knowledge-compile.timer

# Log shows clean last run
tail -30 ~/sniper/logs/knowledge_compile.log
```

## What this replaces / what stays

**Absorbs:** nothing existing gets deleted. This is an additive layer.
**Relieves:** CLAUDE.md agents no longer need to grep `strategies` table state for orientation — they read `knowledge/strategies/active/` instead.
**Does not touch:** Supabase brain, `MEMORY.md`, HANDOFF.md, `docs/`, `index/`.

## Open questions

1. Does the VPS `strategies` table actually expose a clean way to link to `paper_trades` records? If not, the perf table on strategy pages pulls from `strategy_positions` only. Verify before writing the query.
2. Cap of 100 wallets — user said "hard cap"; is 100 right? Current followed+watchlist is ~48. 100 gives 2x headroom before pruning hits. Confirm or pin lower.
3. Retired-page trigger: on first transition from active → retired-eligible, should the script *delete* the active page, or just stop recompiling it? Proposal: delete. Prevents two copies of the same strategy drifting apart.
