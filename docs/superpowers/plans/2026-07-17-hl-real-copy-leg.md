# HL Real Copy Leg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real-money HL execution leg ($15/pos, $75 cap) that tails the paper copy book, with write-ahead intent ledger, deterministic CLOIDs, rails, reconcile, dry-run and testnet gates.

**Architecture:** `run_hl_real.py` tails `hl_copy_book` every 5 s. Pure decision core (`real_exec.py`) + DB layer (`real_store.py`) + SDK adapter (`hl_exchange.py`). Paper loop untouched (control arm). Spec: `docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md`.

**Tech Stack:** Python 3.13, sqlite3 (WAL, `data/hl_intel.sqlite`), `hyperliquid-python-sdk` (pinned), `eth-account` (already installed), pytest.

## Global Constraints

- Real orders sign with a dedicated API wallet (`HL_REAL_API_WALLET_FILE`); funded account = `HL_REAL_ACCOUNT_ADDRESS` (0x898B…3A1d). Master key never loaded by the runner.
- Every SDK market call passes **explicit slippage** (entry 0.005, exit 0.01). SDK default 5% must never be used.
- All closes reduce-only; only leg-owned positions (matched via intent ledger) are ever touched.
- `HL_REAL_ENABLED=0` blocks ENTRIES ONLY — exits, recovery, reconcile always run.
- Eligibility: target allowlisted ∧ `copy_tier='proven'` ∧ `is_cold_start=0` ∧ `detect_ts_ms >= deployment_epoch_ms`. Never adopt pre-existing paper opens.
- CLOID = `"0x" + sha256(f"{deployment_id}|{copy_id}|{leg}").hexdigest()[:32]` — deterministic, 128-bit.
- HL min order $10 notional; sz floored to szDecimals; below-min → `skipped` intent.
- No raw SQL outside `real_store.py`/`intel_store.py` for these tables. Thresholds only via `sniper/settings/hl_real.py` from env.
- Tests must never hit the network — SDK and HTTP always faked.
- Funding/arming is NOT part of this plan (separate operator action).

---

### Task 1: Settings module

**Files:**
- Create: `sniper/settings/hl_real.py`
- Modify: `.env.example` (append block)
- Test: `tests/test_hl_real_settings.py`

