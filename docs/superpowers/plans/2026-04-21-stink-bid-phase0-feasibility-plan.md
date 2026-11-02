# Stink-Bid Backtest Phase 0 — Feasibility Research Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a feasibility memo that answers go/no-go on scientific-rigor stink-bid backtest before any infrastructure is built.

**Architecture:** Pure research tasks. Seven independent probes (0.1–0.7 from the spec) each produce a standalone markdown artifact. Final task synthesizes artifacts into a go/no-go feasibility memo. Lightweight throwaway Python scripts for API probes live under `scripts/feasibility/` — these are NOT production code.

**Tech Stack:** Python 3 + `requests` for API probing, shell for git/file ops, markdown for artifacts. No new dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-21-stink-bid-scientific-backtest-design.md` sections 4.4 (economic threshold), 6.1 (fill realism), 9 (parameter grid), 10 Phase 0 task list (0.1–0.7).

**Locked context from spec:**
- Economic significance threshold = 2× modeled transaction costs
- 4 strata: ATP main / WTA main / Challenger / ALL
- Target detection: 2pp EV at 80% power, α=0.0125 (Bonferroni-4)
- Reference wallet for sampling: `0x56687bf447db6ffa42ffe2204a05edaa20f55839` (top lb_pnl $22M) — already confirmed has historical activity via probe earlier in session
- Grid size in section 9: 518,400 configs per stratum
- DuckDB snapshot storage location when Phase 1 starts: `data/backtest/stink_bid/`

---

## File Structure

**Created by this plan:**
- `docs/superpowers/research/phase0/` — per-task artifact directory
- `docs/superpowers/research/phase0/0.1-polymarket-fees.md` — fee + spread research
- `docs/superpowers/research/phase0/0.2-kalshi-comparison.md` — venue comparison
- `docs/superpowers/research/phase0/0.3-sackmann-audit.md` — Sackmann data coverage
- `docs/superpowers/research/phase0/0.4-gamma-tennis-universe.md` — Polymarket tennis market counts
- `docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl` — raw paginated Gamma responses (one event per line)
- `docs/superpowers/research/phase0/0.5-prices-history-depth.md` — price-history audit
- `docs/superpowers/research/phase0/0.5-price-samples.jsonl` — raw price histories for audited markets
- `docs/superpowers/research/phase0/0.6-power-analysis.md` — statistical power computation
- `docs/superpowers/research/phase0/0.7-min-viable-size.md` — break-even position-size
- `docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md` — synthesis + go/no-go
- `scripts/feasibility/probe_gamma_tennis.py` — Gamma paginator probe
- `scripts/feasibility/probe_prices_history.py` — /prices-history sampler
- `scripts/feasibility/probe_polymarket_spread.py` — bid-ask spread sampler
- `scripts/feasibility/sackmann_audit.py` — Sackmann coverage audit
- `scripts/feasibility/power_analysis.py` — N computation

**Not touched:** no production code modified. Live strategy 41 keeps running as-is.

---

## Task 1: Set up research workspace

**Files:**
- Create: `docs/superpowers/research/phase0/.gitkeep`
- Create: `scripts/feasibility/.gitkeep`

- [ ] **Step 1: Create directories**

```bash
mkdir -p docs/superpowers/research/phase0 scripts/feasibility
touch docs/superpowers/research/phase0/.gitkeep scripts/feasibility/.gitkeep
```

- [ ] **Step 2: Commit scaffolding**

```bash
git add docs/superpowers/research/phase0/.gitkeep scripts/feasibility/.gitkeep
git commit -m "scaffold: phase 0 feasibility research workspace"
```

---

## Task 2: 0.1 Polymarket fee schedule + bid-ask spread research

**Files:**
- Create: `scripts/feasibility/probe_polymarket_spread.py`
- Create: `docs/superpowers/research/phase0/0.1-polymarket-fees.md`

- [ ] **Step 1: Write the spread-probe script**

```python
# scripts/feasibility/probe_polymarket_spread.py
"""Sample bid-ask spread on 10 live tennis markets.

Writes results as JSONL to stdout. For each market:
  - condition_id, title, best_bid, best_ask, spread_abs, spread_pct, mid
"""
import json
import sys
import time

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


def fetch_active_tennis_events(limit=20):
    r = requests.get(
        GAMMA,
        params={
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
            "limit": limit,
        },
        timeout=15,
    )
    r.raise_for_status()
    events = r.json()
    out = []
    for e in events:
        tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
                for t in (e.get("tags") or [])]
        if "tennis" not in tags:
            continue
        for m in (e.get("markets") or []):
            cid = m.get("conditionId")
            slug = m.get("slug") or ""
            event_slug = (e.get("slug") or "").strip()
            if cid and slug == event_slug:  # H2H moneyline
                try:
                    token_ids = json.loads(m.get("clobTokenIds", "[]"))
                except Exception:
                    continue
                if token_ids:
                    out.append({
                        "condition_id": cid,
                        "title": e.get("title"),
                        "token_id": token_ids[0],
                    })
                break
        if len(out) >= 10:
            break
    return out


