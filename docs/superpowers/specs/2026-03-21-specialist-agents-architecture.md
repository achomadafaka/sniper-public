# Specialist Agent Architecture — Design Spec

## Vision

Transform from a generalist system into a team of domain specialists that coordinate through a shared brain. Each specialist deeply understands its domain, owns its strategies, and can query other specialists' knowledge during a single run without waiting for the next timer cycle.

## Current State (as of 2026-03-21)

### Agents running
| Agent | Schedule | Cost/day | What it does |
|-------|----------|----------|-------------|
| World Analyst | every 4h (cron) + every 2h (new timer) | ~$5 | Broad intelligence across all domains |
| Finance Analyst | every 2h (new timer, VPS bot created) | ~$5 | Financial/crypto news → trade ideas |
| Insider Copy Agent | 2x daily (06:00/18:00) | ~$1.70 | Position quality analysis |
| Wallet Scout | 2x daily (09:00/21:00) | ~$1.80 | Wallet discovery + strategy lifecycle |
| Strategy Reviewer | 2x weekly (Wed/Sat 15:00) | ~$0.20 | Plugin performance monitoring |
| Insider Scoring Analyst | weekly (Sun 14:00) | ~$0.50 | Scoring system calibration |
| Brain Health Checker | every 6h | $0 | Deterministic validation |
| Total | | ~$14/day | |

### Paper performance
- 83 wins / 59 losses = 58.5% WR
- PnL: +$385 (wins - losses)
- 49 open positions
- Insider 90+ band: 66.7% WR, +$363 (the real edge)