**Interfaces:**
- Produces: module constants `HL_REAL_ENABLED: bool`, `HL_REAL_DRY_RUN: bool`, `HL_REAL_PER_POS_USD: float (15.0)`, `HL_REAL_MAX_CONCURRENT: int (5)`, `HL_REAL_MAX_TOTAL_NOTIONAL: float (75.0)`, `HL_REAL_MAX_CHASE_BPS: float (100.0)`, `HL_REAL_DAY_LOSS_BREAKER_USD: float (20.0)`, `HL_REAL_ENTRY_SLIPPAGE: float (0.005)`, `HL_REAL_EXIT_SLIPPAGE: float (0.01)`, `HL_REAL_TARGETS: list[str]`, `HL_REAL_API_WALLET_FILE: str`, `HL_REAL_ACCOUNT_ADDRESS: str`, `HL_REAL_BASE_URL: str ("https://api.hyperliquid.xyz")`, `HL_REAL_TAIL_SECONDS: float (5.0)`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for sniper/settings/hl_real.py env-loaded knobs."""
import importlib


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import sniper.settings.hl_real as S
    return importlib.reload(S)


def test_defaults_fail_closed(monkeypatch):
    for k in ("HL_REAL_ENABLED", "HL_REAL_DRY_RUN", "HL_REAL_PER_POS_USD"):
        monkeypatch.delenv(k, raising=False)
    S = _reload(monkeypatch)
    assert S.HL_REAL_ENABLED is False          # fail-closed: disarmed by default
    assert S.HL_REAL_DRY_RUN is True           # fail-closed: dry-run by default
    assert S.HL_REAL_PER_POS_USD == 15.0
    assert S.HL_REAL_MAX_CONCURRENT == 5
    assert S.HL_REAL_MAX_TOTAL_NOTIONAL == 75.0
    assert S.HL_REAL_ENTRY_SLIPPAGE == 0.005 and S.HL_REAL_EXIT_SLIPPAGE == 0.01
    assert S.HL_REAL_TARGETS == ["0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"]


def test_env_overrides(monkeypatch):
    S = _reload(monkeypatch, HL_REAL_ENABLED="1", HL_REAL_TARGETS="0xaa, 0xBB")
    assert S.HL_REAL_ENABLED is True
    assert S.HL_REAL_TARGETS == ["0xaa", "0xbb"]   # lowercased, stripped
```

- [ ] **Step 2: Run test to verify it fails** — `python3 -m pytest tests/test_hl_real_settings.py -q` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement** (follow `sniper/settings/memecoin.py` style — plain `os.getenv` constants):

```python
"""Config knobs for the HL real copy leg (loaded from env). Spec:
docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md"""
import os


def _b(name, default):
    return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes"}


HL_REAL_ENABLED = _b("HL_REAL_ENABLED", False)      # entries-only kill switch
HL_REAL_DRY_RUN = _b("HL_REAL_DRY_RUN", True)       # record intents, never submit
HL_REAL_PER_POS_USD = float(os.getenv("HL_REAL_PER_POS_USD", "15"))
HL_REAL_MAX_CONCURRENT = int(os.getenv("HL_REAL_MAX_CONCURRENT", "5"))
HL_REAL_MAX_TOTAL_NOTIONAL = float(os.getenv("HL_REAL_MAX_TOTAL_NOTIONAL", "75"))
HL_REAL_MAX_CHASE_BPS = float(os.getenv("HL_REAL_MAX_CHASE_BPS", "100"))
HL_REAL_DAY_LOSS_BREAKER_USD = float(os.getenv("HL_REAL_DAY_LOSS_BREAKER_USD", "20"))
HL_REAL_ENTRY_SLIPPAGE = float(os.getenv("HL_REAL_ENTRY_SLIPPAGE", "0.005"))
HL_REAL_EXIT_SLIPPAGE = float(os.getenv("HL_REAL_EXIT_SLIPPAGE", "0.01"))
HL_REAL_TARGETS = [w.strip().lower() for w in os.getenv(
    "HL_REAL_TARGETS", "0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8").split(",") if w.strip()]
HL_REAL_API_WALLET_FILE = os.getenv("HL_REAL_API_WALLET_FILE", "data/hl_api_wallet.json")
HL_REAL_ACCOUNT_ADDRESS = os.getenv("HL_REAL_ACCOUNT_ADDRESS", "")
HL_REAL_BASE_URL = os.getenv("HL_REAL_BASE_URL", "https://api.hyperliquid.xyz")
HL_REAL_TAIL_SECONDS = float(os.getenv("HL_REAL_TAIL_SECONDS", "5"))
```

Append to `.env.example`: one line per knob above with its default and a `# HL real copy leg` header comment.

- [ ] **Step 4: Run test to verify it passes** — `python3 -m pytest tests/test_hl_real_settings.py -q` → 2 passed
- [ ] **Step 5: Commit** — `git add sniper/settings/hl_real.py tests/test_hl_real_settings.py .env.example && git commit -m "feat(hl-real): settings module (fail-closed defaults)"`

---

### Task 2: Schema + store layer

**Files:**
- Modify: `sniper/hyperlive/intel_store.py` (extend `ensure_schema`)
- Create: `sniper/hyperlive/real_store.py`
- Test: `tests/test_hl_real_store.py`

**Interfaces:**
- Produces (all take `conn` first):
  - `ensure_schema(conn)` (intel_store — extended, idempotent)
  - `get_or_create_deployment(conn, *, now_ms) -> dict` — `{"deployment_id": str, "epoch_ms": int}` from `hl_meta` keys `hl_real_deployment_id` / `hl_real_epoch_ms`; creates on first call (`deployment_id = hex(now_ms)`)
  - `record_intent(conn, *, cloid, deployment_id, copy_id, leg, coin, side, requested_usd, requested_sz, state, reason, now_ms)` — INSERT OR IGNORE (idempotent by cloid)
  - `set_intent_state(conn, cloid, state, *, reason=None, now_ms)` 
  - `intents_in(conn, states: list[str]) -> list[dict]`
  - `intent_for(conn, copy_id: int, leg: str) -> dict | None`
  - `record_fill(conn, *, cloid, copy_id, leg, fill_px, fill_sz, filled_ts_ms)` + `finalize_close(conn, *, cloid, realized_usd, paper_twin_pnl_bps, real_net_bps, drift_bps)` + `set_fill_fee(conn, cloid, fee_usd)`
  - `leg_open_positions(conn) -> list[dict]` — open-leg `filled|partial` intents whose close-leg intent is not `filled` — each `{copy_id, coin, side, fill_px, fill_sz, cloid}`
  - `day_realized_usd(conn, day_utc: str) -> float`
  - `breaker_latched(conn) -> str | None` / `latch_breaker(conn, *, day_utc, now_ms)` / `clear_breaker(conn)` (meta key `hl_real_breaker`)

- [ ] **Step 1: Write the failing tests** — cover: idempotent ensure_schema; deployment created once and stable across calls; intent insert idempotent by cloid; state transition updates `updated_ts_ms`; `leg_open_positions` excludes rows with a filled close intent; `day_realized_usd` sums only that UTC day; breaker latch/read/clear round-trip.

```python
"""Store layer for the HL real copy leg (hl_real_intents / hl_real_fills / hl_meta keys)."""
from sniper.hyperlive import intel_store as IS
from sniper.hyperlive import real_store as RS


def _conn(tmp_path):
    c = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def test_deployment_created_once(tmp_path):
    c = _conn(tmp_path)
    d1 = RS.get_or_create_deployment(c, now_ms=1000)
    d2 = RS.get_or_create_deployment(c, now_ms=9999)
    assert d1 == d2 and d1["epoch_ms"] == 1000


def test_intent_idempotent_and_states(tmp_path):
    c = _conn(tmp_path)
    kw = dict(cloid="0xabc", deployment_id="d1", copy_id=7, leg="open", coin="ZEC",
              side="long", requested_usd=15.0, requested_sz=0.1, state="intended",
              reason=None, now_ms=1000)
    RS.record_intent(c, **kw)
    RS.record_intent(c, **kw)                                    # replay-safe
    assert len(RS.intents_in(c, ["intended"])) == 1
    RS.set_intent_state(c, "0xabc", "submitted", now_ms=2000)
    assert RS.intents_in(c, ["submitted"])[0]["updated_ts_ms"] == 2000


def test_leg_open_positions_excludes_closed(tmp_path):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xo1", deployment_id="d", copy_id=1, leg="open", coin="ZEC",
                     side="long", requested_usd=15.0, requested_sz=0.1, state="filled",
                     reason=None, now_ms=1)
    RS.record_fill(c, cloid="0xo1", copy_id=1, leg="open", fill_px=100.0, fill_sz=0.1,
                   filled_ts_ms=1)
    assert [p["copy_id"] for p in RS.leg_open_positions(c)] == [1]
    RS.record_intent(c, cloid="0xc1", deployment_id="d", copy_id=1, leg="close", coin="ZEC",
                     side="long", requested_usd=None, requested_sz=None, state="filled",
                     reason=None, now_ms=2)
    assert RS.leg_open_positions(c) == []


def test_breaker_and_day_pnl(tmp_path):
    c = _conn(tmp_path)
    assert RS.breaker_latched(c) is None
    RS.latch_breaker(c, day_utc="2026-07-18", now_ms=5)
    assert RS.breaker_latched(c) == "2026-07-18"
    RS.clear_breaker(c)
    assert RS.breaker_latched(c) is None
```

- [ ] **Step 2: Run to verify FAIL** — `python3 -m pytest tests/test_hl_real_store.py -q`
- [ ] **Step 3: Implement.** In `intel_store.ensure_schema` add (after existing DDL):

```sql
CREATE TABLE IF NOT EXISTS hl_real_intents (
  cloid TEXT PRIMARY KEY,
  deployment_id TEXT NOT NULL,
  copy_id INTEGER NOT NULL,
  leg TEXT NOT NULL CHECK(leg IN ('open','close')),
  coin TEXT NOT NULL, side TEXT NOT NULL,
  requested_usd REAL, requested_sz REAL,
  state TEXT NOT NULL DEFAULT 'intended',
  reason TEXT,
  created_ts_ms INTEGER NOT NULL, updated_ts_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hl_real_intents_copy_leg ON hl_real_intents(copy_id, leg);
CREATE INDEX IF NOT EXISTS idx_hl_real_intents_state ON hl_real_intents(state);
CREATE TABLE IF NOT EXISTS hl_real_fills (
  cloid TEXT PRIMARY KEY, copy_id INTEGER NOT NULL, leg TEXT NOT NULL,
  fill_px REAL, fill_sz REAL, fee_usd REAL, filled_ts_ms INTEGER,
  realized_usd REAL, paper_twin_pnl_bps REAL, real_net_bps REAL, drift_bps REAL
);
```

`real_store.py` implements the Produces list with plain parameterized SQL; `leg_open_positions` = open-leg intents in (`filled`,`partial`) LEFT JOIN their fill, excluding copy_ids having a close-leg intent in (`filled`,); `day_realized_usd` = `SELECT COALESCE(SUM(realized_usd),0) FROM hl_real_fills WHERE leg='close' AND realized_usd IS NOT NULL AND date(filled_ts_ms/1000,'unixepoch')=?`. Breaker via `hl_meta` (`INSERT OR REPLACE key='hl_real_breaker'`).

- [ ] **Step 4: Run to verify PASS** (plus `tests/test_hyperlive_intel_store.py` still green)
- [ ] **Step 5: Commit** — `feat(hl-real): intent/fill schema + store layer`

---

### Task 3: Pure core — cloid + eligibility

**Files:**
- Create: `sniper/hyperlive/real_exec.py`
- Test: `tests/test_hl_real_exec.py`

**Interfaces:**
- Produces: `derive_cloid(deployment_id: str, copy_id: int, leg: str) -> str`; `eligibility(row: dict, *, allow_targets: list[str], epoch_ms: int) -> tuple[bool, str]`

- [ ] **Step 1: Failing tests**

```python
from sniper.hyperlive import real_exec as RE


def test_cloid_deterministic_128bit():
    a = RE.derive_cloid("dep1", 42, "open")
    assert a == RE.derive_cloid("dep1", 42, "open")
    assert a != RE.derive_cloid("dep1", 42, "close")
    assert a != RE.derive_cloid("dep2", 42, "open")
    assert a.startswith("0x") and len(a) == 34        # 0x + 32 hex = 128 bit


def _row(**kw):
    base = dict(target="0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8",
                copy_tier="proven", is_cold_start=0, detect_ts_ms=2000)
    base.update(kw)
    return base


def test_eligibility_gates():
    allow = ["0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"]
    assert RE.eligibility(_row(), allow_targets=allow, epoch_ms=1500) == (True, "ok")
    assert RE.eligibility(_row(target="0xdead"), allow_targets=allow, epoch_ms=0)[1] == "target_not_allowed"
    assert RE.eligibility(_row(copy_tier="watch_fallback"), allow_targets=allow, epoch_ms=0)[1] == "tier_not_proven"
    assert RE.eligibility(_row(is_cold_start=1), allow_targets=allow, epoch_ms=0)[1] == "cold_start"
    assert RE.eligibility(_row(detect_ts_ms=100), allow_targets=allow, epoch_ms=1500)[1] == "pre_epoch"
```

- [ ] **Step 2: FAIL** → **Step 3: Implement** (sha256 as in Global Constraints; eligibility checks in the order tested, target compare lowercased) → **Step 4: PASS** → **Step 5: Commit** `feat(hl-real): deterministic cloids + eligibility core`

---

### Task 4: Pure core — sizing + entry rails

**Files:**
- Modify: `sniper/hyperlive/real_exec.py`
- Test: `tests/test_hl_real_exec.py` (extend)

**Interfaces:**
- Produces:
  - `size_coin(usd: float, mark: float, sz_decimals: int) -> float | None` (None = below $10 min or bad mark)
  - `chase_bps(side: str, paper_mark: float, mark: float) -> float`
  - `@dataclass EntryDecision(action: str, reason: str, sz: float | None = None)` with `action in ('submit','skip')`
  - `entry_decision(row, *, enabled, breaker_latched, open_n, total_notional, mark, sz_decimals, cfg) -> EntryDecision` — `cfg` is the settings module (duck-typed)

- [ ] **Step 1: Failing tests**

```python
def test_size_coin_floors_and_min_notional():
    assert RE.size_coin(15.0, 100.0, 2) == 0.15
    assert RE.size_coin(15.0, 7.0, 1) == 2.1          # floor(2.142..,1dp)
    assert RE.size_coin(15.0, 200000.0, 5) is None    # rounds to $10-min fail
    assert RE.size_coin(15.0, 0.0, 2) is None


def test_chase_bps_signed_by_side():
    assert round(RE.chase_bps("long", 100.0, 101.0)) == 100    # paid up = positive chase
    assert round(RE.chase_bps("short", 100.0, 99.0)) == 100
    assert RE.chase_bps("long", 100.0, 99.0) < 0


class _Cfg:
    HL_REAL_PER_POS_USD = 15.0
    HL_REAL_MAX_CONCURRENT = 2
    HL_REAL_MAX_TOTAL_NOTIONAL = 30.0
    HL_REAL_MAX_CHASE_BPS = 100.0


def _dec(**kw):
    base = dict(enabled=True, breaker_latched=False, open_n=0, total_notional=0.0,
                mark=100.0, sz_decimals=2, cfg=_Cfg)
    base.update(kw)
    return RE.entry_decision({"side": "long", "entry_mark": 100.0}, **base)


def test_entry_rails_order_and_reasons():
    assert _dec().action == "submit" and _dec().sz == 0.15
    assert _dec(enabled=False).reason == "disabled"
    assert _dec(breaker_latched=True).reason == "breaker_latched"
    assert _dec(open_n=2).reason == "max_concurrent"
    assert _dec(total_notional=20.0).reason == "max_total_notional"   # 20+15 > 30
    assert _dec(mark=101.5).reason == "chase_guard"                   # 150bps > 100
    assert _dec(mark=200000.0, sz_decimals=5).reason == "below_min_notional"
```

- [ ] **Step 2: FAIL** → **Step 3: Implement**

```python
def size_coin(usd, mark, sz_decimals):
    if not mark or mark <= 0:
        return None
    step = 10 ** sz_decimals
    sz = math.floor((usd / mark) * step) / step
    if sz <= 0 or sz * mark < 10.0:                    # HL $10 min order
        return None
    return sz


def chase_bps(side, paper_mark, mark):
    g = (mark - paper_mark) / paper_mark * 1e4
    return g if side == "long" else -g


def entry_decision(row, *, enabled, breaker_latched, open_n, total_notional,
                   mark, sz_decimals, cfg):
    if not enabled:
        return EntryDecision("skip", "disabled")
    if breaker_latched:
        return EntryDecision("skip", "breaker_latched")
    if open_n >= cfg.HL_REAL_MAX_CONCURRENT:
        return EntryDecision("skip", "max_concurrent")
    if total_notional + cfg.HL_REAL_PER_POS_USD > cfg.HL_REAL_MAX_TOTAL_NOTIONAL:
        return EntryDecision("skip", "max_total_notional")
    if chase_bps(row["side"], row["entry_mark"], mark) > cfg.HL_REAL_MAX_CHASE_BPS:
        return EntryDecision("skip", "chase_guard")
    sz = size_coin(cfg.HL_REAL_PER_POS_USD, mark, sz_decimals)
    if sz is None:
        return EntryDecision("skip", "below_min_notional")
    return EntryDecision("submit", "ok", sz=sz)
```

- [ ] **Step 4: PASS** → **Step 5: Commit** `feat(hl-real): sizing + entry rails core`

---

### Task 5: Pure core — order-result normalization + recovery settlement + drift math

**Files:**
- Modify: `sniper/hyperlive/real_exec.py`
- Test: `tests/test_hl_real_exec.py` (extend)

**Interfaces:**
- Produces:
  - `normalize_order_result(resp: dict | Exception, requested_sz: float) -> dict` — `{"state": "filled|partial|rejected|ambiguous", "fill_px": float|None, "fill_sz": float|None}` from the SDK order response (`resp["response"]["data"]["statuses"][0]` carrying `filled {totalSz, avgPx}` or `error`); any exception/malformed → `ambiguous`
  - `settle_from_status(status: dict | None) -> str | None` — from `query_order_by_cloid` result to final state, `None` = still unknown
  - `close_accounting(open_fill: dict, close_fill: dict, *, fees_usd: float, paper_pnl_bps: float, side: str) -> dict` — `{"realized_usd", "real_net_bps", "drift_bps"}`; `drift_bps = real_net_bps - paper_pnl_bps` (signed; kill reads use mean drift < -100)

- [ ] **Step 1: Failing tests** — filled (totalSz == requested), partial (0 < totalSz < requested), error status → rejected, raised exception → ambiguous; `close_accounting` long: open 0.15@100, close 0.15@102, fees 0.03 → realized ≈ 0.27, real_net_bps ≈ 180, drift vs paper 200 → −20; short symmetric.

```python
def test_normalize_order_result_states():
    ok = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.15", "avgPx": "101.0"}}]}}}
    r = RE.normalize_order_result(ok, requested_sz=0.15)
    assert r == {"state": "filled", "fill_px": 101.0, "fill_sz": 0.15}
    part = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.10", "avgPx": "101.0"}}]}}}
    assert RE.normalize_order_result(part, requested_sz=0.15)["state"] == "partial"
    err = {"status": "ok", "response": {"data": {"statuses": [{"error": "slippage"}]}}}
    assert RE.normalize_order_result(err, requested_sz=0.15)["state"] == "rejected"
    assert RE.normalize_order_result(RuntimeError("timeout"), 0.15)["state"] == "ambiguous"


def test_close_accounting_signed_drift():
    out = RE.close_accounting({"fill_px": 100.0, "fill_sz": 0.15},
                              {"fill_px": 102.0, "fill_sz": 0.15},
                              fees_usd=0.03, paper_pnl_bps=200.0, side="long")
    assert abs(out["realized_usd"] - 0.27) < 1e-9
    assert abs(out["real_net_bps"] - 180.0) < 0.01
    assert abs(out["drift_bps"] - (-20.0)) < 0.01
    short = RE.close_accounting({"fill_px": 100.0, "fill_sz": 0.15},
                                {"fill_px": 98.0, "fill_sz": 0.15},
                                fees_usd=0.0, paper_pnl_bps=100.0, side="short")
    assert short["realized_usd"] > 0 and abs(short["real_net_bps"] - 200.0) < 0.01
```

- [ ] **Step 2: FAIL** → **Step 3: Implement** (`realized_usd = sign*(close_px-open_px)*min(sz) - fees`; `real_net_bps = realized_usd / (open_px*open_sz) * 1e4`) → **Step 4: PASS** → **Step 5: Commit** `feat(hl-real): order normalization + close accounting`

---

### Task 6: Pure core — reconcile diff

**Files:**
- Modify: `sniper/hyperlive/real_exec.py`
- Test: `tests/test_hl_real_exec.py` (extend)

**Interfaces:**
- Produces: `reconcile(book: list[dict], exchange_positions: dict[str, dict], *, sz_tol: float = 1e-9) -> list[dict]` — book from `real_store.leg_open_positions`; exchange `{coin: {"szi": float}}`; returns faults `[{"kind": "missing_on_exchange"|"unknown_on_exchange"|"size_mismatch", "coin": ..., "copy_id": ...|None}]`; empty = clean

- [ ] **Step 1: Failing tests** — clean match (long → positive szi); missing on exchange; unknown coin on exchange; size mismatch beyond tolerance; sign encodes side (book long vs negative szi → mismatch).
- [ ] **Step 2: FAIL** → **Step 3: Implement** → **Step 4: PASS** → **Step 5: Commit** `feat(hl-real): reconcile diff core`

---

### Task 7: SDK adapter

**Files:**
- Modify: `requirements.txt` (add pinned `hyperliquid-python-sdk`)
- Create: `sniper/hyperlive/hl_exchange.py`
- Test: `tests/test_hl_exchange_adapter.py`

**Interfaces:**
- Produces: `class HlExchange` with `__init__(self, *, api_wallet_file, account_address, base_url, entry_slippage, exit_slippage, sdk_exchange=None, sdk_info=None)` (injected fakes in tests; real SDK built lazily otherwise) and methods:
  - `market_open(coin, is_buy, sz, cloid_hex) -> dict` (raw SDK resp; ALWAYS passes `slippage=entry_slippage` and the cloid)
  - `market_close(coin, cloid_hex) -> dict` (SDK `market_close`; ALWAYS `slippage=exit_slippage`; SDK issues reduce-only IOC)
  - `set_isolated_1x(coin)` (`update_leverage(1, coin, is_cross=False)`)
  - `order_status(cloid_hex) -> dict | None`; `positions() -> dict[str, dict]` (from `user_state` assetPositions → `{COIN: {"szi": float, "entry_px": float}}`); `sz_decimals(coin) -> int` (cached from `info.meta()`); `fee_for_cloid(cloid_hex) -> float | None` (scan `user_fills`, sum `fee` for matching cloid)

- [ ] **Step 1: Install + verify SDK surface** — `pip install "hyperliquid-python-sdk"` then pin the installed version in `requirements.txt`. Introspect before coding (the adapter must match reality, not memory): `python3 -c "from hyperliquid.exchange import Exchange; import inspect; print(inspect.signature(Exchange.market_open)); print(inspect.signature(Exchange.market_close)); print(inspect.signature(Exchange.update_leverage))"` and same for `Info.query_order_by_cloid`, `Info.user_state`, `Info.meta`, `Info.user_fills`, `hyperliquid.utils.types.Cloid`. Adjust the adapter calls below to the ACTUAL signatures; keep the adapter's own interface as specified.
- [ ] **Step 2: Failing tests with a fake SDK** — fake `Exchange`/`Info` records every call + kwargs. Assert: `market_open` passes `slippage == 0.005` and a `Cloid`; `market_close` passes `slippage == 0.01`; `set_isolated_1x` passes `(1, coin, is_cross=False)`; `positions()` parses a canned `user_state` payload; `sz_decimals` reads canned `meta`; missing-cloid `order_status` returns None. A test MUST fail if any call omits explicit slippage.
- [ ] **Step 3: FAIL → implement adapter → PASS.** Constructor stores config; `_ex`/`_info` lazily constructed from the SDK using `eth_account.Account.from_key(json.load(api_wallet_file)["private_key"])` with `account_address=` the master (API-wallet signing per spec §4) unless fakes injected.
- [ ] **Step 4: Commit** `feat(hl-real): SDK adapter (explicit slippage, api-wallet signing)`

---

### Task 8: Notifier

**Files:**
- Create: `sniper/hyperlive/real_notify.py`
- Test: `tests/test_hl_real_notify.py`

**Interfaces:**
- Produces: `notify(text: str) -> bool` — prefixes `[hl-real]`, delegates to `sniper.services.memecoin_telegram.send_telegram` (verified: resolves `MEMECOIN_TG_*` then `TELEGRAM_*`, fail-closed `(False, 0.0)` when unconfigured); returns its `ok`. Never raises.

- [ ] Steps: failing test (monkeypatch `send_telegram`, assert prefix + passthrough + exception swallowed) → implement (~10 lines) → PASS → commit `feat(hl-real): telegram notifier`

---

### Task 9: Runner loop

**Files:**
- Create: `scripts/run_hl_real.py`
- Test: `tests/test_run_hl_real.py`

**Interfaces:**
- Consumes: everything above. Structure mirrors `scripts/run_hl_copy.py` (argparse, `--once`, `--db-path` default `/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite`, stderr log line per cycle, heartbeat JSON).
- Produces: `cycle(conn, ex, cfg, now_ms) -> dict` (importable, fully injectable) returning counters `{"opened": int, "closed": int, "skipped": int, "faults": int}`; `main(argv)`.

Cycle order (each phase wrapped in try/except with `logger.exception` + `notify` on new fault):
1. `settle_pending` — for intents in (`submitted`,`ambiguous`): `ex.order_status(cloid)` → `settle_from_status`; fee backfill via `fee_for_cloid` for fills missing `fee_usd`.
2. `process_closes` — ALWAYS (kill switch never blocks): for each `real_store.leg_open_positions`, if its paper twin row is closed (`status!='open'` in `hl_copy_book`) and no close intent yet → derive close cloid, `record_intent(state='intended')`, then (unless DRY_RUN) `market_close` → `normalize_order_result` → `set_intent_state` + `record_fill` + `close_accounting` (paper twin `pnl_bps` from the copy row) + `finalize_close`; DRY_RUN → state `dry_run`.
3. `process_opens` — only if `HL_REAL_ENABLED` and breaker clear: new `hl_copy_book` rows with `detect_ts_ms >= epoch` and no open intent → `eligibility` → `entry_decision` (marks via `hl_exchange.positions`/`fetch_marks`; sz_decimals via adapter) → skip-intents recorded with reason; submits: `set_isolated_1x` once per coin (cache), `record_intent('intended')` THEN `market_open` (write-ahead), normalize → state + fill.
4. Day-roll + breaker: if `day_realized_usd(today) <= -cfg.HL_REAL_DAY_LOSS_BREAKER_USD` and not latched → `latch_breaker` + notify.
5. Every 6th cycle: `reconcile(leg_open_positions, ex.positions())`; faults → notify once per fault signature + block opens (in-memory flag) until clean or resolved.
6. Heartbeat `data/hl_real.heartbeat`: `{ts, enabled, dry_run, open_n, pending, breaker, faults}`.

- [ ] **Step 1: Failing tests** (fake conn via real intel_store on tmp DB + seeded `hl_copy_book` rows; fake exchange). Cover, minimum:
  - eligible paper open → open intent + fill recorded (fake returns filled)
  - paper close → close intent + `finalize_close` with correct drift sign
  - `HL_REAL_ENABLED=0` → opens skipped (`disabled`), closes STILL processed
  - DRY_RUN → intents recorded `dry_run`, fake exchange NEVER called
  - crash recovery: intent left `submitted`, fake `order_status` says filled → settled, no duplicate submit (cloid idempotence)
  - pre-epoch paper row → single `skipped/pre_epoch` intent, never resubmitted
  - reconcile fault → `faults>0` + opens blocked that cycle
- [ ] **Step 2: FAIL → implement → PASS** (also `python3 -m pytest tests/test_hyperlive* tests/test_hl_* -q` full green)
- [ ] **Step 3: Commit** `feat(hl-real): runner loop (write-ahead intents, entries-only kill switch)`

---

### Task 10: Ops CLI (resolve / breaker / epoch)

**Files:**
- Create: `scripts/hl_real_resolve.py`
- Test: `tests/test_hl_real_resolve.py`

**Interfaces:**
- Produces CLI: `--list` (ambiguous intents + faults), `--settle CLOID --as filled|rejected` (manual after exchange-UI check), `--clear-breaker`, `--show-deployment`. No `--reset-epoch` (YAGNI — re-arming a new deployment = new DB row via `--new-deployment`, which archives nothing and just writes new meta keys after operator confirm `--yes`).

- [ ] Steps: failing tests for settle + clear-breaker + new-deployment-requires-yes → implement (argparse, reuse real_store) → PASS → commit `feat(hl-real): ops CLI`

---

### Task 11: Watchdog + paper cadence

**Files:**
- Create: `scripts/hl_real_watchdog.sh` (copy `scripts/hl_copy_watchdog.sh` shape: pgrep exact match → relaunch `run_hl_real.py` with nohup, log to `.logs/hl_real.log`)
- Modify: `scripts/hl_copy_watchdog.sh` — relaunch flag `--interval-seconds 120` → `--interval-seconds 30` (measured: 19 s cycle + sleep → ~50 s cadence)

- [ ] **Step 1:** Write watchdog; `bash -n` both scripts (syntax check).
- [ ] **Step 2:** Restart paper runner under new cadence: `kill <run_hl_copy pid>` → watchdog relaunches within 1 min → verify two consecutive `.logs/hl_copy.log` lines ~50 s apart and `open=`/`closed=` counters continuous (no book reset).
- [ ] **Step 3:** Print (do NOT install) the cron line for the real watchdog — installing it is part of the arming runbook, not this build: `* * * * * /usr/bin/env bash /home/bigpapi/Desktop/Sniper/scripts/hl_real_watchdog.sh >/dev/null 2>&1`
- [ ] **Step 4: Commit** `feat(hl-real): watchdog + paper cadence 120s->30s flag`

---

### Task 12: Status surface

**Files:**
- Modify: `sniper/routes/api_hyperlive.py` (`_copy_scorecard_payload`)
- Test: `tests/test_api_hyperlive_copy_scorecard.py` (extend)

**Interfaces:**
- Produces: payload key `hl_real_status`: `{"deployment": {...}|None, "enabled": bool, "dry_run": bool, "open_n": int, "intents": {state: n}, "closes_n": int, "mean_drift_bps": float|None, "day_realized_usd": float, "breaker": str|None}` — read-only from real_store; `closes_n`/`mean_drift_bps` from `hl_real_fills` where `drift_bps IS NOT NULL`. NOTE: `mean_drift_bps` is surfaced for ops honesty but the §11 verdict stays sealed until 20 closes — the UI label must say "read at n=20".

- [ ] Steps: failing test (seed intents/fills → assert block shape + drift mean) → implement → PASS → commit `feat(hl-real): status block in copy-scorecard payload`

---

### Task 13: Agent-wallet setup + testnet smoke (operator-gated)

**Files:**
- Create: `scripts/hl_real_setup_agent.py`, `scripts/hl_real_testnet_smoke.py`

**Interfaces:**
- `hl_real_setup_agent.py`: generates a fresh secp256k1 key → writes `data/hl_api_wallet.json` `{"address", "private_key"}` chmod 0600 → signs ApproveAgent with the MASTER key (`MEMECOIN_EVM_KEYPAIR`, loaded only in this one-shot script, never by the runner) via SDK `exchange.approve_agent()` → prints the agent address. `--testnet` flag targets `https://api.hyperliquid-testnet.xyz`.
- `hl_real_testnet_smoke.py`: against testnet + a testnet-funded account: `set_isolated_1x("ETH")` → `market_open` $15 → poll `order_status` by cloid → `market_close` → assert both fills land and print px/sz/fee. Exits non-zero on any failure.

- [ ] **Step 1:** Write both scripts (reuse `HlExchange`; no new logic). Unit-test only the pure parts (key file shape, 0600 perms) in `tests/test_hl_real_setup.py`; network paths are exercised by the smoke run itself.
- [ ] **Step 2:** Commit `feat(hl-real): agent setup + testnet smoke scripts`
- [ ] **Step 3 (OPERATOR GATE — do not run unattended):** testnet faucet + smoke run, then dry-run soak on mainnet data (`HL_REAL_DRY_RUN=1`, watch intents accrue vs paper opens for ≥24 h), THEN the separate funding decision (spec §12).

---

## Verification (whole-feature)

1. `python3 -m pytest tests/test_hl_real* tests/test_hl_exchange_adapter.py tests/test_run_hl_real.py tests/test_hyperlive* tests/test_api_hyperlive* -q` — all green.
2. `python3 scripts/validate_schema_refs.py` — clean.
3. `python3 scripts/brain_index.py generate` — index fresh (new files indexed).
4. Dry-run once: `HL_REAL_DRY_RUN=1 python3 scripts/run_hl_real.py --once` → heartbeat written, 0 submissions, intents match currently-eligible rows (expected: zero — no post-epoch proven opens yet).
5. Paper cadence: two log lines ~50 s apart, book counters continuous.