def fetch_book(token_id):
    try:
        r = requests.get(CLOB_BOOK, params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def main():
    markets = fetch_active_tennis_events(limit=30)
    for m in markets:
        book = fetch_book(m["token_id"])
        bids = book.get("bids", []) if isinstance(book, dict) else []
        asks = book.get("asks", []) if isinstance(book, dict) else []
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        spread_abs = (best_ask - best_bid) if (best_bid and best_ask) else None
        mid = ((best_bid + best_ask) / 2) if (best_bid and best_ask) else None
        spread_pct = (spread_abs / mid * 100) if (spread_abs and mid) else None
        print(json.dumps({
            "condition_id": m["condition_id"],
            "title": m["title"],
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_abs": spread_abs,
            "spread_pct": spread_pct,
            "mid": mid,
        }))
        time.sleep(0.2)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the spread probe and capture output**

```bash
python3 scripts/feasibility/probe_polymarket_spread.py > /tmp/spread_samples.jsonl
cat /tmp/spread_samples.jsonl
```
Expected: 5–10 lines of JSON, each with `best_bid`, `best_ask`, `spread_abs`, `spread_pct`.

- [ ] **Step 3: Research Polymarket fee schedule**

Read these sources, capture findings in section 2 of the artifact below:
- https://docs.polymarket.com/ (search "fees")
- https://docs.polymarket.com/developers/resources/fees (canonical)
- Polymarket governance / forum posts announcing fee changes (search: `site:forum.polymarket.com fees`)
- GitHub Issues on `polymarket/docs` or `polymarket/clob-client` referencing fee changes

Find: current fee rate (maker/taker), historical fee rate at key epochs, gas cost per trade on Polygon (approximate).

- [ ] **Step 4: Write the 0.1 artifact**

```markdown
# 0.1 Polymarket Fee Schedule + Bid-Ask Spread Research

**Date:** 2026-04-21
**Spec section:** 4.4 (economic significance threshold), 6.1 (spread haircut), 6.6 (per-epoch fees), 10 Phase 0.1

## 1. Current fee schedule

| Side | Rate | Source |
|---|---|---|
| Maker | [X]% | [doc url] |
| Taker | [Y]% | [doc url] |
| Gas (per tx) | [Z] USDC equivalent | Polygon gas history |

## 2. Historical fee schedule (epoch timeline)

| Epoch start | Epoch end | Maker rate | Taker rate | Reason | Source |
|---|---|---|---|---|---|
| [date] | [date] | [X]% | [Y]% | [governance vote / launch / migration] | [url] |

## 3. Observed bid-ask spread (live sample, 10 tennis markets)

Raw data: `0.1-spread-samples.jsonl` (copy of /tmp/spread_samples.jsonl)

| Stat | Value |
|---|---|
| Markets sampled | N |
| Median spread (¢) | [...] |
| Median spread (% of mid) | [...] |
| 90th percentile spread | [...] |
| Median mid price | [...] |
| Empty books (no bids/asks) | N |

## 4. Implications for backtest

- **Fill-realism haircut:** modeled spread = [median]% of notional per leg (entry + exit = 2× haircut)
- **Fee modeling:** use per-epoch lookup table (columns in section 2)
- **Economic significance threshold for H1:** 2× (fee + spread + gas) = [X]% per round-trip
- **Data completeness:** [complete / gaps / missing epochs — describe]

## 5. Open questions
[List anything unresolved]
```

Fill every bracketed field with actual research findings. No `[TBD]` or `[X]` left.

- [ ] **Step 5: Copy spread samples into artifacts dir**

```bash
cp /tmp/spread_samples.jsonl docs/superpowers/research/phase0/0.1-spread-samples.jsonl
```

- [ ] **Step 6: Commit**

```bash
git add scripts/feasibility/probe_polymarket_spread.py docs/superpowers/research/phase0/0.1-polymarket-fees.md docs/superpowers/research/phase0/0.1-spread-samples.jsonl
git commit -m "research(phase0.1): polymarket fees + bid-ask spread"
```

---

## Task 3: 0.2 Kalshi comparison

**Files:**
- Create: `docs/superpowers/research/phase0/0.2-kalshi-comparison.md`

- [ ] **Step 1: Research Kalshi tennis offerings**

Visit:
- https://kalshi.com/markets — search "tennis"
- https://kalshi.com/docs (search for fees)
- https://trading-api.readme.io/ (Kalshi API docs, fee schedule section)

Capture:
- Does Kalshi list tennis H2H matches? How many currently open?
- Fee schedule (maker/taker)
- Typical liquidity on sampled tennis contract (sample 5 markets if available)

- [ ] **Step 2: Write the 0.2 artifact**

```markdown
# 0.2 Kalshi vs Polymarket Venue Comparison

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.2

## 1. Tennis contract availability

| Venue | Live tennis contracts (today) | H2H match format | Futures/outrights |
|---|---|---|---|
| Polymarket | [N] | Yes | Yes |
| Kalshi | [N] | [Yes/No] | [Yes/No] |

## 2. Fee comparison

| Side | Polymarket | Kalshi |
|---|---|---|
| Maker | [%] | [%] |
| Taker | [%] | [%] |
| Gas / network | [approx] | [approx / none if off-chain] |

## 3. Liquidity sample (5 tennis markets each if available)

| Venue | Median 24h volume | Median spread (%) | Median depth at best bid |
|---|---|---|---|
| Polymarket | [...] | [...] | [...] |
| Kalshi | [...] | [...] | [...] |

## 4. Decision

**Venue for stink-bid v1 backtest:** [Polymarket / Kalshi]

**Rationale:** [2-3 sentences — typically Polymarket wins on fees if 0% currently AND tennis liquidity is adequate]

## 5. Kalshi as fallback?
[Note whether Kalshi is a viable fallback if Polymarket data depth is shallow]
```

Fill every bracketed field.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/research/phase0/0.2-kalshi-comparison.md
git commit -m "research(phase0.2): kalshi comparison + venue decision"
```

---

## Task 4: 0.3 Sackmann data audit

**Files:**
- Create: `scripts/feasibility/sackmann_audit.py`
- Create: `docs/superpowers/research/phase0/0.3-sackmann-audit.md`

- [ ] **Step 1: Clone Sackmann repos into local cache**

```bash
mkdir -p data/feasibility_cache
git clone --depth 1 https://github.com/JeffSackmann/tennis_atp.git data/feasibility_cache/tennis_atp
git clone --depth 1 https://github.com/JeffSackmann/tennis_wta.git data/feasibility_cache/tennis_wta
```
Expected: `atp_matches_2024.csv`, `atp_matches_2025.csv`, `atp_matches_2026.csv`, `wta_matches_2024.csv`, `wta_matches_2025.csv`, `wta_matches_2026.csv` present in respective dirs.

- [ ] **Step 2: Write the audit script**

```python
# scripts/feasibility/sackmann_audit.py
"""Audit Sackmann data coverage for 2024-2026 ATP + WTA.

Reports:
  - Total match count per year per tour
  - Field completeness: winner_name, loser_name, score, minutes, surface, tourney_level
  - Per tourney_level counts
  - Per surface counts
"""
import csv
import os
import sys
from collections import Counter

ROOT = "data/feasibility_cache"
YEARS = [2024, 2025, 2026]
TOURS = [("atp", "tennis_atp"), ("wta", "tennis_wta")]
FIELDS = ["winner_name", "loser_name", "score", "minutes", "surface", "tourney_level"]


def audit(tour_code, dir_name):
    total = 0
    field_nonempty = Counter()
    by_level = Counter()
    by_surface = Counter()
    by_year = Counter()
    for year in YEARS:
        path = os.path.join(ROOT, dir_name, f"{tour_code}_matches_{year}.csv")
        if not os.path.exists(path):
            print(f"  MISSING: {path}", file=sys.stderr)
            continue
        with open(path) as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                total += 1
                by_year[year] += 1
                for fld in FIELDS:
                    if row.get(fld, "").strip():
                        field_nonempty[fld] += 1
                by_level[row.get("tourney_level", "?")] += 1
                by_surface[row.get("surface", "?")] += 1
    print(f"\n== {tour_code.upper()} ==")
    print(f"Total matches 2024-2026: {total}")
    print(f"By year: {dict(sorted(by_year.items()))}")
    print("Field completeness:")
    for fld in FIELDS:
        pct = (field_nonempty[fld] / total * 100) if total else 0
        print(f"  {fld}: {field_nonempty[fld]} ({pct:.1f}%)")
    print("By tourney_level:")
    for lvl, cnt in by_level.most_common():
        print(f"  {lvl}: {cnt}")
    print("By surface:")
    for surf, cnt in by_surface.most_common():
        print(f"  {surf}: {cnt}")


def main():
    for code, dirname in TOURS:
        audit(code, dirname)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the audit**

```bash
python3 scripts/feasibility/sackmann_audit.py > /tmp/sackmann_audit.txt
cat /tmp/sackmann_audit.txt
```
Expected: output for ATP and WTA with total counts, year breakdown, field %s, level/surface counts.

- [ ] **Step 4: Sample alignment test with 10 known Polymarket tennis markets**

Pull 10 recent closed Polymarket tennis events (from active period; use Gamma). For each, extract player surnames + match date. Then search Sackmann CSVs for matches with matching (winner_name OR loser_name) + date within ±2 days. Record match/no-match.

Write a short script or do it manually (10 samples is tractable manually). Example query:

```python
# Pseudo-code for one market:
# Polymarket title: "Madrid Open: Tatjana Maria vs Laura Samson" endDate 2026-04-28
# Sackmann lookup:
#   grep "Maria" wta_matches_2026.csv | grep "Samson" | check date proximity
```

Record result: N of 10 aligned successfully.

- [ ] **Step 5: Write the 0.3 artifact**

```markdown
# 0.3 Sackmann Data Audit

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.3, fidelity item #5 (ESPN gate via Sackmann)

## 1. Coverage summary (2024-2026)

| Tour | Total matches | 2024 | 2025 | 2026 (YTD) |
|---|---|---|---|---|
| ATP | [N] | [n] | [n] | [n] |
| WTA | [N] | [n] | [n] | [n] |

## 2. Field completeness

| Field | ATP % non-empty | WTA % non-empty |
|---|---|---|
| winner_name | [%] | [%] |
| loser_name | [%] | [%] |
| score | [%] | [%] |
| minutes (duration) | [%] | [%] |
| surface | [%] | [%] |
| tourney_level | [%] | [%] |

## 3. Distribution by tournament level

| Level | ATP count | WTA count |
|---|---|---|
| G (Grand Slam) | ... | ... |
| M (Masters 1000 / WTA 1000) | ... | ... |
| A (ATP 500 / WTA 500) | ... | ... |
| ... | | |

## 4. Distribution by surface

| Surface | ATP count | WTA count |
|---|---|---|
| Hard | ... | ... |
| Clay | ... | ... |
| Grass | ... | ... |
| Carpet | ... | ... |

## 5. Polymarket-to-Sackmann alignment test (10 samples)

| # | Polymarket market | Sackmann match found | Notes |
|---|---|---|---|
| 1 | [title / date] | yes / no | [e.g., unicode Ż) |
| 2 | ... | | |
| 10 | ... | | |

**Alignment success rate:** [N / 10]

## 6. Gate-simulation feasibility

- **Set scores available:** [Y/N from `score` field — it's typically "6-3 6-4" format for bo3, encodes per-set winner]
- **Duration available:** [N% of matches have `minutes` field populated — target >80% for reliable timing proxy]
- **Surface/level available:** [N% — target >95%]

## 7. Go/no-go on (c) primary gate option

**Decision:** [Go / no-go / partial — use Sackmann for X but fall back to price-proxy for Y]

## 8. Open questions
[anything unresolved]
```

- [ ] **Step 6: Commit**

```bash
git add scripts/feasibility/sackmann_audit.py docs/superpowers/research/phase0/0.3-sackmann-audit.md
git commit -m "research(phase0.3): sackmann data audit + polymarket alignment test"
```

Note: `data/feasibility_cache/` is gitignored (add to .gitignore if not already) to avoid committing the Sackmann CSVs.

- [ ] **Step 7: Ensure feasibility cache gitignored**

```bash
grep -q "^data/feasibility_cache/" .gitignore || echo "data/feasibility_cache/" >> .gitignore
git add .gitignore 2>/dev/null || true
git commit -m "chore: gitignore feasibility cache" 2>/dev/null || true
```

---

## Task 5: 0.4 Polymarket Gamma tennis market universe

**Files:**
- Create: `scripts/feasibility/probe_gamma_tennis.py`
- Create: `docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl`
- Create: `docs/superpowers/research/phase0/0.4-gamma-tennis-universe.md`

- [ ] **Step 1: Write the paginator probe**

```python
# scripts/feasibility/probe_gamma_tennis.py
"""Paginate Polymarket Gamma /events?closed=true&tag=tennis, dump all tennis events.

Writes JSONL to stdout. Each line is one tennis-tagged event (full JSON).
"""
import json
import sys
import time

import requests

GAMMA = "https://gamma-api.polymarket.com/events"
PAGE_LIMIT = 500


def is_tennis(event):
    tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
            for t in (event.get("tags") or [])]
    return "tennis" in tags


def main():
    offset = 0
    total = 0
    tennis_count = 0
    while True:
        r = requests.get(
            GAMMA,
            params={
                "closed": "true",
                "limit": PAGE_LIMIT,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
            },
            timeout=20,
        )
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        for e in page:
            total += 1
            if is_tennis(e):
                tennis_count += 1
                print(json.dumps(e))
        print(f"[probe] offset={offset} page_size={len(page)} tennis_in_page={sum(1 for e in page if is_tennis(e))} total_events_seen={total} tennis_total={tennis_count}", file=sys.stderr)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        time.sleep(0.3)
    print(f"[probe] DONE total_events={total} tennis={tennis_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the paginator (may take minutes)**

```bash
python3 scripts/feasibility/probe_gamma_tennis.py \
    > docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl \
    2> /tmp/gamma_probe.log
tail -5 /tmp/gamma_probe.log
wc -l docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl
```
Expected: log ends with `DONE total_events=N tennis=M`. File has M lines.

**If this exits early or hits rate limits:** resume by editing `offset` in the script to the last successful offset + PAGE_LIMIT.

- [ ] **Step 3: Analyze the dump — counts by year and tour**

```bash
python3 << 'EOF'
import json
from collections import Counter

by_year = Counter()
by_tour_guess = Counter()
n_with_outcomeprice = 0
n_with_enddate = 0
n_with_conditionid = 0
total = 0

with open("docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl") as f:
    for line in f:
        e = json.loads(line)
        total += 1
        end_date = (e.get("endDate") or "")[:4]
        if end_date:
            by_year[end_date] += 1
            n_with_enddate += 1

        # Tour guess from tags or title
        tags = [t.get("slug", "") if isinstance(t, dict) else str(t) for t in (e.get("tags") or [])]
        title = (e.get("title") or "").lower()
        if "wta" in tags or "wta" in title:
            by_tour_guess["wta"] += 1
        elif "atp" in tags or "atp" in title:
            by_tour_guess["atp"] += 1
        elif "itf" in tags or "challenger" in tags or "challenger" in title or "qualifier" in title:
            by_tour_guess["challenger_or_itf"] += 1
        else:
            by_tour_guess["unknown"] += 1

        # Completeness: check first market
        if (e.get("markets") or [{}])[0].get("conditionId"):
            n_with_conditionid += 1
        for m in (e.get("markets") or []):
            if m.get("outcomePrices"):
                n_with_outcomeprice += 1
                break

print(f"total tennis events: {total}")
print(f"  with endDate: {n_with_enddate} ({n_with_enddate/total*100:.1f}%)")
print(f"  with ≥1 market conditionId: {n_with_conditionid} ({n_with_conditionid/total*100:.1f}%)")
print(f"  with ≥1 market outcomePrices: {n_with_outcomeprice} ({n_with_outcomeprice/total*100:.1f}%)")
print(f"by year: {dict(sorted(by_year.items()))}")
print(f"by tour guess: {dict(by_tour_guess)}")
EOF
```
Capture the output — it goes into the artifact.

- [ ] **Step 4: Write the 0.4 artifact**

```markdown
# 0.4 Polymarket Gamma Tennis Market Universe

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.4, fidelity item #7 (survivorship)

## 1. Pagination result

- Total tennis-tagged closed events pulled: [N]
- Pagination shape: [M] pages × 500 per page
- API rate limits hit: [Y/N + handling]
- Dump artifact: `0.4-gamma-tennis-events.jsonl` ([N] lines)

## 2. Counts by year (based on endDate)

| Year | Event count |
|---|---|
| 2026 | [n] |
| 2025 | [n] |
| 2024 | [n] |
| 2023 | [n] |
| earlier | [n] |

## 3. Counts by tour guess

| Tour | Event count |
|---|---|
| ATP | [n] |
| WTA | [n] |
| Challenger / ITF | [n] |
| Unknown (tag/title ambiguous) | [n] |

Note: tour classification needs refinement in Phase 1. This is a rough count for feasibility.

## 4. Field completeness

| Field | % populated |
|---|---|
| endDate | [%] |
| conditionId on first market | [%] |
| outcomePrices on ≥1 market | [%] |
| tags.slug == "tennis" | 100% (filter criterion) |

## 5. Implications for stratification

Target strata (from spec 6.10): ATP main / WTA main / Challenger / ALL.

With [N ATP / N WTA / N Challenger] events per stratum across 2024–2026, we have [sufficient / borderline / insufficient] data for Phase 0.6 power analysis.

## 6. Open questions
[any unresolved]
```

- [ ] **Step 5: Commit**

```bash
git add scripts/feasibility/probe_gamma_tennis.py docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl docs/superpowers/research/phase0/0.4-gamma-tennis-universe.md
git commit -m "research(phase0.4): gamma tennis market universe pull + counts"
```

---

## Task 6: 0.5 /prices-history depth audit

**Files:**
- Create: `scripts/feasibility/probe_prices_history.py`
- Create: `docs/superpowers/research/phase0/0.5-price-samples.jsonl`
- Create: `docs/superpowers/research/phase0/0.5-prices-history-depth.md`

- [ ] **Step 1: Write the price-history sampler**

```python
# scripts/feasibility/probe_prices_history.py
"""Sample /prices-history for 10 historical tennis markets — audit depth and granularity.

Input: docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl
Output: JSONL to stdout, one line per sampled market with:
  - condition_id, token_id, end_date, title
  - fidelity_60 sample: n_points, first_ts, last_ts, gap_stats
  - fidelity_1 sample: n_points or "unsupported"
"""
import json
import random
import sys
import time
from datetime import datetime, timezone

import requests

CLOB = "https://clob.polymarket.com/prices-history"
INPUT = "docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl"


def iso_to_ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch_history(token_id, start_ts, end_ts, fidelity):
    try:
        r = requests.get(
            CLOB,
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": fidelity,
            },
            timeout=20,
        )
        if not r.ok:
            return {"error": f"HTTP {r.status_code}", "body": r.text[:200]}
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def pick_sample(path, n=10):
    events = []
    with open(path) as f:
        for line in f:
            e = json.loads(line)
            end_date = e.get("endDate")
            markets = e.get("markets") or []
            if not end_date or not markets:
                continue
            first = markets[0]
            cid = first.get("conditionId")
            event_slug = e.get("slug", "")
            if first.get("slug") != event_slug:
                continue
            try:
                tids = json.loads(first.get("clobTokenIds", "[]"))
            except Exception:
                continue
            if not tids:
                continue
            events.append({
                "condition_id": cid,
                "token_id": tids[0],
                "end_date": end_date,
                "title": e.get("title", ""),
            })
    random.seed(42)
    random.shuffle(events)
    return events[:n]


