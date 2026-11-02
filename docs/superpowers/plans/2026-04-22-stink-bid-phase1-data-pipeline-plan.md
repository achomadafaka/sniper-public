# Stink-Bid Backtest Phase 1 — Data Pipeline Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the idempotent data pipeline that powers the backtest. Outputs a versioned DuckDB snapshot under `data/backtest/stink_bid/` containing: closed Polymarket tennis H2H events, per-token price series, per-market trade windows, parsed Sackmann match outcomes, and Polymarket↔Sackmann alignment records.

**Architecture:** Methods on `PolymarketClient` (data access), a new `data/sackmann.py` module (CSV parser + scraper fallback for 2025+), a new `data/backtest/schema.py` (DuckDB DDL), and an idempotent backfill script `scripts/backfill_stink_bid_backtest.py` that orchestrates all sources.

**Tech Stack:** Python 3 + `requests` + `duckdb` + `csv`. `duckdb` is the only new dependency — add to `requirements.txt`. No `pandas` required (use DuckDB DataFrame or raw dicts).

**Spec reference:** `docs/superpowers/specs/2026-04-21-stink-bid-scientific-backtest-design.md` section 10 Phase 1 (1.1–1.7). Feasibility memo: `docs/superpowers/research/2026-04-21-backtest-feasibility-memo.md`.