### Active strategies
- 3 Scout copy strategies (Frank0951, Valued, ProfessionalPunter)
- 5 plugin strategies (Political NO, Sports NO, Sports Line, News Intel, Cross Arb)
- 2 analyst strategies (#28 Geo Intel, #29 Finance Intel — created by VPS bot)
- 2 paused (BTC Momentum, Term Structure)

---

## Target Architecture

### Layer 1: Intelligence (discovers opportunities)

**Geopolitics Analyst** (replaces current World Analyst's geo scope)
- Owns: `world_state/` brain section
- Scope: conflicts, sanctions, elections, alliances, regime changes
- Sources: ACLED, GDELT, Guardian, NYT, 22 geo RSS feeds
- Outputs: world events, market boosts for political markets, geo trade ideas
- Owns strategies: Political NO Value, any future political/geo strategies
- Schedule: every 4h (00, 04, 08, 12, 16, 20 UTC)

**Finance Analyst** (replaces current World Analyst's finance scope)
- Owns: `markets/` brain section
- Scope: Fed/rates, equities, commodities, crypto, macro data
- Sources: Marketaux, NewsData, GNews, finance RSS (MarketWatch, CoinDesk, etc.)
- Outputs: market boosts for financial markets, finance trade ideas
- Owns strategies: crypto/commodity strategies, any future financial strategies
- Schedule: every 4h (02, 06, 10, 14, 18, 22 UTC) — offset 2h from geo

**Key principle**: Neither specialist owns quant strategies (BTC Momentum, Term Structure, Cross Arb) — those are math-based, not domain-knowledge-based. Strategy Reviewer continues to monitor them.

### Layer 2: Cross-Domain Query Tool

`agents/tools/domain_query.py` — CLI tool any agent calls via Bash to query another domain's knowledge.

```bash
# Finance analyst wants geo context on Hormuz
python3 agents/tools/domain_query.py --domain geopolitics --topic "hormuz strait"

# Returns structured data from brain:
# - Latest world_events matching "hormuz"
# - Country profile for Iran (country:IR)
# - Geo analyst's latest observation mentioning Hormuz
# - Freshness timestamp (how old is this data?)
# - Flag if data is >6h stale

# Geo analyst wants market impact of sanctions
python3 agents/tools/domain_query.py --domain finance --topic "oil sanctions"

# Returns:
# - Latest ticker data for OIL
# - Finance analyst's latest observation mentioning oil/sanctions
# - Any finance trade ideas related to oil
# - Market boosts for oil-related condition_ids
```

**How it works** (no LLM call, pure data retrieval, costs $0):
1. Parse `--domain` to determine which brain section to read
2. Parse `--topic` as search keywords
3. Query relevant Supabase data:
   - `world_events` or `memories` for the domain
   - `strategy_agent_observations` filtered by agent type
   - `agent_predictions` for the domain
   - Country profiles if topic matches a country
4. Return structured JSON with all matching data + freshness indicators

### Layer 3: Flags (urgent cross-domain alerts)

When a specialist discovers something that urgently affects another domain:

```bash
# Geo analyst: Hormuz closure affects oil markets
python3 agents/tools/domain_query.py --flag --from geopolitics --to finance \
  --urgency high --message "Hormuz Strait closure Day 22 — oil supply disruption confirmed"

# Finance analyst: sanctions are biting, affects ceasefire timeline
python3 agents/tools/domain_query.py --flag --from finance --to geopolitics \
  --urgency medium --message "Oil at $98, Iran revenue collapsing, may accelerate ceasefire talks"
```

Flags stored as Supabase memories in `flags/` section:
- `flags/geo→finance:hormuz-closure` (knowledge type, expires in 24h)
- Each agent checks `flags/` directed at them in Step 1

### Layer 4: Strategy Ownership

| Strategy | Owner | Why |
|----------|-------|-----|
| Political NO Value | Geopolitics Analyst | Edge = understanding political outcomes |
| Any future political strategies | Geopolitics Analyst | Domain knowledge |
| Strategy #28 Geo Intel | Geopolitics Analyst | Its own trade ideas |
| Sports NO Value | Neither (quant-ish) | Edge = price math, not domain knowledge |
| Sports Line Dislocation | Neither (quant) | Edge = odds comparison |
| BTC Momentum | Neither (quant) | Edge = technical momentum |
| Term Structure | Neither (quant) | Edge = time-value math |
| Cross Arb | Neither (quant) | Edge = price arbitrage |
| News Intel (#22) | Shared | Receives ideas from both analysts |
| Strategy #29 Finance Intel | Finance Analyst | Its own trade ideas |
| Insider copy strategies (#25-27) | Wallet Scout | Wallet lifecycle |

**What "owning" means:**
- Can pause/flag the strategy (via action_tool)
- Recommends parameter changes with domain evidence
- Reviews strategy performance in their run
- Does NOT directly modify strategy params (Strategy Reviewer is the auditor)

### Layer 5: Execution (unchanged)
- Paper Trader — reads market_boosts, executes insider copy trades
- Strategy Runner — runs all plugin strategies
- News Intel Plugin — enters trade-idea-driven positions (now with source_agent filter)

### Layer 6: Quality (unchanged)
- Insider Scoring Analyst — calibrates paper trader scoring
- Strategy Reviewer — audits ALL strategies (second pair of eyes)
- Brain Health Checker — system-wide validation

---

## Brain Structure (updated)

```
BRAIN (Supabase)
├── world_state/          → Geopolitics Analyst WRITES
│   ├── world_state         (narrative intelligence)
│   ├── market_boosts_current (geo-relevant boosts only)
│   └── world_events table  (structured events)
│
├── markets/              → Finance Analyst WRITES
│   ├── markets/state       (financial narrative)
│   ├── markets/boosts      (finance-relevant boosts)
│   └── markets/tickers     (latest price snapshots)
│
├── flags/                → Any specialist WRITES, target READS
│   ├── flags/geo→finance:*
│   └── flags/finance→geo:*
│
├── portfolio/            → System sync-state (every 30min)
├── wallets/              → Wallet Scout
├── signals/              → Insider Scoring Analyst
├── strategies/           → Strategy Performance Reviewer
├── predictions/          → All agents
├── health/               → Brain Health Checker
├── knowledge/            → Static + country profiles
└── meta/                 → Human preferences
```

---

## Testing Strategy

### What we DON'T test today (and should)

1. **Price flow end-to-end**: trade idea → news_intel plugin → strategy_position. Verify price is correct for BUY_YES and BUY_NO at every step.
2. **Agent output validation**: After an agent runs, verify it wrote to the correct brain section and produced observations.
3. **Cross-domain query**: domain_query.py returns correct data from the right brain section.
4. **Flag lifecycle**: Flag created → agent reads it → flag expires.
5. **Strategy ownership boundaries**: Domain expert can pause its owned strategy but not others.

### New test files needed
- `tests/test_domain_query.py` — domain_query tool returns correct data
- `tests/test_price_flow_e2e.py` — BUY_YES and BUY_NO price correctness end-to-end
- `tests/test_agent_brain_sections.py` — agents write to correct sections, don't write to others'
- `tests/test_flags.py` — flag creation, reading, expiry

---

## Scheduling (resource-constrained, staggered)

```
UTC   Agent                     Type            Frequency
00:00 Geopolitics Analyst       Intelligence    every 4h
00:30 Brain Health              Quality         every 6h
01:00 (gap)
02:00 Finance Analyst           Intelligence    every 4h
02:30 News Intel pipeline       Execution       every 2h
03:00 (gap)
04:00 Geopolitics Analyst       Intelligence    every 4h
04:30 (gap)
05:00 Sync-state                System          every 30min
06:00 Insider Copy Agent        Quality         2x daily
06:30 Brain Health              Quality         every 6h
07:00 (gap)
08:00 Geopolitics Analyst       Intelligence    every 4h
08:30 News Intel pipeline       Execution       every 2h
09:00 Wallet Scout              Execution       2x daily
10:00 Finance Analyst           Intelligence    every 4h
11:00 (gap)
12:00 Geopolitics Analyst       Intelligence    every 4h
12:30 Brain Health              Quality         every 6h
13:00 (gap)
14:00 Finance Analyst           Intelligence    every 4h
14:30 News Intel pipeline       Execution       every 2h
15:00 Strategy Reviewer         Quality         2x weekly
16:00 Geopolitics Analyst       Intelligence    every 4h
17:00 (gap)
18:00 Insider Copy Agent        Quality         2x daily
18:30 Brain Health              Quality         every 6h
19:00 (gap)
20:00 Geopolitics Analyst       Intelligence    every 4h
20:30 News Intel pipeline       Execution       every 2h
21:00 Wallet Scout              Execution       2x daily
22:00 Finance Analyst           Intelligence    every 4h
23:00 (gap)

Weekly additions:
Sun 14:00 Insider Scoring Analyst  Quality      weekly
Wed/Sat 15:00 Strategy Reviewer    Quality      2x weekly

Daily cost: ~$14-16/day
```

---

## Implementation Order

### Phase 1: Foundation (build before specialists)
1. Build `agents/tools/domain_query.py` — cross-domain query tool
2. Add flag mechanism to domain_query.py
3. Write end-to-end price flow tests
4. Write agent brain section tests

### Phase 2: Specialize the analysts
5. Refactor World Analyst → Geopolitics Analyst (narrower scope, deeper analysis)
6. Wire Finance Analyst properly (fix import error, test, add brain section)
7. Split `market_boosts_current` into geo boosts + finance boosts
8. Update scheduling to staggered timeline

### Phase 3: Strategy ownership
9. Give Geopolitics Analyst ownership of Political NO Value
10. Give Finance Analyst ownership of its trade idea strategies
11. Update Strategy Reviewer to respect ownership boundaries
12. Test ownership constraints

### Phase 4: Real money preparation (future)
13. Separate wallet architecture per strategy group
14. Risk management framework per wallet
15. Real money gate criteria and monitoring

---

## Cost projection

| Item | Current | After specialist split |
|------|---------|----------------------|
| Geo Analyst (6x/day) | $5.10 (World Analyst) | $5.10 |
| Finance Analyst (6x/day) | $0 (new) | $5.10 |
| Insider Copy (2x/day) | $1.70 | $1.70 |
| Wallet Scout (2x/day) | $1.80 | $1.80 |
| Strategy Reviewer (2x/week) | $0.20 | $0.20 |
| Insider Scoring (1x/week) | $0.50 | $0.50 |
| Brain Health (4x/day) | $0 | $0 |
| News Intel pipeline | $0 | $0 |
| **Total** | **~$9/day** | **~$14/day** |

Increase: +$5/day ($150/month) for a dedicated finance specialist with 60 focused turns per run.