def summarize(data):
    if not data or "error" in data:
        return {"error": data.get("error", "unknown")}
    hist = data.get("history") or []
    if not hist:
        return {"n_points": 0}
    ts = [p.get("t") for p in hist if p.get("t")]
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    return {
        "n_points": len(hist),
        "first_ts": min(ts) if ts else None,
        "last_ts": max(ts) if ts else None,
        "median_gap_sec": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "max_gap_sec": max(gaps) if gaps else None,
    }


def main():
    samples = pick_sample(INPUT, n=10)
    for s in samples:
        end_ts = iso_to_ts(s["end_date"])
        if not end_ts:
            continue
        start_ts = end_ts - 86400 * 7  # 7 days before end

        f60 = fetch_history(s["token_id"], start_ts, end_ts, fidelity=60)
        time.sleep(0.2)
        f1 = fetch_history(s["token_id"], start_ts, end_ts, fidelity=1)
        time.sleep(0.2)

        print(json.dumps({
            "condition_id": s["condition_id"],
            "token_id": s["token_id"],
            "title": s["title"],
            "end_date": s["end_date"],
            "start_ts": start_ts,
            "end_ts": end_ts,
            "fidelity_60": summarize(f60),
            "fidelity_1": summarize(f1),
        }))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the sampler**