**Locked context from Phase 0 memo:**
- Venue: Polymarket. Sports taker fee formula `0.03 × p × (1-p)`; sells exempt; maker 0%; gas 0.
- H1 effect size: 4pp (spec amended).
- `cycle_seconds` grid: {60, 300, 900}. Min observable timescale 60s.
- Backtest window: primary mid-2025 → 2026-Q2; 2024 available as robustness stratum.
- Sackmann has 2024 only. 2025+ requires ATP/WTA scraper (this plan's Task 8).
- H2H market identification: prefer `sportsMarketType == "moneyline"` and fall back to `market.question == event.title`; exclude set, totals, handicap, and doubles markets because the 2025/2026 Tennis-Data result files used here are singles-only.
- Tour classification: slug prefix (`atp-`, `wta-`, tournament prefix for Slams).
- Gamma corpus size target: 5,840 H2H events already known from 0.4 dump; refresh monthly.
- DuckDB snapshot location: `data/backtest/stink_bid/backtest.db`.

---

## File Structure

**Created by this plan:**
- `data/sackmann.py` — CSV parser, scraper for 2025+ outcomes, alignment helpers
- `data/backtest/__init__.py`
- `data/backtest/schema.py` — DuckDB DDL + migration runner
- `data/backtest/stink_bid/` — runtime DuckDB snapshot dir (gitignored)
- `scripts/backfill_stink_bid_backtest.py` — idempotent backfill orchestrator
- `scripts/scrape_atp_wta_results.py` — 2025+ match-results scraper
- `tests/backtest/__init__.py`
- `tests/backtest/test_polymarket_client_bulk.py` — 1.1, 1.2, 1.3 tests
- `tests/backtest/test_sackmann.py` — 1.4, 1.5 tests
- `tests/backtest/test_schema.py` — 1.6 tests
- `tests/backtest/test_backfill.py` — 1.7 orchestrator tests
- `tests/backtest/test_scraper.py` — 1.8 scraper tests
- `tests/backtest/fixtures/sackmann_atp_sample.csv` — tiny fixture
- `tests/backtest/fixtures/gamma_event_sample.json` — tiny fixture

**Modified:**
- `data/polymarket_client.py` — adds `get_prices_series`, `get_trades_window`, `get_resolved_tennis_markets`
- `requirements.txt` — adds `duckdb`
- `.gitignore` — adds `data/backtest/stink_bid/*.db`, `data/backtest/stink_bid/*.db.wal`

**Not touched:** Production runtime code (`paper_trader.py`, `alerter.py`, `strategy_runner.py`, etc.). This is research infrastructure, isolated from live trading.

---

## Task 1: Set up Phase 1 workspace + deps

**Files:**
- Create: `data/backtest/__init__.py`, `data/backtest/stink_bid/.gitkeep`
- Create: `tests/backtest/__init__.py`, `tests/backtest/fixtures/.gitkeep`
- Modify: `requirements.txt`, `.gitignore`

- [ ] **Step 1: Directories + empty package markers**

```bash
mkdir -p data/backtest/stink_bid tests/backtest/fixtures
touch data/backtest/__init__.py tests/backtest/__init__.py
touch data/backtest/stink_bid/.gitkeep tests/backtest/fixtures/.gitkeep
```

- [ ] **Step 2: Add `duckdb` to requirements + gitignore runtime DB**

```bash
grep -q "^duckdb" requirements.txt || echo "duckdb>=1.0" >> requirements.txt
grep -q "^data/backtest/stink_bid/\*\.db" .gitignore || {
  echo "data/backtest/stink_bid/*.db" >> .gitignore
  echo "data/backtest/stink_bid/*.db.wal" >> .gitignore
  echo "data/backtest/stink_bid/*.duckdb" >> .gitignore
}
```

- [ ] **Step 3: Install and smoke-test**

```bash
pip install duckdb
python3 -c "import duckdb; c = duckdb.connect(':memory:'); print(c.execute('SELECT 42').fetchone())"
```
Expected: `(42,)`.

- [ ] **Step 4: Commit scaffolding**

```bash
git add data/backtest/__init__.py data/backtest/stink_bid/.gitkeep \
  tests/backtest/__init__.py tests/backtest/fixtures/.gitkeep \
  requirements.txt .gitignore
git commit -m "scaffold(backtest): phase 1 workspace + duckdb dep"
```

**Acceptance:** `pytest tests/backtest/ -q` exits 0 (empty suite is a pass).

---

## Task 2: 1.1 — `PolymarketClient.get_prices_series(token_id, start_ts, end_ts, fidelity=1)`

**Extract from:** `data/polymarket_client.py:173-223` (_get_clob_snapshot returns nearest-point; we want full series), `scripts/feasibility/probe_prices_history.py:24-44` (clean wrapper).

**Files:**
- Modify: `data/polymarket_client.py`
- Create: `tests/backtest/test_polymarket_client_bulk.py`

- [ ] **Step 1: Write failing test first (TDD)**

`tests/backtest/test_polymarket_client_bulk.py` initial test: given a mock `_get` that returns `{"history": [{"t": 1700000000, "p": 0.5}, {"t": 1700000060, "p": 0.52}]}`, `get_prices_series(token_id='x', start_ts=0, end_ts=99999, fidelity=1)` returns a list of two dicts with `{timestamp, price}` keys.

```python
def test_get_prices_series_returns_all_points(monkeypatch):
    from data.polymarket_client import PolymarketClient
    client = PolymarketClient()
    canned = {"history": [{"t": 1700000000, "p": 0.5}, {"t": 1700000060, "p": 0.52}]}
    monkeypatch.setattr(client, "_get", lambda *a, **kw: canned)
    series = client.get_prices_series("tokX", start_ts=0, end_ts=99999, fidelity=1)
    assert len(series) == 2
    assert series[0] == {"timestamp": 1700000000, "price": 0.5}
    assert series[1] == {"timestamp": 1700000060, "price": 0.52}

def test_get_prices_series_handles_missing_history(monkeypatch):
    from data.polymarket_client import PolymarketClient
    client = PolymarketClient()
    monkeypatch.setattr(client, "_get", lambda *a, **kw: None)
    assert client.get_prices_series("tokX", 0, 1) == []

def test_get_prices_series_sorts_by_timestamp(monkeypatch):
    from data.polymarket_client import PolymarketClient
    client = PolymarketClient()
    out_of_order = {"history": [{"t": 200, "p": 0.9}, {"t": 100, "p": 0.8}]}
    monkeypatch.setattr(client, "_get", lambda *a, **kw: out_of_order)
    series = client.get_prices_series("tokX", 0, 300, fidelity=1)
    assert [p["timestamp"] for p in series] == [100, 200]
```

- [ ] **Step 2: Implement `get_prices_series`**

Add to `data/polymarket_client.py` (near `_get_clob_snapshot`):

```python
def get_prices_series(
    self,
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int = 1,
) -> list:
    """Fetch all prices-history points for a token in [start_ts, end_ts].

    fidelity=1 → 1-minute bars (default; universally supported per Phase 0.5).
    fidelity=60 → 60-minute bars (hourly; also supported).

    Returns list of {timestamp: int, price: float} sorted ascending.
    Empty list on error or empty response.
    """
    url = f"{self.CLOB_API}/prices-history"
    params = {
        "market": token_id,
        "startTs": start_ts,
        "endTs": end_ts,
        "fidelity": fidelity,
    }
    data = self._get(url, params)
    if not data or "history" not in data:
        return []
    hist = data.get("history") or []
    out = []
    for p in hist:
        ts = p.get("t")
        price = p.get("p")
        if ts is None or price is None:
            continue
        out.append({"timestamp": int(ts), "price": float(price)})
    out.sort(key=lambda x: x["timestamp"])
    return out
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/backtest/test_polymarket_client_bulk.py::test_get_prices_series_returns_all_points -v
pytest tests/backtest/test_polymarket_client_bulk.py::test_get_prices_series_handles_missing_history -v
pytest tests/backtest/test_polymarket_client_bulk.py::test_get_prices_series_sorts_by_timestamp -v
```
All 3 pass.

- [ ] **Step 4: Smoke-test against live API**

```bash
python3 -c "
from data.polymarket_client import PolymarketClient
c = PolymarketClient()
# Use a token from Task 2 probe (Madrid Open example)
series = c.get_prices_series('18367185763031359141422530123844322437997783751631209058497927193153636637878', 1713744000, 1714348800, fidelity=1)
print(f'points: {len(series)}')
if series:
    print(f'first: {series[0]}')
    print(f'last: {series[-1]}')
"
```
Expect non-zero point count with sane timestamps and prices in [0,1].

- [ ] **Step 5: Commit**

```bash
git add data/polymarket_client.py tests/backtest/test_polymarket_client_bulk.py
git commit -m "feat(backtest/1.1): PolymarketClient.get_prices_series for bulk price history"
```

---

## Task 3: 1.2 — `PolymarketClient.get_trades_window(condition_id, start_ts, end_ts, max_pages=50)`

**Extract from:** `polymarket_api.py:568-586` (fetch_market_trades — no time filter or pagination today).

**Verified pre-plan (2026-04-22):** Polymarket `/trades` returns **descending** timestamp order (newest first). This is what the early-stop pagination logic below assumes. The hash field is `transactionHash` (always populated in sampled responses).

**Critical gotcha:** a single `transactionHash` can correspond to **multiple fill rows** when the taker's order matches across several resting orders. In a 100-trade sample we observed 47 unique txhashes — ~53% duplication. The downstream DuckDB schema (Task 7) uses a surrogate row key (`row_hash`), not `transactionHash` alone, as the primary key. `get_trades_window` returns raw rows; de-duplication is the caller's responsibility.

**Files:**
- Modify: `data/polymarket_client.py`
- Modify: `tests/backtest/test_polymarket_client_bulk.py`

- [ ] **Step 1: Write failing tests**

Cover:
1. Basic: returns trades with timestamp in [start_ts, end_ts].
2. Out-of-window: trades outside are filtered.
3. Pagination: follows `offset` + `limit` until page is under `limit`.
4. Early stop: if an entire page is older than `start_ts`, stop paginating.
5. Max-pages guard: hitting cap returns what we have (log warning, no exception).

```python
def test_get_trades_window_filters_by_timestamp(monkeypatch):
    ...
def test_get_trades_window_paginates_until_exhausted(monkeypatch):
    ...
def test_get_trades_window_stops_when_page_fully_older(monkeypatch):
    ...
def test_get_trades_window_respects_max_pages_guard(monkeypatch):
    ...
```

- [ ] **Step 2: Implement**

```python
def get_trades_window(
    self,
    condition_id: str,
    start_ts: int,
    end_ts: int,
    page_size: int = 200,
    max_pages: int = 50,
) -> list:
    """Fetch all trades for a market with timestamp in [start_ts, end_ts].

    Paginates Polymarket /trades via offset+limit, returns de-duped list.
    Stops early when an entire page is fully older than start_ts.
    Hitting max_pages logs a warning and returns what we have.
    """
    url = f"{self.DATA_API}/trades"
    trades = []
    offset = 0
    for page_num in range(max_pages):
        params = {
            "market": condition_id,
            "limit": page_size,
            "offset": offset,
            "takerOnly": "false",
        }
        page = self._get(url, params)
        if not page or not isinstance(page, list):
            break
        # Each trade has 'timestamp' (seconds or ms — normalize)
        page_in_window = []
        page_older_count = 0
        for t in page:
            ts = t.get("timestamp", 0)
            if ts > 1_000_000_000_000:
                ts //= 1000
            t["_ts_sec"] = int(ts)
            if ts < start_ts:
                page_older_count += 1
            elif start_ts <= ts <= end_ts:
                page_in_window.append(t)
        trades.extend(page_in_window)
        if page_older_count == len(page):
            break  # fully past our window
        if len(page) < page_size:
            break
        offset += page_size
    else:
        import logging
        logging.getLogger(__name__).warning(
            "get_trades_window hit max_pages=%d for %s", max_pages, condition_id
        )
    return trades
```

- [ ] **Step 3: Run tests + smoke**

```bash
pytest tests/backtest/test_polymarket_client_bulk.py -v
python3 -c "
from data.polymarket_client import PolymarketClient
c = PolymarketClient()
trades = c.get_trades_window('0x683e365262992a5a4bfa383b9b91bdbc89e469813170077de0d8d170781f9efb', 1714000000, 1714500000, max_pages=3)
print(f'trades: {len(trades)}')
print(trades[:2] if trades else 'none')
"
```

- [ ] **Step 4: Commit**

```bash
git add data/polymarket_client.py tests/backtest/test_polymarket_client_bulk.py
git commit -m "feat(backtest/1.2): PolymarketClient.get_trades_window with time filter + pagination"
```

---

## Task 4: 1.3 — `PolymarketClient.get_resolved_tennis_markets(since_ts, until_ts=None)`

**Extract from:** `scripts/feasibility/probe_gamma_tennis.py:24-70` (paginator), Phase 0.4 dump validates behavior.

**Files:**
- Modify: `data/polymarket_client.py`
- Modify: `tests/backtest/test_polymarket_client_bulk.py`

- [ ] **Step 1: Failing tests**

1. Filters to events with tag `tennis`.
2. Filters by `endDate ∈ [since_ts, until_ts]` when both given.
3. Picks the H2H market (`market.question == event.title`).
4. Returns dicts with: `condition_id`, `token_id_yes` (first in `clobTokenIds`), `title`, `end_date`, `start_date`, `slug` (event slug).
5. Paginates until empty page.

- [ ] **Step 2: Implement**

```python
def get_resolved_tennis_markets(
    self,
    since_ts: Optional[int] = None,
    until_ts: Optional[int] = None,
    page_size: int = 500,
    max_pages: int = 500,
) -> list:
    """Paginate Gamma /events?closed=true, filter tennis H2H, return structured dicts.

    Filter tags: 'tennis' must be in event.tags.
    Filter title: must contain ' vs ' (case insensitive, with or without '.').
    Filter date: endDate ∈ [since_ts, until_ts] if provided (inclusive).
    Pick H2H market: market.question.strip() == event.title.strip().
    Return: list of dicts with condition_id, token_id_yes, title, end_date (iso),
            start_date (iso), event_slug, tour_prefix.
    """
    import re
    out = []
    seen_ids = set()
    for year_start, year_end in _year_windows(since_ts, until_ts):
        offset = 0
        for _ in range(max_pages):
            params = {
                "closed": "true",
                "limit": page_size,
                "offset": offset,
                "end_date_min": year_start,
                "end_date_max": year_end,
            }
            page = self._get(f"{self.GAMMA_API}/events", params)
            if not page or not isinstance(page, list):
                break
            for e in page:
                eid = e.get("id")
                if eid in seen_ids:
                    continue
                tags = [t.get("slug", "") if isinstance(t, dict) else str(t)
                        for t in (e.get("tags") or [])]
                if "tennis" not in tags:
                    continue
                etitle = (e.get("title") or "").strip()
                if not re.search(r"\bvs\.?\b", etitle, re.IGNORECASE):
                    continue
                markets = e.get("markets") or []
                h2h_market = None
                for m in markets:
                    if (m.get("question") or "").strip() == etitle and m.get("conditionId"):
                        h2h_market = m
                        break
                if not h2h_market:
                    continue
                try:
                    tids = json.loads(h2h_market.get("clobTokenIds", "[]"))
                except Exception:
                    continue
                if not tids:
                    continue
                slug = (e.get("slug") or "").strip()
                tour_prefix = slug.split("-", 1)[0] if slug else ""
                out.append({
                    "condition_id": h2h_market["conditionId"],
                    "token_id_yes": tids[0],
                    "token_id_no": tids[1] if len(tids) > 1 else None,
                    "title": etitle,
                    "end_date": e.get("endDate"),
                    "start_date": e.get("startDate"),
                    "event_slug": slug,
                    "tour_prefix": tour_prefix,
                    "event_id": eid,
                })
                seen_ids.add(eid)
            if len(page) < page_size:
                break
            offset += page_size
            time.sleep(0.15)
    return out


def _year_windows(since_ts, until_ts):
    """Yield (start_iso, end_iso) per calendar year to work around Gamma's
    depth-limited pagination (see Phase 0.4 finding)."""
    from datetime import datetime, timezone
    if since_ts is None:
        # Default: from 2024-01-01 (Polymarket's earliest tennis)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    else:
        start = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    if until_ts is None:
        end = datetime.now(timezone.utc)
    else:
        end = datetime.fromtimestamp(until_ts, tz=timezone.utc)
    y = start.year
    while y <= end.year:
        yield (f"{y}-01-01T00:00:00Z", f"{y}-12-31T23:59:59Z")
        y += 1
```

- [ ] **Step 3: Tests + smoke**

```bash
pytest tests/backtest/test_polymarket_client_bulk.py::test_get_resolved_tennis_markets_tennis_only -v
pytest tests/backtest/test_polymarket_client_bulk.py::test_get_resolved_tennis_markets_date_filter -v
pytest tests/backtest/test_polymarket_client_bulk.py -v  # all pass
python3 -c "
from data.polymarket_client import PolymarketClient
import datetime as dt
c = PolymarketClient()
# Pull last 30 days
since = int((dt.datetime.now() - dt.timedelta(days=30)).timestamp())
markets = c.get_resolved_tennis_markets(since_ts=since)
print(f'markets: {len(markets)}')
print(markets[0] if markets else 'none')
"
```

- [ ] **Step 4: Commit**

```bash
git add data/polymarket_client.py tests/backtest/test_polymarket_client_bulk.py
git commit -m "feat(backtest/1.3): PolymarketClient.get_resolved_tennis_markets paginator"
```

---

## Task 5: 1.4 — Sackmann CSV parser (`data/sackmann.py`)

**Extract from:** `scripts/feasibility/sackmann_audit.py:26-48` (CSV iteration pattern).

**Files:**
- Create: `data/sackmann.py`
- Create: `tests/backtest/test_sackmann.py`
- Create: `tests/backtest/fixtures/sackmann_atp_sample.csv` (~10 real rows pulled from `data/feasibility_cache/tennis_atp/atp_matches_2024.csv`)

- [ ] **Step 1: Generate fixture**

```bash
head -12 data/feasibility_cache/tennis_atp/atp_matches_2024.csv > tests/backtest/fixtures/sackmann_atp_sample.csv
wc -l tests/backtest/fixtures/sackmann_atp_sample.csv  # expect 12 (header + 11)
```

- [ ] **Step 2: Write failing tests**

```python
# tests/backtest/test_sackmann.py

from pathlib import Path
from data.sackmann import parse_sackmann_csv, SackmannMatch

FIXTURE = Path(__file__).parent / "fixtures" / "sackmann_atp_sample.csv"

def test_parse_sackmann_csv_returns_list_of_dataclass():
    matches = parse_sackmann_csv(FIXTURE, tour="atp_main")
    assert len(matches) == 11
    m = matches[0]
    assert isinstance(m, SackmannMatch)
    assert m.tour == "atp_main"
    assert m.winner_name
    assert m.loser_name
    assert m.tourney_date  # YYYYMMDD string
    assert m.surface in {"Hard", "Clay", "Grass", "Carpet", None}

def test_parse_sackmann_csv_handles_missing_file():
    matches = parse_sackmann_csv(Path("/nonexistent"), tour="atp_main")
    assert matches == []

def test_parse_sackmann_csv_parses_minutes_as_int():
    matches = parse_sackmann_csv(FIXTURE, tour="atp_main")
    for m in matches:
        assert m.minutes is None or isinstance(m.minutes, int)
```

- [ ] **Step 3: Implement `data/sackmann.py`**

```python
"""Sackmann tennis match data parser + Polymarket alignment helpers.

Sackmann's public repos (https://github.com/JeffSackmann/tennis_atp and _wta)
publish per-year CSVs. We parse them into SackmannMatch records and join against
Polymarket event titles via surname + date window.
"""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

LOG = logging.getLogger(__name__)

VALID_TOURS = {"atp_main", "wta_main", "atp_chal", "wta_itf"}


@dataclass
class SackmannMatch:
    tour: str  # atp_main | wta_main | atp_chal | wta_itf
    winner_name: str
    loser_name: str
    tourney_date: str  # YYYYMMDD
    tourney_name: str
    surface: Optional[str]
    tourney_level: Optional[str]
    minutes: Optional[int]
    score: Optional[str]


def parse_sackmann_csv(path: Path, tour: str) -> list[SackmannMatch]:
    if tour not in VALID_TOURS:
        raise ValueError(f"tour must be in {VALID_TOURS}")
    path = Path(path)
    if not path.exists():
        LOG.info("Sackmann CSV missing: %s", path)
        return []
    out = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            minutes_raw = row.get("minutes", "").strip()
            try:
                minutes = int(minutes_raw) if minutes_raw else None
            except ValueError:
                minutes = None
            out.append(SackmannMatch(
                tour=tour,
                winner_name=row.get("winner_name", ""),
                loser_name=row.get("loser_name", ""),
                tourney_date=row.get("tourney_date", ""),
                tourney_name=row.get("tourney_name", ""),
                surface=row.get("surface") or None,
                tourney_level=row.get("tourney_level") or None,
                minutes=minutes,
                score=row.get("score") or None,
            ))
    return out


def load_all(cache_root: Path, years: Iterable[int]) -> list[SackmannMatch]:
    """Load every year+tour CSV from cache, concat, return."""
    cache_root = Path(cache_root)
    plans = [
        (cache_root / "tennis_atp" / "atp_matches_{y}.csv", "atp_main"),
        (cache_root / "tennis_atp" / "atp_matches_qual_chall_{y}.csv", "atp_chal"),
        (cache_root / "tennis_wta" / "wta_matches_{y}.csv", "wta_main"),
        (cache_root / "tennis_wta" / "wta_matches_qual_itf_{y}.csv", "wta_itf"),
    ]
    matches = []
    for year in years:
        for pat, tour in plans:
            p = Path(str(pat).format(y=year))
            matches.extend(parse_sackmann_csv(p, tour=tour))
    return matches


def normalize_name(name: str) -> str:
    """Strip diacritics, lowercase, keep [a-z ] only."""
    nfd = unicodedata.normalize("NFD", name)
    ascii_only = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z ]+", "", ascii_only.lower()).strip()


def surnames_from_title(title: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (surname1, surname2) from 'Prefix: Player A vs Player B' format."""
    core = title.split(":")[-1].strip()
    parts = re.split(r"\s+vs\.?\s+", core, flags=re.IGNORECASE)
    if len(parts) != 2:
        return (None, None)
    n1 = parts[0].strip().split()[-1] if parts[0].strip() else None
    n2 = parts[1].strip().split()[-1] if parts[1].strip() else None
    return (n1, n2)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/backtest/test_sackmann.py -v
```
All pass.

- [ ] **Step 5: Commit**

```bash
git add data/sackmann.py tests/backtest/test_sackmann.py tests/backtest/fixtures/sackmann_atp_sample.csv
git commit -m "feat(backtest/1.4): Sackmann CSV parser + name normalization"
```

---

## Task 6: 1.5 — Polymarket↔Sackmann alignment

**Extract from:** inline logic in Phase 0 Task 4 alignment test.

**Files:**
- Modify: `data/sackmann.py` (add alignment functions)
- Modify: `tests/backtest/test_sackmann.py`

- [ ] **Step 1: Failing tests**

1. Matches surname1 + surname2 present in `(winner + loser)` case-insensitive, diacritics-stripped.
2. Returns `None` when either surname missing.
3. Date delta ≤ 21 days (configurable window).
4. Returns alignment-confidence score (higher = tighter date match).
5. Choose closest match when multiple Sackmann rows match both surnames.

```python
def test_align_event_to_sackmann_top_hit():
    sackmann = [
        SackmannMatch("atp_main", "Jannik Sinner", "Taylor Fritz", "20240908", "Us Open", "Hard", "G", 120, "6-3 6-4 6-4"),
    ]
    match = align_event_to_sackmann(
        title="US Open Final: Sinner vs. Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sackmann,
        window_days=21,
    )
    assert match.winner_name == "Jannik Sinner"
    assert match.loser_name == "Taylor Fritz"
    assert match.alignment_confidence > 0.9  # exact date hit

def test_align_rejects_outside_window():
    sackmann = [SackmannMatch("atp_main", "Jannik Sinner", "Taylor Fritz", "20230908", "Us Open", "Hard", "G", 120, None)]
    result = align_event_to_sackmann(
        title="US Open Final: Sinner vs. Fritz",
        end_date_iso="2024-09-08T00:00:00Z",
        sackmann_matches=sackmann,
        window_days=21,
    )
    assert result is None

def test_align_handles_unicode_surnames():
    # Polymarket title uses ASCII "Swiatek"; Sackmann stores Unicode "Świątek".
    # Alignment must succeed despite diacritic difference.
    sackmann = [SackmannMatch("wta_main", "Iga Świątek", "Coco Gauff", "20240128", "Australian Open", "Hard", "G", 100, None)]
    m = align_event_to_sackmann(
        title="Australian Open: Swiatek vs. Gauff",
        end_date_iso="2024-01-28T00:00:00Z",
        sackmann_matches=sackmann,
    )
    assert m is not None
    assert m.sackmann.winner_name == "Iga Świątek"
    assert m.sackmann.loser_name == "Coco Gauff"
```

- [ ] **Step 2: Implement**

```python
@dataclass
class AlignedMatch:
    sackmann: SackmannMatch
    date_delta_days: int
    alignment_confidence: float  # 1.0 = exact date hit, declines with delta

    @property
    def winner_name(self): return self.sackmann.winner_name
    @property
    def loser_name(self): return self.sackmann.loser_name


def align_event_to_sackmann(
    title: str,
    end_date_iso: str,
    sackmann_matches: list[SackmannMatch],
    window_days: int = 21,
) -> Optional[AlignedMatch]:
    n1, n2 = surnames_from_title(title)
    if not n1 or not n2:
        return None
    nn1, nn2 = normalize_name(n1), normalize_name(n2)
    try:
        target = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    best = None
    for m in sackmann_matches:
        try:
            mdate = datetime.strptime(m.tourney_date, "%Y%m%d")
        except Exception:
            continue
        delta = abs((mdate.replace(tzinfo=target.tzinfo) - target).days)
        if delta > window_days:
            continue
        combined = f"{normalize_name(m.winner_name)} {normalize_name(m.loser_name)}"
        if nn1 in combined and nn2 in combined:
            conf = max(0.0, 1.0 - delta / window_days)
            if best is None or conf > best.alignment_confidence:
                best = AlignedMatch(sackmann=m, date_delta_days=delta, alignment_confidence=conf)
    return best


def alignment_success_rate(
    events: list[dict],
    sackmann: list[SackmannMatch],
    window_days: int = 21,
) -> tuple[int, int, list[dict]]:
    """Return (hits, total, diagnostics[]). Each diagnostic row logs success/failure per event."""
    hits = 0
    rows = []
    for e in events:
        m = align_event_to_sackmann(
            title=e.get("title", ""),
            end_date_iso=e.get("end_date", ""),
            sackmann_matches=sackmann,
            window_days=window_days,
        )
        rows.append({
            "condition_id": e.get("condition_id"),
            "title": e.get("title"),
            "end_date": e.get("end_date"),
            "matched": m is not None,
            "winner": m.winner_name if m else None,
            "confidence": m.alignment_confidence if m else 0.0,
        })
        if m is not None:
            hits += 1
    return (hits, len(events), rows)
```

- [ ] **Step 3: Tests + integration spike**

```bash
pytest tests/backtest/test_sackmann.py -v
python3 -c "
from data.sackmann import load_all, alignment_success_rate
from data.polymarket_client import PolymarketClient
import datetime as dt

sack = load_all('data/feasibility_cache', years=[2024])
client = PolymarketClient()
events = client.get_resolved_tennis_markets(
    since_ts=int(dt.datetime(2024,1,1).timestamp()),
    until_ts=int(dt.datetime(2024,12,31).timestamp()),
)
hits, total, _ = alignment_success_rate(events, sack, window_days=21)
print(f'alignment: {hits}/{total} = {hits/total*100:.1f}%' if total else 'no events')
"
```

Expected: ≥90% alignment rate. If below, inspect diagnostics rows for failure patterns (Unicode, event-title format variations, missing Sackmann rows).

- [ ] **Step 4: Commit**

```bash
git add data/sackmann.py tests/backtest/test_sackmann.py
git commit -m "feat(backtest/1.5): Polymarket↔Sackmann alignment with confidence score"
```

---

## Task 7: 1.6 — DuckDB schema (`data/backtest/schema.py`)

**Files:**
- Create: `data/backtest/schema.py`
- Create: `tests/backtest/test_schema.py`

- [ ] **Step 1: Failing test**

```python
# tests/backtest/test_schema.py

import duckdb
from data.backtest.schema import apply_schema, SCHEMA_VERSION

def test_apply_schema_creates_all_tables(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    expected = {
        "gamma_events", "prices_history", "trades",
        "sackmann_matches", "event_alignment", "schema_version",
    }
    assert expected <= tables

def test_apply_schema_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    apply_schema(con)  # second run should not error
    version = con.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION

def test_schema_has_expected_columns(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    cols = {r[1] for r in con.execute("DESCRIBE gamma_events").fetchall()}
    assert {"condition_id", "title", "end_date", "token_id_yes"} <= cols
```

- [ ] **Step 2: Implement `data/backtest/schema.py`**

```python
"""DuckDB schema for the stink-bid backtest snapshot.

Idempotent. apply_schema() is safe to call on an existing DB — it creates
tables if missing, records version, and can migrate forward in future.
"""
from __future__ import annotations

import duckdb

SCHEMA_VERSION = 1

DDL = [
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        git_commit VARCHAR
    )""",
    """CREATE TABLE IF NOT EXISTS gamma_events (
        condition_id VARCHAR PRIMARY KEY,
        event_id VARCHAR,
        title VARCHAR,
        event_slug VARCHAR,
        tour_prefix VARCHAR,
        token_id_yes VARCHAR NOT NULL,
        token_id_no VARCHAR,
        start_date TIMESTAMP,
        end_date TIMESTAMP,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS idx_gamma_events_enddate ON gamma_events(end_date)""",
    """CREATE INDEX IF NOT EXISTS idx_gamma_events_tour ON gamma_events(tour_prefix)""",

    """CREATE TABLE IF NOT EXISTS prices_history (
        token_id VARCHAR NOT NULL,
        timestamp_sec BIGINT NOT NULL,
        price DOUBLE NOT NULL,
        fidelity INTEGER NOT NULL,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (token_id, timestamp_sec, fidelity)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_prices_token_ts ON prices_history(token_id, timestamp_sec)""",

    """CREATE TABLE IF NOT EXISTS trades (
        row_hash VARCHAR PRIMARY KEY,     -- sha256(transactionHash|asset|price|size|side|outcome)
        condition_id VARCHAR NOT NULL,
        transaction_hash VARCHAR,          -- on-chain tx (not unique — one tx can have N fills)
        asset VARCHAR,
        timestamp_sec BIGINT NOT NULL,
        outcome VARCHAR,
        price DOUBLE,
        size DOUBLE,
        side VARCHAR,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE INDEX IF NOT EXISTS idx_trades_cid_ts ON trades(condition_id, timestamp_sec)""",
    """CREATE INDEX IF NOT EXISTS idx_trades_txhash ON trades(transaction_hash)""",

    """CREATE TABLE IF NOT EXISTS sackmann_matches (
        tour VARCHAR NOT NULL,
        tourney_date VARCHAR NOT NULL,
        winner_name VARCHAR NOT NULL,
        loser_name VARCHAR NOT NULL,
        tourney_name VARCHAR,
        surface VARCHAR,
        tourney_level VARCHAR,
        minutes INTEGER,
        score VARCHAR,
        source VARCHAR DEFAULT 'sackmann',
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tour, tourney_date, winner_name, loser_name)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_sack_date ON sackmann_matches(tourney_date)""",

    """CREATE TABLE IF NOT EXISTS event_alignment (
        condition_id VARCHAR PRIMARY KEY,
        sack_tour VARCHAR,
        sack_date VARCHAR,
        sack_winner VARCHAR,
        sack_loser VARCHAR,
        date_delta_days INTEGER,
        confidence DOUBLE,
        aligned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]


def apply_schema(con: duckdb.DuckDBPyConnection, git_commit: str | None = None) -> None:
    for stmt in DDL:
        con.execute(stmt)
    # Upsert schema version
    con.execute(
        "INSERT OR REPLACE INTO schema_version (version, git_commit) VALUES (?, ?)",
        [SCHEMA_VERSION, git_commit],
    )


def open_backtest_db(path: str = "data/backtest/stink_bid/backtest.db",
                    git_commit: str | None = None) -> duckdb.DuckDBPyConnection:
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = duckdb.connect(path)
    apply_schema(con, git_commit=git_commit)
    return con
```

- [ ] **Step 3: Tests + smoke**

```bash
pytest tests/backtest/test_schema.py -v
python3 -c "
from data.backtest.schema import open_backtest_db
con = open_backtest_db()
for t in con.execute('SHOW TABLES').fetchall():
    print(t)
con.close()
"
```

- [ ] **Step 4: Commit**

```bash
git add data/backtest/schema.py tests/backtest/test_schema.py
git commit -m "feat(backtest/1.6): duckdb schema for backtest snapshot"
```

---

## Task 8: 1.7 — Idempotent backfill orchestrator

**Files:**
- Create: `scripts/backfill_stink_bid_backtest.py`
- Create: `tests/backtest/test_backfill.py`

- [x] **Step 1: Design the CLI**

```
usage: backfill_stink_bid_backtest.py [--since-year Y] [--until-year Y] [--skip STEP] [--only STEP] [--dry-run]

Steps (run in order):
  1. gamma_events      — paginate Gamma, upsert into gamma_events
  2. prices_history    — for each event, fetch 1-min bars for [start_ts-7d, end_ts+1d]
  3. trades            — for each event, fetch trades in same window
  4. sackmann_load     — parse cached CSVs into sackmann_matches
  5. alignment         — compute event_alignment rows
```

- [x] **Step 2: Write step functions + smoke-tested CLI**

Each step respects existing cached rows (e.g. `WHERE token_id NOT IN (SELECT DISTINCT token_id FROM prices_history)`).

```python
#!/usr/bin/env python3
"""Backfill the stink-bid backtest snapshot idempotently."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.polymarket_client import PolymarketClient
from data.sackmann import load_all, alignment_success_rate, align_event_to_sackmann
from data.backtest.schema import open_backtest_db

LOG = logging.getLogger(__name__)


def step_gamma_events(con, client, since_year, until_year):
    since_ts = int(datetime(since_year, 1, 1, tzinfo=timezone.utc).timestamp())
    until_ts = int(datetime(until_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    events = client.get_resolved_tennis_markets(since_ts=since_ts, until_ts=until_ts)
    LOG.info("gamma_events: %d events to upsert", len(events))
    for e in events:
        con.execute("""
            INSERT OR REPLACE INTO gamma_events
            (condition_id, event_id, title, event_slug, tour_prefix,
             token_id_yes, token_id_no, start_date, end_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            e["condition_id"], e.get("event_id"), e["title"], e["event_slug"],
            e["tour_prefix"], e["token_id_yes"], e.get("token_id_no"),
            e.get("start_date"), e.get("end_date"),
        ])
    return len(events)


def step_prices_history(con, client, fidelity=1):
    # pick events lacking any price data yet
    rows = con.execute("""
        SELECT condition_id, token_id_yes, token_id_no, start_date, end_date
        FROM gamma_events
        WHERE token_id_yes NOT IN (SELECT DISTINCT token_id FROM prices_history WHERE fidelity=?)
    """, [fidelity]).fetchall()
    LOG.info("prices_history: %d events to fetch at fidelity=%d", len(rows), fidelity)
    count = 0
    for cid, tok_yes, tok_no, start_dt, end_dt in rows:
        end_ts = int(end_dt.timestamp()) if end_dt else int(time.time())
        start_ts = end_ts - 7 * 86400
        for tid in [tok_yes, tok_no]:
            if not tid: continue
            series = client.get_prices_series(tid, start_ts, end_ts + 86400, fidelity=fidelity)
            for pt in series:
                con.execute("""
                    INSERT OR IGNORE INTO prices_history (token_id, timestamp_sec, price, fidelity)
                    VALUES (?, ?, ?, ?)
                """, [tid, pt["timestamp"], pt["price"], fidelity])
            count += len(series)
            time.sleep(0.15)
    return count


def _trade_row_hash(cid, tx, asset, price, size, side, outcome) -> str:
    import hashlib
    key = f"{cid}|{tx or ''}|{asset or ''}|{price}|{size}|{side or ''}|{outcome or ''}"
    return hashlib.sha256(key.encode()).hexdigest()


def step_trades(con, client):
    rows = con.execute("""
        SELECT condition_id, start_date, end_date
        FROM gamma_events
        WHERE condition_id NOT IN (SELECT DISTINCT condition_id FROM trades)
    """).fetchall()
    LOG.info("trades: %d events to fetch", len(rows))
    count = 0
    for cid, start_dt, end_dt in rows:
        end_ts = int(end_dt.timestamp()) if end_dt else int(time.time())
        start_ts = end_ts - 7 * 86400
        trades = client.get_trades_window(cid, start_ts, end_ts + 86400)
        for t in trades:
            tx = t.get("transactionHash") or t.get("hash")
            asset = t.get("asset")
            price = float(t.get("price", 0) or 0)
            size = float(t.get("size", 0) or 0)
            side = t.get("side")
            outcome = t.get("outcome")
            row_hash = _trade_row_hash(cid, tx, asset, price, size, side, outcome)
            con.execute("""
                INSERT OR IGNORE INTO trades
                (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [row_hash, cid, tx, asset, t.get("_ts_sec"), outcome, price, size, side])
        count += len(trades)
        time.sleep(0.15)
    return count


def step_sackmann_load(con, cache_root, years):
    matches = load_all(cache_root, years=years)
    LOG.info("sackmann_load: %d matches to upsert", len(matches))
    for m in matches:
        con.execute("""
            INSERT OR REPLACE INTO sackmann_matches
            (tour, tourney_date, winner_name, loser_name, tourney_name, surface,
             tourney_level, minutes, score, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sackmann')
        """, [m.tour, m.tourney_date, m.winner_name, m.loser_name, m.tourney_name,
              m.surface, m.tourney_level, m.minutes, m.score])
    return len(matches)


def step_alignment(con, window_days=21):
    events = con.execute("SELECT condition_id, title, end_date FROM gamma_events").fetchall()
    from data.sackmann import SackmannMatch as _SM
    # SELECT in dataclass-field order so positional-construction works:
    sack_rows = con.execute("""
        SELECT tour, winner_name, loser_name, tourney_date, tourney_name,
               surface, tourney_level, minutes, score
        FROM sackmann_matches
    """).fetchall()
    sack = [_SM(*r) for r in sack_rows]
    hits = 0
    for cid, title, end_dt in events:
        end_iso = end_dt.isoformat() if end_dt else ""
        m = align_event_to_sackmann(title=title, end_date_iso=end_iso,
                                     sackmann_matches=sack, window_days=window_days)
        if m:
            hits += 1
            con.execute("""
                INSERT OR REPLACE INTO event_alignment
                (condition_id, sack_tour, sack_date, sack_winner, sack_loser,
                 date_delta_days, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [cid, m.sackmann.tour, m.sackmann.tourney_date,
                  m.sackmann.winner_name, m.sackmann.loser_name,
                  m.date_delta_days, m.alignment_confidence])
    LOG.info("alignment: %d/%d events aligned (%.1f%%)",
             hits, len(events), 100 * hits / max(len(events), 1))
    return hits


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--since-year", type=int, default=2024)
    p.add_argument("--until-year", type=int, default=datetime.now().year)
    p.add_argument("--sackmann-cache", default="data/feasibility_cache")
    p.add_argument("--skip", action="append", default=[])
    p.add_argument("--only", default=None)
    p.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    args = p.parse_args()

    client = PolymarketClient()
    con = open_backtest_db(args.db)
    years = list(range(args.since_year, args.until_year + 1))

    STEPS = [
        ("gamma_events", lambda: step_gamma_events(con, client, args.since_year, args.until_year)),
        ("prices_history", lambda: step_prices_history(con, client)),
        ("trades", lambda: step_trades(con, client)),
        ("sackmann_load", lambda: step_sackmann_load(con, args.sackmann_cache, years)),
        ("alignment", lambda: step_alignment(con)),
    ]
    for name, fn in STEPS:
        if args.only and args.only != name: continue
        if name in args.skip: continue
        t0 = time.time()
        n = fn()
        LOG.info("step %s: returned=%s elapsed=%.1fs", name, n, time.time() - t0)

    con.close()


if __name__ == "__main__":
    main()
```

- [x] **Step 3: Tests**

Integration-style tests against a tmp DB + mocked client:

```python
def test_backfill_gamma_events_inserts_rows(tmp_path, monkeypatch):
    ...
def test_backfill_idempotent(tmp_path, monkeypatch):
    # Run twice, verify no duplicates
    ...
def test_backfill_skip_already_cached(tmp_path, monkeypatch):
    # Second run of prices_history step should not re-fetch
    ...
```

- [x] **Step 4: Smoke-test end-to-end on a small slice**

```bash
python3 scripts/backfill_stink_bid_backtest.py --since-year 2024 --until-year 2024
# Expected: event count ~29-42, alignment ~90-100%, reasonable run time
python3 -c "
import duckdb
con = duckdb.connect('data/backtest/stink_bid/backtest.db')
for t in ['gamma_events','prices_history','trades','sackmann_matches','event_alignment']:
    n = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_stink_bid_backtest.py tests/backtest/test_backfill.py
git commit -m "feat(backtest/1.7): idempotent backfill orchestrator"
```

---

## Task 9: 1.8 — ATP/WTA scraper for 2025+ match outcomes (LOAD-BEARING)

Sackmann repo has 2024 only (per Phase 0.3). We need 2025-2026 outcomes for the backtest's val+test windows. **This task is load-bearing for the GO-WITH-SCOPE-REDUCTION memo decision**: 2024 alone has only 29 H2H events (Task 5 confirmed), which fails Task 7 power analysis at every stratification (needs 60/stratum at 4pp effect). Without the scraper, 2025+ Polymarket events (5,811 H2H markets) have no outcome labels → they drop out of the aligned sample → backtest is infeasible. "2024-only" is not a viable fallback — it's backtest failure.

**Files:**
- Create: `scripts/scrape_atp_wta_results.py`
- Modify: `data/sackmann.py` (add `load_scraped()`)
- Create: `tests/backtest/test_scraper.py`
- Create: `docs/superpowers/research/phase1/scraper-correctness-check.md` (correctness audit)

- [x] **Step 1: Pick source + test connectivity**

Candidates in order of preference:
1. `tennisabstract.com` (Sackmann's own frontend) — scraping his own data, schema should match perfectly
2. ATP/WTA official results pages (structured per-match; more work to scrape)
3. `tennis-data.co.uk` (commercial; last resort; may need account)

Spike: fetch one sample page from each, see which parses cleanest. Record findings as a comment in the scraper file. The 2025/2026 result files used for this pass come from `http://www.tennis-data.co.uk/alldata.php`.

- [x] **Step 2: Implement + unit test**

Scraper returns `list[SackmannMatch]` with `source='scraped'` in the dataclass (add `source` as a field; default `'sackmann'`). Writes to an intermediate CSV file at `data/feasibility_cache/scraped/<tour>_<year>.csv` matching Sackmann's column schema so it slots into the existing `load_all()` pipeline via a new `load_scraped()` helper.

Unit test: mock HTTP response, confirm parser extracts winner/loser/date/duration/surface/level correctly.

- [ ] **Step 3 (HARD GATE): Correctness audit — sample 20 scraped 2025 matches, hand-verify against ATP/WTA official results**

- Scrape 100+ 2025 matches across ATP + WTA.
- Random-sample 20 of them.
- For each, open the ATP or WTA official results page for that match day and verify: winner, loser, score, approximate duration. Record in `docs/superpowers/research/phase1/scraper-correctness-check.md`.
- **Pass criterion: ≥18/20 match exactly.** If <18/20: scraper has a systematic bug; fix and re-sample before proceeding.

- [ ] **Step 4: Alignment uplift report**

Re-run the full backfill (Task 8 steps 4 + 5) after scraper completes; report alignment rate delta. **Pass criterion: post-scraper alignment rate ≥ 85% across the full 2024–2026 event universe.** Log failures for diagnostic review.

- [ ] **Step 5: Commit**

```bash
git add scripts/scrape_atp_wta_results.py data/sackmann.py tests/backtest/test_scraper.py \
  docs/superpowers/research/phase1/scraper-correctness-check.md
git commit -m "feat(backtest/1.8): ATP/WTA scraper for 2025+ match outcomes"
```

**Escalation path:** if all three source candidates are intractable (rate-limit + anti-bot + schema mismatch), STOP and escalate to the memo. Options include (a) relaxing the analysis window to "Polymarket resolution as outcome source" with explicit tautology caveat, (b) purchasing commercial tennis-data.co.uk historical feed, (c) delaying Phase 2 until Sackmann upstream releases 2025 data. Do **not** silently proceed with 2024-only — that moves the ceiling from "inconclusive backtest" to "pre-registered plan fails open."

---

## Task 10: Verify + snapshot

**Files:**
- Create: `docs/superpowers/research/2026-04-22-phase1-pipeline-verification.md`

- [ ] **Step 1: Full backfill run + metrics capture**

```bash
python3 scripts/backfill_stink_bid_backtest.py --since-year 2024 --until-year 2026 2>&1 | tee /tmp/phase1_backfill.log
```

Record end-state:
- Row counts per table
- Alignment success rate (before + after scraper)
- Runtime per step
- Disk footprint of `data/backtest/stink_bid/backtest.db`
- Any API failures / retries needed

- [ ] **Step 2: Integrity spot checks**

```python
# 1. No event missing either token_id
# 2. No price outside [0, 1]
# 3. No trade size < 0 or price outside [0, 1]
# 4. Alignment confidence distribution (how tight are date matches)
# 5. For a random event, verify prices_history and trades time windows overlap the event's start/end dates
```

- [ ] **Step 2b: Tour classifier precision audit** (memo risk #3)

Phase 0 memo flagged that slug-prefix tour classification is a heuristic, not yet validated on a labeled subset. Run the spike now, before the verification memo is written, so we know whether any stratum drops for precision < 95%.

- Draw 100 random events from `gamma_events`.
- Hand-label each with its true tour (ATP / WTA / Grand Slam / Challenger / ITF / Doubles / Other) by looking at the event `title` + `event_slug` + one check against ATP/WTA official site if ambiguous.
- Compare against the `tour_prefix` stored by Task 4.
- Compute per-stratum precision. **Pass criterion: ATP and WTA each ≥95% precision.** If either stratum fails, flag in the verification memo and recommend dropping it OR refining the classifier before Phase 2.
- Record findings in `docs/superpowers/research/phase1/tour-classifier-audit.md`.

- [ ] **Step 3: Write verification memo**

Short doc (≤200 lines) covering:
- Final row counts
- Alignment rate + delta from scraper
- Performance: total runtime, per-step breakdown
- Known caveats (missing data, alignment failures)
- Ready-for-Phase-2: yes/no

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/research/2026-04-22-phase1-pipeline-verification.md
git commit -m "research(phase1): data pipeline verification + snapshot metrics"
git commit --allow-empty -m "phase 1 complete: data pipeline ready for phase 2"
```

---

## Self-review

**Spec coverage:**

| Spec section 10 Phase 1 item | Task # |
|---|---|
| 1.1 get_prices_series | Task 2 |
| 1.2 get_trades_window | Task 3 |
| 1.3 Gamma tennis paginator | Task 4 |
| 1.4 Sackmann fetcher + parser | Task 5 |
| 1.5 Market-to-Sackmann alignment | Task 6 |
| 1.6 DuckDB schema | Task 7 |
| 1.7 Idempotent backfill | Task 8 |
| (memo add) 2025+ scraper for Sackmann gap | Task 9 |
| Verification + snapshot | Task 10 |

**TDD coverage:** every method-adding task has a failing-tests-first step before implementation.

**Idempotence:** backfill uses `INSERT OR IGNORE` / `INSERT OR REPLACE` with appropriate PRIMARY KEYs throughout. Re-running the script is a no-op on cached data.

**Isolation from production:** no production file is modified. `requirements.txt` gets `duckdb` added. `.gitignore` adds backtest DB files.

**Acceptance for Phase 1 → Phase 2 handoff (all four required):**
- All tests pass (`pytest tests/backtest/ -q`).
- `data/backtest/stink_bid/backtest.db` populated for the primary window (mid-2025 → 2026-Q2, + 2024 robustness). Event count ≥ 2,000 aligned events in the 2025-2026 slice.
- **Task 9 scraper correctness audit passed** (≥18/20 hand-verified scraped 2025 matches) — or explicit escalation per Task 9 escalation path.
- Post-scraper alignment rate ≥ 85% across 2024–2026.
- **Tour classifier audit** shows ATP and WTA strata each at ≥95% precision (or memo flags the stratum as dropped).
- Verification memo committed with decision "ready for Phase 2" explicit.

If any acceptance item fails, STOP and escalate — do not proceed to Phase 2.