```bash
python3 scripts/feasibility/probe_prices_history.py > docs/superpowers/research/phase0/0.5-price-samples.jsonl
cat docs/superpowers/research/phase0/0.5-price-samples.jsonl
```
Expected: 10 lines of JSON, each with `fidelity_60` summary and `fidelity_1` summary.

- [ ] **Step 3: Tally results**

```bash
python3 << 'EOF'
import json
from statistics import median, mean

f60_points = []
f1_supported = 0
f1_points = []
f60_gaps_median = []
f60_gaps_max = []

with open("docs/superpowers/research/phase0/0.5-price-samples.jsonl") as f:
    for line in f:
        r = json.loads(line)
        n60 = r["fidelity_60"].get("n_points", 0)
        if n60:
            f60_points.append(n60)
            mg = r["fidelity_60"].get("median_gap_sec")
            xg = r["fidelity_60"].get("max_gap_sec")
            if mg: f60_gaps_median.append(mg)
            if xg: f60_gaps_max.append(xg)
        n1 = r["fidelity_1"].get("n_points", 0)
        if "error" not in r["fidelity_1"] and n1 > 0:
            f1_supported += 1
            f1_points.append(n1)

def summ(xs, label):
    if not xs:
        return f"  {label}: no data"
    return f"  {label}: n={len(xs)} min={min(xs)} max={max(xs)} median={median(xs):.0f} mean={mean(xs):.0f}"

print("fidelity=60 (1-min bars):")
print(summ(f60_points, "n_points per 7d window"))
print(summ(f60_gaps_median, "median gap (sec)"))
print(summ(f60_gaps_max, "max gap (sec)"))
print()
print(f"fidelity=1 (1-sec bars): supported in {f1_supported}/10 samples")
print(summ(f1_points, "n_points per 7d window (when supported)"))
EOF
```

Capture output for artifact.

- [ ] **Step 4: Write the 0.5 artifact**

```markdown
# 0.5 /prices-history Depth Audit

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.5, fidelity item #3 (price granularity)

## 1. Sample selection

- Sampled: 10 random historical tennis markets from the 0.4-gamma-tennis-events dump (seed=42)
- Query window: 7 days before each market's `endDate`
- Raw sample dump: `0.5-price-samples.jsonl`

## 2. Fidelity=60 (1-min bars) availability

| Stat | Value |
|---|---|
| Samples with data | [N / 10] |
| Median points per 7d window | [...] |
| Median bar gap (sec) | [...] |
| Max bar gap (sec) | [...] |

Interpretation: [1-min is the standard granularity / gaps concentrated in [...] / etc.]

## 3. Fidelity=1 (1-sec bars) availability

| Stat | Value |
|---|---|
| Samples with fidelity=1 data | [N / 10] |
| Median points per 7d window (when available) | [...] |

Interpretation: [either "widely supported, use for sub-minute capture" / "unsupported, accept 1-min limit" / "partial support with gaps"]

## 4. Depth of history

- Oldest sampleable market endDate in the Gamma universe: [YYYY-MM-DD]
- Available history window: [e.g., 2023-01 to today]
- Strata viability at this depth: [OK / borderline — cross-reference with 0.4 counts]

## 5. Gap anomalies observed

[Any unusual patterns: missing data during specific months, large gaps in specific markets, etc.]

## 6. Decision on `cycle_seconds` grid

Spec 9 grid is `{30, 60, 300, 900}`. With fidelity-60 price data, cycle_seconds < 60 is not simulatable with confidence (we can't observe price dips shorter than 60s).

- **If fidelity=1 supported:** cycle_seconds grid stays as-is.
- **If fidelity=1 unsupported:** cycle_seconds grid becomes `{60, 300, 900}` (3 values instead of 4). Grid shrinks to 388,800 configs per stratum.

**Adjustment for spec:** [record decision here]

## 7. Open questions
```

Fill every bracketed field. The section 6 decision is load-bearing for Phase 1+.

- [ ] **Step 5: Commit**

```bash
git add scripts/feasibility/probe_prices_history.py docs/superpowers/research/phase0/0.5-price-samples.jsonl docs/superpowers/research/phase0/0.5-prices-history-depth.md
git commit -m "research(phase0.5): prices-history depth + granularity audit"
```

---

## Task 7: 0.6 Statistical power analysis

**Files:**
- Create: `scripts/feasibility/power_analysis.py`
- Create: `docs/superpowers/research/phase0/0.6-power-analysis.md`

- [ ] **Step 1: Write the power analysis script**

```python
# scripts/feasibility/power_analysis.py
"""Compute required sample size per stratum.

Detect effect size = 0.02 EV (2pp) on attempted markets.
Assumed SD of per-market net PnL = sigma (see sensitivity table).
Significance: alpha = 0.0125 (Bonferroni-4 for 4 strata at overall 0.05).
Power: 0.80.
Test: one-sample t-test against H0: EV=0.

Reports required N for a range of sigma values.
"""
import math


def required_n(effect, sigma, alpha, power):
    """One-sample t-test sample size approximation via normal.

    n ≈ ((Z_{1-alpha} + Z_{power}) * sigma / effect)^2
    """
    from statistics import NormalDist
    z_alpha = NormalDist().inv_cdf(1 - alpha)   # one-sided
    z_beta = NormalDist().inv_cdf(power)
    return math.ceil(((z_alpha + z_beta) * sigma / effect) ** 2)


def main():
    effect = 0.02  # 2pp EV per market
    alpha = 0.0125  # Bonferroni factor 4
    power = 0.80
    print(f"Detection target: {effect} EV per attempted market")
    print(f"alpha={alpha} (Bonferroni-4), power={power}")
    print(f"One-sided one-sample test against H0: EV=0")
    print()
    print(f"{'sigma':>8} | {'required_N':>12} | {'interp':40}")
    print("-" * 70)
    for sigma in [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30, 0.50]:
        n = required_n(effect, sigma, alpha, power)
        interp = (
            "trivially feasible" if n < 100 else
            "modest sample" if n < 500 else
            "substantial sample" if n < 2000 else
            "large sample, tight" if n < 10000 else
            "infeasible"
        )
        print(f"{sigma:>8.2f} | {n:>12} | {interp}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run and capture**

```bash
python3 scripts/feasibility/power_analysis.py > /tmp/power_analysis.txt
cat /tmp/power_analysis.txt
```

- [ ] **Step 3: Estimate realistic sigma for tennis stink-bid fills**

This is a judgment step. Reason:
- Loss case: buy at 55¢, resolve at 0¢, pnl = -55¢ per $1 notional
- Win case: buy at 55¢, resolve at 100¢, pnl = +82¢ per $1 notional [(100-55)/55]
- Binary-ish outcome → sigma on net_pnl is approximately sqrt(p(1-p)) × range ≈ 0.5 × 1.37 ≈ **~0.7 per $1 notional**

But most attempted markets don't fill (binary at the attempt level):
- If fill rate is 10%, expected value per attempted market = 0.10 × E[net | fill] + 0.90 × 0
- Variance per attempted market is LOWER than per-filled because 90% are zero
- Approximate sigma on per-attempt net PnL: ≈ fill_rate × sigma_per_filled ≈ 0.10 × 0.7 ≈ **0.07–0.10**

Use **sigma = 0.10** as baseline, ±0.05 sensitivity.

- [ ] **Step 4: Cross-reference with Gamma counts (from Task 5)**

Look at 0.4-gamma-tennis-universe.md counts by tour. Write into artifact:

| Stratum | Estimated markets (from 0.4) | Required N at sigma=0.10 | Feasible? |
|---|---|---|---|
| ATP | [count] | [N from table] | Y/N |
| WTA | [count] | [N from table] | Y/N |
| Challenger | [count] | [N from table] | Y/N |
| ALL | [sum] | [N from table] | Y/N |

- [ ] **Step 5: Write the 0.6 artifact**

```markdown
# 0.6 Statistical Power Analysis

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.6, 4.5.3 (FDR), 6.10 (stratification), 9.3 (correction)

## 1. Target detection

| Parameter | Value |
|---|---|
| Effect size (EV per attempted market) | 0.02 (2pp) |
| Significance alpha (Bonferroni-4) | 0.0125 |
| Power | 0.80 |
| Test | one-sided one-sample against H0: EV=0 |

## 2. Sigma estimation reasoning

Reasoning: [paste the paragraph from Step 3]

**Baseline sigma assumed:** 0.10 per attempted market
**Sensitivity range:** 0.05 to 0.20

## 3. Required N by sigma

[paste the table from Step 2 output]

## 4. Feasibility by stratum

[paste the table from Step 4]

## 5. Decision

**Stratified analysis feasibility:**
- ATP main: [feasible / borderline / infeasible]
- WTA main: [feasible / borderline / infeasible]
- Challenger: [feasible / borderline / infeasible]
- ALL (pooled): [feasible / borderline / infeasible]

**If a stratum is infeasible at sigma=0.10:** [options — reduce strata granularity / accept weaker power / pool with adjacent stratum / etc.]

## 6. Open questions
[any unresolved — particularly: true sigma won't be known until Phase 4 results come in; this is the best pre-registration estimate]
```

- [ ] **Step 6: Commit**

```bash
git add scripts/feasibility/power_analysis.py docs/superpowers/research/phase0/0.6-power-analysis.md
git commit -m "research(phase0.6): statistical power analysis per stratum"
```

---

## Task 8: 0.7 Min-viable position size computation

**Files:**
- Create: `docs/superpowers/research/phase0/0.7-min-viable-size.md`

- [ ] **Step 1: Compute min-viable position from fee + spread findings**

Open `0.1-polymarket-fees.md` for current fee rate and observed spread. Compute:

```
round_trip_cost_pct = 2 × (fee_rate + half_spread_pct)
gas_per_trade_usdc = [from 0.1]
break_even_position = gas_per_trade_usdc / (target_ev_pct - round_trip_cost_pct)
```

Do this for two target EV levels: 2% (at-threshold) and 5% (comfortable).

- [ ] **Step 2: Write the 0.7 artifact**

```markdown
# 0.7 Min-Viable Position Size

**Date:** 2026-04-21
**Spec section:** 10 Phase 0.7, 4.4 (economic significance threshold)

## 1. Cost assumptions (from 0.1)

| Cost component | Value |
|---|---|
| Fee per leg (maker or taker, whichever likelier for us) | [%] |
| Half bid-ask spread (median observed) | [%] |
| Gas per trade (Polygon) | [USDC eq] |

Round-trip cost percentage = 2 × (fee + half_spread) = [%]
Round-trip fixed cost = 2 × gas = [USDC eq]

## 2. Break-even position size table

For a trade to be profitable, expected PnL must exceed total costs:
```
position_size × EV_pct > position_size × round_trip_cost_pct + 2 × gas
→ position_size > (2 × gas) / (EV_pct - round_trip_cost_pct)
```

| Target EV | Break-even position size |
|---|---|
| 2% (threshold) | $[...] |
| 3% | $[...] |
| 5% | $[...] |
| 10% | $[...] |

## 3. Implications for the grid

Spec section 9 has `position_size_usdc ∈ {10, 25, 50}`.

- If break-even at target EV 5% is ~$[X]: position_size=$10 is [viable/not viable] at 5% EV
- Spec's economic significance threshold (2× round-trip cost = [%]): [is / isn't] above the 5% level

**Recommendation:** [keep grid as-is / expand to include $[X]+ / drop $10 value from grid]

## 4. Capacity note

For a $25 position at 10% EV: expected profit per filled trade ≈ $2.50. At ~100 fills/year: ~$250/year. Real money deploy would need to scale position_size 10-100× to produce meaningful revenue. Capacity analysis (will scaled bids move the market?) is explicitly deferred to Phase 6.

## 5. Open questions
```

Fill every bracket.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/research/phase0/0.7-min-viable-size.md
git commit -m "research(phase0.7): min-viable position size + grid implications"
```

---

## Task 9: Synthesize feasibility memo

**Files:**
- Create: `docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md`

- [ ] **Step 1: Read all 7 artifacts in order, pull key findings**

```bash
ls docs/superpowers/research/phase0/*.md
for f in docs/superpowers/research/phase0/0.{1,2,3,4,5,6,7}-*.md; do
  echo "=== $f ==="
  head -80 "$f"
  echo
done | less
```

- [ ] **Step 2: Write the memo**

```markdown
# Stink-Bid Scientific Backtest — Feasibility Memo

**Date:** 2026-04-21
**Plan:** `docs/superpowers/plans/2026-04-21-stink-bid-phase0-feasibility-plan.md`
**Spec:** `docs/superpowers/specs/2026-04-21-stink-bid-scientific-backtest-design.md`
**Decision:** [GO / NO-GO / GO WITH SCOPE REDUCTION]

## TL;DR

[2-3 sentences. Include: the headline feasibility answer, the most-critical bottleneck if any, and what the next phase looks like.]

## 1. Venue (from 0.2)

[3-5 lines. Decision: Polymarket or Kalshi for v1 backtest, with 1-sentence justification from 0.2.]

## 2. Data depth (from 0.3, 0.4, 0.5)

[Paragraph summarizing Gamma count by tour + year, Sackmann alignment rate, price-history depth. Call out any stratum that's undersupplied.]

## 3. Statistical feasibility (from 0.6)

[Paragraph. Feasibility per stratum at target sigma. If any stratum is infeasible, state the scope-reduction recommendation.]

## 4. Cost + grid implications (from 0.1, 0.7)

[Paragraph. Current fee rate, observed spread, break-even position, decision on cycle_seconds grid adjustment based on fidelity support.]

## 5. Grid adjustments vs spec

The spec (section 9) locked these dimensions:

| Parameter | Spec value | Memo decision | Reason |
|---|---|---|---|
| cycle_seconds | {30,60,300,900} | [...] | fidelity support from 0.5 |
| position_size_usdc | {10,25,50} | [...] | break-even from 0.7 |
| [other] | [...] | [...] | [...] |

If any adjustments are needed, they are captured here and the spec's section 9 will be amended before Phase 1 begins.

## 6. Ready-or-not checklist

- [ ] 0.1 Fee schedule complete and modeled
- [ ] 0.2 Venue decision documented
- [ ] 0.3 Sackmann coverage + alignment ≥ target (recommend ≥90%)
- [ ] 0.4 Gamma tennis universe pulled, counted, validated
- [ ] 0.5 Prices-history depth + granularity confirmed
- [ ] 0.6 Power analysis shows viability per stratum
- [ ] 0.7 Break-even position size computed and compared to grid

## 7. Decision

**[GO / NO-GO / GO WITH SCOPE REDUCTION]**

Rationale: [3-5 sentences. Explicitly tie to the checklist items.]

**If GO:** Phase 1 (data pipeline) is unblocked. Proceed.

**If GO WITH SCOPE REDUCTION:**
- Reduction: [describe]
- Spec amendment needed: [file + section + change]
- Then Phase 1 can proceed.

**If NO-GO:**
- Blocker: [describe]
- Mitigation options: [list]
- Revisit after: [specify trigger]

## 8. Risks flagged for Phase 1+

[Any caveats from research that should inform implementation — e.g., "Sackmann alignment at 85% means fuzzy-match fallback needed in Phase 1.5" or "Gamma pagination rate-limited at 300/min — backfill script needs throttle"]

## 9. Artifacts referenced

- `docs/superpowers/research/phase0/0.1-polymarket-fees.md`
- `docs/superpowers/research/phase0/0.2-kalshi-comparison.md`
- `docs/superpowers/research/phase0/0.3-sackmann-audit.md`
- `docs/superpowers/research/phase0/0.4-gamma-tennis-universe.md`
- `docs/superpowers/research/phase0/0.4-gamma-tennis-events.jsonl`
- `docs/superpowers/research/phase0/0.5-prices-history-depth.md`
- `docs/superpowers/research/phase0/0.5-price-samples.jsonl`
- `docs/superpowers/research/phase0/0.6-power-analysis.md`
- `docs/superpowers/research/phase0/0.7-min-viable-size.md`
```

Fill every bracket from the 7 artifacts. No placeholders in the final memo.

- [ ] **Step 3: Self-review the memo**

Check:
- TL;DR actually summarizes (not generic)
- Decision is explicit (GO / NO-GO / SCOPE REDUCTION) — no hedging
- Every section 1-4 cites the artifact number it draws from
- All 7 checklist items in section 6 are ticked
- Section 5 lists every grid-adjustment amendment to the spec (or says "none")
- Section 7 rationale ties to specific checklist items
- Section 8 captures caveats that Phase 1 needs to know

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md
git commit -m "research(phase0): feasibility memo — $(grep '^\*\*Decision:\*\*' docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md | head -1 | sed 's/\*\*//g')"
```

---

## Task 10: Update tracking + spec amendment if needed

**Files:**
- Modify: `docs/superpowers/specs/2026-04-21-stink-bid-scientific-backtest-design.md` (only if memo's section 5 lists adjustments)

- [ ] **Step 1: Apply spec amendments if the memo flagged any**

If memo section 5 listed grid adjustments (e.g., cycle_seconds shrunk from {30,60,300,900} to {60,300,900} because fidelity=1 unsupported), edit the spec's section 9.1 to match. Recompute the grid total and update all downstream count references (sections 4.2, 4.5.3, 9.3, 11, 14 Benjamini-Hochberg line, fidelity #12).

If memo flagged "no adjustments," skip this step.

- [ ] **Step 2: Commit the spec amendment (if applied)**

```bash
git add docs/superpowers/specs/2026-04-21-stink-bid-scientific-backtest-design.md
git commit -m "spec amendment: apply phase 0 feasibility findings"
```

- [ ] **Step 3: Verify end-state**

```bash
ls docs/superpowers/research/phase0/ | wc -l
ls docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md
grep "^\*\*Decision:\*\*" docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md
git log --oneline -15
```

Expected:
- 7+ files under phase0/ (7 markdowns + 2 JSONL dumps + optional .gitkeep)
- Feasibility memo exists
- Decision line is explicit (GO / NO-GO / GO WITH SCOPE REDUCTION)
- Git log shows 8-10 phase 0 commits

- [ ] **Step 4: Final commit with phase 0 completion marker**

```bash
git commit --allow-empty -m "phase 0 complete: feasibility research done, memo decision = $(grep '^\*\*Decision:\*\*' docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md | head -1 | sed 's/\*\*//g' | tr -d '\n')"
```

---

## Self-review

**Spec coverage check:**

| Spec section 10 Phase 0 item | Task # |
|---|---|
| 0.1 Polymarket fee schedule + spread | Task 2 |
| 0.2 Kalshi comparison | Task 3 |
| 0.3 Sackmann data audit | Task 4 |
| 0.4 Gamma tennis market universe | Task 5 |
| 0.5 /prices-history depth | Task 6 |
| 0.6 Power analysis | Task 7 |
| 0.7 Min-viable position size | Task 8 |
| Memo synthesis + go/no-go | Task 9 |
| Spec amendment + verification | Task 10 |

All 7 sub-tasks covered. Plus synthesis + verification tasks.

**Placeholder scan:** all bracketed `[...]` markers in artifact templates are INSTRUCTIONS to the engineer to fill with findings — acceptable because they're in research artifacts, not the plan's executable steps. The plan's own steps contain concrete code, commands, and acceptance criteria.

**Type consistency:** API endpoint names (`/events`, `/prices-history`, `/book`, `/trades`) and parameter names (`limit`, `offset`, `fidelity`, `tag`, `closed`, `active`, `order`) consistent across Task 2-6 scripts.

**Contextual gotchas already encoded:**
- Task 4 notes Sackmann cache gitignored to avoid committing CSVs
- Task 6 flags fidelity=1 support as load-bearing for cycle_seconds grid
- Task 9 memo decision is explicit go/no-go with no hedging allowed
- Task 10 handles spec amendments if memo flags any (critical: don't silently let spec and actual grid drift)
