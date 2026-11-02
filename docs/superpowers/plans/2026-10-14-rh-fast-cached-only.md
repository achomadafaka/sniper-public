# 4663 Fast Lane Cached-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The 4663 fast lane sends only on a cached safety verdict, and every send records its time
from feed arrival and the provider that accepted it. The Chainstack inline host keeps the cache
warm while idle. eth_keys signs and recovers on coincurve, proven byte-identical. The m5 gate read
gets its own substage mark.

**Architecture:**
- The lane refuses a cache miss with the precheck's own reason. The dispatcher releases the
  `LaneHolds` entry, so the on-chain lane and J7 judge it as they do today.
- A second background thread warms `SafetyCache` through the inline host, idle by construction
  while cached-only is on.
- coincurve is a dependency switch. eth_keys picks its backend at import, so there is no code
  change on the signing path.

**Tech Stack:** Python 3.13, SQLite (lane-local `data/memecoin_rh_fast_lane.db`), eth_account /
eth_keys, coincurve 21.0.0, pytest (`-n 4` max).

**Spec:** `docs/superpowers/specs/2026-10-14-rh-fast-cached-only-design.md`. Revision 2 in its
section 0 governs: (a1) dropped, the shadow move dropped, the m5 prefetch rejected, and the
Chainstack warmer in its 3.6.

## Global Constraints

- Knob `MEMECOIN_RH_FAST_CACHED_ONLY`, default `1`, read per signal; only `CHAIN_ID == 4663`.
- Knob `MEMECOIN_RH_FAST_INLINE_WARM`, default `1`.
- Knob `MEMECOIN_RH_FAST_INLINE_WARM_GAP_S`, default `0.25`.
- Knob `MEMECOIN_WARM_STATS_GAP_S`, default `1.0`: one stats pacer shared by every lane's warmer (Task 0).
- Warmer pause on a 429 or quota answer: 600 s, counted.
- `coincurve==21.0.0` pinned in `requirements.txt`. The runtime install is Alpha's at deploy, never
  ours.
- No new synchronous provider call, lock or retry on the fill path. New columns are additive
  (`add_missing_columns`). Unknown is NULL, never 0.
- Tests: targeted files only, `-n 4` at most, watchdog tests excluded. Gate every commit on the
  pytest exit code in the same command. Commit with explicit pathspecs.

## Review Focus

1. **A cached verdict with missing facts or stats.** It must still refuse (`no_mcap` /
   `no_market_stats`) with no inline read. Covered in Task 1.
2. **The knob turned off while the warmer runs.** The warmer must stop issuing inline reads before
   the fill path uses the host again. Covered in Task 5.
3. **Two warmers picking the same coin.** The second run returns at once, with no duplicate
   provider calls. Covered in Task 5.
4. **A provider failure on the inline host.** It must never cache a refusal as an unsafe verdict.
   Covered in Task 5.
5. **A receipt clock from another boot, or absent.** `feed_to_send_ms` is NULL, never 0 or
   negative. Covered in Task 2.

---

### Task 0: The shared warm set, tier rule and stats pacer (agreed with Beta; 56 builds on this SHA)

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`: `armed_bought_mints`, `armed_sold_mints`
- Modify: `sniper/settings/memecoin.py`: `safety_warm_window_s(chain)`, `MEMECOIN_WARM_STATS_GAP_S`
- Modify: `sniper/services/memecoin_rh_fast_decide.py`:
  - `make_candidates`: tier 1 and activity at 10+rank;
  - the candidates block: the helpers, keeping the previous set on a read failure;
  - delete `_armed_bought_mints`.
- Modify: `sniper/services/memecoin_rh_fast_lane.py`:
  - `WarmStatsPacer` and the module-level `WARM_STATS`;
  - `SafetyCache` per-tier lookup counters.
- Create: `tests/test_memecoin_safety_warm_tiers.py`

**Interfaces:**
- Produces:
  - `db_memecoin.armed_bought_mints(ledger_path, chain, since_iso) -> set[str]`;
  - `db_memecoin.armed_sold_mints(ledger_path, chain, since_iso) -> set[str]`;
  - `cfg.safety_warm_window_s(chain) -> float` (env `MEMECOIN_SAFETY_WARM_WINDOW_S_<chain>`, default 86400);
  - `make_candidates(tape, now, *, signal_mints, armed_mints, k, recent_mints=())`;
  - `WARM_STATS.take() -> bool` (one stats call per `MEMECOIN_WARM_STATS_GAP_S` across every lane's warmer in the process);
  - `SafetyCache.lookups` (Counter `"<tier>:hit|miss"`) and `SafetyCache.hit_age_s` (per-tier `[sum, n]`).
- Tiers (priority):
  - -1: pre-buy;
  - 0: signal coins and coins any lane decided on in the last 1 h;
  - 1: decided on or sold by an armed wallet in the window;
  - 10+rank: activity.
  `SafetyCache.next_due` takes the minimum, so activity runs only when no tier 0/1 coin is due.

- [ ] **Step 1: Failing tests**

```python
"""The shared safety warm set (lambda + Beta, 2026-10-14): tiers, the armed helpers, one stats pacer."""
import sqlite3
import time

from sniper.services import memecoin_rh_fast_decide as fast
from sniper.services import memecoin_rh_fast_lane as lane


def test_tiers_order_signals_then_the_window_then_activity(monkeypatch):
    monkeypatch.setattr(fast, "lane_route", lambda tape, mint, now: {"kind": "v4_native", "pool": "p", "key": {}})

    class Tape:
        def busiest(self, now):
            return ["0xbusy", "0xrecent"]
    got = {e["mint"]: e["priority"] for e in fast.make_candidates(
        Tape(), 0.0, signal_mints={"0xsig"}, armed_mints={"0xhour"}, k=5, recent_mints={"0xrecent", "0xhour"})}
    assert got == {"0xsig": 0, "0xhour": 0, "0xrecent": 1, "0xbusy": 11}


def _ledger(path):
    from sniper import db
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    db.add_wallet(conn, "uid", "Degen")
    db.set_wallet_real_approved(conn, "uid", True, chains=["4663"], force=True)
    return conn


def test_the_armed_helpers_read_decided_and_sold_mints_for_one_chain(tmp_path):
    from sniper.persistence import db_memecoin as m
    path = str(tmp_path / "ledger.db")
    conn = _ledger(path)
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, decision_started_at, "
                 "completed_at) VALUES ('a', '4663', '0xAA', '2026-10-14T10:00:00+00:00', '2026-10-14T10:00:01+00:00')")
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, network_id, mint, decision_started_at, "
                 "completed_at) VALUES ('b', '56', '0xbb', '2026-10-14T10:00:00+00:00', '2026-10-14T10:00:01+00:00')")
    conn.execute("INSERT INTO memecoin_events (source_user_id, mint, side, created_at, network_id) "
                 "VALUES ('uid', '0xCC', 'sell', '2026-10-14T10:00:00+00:00', '4663')")
    conn.commit()
    since = "2026-10-14T09:00:00+00:00"
    assert m.armed_bought_mints(path, "4663", since) == {"0xaa"}
    assert m.armed_sold_mints(path, "4663", since) == {"0xcc"}
    assert m.armed_bought_mints(path, "4663", "2026-10-14T11:00:00+00:00") == set()


def test_one_stats_pacer_serves_every_warmer_in_the_process():
    clock = [100.0]
    pacer = lane.WarmStatsPacer(gap_s=1.0, clock=lambda: clock[0])
    assert pacer.take() and not pacer.take()
    clock[0] += 1.0
    assert pacer.take()


def test_lookups_count_hits_and_misses_by_tier():
    cache = lane.SafetyCache(lambda m, usd=None: (True, "r"), ttl_s=60, usd=50)
    cache.note_candidates([{"mint": "0xa", "priority": 0}, {"mint": "0xb", "priority": 12}], now=1000.0)
    cache._verdicts["0xa"] = (True, "r", 990.0, 0.0)
    assert cache.reason("0xa", now=1000.0) is None and cache.reason("0xb", now=1000.0) == "no_safety_verdict"
    assert cache.lookups == {"0:hit": 1, "activity:miss": 1} and cache.hit_age_s["0"] == [10.0, 1]
```

Adjust the two `INSERT`s to the tables' NOT NULL columns, from
`python3 scripts/brain_index.py schema memecoin_entry_attempts` / `memecoin_events`, if
`ensure_memecoin_tables` requires more.

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_safety_warm_tiers.py -q -p no:cacheprovider`

Expected: FAIL (`unexpected keyword argument 'recent_mints'`, `armed_bought_mints` missing,
`WarmStatsPacer` missing, `lookups` missing).

- [ ] **Step 3: Implement**

`db_memecoin.py`:

```python
def armed_bought_mints(ledger_path, chain, since_iso):
    """The mints ANY lane decided on for `chain` since `since_iso` (memecoin_entry_attempts): the
    safety warm set, not fills. Read-only, 250 ms busy timeout; an unreadable ledger raises and the
    caller keeps its previous set (lambda + Beta, 2026-10-14)."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(ledger_path))
    try:
        conn.execute("PRAGMA busy_timeout=250")
        return {str(r[0]).lower() for r in conn.execute(
            "SELECT DISTINCT mint FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_completed "
            "WHERE completed_at >= ? AND network_id = ? AND mint IS NOT NULL", (since_iso, str(chain)))}
    finally:
        conn.close()


def armed_sold_mints(ledger_path, chain, since_iso):
    """The mints wallets armed on `chain` sold since `since_iso` (memecoin_events side='sell'), one
    walk of idx_mc_events_user_time per armed wallet. Same read rules as armed_bought_mints."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(ledger_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=250")
        out = set()
        for uid in sorted(real_approved_user_ids(conn, network_id=chain)):
            out.update(str(r[0]).lower() for r in conn.execute(
                "SELECT DISTINCT mint FROM memecoin_events WHERE source_user_id = ? AND created_at >= ? "
                "AND side = 'sell' AND network_id = ? AND mint IS NOT NULL", (uid, since_iso, str(chain))))
        return out
    finally:
        conn.close()
```

`settings/memecoin.py`:

```python
def safety_warm_window_s(chain):
    """The safety warm set's tier-1 window per chain (MEMECOIN_SAFETY_WARM_WINDOW_S_<chain>, 24 h)."""
    return float(os.getenv("MEMECOIN_SAFETY_WARM_WINDOW_S_%s" % chain, "86400"))


MEMECOIN_WARM_STATS_GAP_S = float(os.getenv("MEMECOIN_WARM_STATS_GAP_S", "1.0"))
```

`make_candidates` gains `recent_mints=()`:
- after the tier-0 loop, add every routed mint of `set(recent_mints) - listed` with priority 1;
- the activity loop appends `priority=10 + rank`;
- update the docstring.

The decide's candidates block replaces `_armed_bought_mints(...)`. `warm` is a dict kept in
`start_decide`'s closure, initialised `{"hour": set(), "recent": set()}`.

```python
                    try:
                        now_s = real_utc_now()
                        iso = lambda s: datetime.fromtimestamp(s, timezone.utc).isoformat()
                        hour = m.armed_bought_mints(ledger_path, "4663", iso(now_s - 3600))
                        window = cfg.safety_warm_window_s("4663")
                        recent = (m.armed_bought_mints(ledger_path, "4663", iso(now_s - window))
                                  | m.armed_sold_mints(ledger_path, "4663", iso(now_s - window)))
                        warm.update(hour=hour, recent=recent)
                    except Exception:
                        logger.warning("rh fast: armed warm set unreadable; keeping the last one", exc_info=True)
                    armed = warm["hour"]
```

Pass `recent_mints=warm["recent"]` to `make_candidates`. Delete `_armed_bought_mints`, and repoint
its callers and tests (`grep -rn _armed_bought_mints`).

`memecoin_rh_fast_lane.py`:

```python
class WarmStatsPacer:
    """One DexScreener stats call per `gap_s` across every lane's warmer in this process (lambda +
    Beta, 2026-10-14): the provider's 429 is per IP, stops every lane stats call until restart, and
    the funded breaker shares it."""

    def __init__(self, gap_s, clock=time.monotonic):
        self.gap_s, self.clock, self._at, self._lock = float(gap_s), clock, -1e18, threading.Lock()

    def take(self):
        with self._lock:
            if self.clock() - self._at < self.gap_s:
                return False
            self._at = self.clock()
            return True


WARM_STATS = WarmStatsPacer(lane_cfg.MEMECOIN_WARM_STATS_GAP_S)
```

`SafetyCache`:
- `__init__`: `self.lookups, self.hit_age_s = collections.Counter(), {}`.
- In `reason()`, under the lock it already holds: `tier = self._tier(mint)`, where `_tier`
  returns `str(p)` for p in (-1, 0, 1), `"activity"` for p >= 10, `"none"` for no priority.
- On a fresh pass: `self.lookups[tier + ":hit"] += 1` and add `now - v[2]` to
  `self.hit_age_s.setdefault(tier, [0.0, 0])`.
- On any fallback: `self.lookups[tier + ":miss"] += 1`.

`write_heartbeat` adds a `warm_tiers` TEXT column, set to
`json.dumps({"lookups": cache.lookups, "hit_age_s": cache.hit_age_s}, sort_keys=True)`.

- [ ] **Step 4: Green; the covering set; commit; give Beta the SHA**

```bash
files0=$(grep -l "make_candidates\|_armed_bought_mints\|SafetyCache\|memecoin_rh_fast_decide" tests/*.py | grep -v watchdog | tr '\n' ' '); python3 -m pytest $files0 tests/test_memecoin_safety_warm_tiers.py -q -n 4 -p no:cacheprovider; rc=$?; python3 -m pytest tests/test_memecoin_safety_warm_tiers.py -q -p no:cacheprovider; rc2=$?; [ $rc -eq 0 ] && [ $rc2 -eq 0 ] && git add sniper/persistence/db_memecoin.py sniper/settings/memecoin.py sniper/services/memecoin_rh_fast_decide.py sniper/services/memecoin_rh_fast_lane.py tests/test_memecoin_safety_warm_tiers.py && git commit -m "feat(safety warm): shared armed warm set, tier rule and one stats pacer (4663 and 56)"
```

---

### Task 1: Cached-only refusal on 4663

**Files:**
- Modify: `sniper/settings/memecoin.py` (next to `MEMECOIN_RH_FAST_PARALLEL_SAFETY`, ~line 6540)
- Modify: `sniper/services/memecoin_rh_fast_lane.py` (`LiveLane.handle`, ~line 2206; new method
  `_cached_only`)
- Create: `tests/test_memecoin_rh_fast_cached_only.py`
- Modify: the fixtures of existing inline-path tests that go red (Step 5)

**Interfaces:**
- Produces: `LiveLane._cached_only() -> bool`; `cfg.MEMECOIN_RH_FAST_CACHED_ONLY: bool`.

- [ ] **Step 1: Write the failing tests**

```python
"""4663 fast lane, cached-only (spec 2026-10-14 rh-fast-cached-only §3.1): a safety cache miss is
refused at once with the precheck's reason. No inline read, no hook, no attempt row, and the lane's
hold is released so the ordinary path judges the buy."""
import time

import pytest

from test_memecoin_rh_fast_entry import MINT, _go, _payload, _setup, _stats  # noqa: F401


@pytest.fixture
def miss(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL", "https://own-4663.example.test/key")
    built = _setup(monkeypatch, tmp_path, "live")
    built[0]["cache"]._verdicts.pop(MINT, None)
    yield built
    built[0]["live"]._safety_pool.shutdown(wait=True)


def _knob(monkeypatch, on):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", on, raising=False)


def _spy(live):
    calls = []
    live.inline_safety_fn = lambda m, usd=None: calls.append("safety") or (True, None)
    live.inline_facts_fn = lambda m: calls.append("facts") or (18, 10 ** 27)
    live.stats_fn = lambda m: calls.append("stats") or _stats()
    return calls


def _attempts(ledger):
    return ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0]


@pytest.mark.parametrize("verdict, reason", [
    (None, "no_safety_verdict"),
    ((True, None, time.time() - 3600, 0.0), "safety_stale"),
])
def test_a_cache_miss_refuses_without_any_inline_read(miss, monkeypatch, verdict, reason):
    from sniper.services import memecoin_rh_fast_lane as lane
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    if verdict is not None:
        built["cache"]._verdicts[MINT] = verdict
    calls = _spy(built["live"])
    p = _payload()
    rec = _go(built, p)
    assert rec["reason"] == reason and calls == [] and not sender.raws, rec
    assert _attempts(ledger) == 0
    assert not lane.LANE_HOLDS.holds(p["tx_hash"], MINT)     # the ordinary path is free


def test_with_the_knob_off_a_miss_reads_inline_as_before(miss, monkeypatch):
    _knob(monkeypatch, False)
    built, sender, net, ledger = miss
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert "safety" in calls and sender.raws, (rec, calls)


def test_a_cached_pass_still_sends_with_the_knob_on(miss, monkeypatch):
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert sender.raws and calls == [], (rec, calls)


def test_a_cached_pass_without_stats_refuses_without_an_inline_read(miss, monkeypatch):
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    built["cache"]._stats.pop(MINT, None)
    calls = _spy(built["live"])
    rec = _go(built, _payload())
    assert rec["reason"] == "no_market_stats" and calls == [] and not sender.raws, rec
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider`

Expected: `test_a_cache_miss_refuses_without_any_inline_read` FAILS (inline calls happen and the
lane sends). The others may pass.

- [ ] **Step 3: Implement**

In `sniper/settings/memecoin.py`, after the `MEMECOIN_RH_FAST_PARALLEL_SAFETY` block:

```python
# 4663 CACHED-ONLY (spec 2026-10-14 rh-fast-cached-only §3.1): a safety cache miss is refused at once
# and the ordinary path judges the buy. 0 restores today's inline read on a miss.
MEMECOIN_RH_FAST_CACHED_ONLY = os.getenv(
    "MEMECOIN_RH_FAST_CACHED_ONLY", "1").strip().lower() in ("1", "on", "true", "yes")
```

In `LiveLane.handle`, change the inline condition:

```python
        if (reason in ("no_safety_verdict", "safety_stale") and self.inline_safety_fn is not None
                and not self._cached_only()):
```

Add the method next to `_initiation`:

```python
    def _cached_only(self):
        """4663 sends only on a cached verdict (spec 2026-10-14 rh-fast-cached-only §3.1): a miss
        keeps the precheck's refusal and the ordinary path judges the buy. Read per signal."""
        return self.CHAIN_ID == 4663 and lane_cfg.MEMECOIN_RH_FAST_CACHED_ONLY
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider`

Expected: 5 passed.

- [ ] **Step 5: Pin the knob off where a test exercises the inline path on purpose**

Run the covering set:

```bash
files=$(grep -l "memecoin_rh_fast_lane\|inline_safety_fn\|rh_fast_entry" tests/*.py | grep -v watchdog | tr '\n' ' '); python3 -m pytest $files -q -n 4 -p no:cacheprovider 2>&1 | tail -15
```

For each red test whose subject is the 4663 inline read (`test_memecoin_rh_parallel_safety.py`'s
`dedicated` fixture, the inline tests in `test_memecoin_rh_fast_entry.py`,
`test_memecoin_rh_fast_safety*.py`, `test_memecoin_safety_pass_basis.py`), add to its fixture:

```python
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", False, raising=False)
```

Any red test whose subject is NOT the inline path is a real finding. Stop and report it.
Set `PINNED` to the space-separated list of fixture files edited in this step.

- [ ] **Step 6: Re-run the covering set and the new file alone; commit on green**

```bash
python3 -m pytest $files -q -n 4 -p no:cacheprovider; rc=$?; python3 -m pytest tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider; rc2=$?; if [ $rc -eq 0 ] && [ $rc2 -eq 0 ]; then git add sniper/settings/memecoin.py sniper/services/memecoin_rh_fast_lane.py tests/test_memecoin_rh_fast_cached_only.py $PINNED && git commit -m "feat(4663 lane): cached-only sends; a safety cache miss goes to the ordinary path"; fi
```

---

### Task 2: Record feed-to-send, accepted_by and state_class on every send

**Files:**
- Modify: `sniper/services/memecoin_rh_fast_lane.py`:
  - `LiveLane._send` (~line 2840): `t_send_ns`, `feed_to_send_ms`, `accepted_by`;
  - `LiveLane.handle` rec init: `state_class`;
  - `write_live_record`: three columns;
  - `write_heartbeat`: a `send_counters` column; new `send_counters(db)`.
- Test: `tests/test_memecoin_rh_fast_cached_only.py` (append)

**Interfaces:**
- Consumes: `sniper.util.receipt_clock.elapsed_receipt_ms(value, monotonic_ns, *, boot_id=None)`.
- Produces: `state_class(payload) -> "exact" | "fresh" | None`; `send_counters(db) -> dict`;
  `rh_fast_lane_live` columns `feed_to_send_ms REAL`, `accepted_by TEXT`, `state_class TEXT`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_a_send_records_feed_to_send_accepted_by_and_state_class(miss, monkeypatch):
    import sqlite3
    from sniper.util.receipt_clock import ReceiptClock
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    p = _payload(receipt_clock=ReceiptClock("test-feed").capture())
    _go(built, p)
    row = sqlite3.connect(built["db_path"]).execute(
        "SELECT feed_to_send_ms, accepted_by, state_class FROM rh_fast_lane_live "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] is not None and 0 <= row[0] < 5000 and row[2] == "exact", row
    assert row[1] is None                         # the test's injected sender is the legacy seam


def test_an_absent_or_foreign_receipt_clock_is_null_never_zero(miss, monkeypatch):
    import sqlite3
    _knob(monkeypatch, True)
    built, sender, net, ledger = miss
    built["cache"]._verdicts[MINT] = (True, None, time.time(), 0.0)
    _go(built, _payload(receipt_clock=None))
    (got,) = sqlite3.connect(built["db_path"]).execute(
        "SELECT feed_to_send_ms FROM rh_fast_lane_live ORDER BY id DESC LIMIT 1").fetchone()
    assert got is None


def test_state_class_names_the_decides_state():
    from sniper.services.memecoin_rh_fast_lane import state_class
    assert [state_class({"state_kind": k}) for k in ("tape_post", "pre", "receipt_leg", None)] == [
        "exact", "fresh", None, None]


def test_send_counters_summarise_sends_by_class(tmp_path):
    import sqlite3
    from sniper.services import memecoin_rh_fast_lane as lane
    db = sqlite3.connect(str(tmp_path / "l.db"))
    db.executescript(lane._LIVE)
    lane.add_missing_columns(db, "rh_fast_lane_live", lane.LIVE_COLUMNS)
    now = __import__("sniper.util.clock", fromlist=["x"]).real_utc_now_iso()
    for cls, reason, ms in (("exact", "filled", 20.0), ("exact", "reverted", 30.0), ("fresh", "filled", None)):
        db.execute("INSERT INTO rh_fast_lane_live (reason, send_reply, state_class, feed_to_send_ms, "
                   "written_at) VALUES (?,?,?,?,?)", (reason, "accepted", cls, ms, now))
    got = lane.send_counters(db)
    assert got["by_class"] == {"exact": {"sends": 2, "reverted": 1}, "fresh": {"sends": 1, "reverted": 0}}
    assert got["feed_to_send_ms"]["n"] == 2 and got["feed_to_send_ms"]["p50"] in (20.0, 30.0)
```

If `_payload(receipt_clock=...)` is dropped by `bind_native_prices`, set it on the dict after
`_payload()` returns instead.

- [ ] **Step 2: Run to verify red**

Run: `python3 -m pytest tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider -k "records or receipt_clock or state_class or counters"`

Expected: FAIL (`no such column: feed_to_send_ms`, `cannot import name 'state_class'`).

- [ ] **Step 3: Implement**

Module level, near `hook_reason`:

```python
def state_class(payload):
    """The decide's state behind a send: `exact` (the source's own tape swap), `fresh` (a pre-state
    inside the decide's lag bound), None otherwise (shared or unknown)."""
    return {"tape_post": "exact", "pre": "fresh"}.get((payload or {}).get("state_kind"))


LIVE_COLUMNS = (("copy_floor", "TEXT"), ("initiation", "TEXT"), ("execution_evidence_json", "TEXT"),
                ("feed_to_send_ms", "REAL"), ("accepted_by", "TEXT"), ("state_class", "TEXT"))


def send_counters(db, *, window="-1 day"):
    """Sends in the last day by state class (sends, reverted), and feed-to-send p50/p90 over the
    sends that carried a same-boot receipt clock. Heartbeat only; never on the fill path."""
    rows = db.execute("SELECT state_class, reason, feed_to_send_ms FROM rh_fast_lane_live "
                      "WHERE send_reply IS NOT NULL AND written_at >= strftime('%Y-%m-%dT%H:%M:%S', "
                      "'now', ?)", (window,)).fetchall()
    by = {}
    for cls, reason, _ms in rows:
        slot = by.setdefault(cls or "unknown", {"sends": 0, "reverted": 0})
        slot["sends"] += 1
        slot["reverted"] += int(reason == "reverted")
    ms = sorted(m for _c, _r, m in rows if m is not None)
    pick = (lambda f: ms[min(len(ms) - 1, int(len(ms) * f))]) if ms else (lambda f: None)
    return {"by_class": by, "feed_to_send_ms": {"n": len(ms), "p50": pick(.5), "p90": pick(.9)}}
```

`write_live_record`: replace its `add_missing_columns(...)` call with
`add_missing_columns(db, "rh_fast_lane_live", LIVE_COLUMNS)`. Add the three names to the INSERT
column list and to its values (`rec.get("feed_to_send_ms"), rec.get("accepted_by"),
rec.get("state_class")`), and add three `?`.

`LiveLane.handle`, in the `rec = {...}` initialiser, add `"state_class": state_class(payload),`.

`LiveLane._send`: at `t_send = time.perf_counter()`, add the line below it:

```python
            t_send_ns = time.monotonic_ns()
```

After `rec["send_reply"] = kind`:

```python
        from sniper.util.receipt_clock import elapsed_receipt_ms
        rec["feed_to_send_ms"] = elapsed_receipt_ms(payload.get("receipt_clock"), t_send_ns)
        rec["accepted_by"] = submitted.accepted_by if submitted is not None else None
```

`write_heartbeat`:
- add `("send_counters", "TEXT")` to its `add_missing_columns` list;
- add `send_counters` to the INSERT;
- its value is `json.dumps(send_counters(db), sort_keys=True)`, read on the same `db` connection.

- [ ] **Step 4: Run green, then the covering set from Task 1; commit on green**

```bash
python3 -m pytest $files tests/test_memecoin_rh_fast_cached_only.py -q -n 4 -p no:cacheprovider; rc=$?; [ $rc -eq 0 ] && git add sniper/services/memecoin_rh_fast_lane.py tests/test_memecoin_rh_fast_cached_only.py && git commit -m "feat(4663 lane): record feed-to-send, accepted_by and state_class on every send"
```

---

### Task 3: The `onchain_m5_read` substage mark

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py` (~line 4391, right after
  `_onchain_m5 = _own_m5_for_gate(...)`)
- Test: `tests/test_memecoin_evm_pre_ladder_probe.py` (append)

- [ ] **Step 1: Failing test** (append)

```python
def test_the_m5_gate_read_has_its_own_mark_before_the_tape_shadow(monkeypatch):
    # 2026-10-14: chase_tape_move_shadow's 5-11 ms window held this gate read; the mark separates them.
    names, _by = _run(monkeypatch, "4663", "0xabc")
    assert "onchain_m5_read" in names, names
    if "chase_tape_move_shadow" in names:
        assert names.index("onchain_m5_read") < names.index("chase_tape_move_shadow"), names
```

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_evm_pre_ladder_probe.py -q -p no:cacheprovider -k m5_gate_read`

Expected: FAIL (`onchain_m5_read` not in names).

- [ ] **Step 3: Implement**

```python
        _onchain_m5 = _own_m5_for_gate(ev, identity.network_id, st)
        if trace is not None:
            _mark_probe(trace, "onchain_m5_read")
        _shadow_tape_move(trace, ev, identity.network_id, st)
```

- [ ] **Step 4: Green, then commit**

```bash
files3=$(grep -l "probe_pre_volume\|chase_tape_move_shadow\|substage" tests/*.py | grep -v watchdog | tr '\n' ' '); python3 -m pytest $files3 -q -n 4 -p no:cacheprovider; rc=$?; [ $rc -eq 0 ] && git add sniper/services/memecoin_real_runner.py tests/test_memecoin_evm_pre_ladder_probe.py && git commit -m "feat(entry trace): mark the m5 gate read apart from the tape-move shadow"
```

If a test asserts the exact substage list, it goes red here. Add `onchain_m5_read` to its
expected list only when its subject is the trace shape.

---

### Task 4: coincurve, with a parity test and a startup backend line

**Files:**
- Modify: `requirements.txt` (add `coincurve==21.0.0` next to `eth-account`)
- Create: `tests/test_eth_keys_backend_parity.py`
- Modify: `sniper/services/memecoin_rh_fast_lane.py` (new `eth_keys_backend()`; one log line in
  `start_lane`)
- Modify: `sniper/services/memecoin_rh_feed_shadow.py` (one log line at start)

- [ ] **Step 1: Measure first, in a scratch venv**

Blocked until the operator says yes in lambda's own chat.

```bash
S=/tmp/claude-1000/-home-bigpapi-Desktop-Sniper/ddef7890-525b-44da-b716-d3b638bdc595/scratchpad
python3 -m venv --system-site-packages $S/venv-cc && $S/venv-cc/bin/pip install -q coincurve==21.0.0
```

Benchmark script `$S/lat/ecc_bench.py`:
- sign the production-shaped tx (`{"to","data" 600 B,"value","gas","gasPrice","nonce","chainId":4663}`)
  200 times;
- `Account.recover_transaction` the result 200 times;
- under `ECC_BACKEND_CLASS=eth_keys.backends.NativeECCBackend` and
  `eth_keys.backends.CoinCurveECCBackend`.

Print p50/p90 for each and send the numbers to Alpha. Baseline: native sign p50 3.1 ms.

- [ ] **Step 2: Failing parity tests**

```python
"""eth_keys backends (spec 2026-10-14 rh-fast-cached-only §3.2): coincurve must sign the same bytes
and recover the same sender as the pure-Python native backend, or it does not ship."""
import json
import os
import random
import subprocess
import sys

import pytest

coincurve = pytest.importorskip("coincurve", reason="coincurve absent: eth_keys runs on the native backend")

N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def test_key_level_signatures_and_recovery_are_identical():
    from eth_keys import KeyAPI
    from eth_keys.backends import CoinCurveECCBackend, NativeECCBackend
    native, fast = KeyAPI(NativeECCBackend), KeyAPI(CoinCurveECCBackend)
    rng = random.Random(4663)
    for _ in range(300):
        secret = rng.randrange(1, N).to_bytes(32, "big")
        digest = rng.randbytes(32)
        a = native.PrivateKey(secret).sign_msg_hash(digest)
        b = fast.PrivateKey(secret).sign_msg_hash(digest)
        assert a.to_bytes() == b.to_bytes()
        assert a.recover_public_key_from_msg_hash(digest) == b.recover_public_key_from_msg_hash(digest)


_SCRIPT = r"""
import json, sys
from eth_account import Account
from eth_keys.backends import get_backend
acct = Account.from_key(bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"))
txs = [dict(to="0x" + "11" * 20, data="0x" + "ab" * 600, value=10 ** 16, gas=1200000, gasPrice=40_000_000,
            nonce=42, chainId=4663),
       dict(type=2, to="0x" + "22" * 20, data="0x" + "cd" * 300, value=0, gas=400000,
            maxFeePerGas=3 * 10 ** 9, maxPriorityFeePerGas=10 ** 9, nonce=7, chainId=56)]
out = []
for tx in txs:
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    out.append([raw, Account.recover_transaction(signed.raw_transaction)])
print(json.dumps({"backend": type(get_backend()).__name__, "txs": out}))
"""


def _run(backend):
    env = dict(os.environ, ECC_BACKEND_CLASS="eth_keys.backends.%s" % backend)
    got = subprocess.run([sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True,
                         timeout=60, check=True)
    return json.loads(got.stdout)


def test_eth_account_signs_identical_raw_transactions_and_recovers_the_same_sender():
    native, fast = _run("NativeECCBackend"), _run("CoinCurveECCBackend")
    assert native["backend"] == "NativeECCBackend" and fast["backend"] == "CoinCurveECCBackend"
    assert native["txs"] == fast["txs"]
```

Run: `$S/venv-cc/bin/python -m pytest tests/test_eth_keys_backend_parity.py -q -p no:cacheprovider`

Expected: 2 passed in the venv. In the runtime interpreter (no coincurve), the module skips with
its reason. If parity FAILS, stop: coincurve does not ship.

- [ ] **Step 3: The startup line and the pin**

`memecoin_rh_fast_lane.py`:

```python
def eth_keys_backend():
    """The eth_keys backend this process signs and recovers with (coincurve when installed)."""
    try:
        from eth_keys.backends import get_backend
        return type(get_backend()).__name__
    except Exception as exc:  # noqa: BLE001 - a log line never stops a start
        return "unknown:%s" % type(exc).__name__
```

In `start_lane`, next to the native-submission log line, add
`log("[rh-fast-lane] eth_keys backend: %s" % eth_keys_backend())`. In the feed shadow's start
(`memecoin_rh_feed_shadow`, where it logs its own start), add
`log("[rh-feed] eth_keys backend: %s" % eth_keys_backend())`, importing the helper from the lane
module (the feed already imports it lazily).

`requirements.txt`: add `coincurve==21.0.0`.

Test (append to `tests/test_memecoin_rh_fast_cached_only.py`):

```python
def test_the_backend_name_is_logged_at_start():
    from sniper.services.memecoin_rh_fast_lane import eth_keys_backend
    assert eth_keys_backend() in ("NativeECCBackend", "CoinCurveECCBackend")
```

- [ ] **Step 4: Commit on green**

```bash
python3 -m pytest tests/test_eth_keys_backend_parity.py tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider; rc=$?; [ $rc -eq 0 ] && git add requirements.txt tests/test_eth_keys_backend_parity.py sniper/services/memecoin_rh_fast_lane.py sniper/services/memecoin_rh_feed_shadow.py tests/test_memecoin_rh_fast_cached_only.py && git commit -m "build: coincurve 21.0.0 for eth_keys, parity-tested against the native backend"
```

Deploy note for Alpha (operator step):
- Install: `/home/bigpapi/.pyenv/versions/3.13.0/bin/python3 -m pip install coincurve==21.0.0`.
- Verify: `/home/bigpapi/.pyenv/versions/3.13.0/bin/python3 -c "from eth_keys.backends import get_backend; print(type(get_backend()).__name__)"`
  must print `CoinCurveECCBackend`.
- Rollback: `/home/bigpapi/.pyenv/versions/3.13.0/bin/python3 -m pip uninstall -y coincurve`, then
  restart.
- It takes effect at each process's next start, whatever SHA is deployed.

---

### Task 5: The Chainstack idle warmer

**Files:**
- Modify: `sniper/settings/memecoin.py` (three knobs)
- Modify: `sniper/services/memecoin_rh_fast_lane.py`:
  - `background_safety` (rotation optional);
  - `SafetyCache` (in-flight claims, `run_one` overrides);
  - new class `InlineWarmer`;
  - `build_lane` (warm fns in the lane dict);
  - `start_lane` (the thread);
  - `write_heartbeat` (an `inline_warm` column).
- Create: `tests/test_memecoin_rh_inline_warmer.py`

**Interfaces:**
- Produces:
  - `SafetyCache.run_one(mint, *, now, safety_fn=None, facts_fn=None, stats=True)` returns the
    verdict tuple, or None (error, or another worker holds the coin);
  - `SafetyCache.next_due(now)` skips coins in flight;
  - `background_safety(check, rotation, paced)` accepts `rotation=None`;
  - `InlineWarmer(cache, *, safety_fn, facts_fn, host, gap_s, stats_pacer=None, pause_s=600.0,
    clock=time.monotonic, log=print)` with `.step(now) -> float` (seconds to wait) and `.counts`.

- [ ] **Step 1: Failing tests**

```python
"""The 4663 inline host warms the safety cache while idle (spec 2026-10-14 rh-fast-cached-only,
rev 2 §3.6; operator via Alpha: Chainstack 'unlimited / plenty')."""
import threading
import time
from types import SimpleNamespace

from sniper.services import memecoin_rh_fast_lane as lane


class Host:
    def __init__(self):
        self.counts = {"calls": 0, "http_429": 0}
        self._paused = False

    def paused(self):
        return self._paused

    def dead(self):
        return False


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _cache(stats_fn=None):
    cache = lane.SafetyCache(lambda m, usd=None: (True, "research"), ttl_s=60, usd=50,
                             facts_fn=lambda m: (18, 10 ** 27), stats_fn=stats_fn)
    cache.note_candidates([{"mint": "0xa", "priority": 0}, {"mint": "0xb", "priority": 3}], now=1000.0)
    return cache


def _warmer(cache, host, clock, safety=None, **kw):
    return lane.InlineWarmer(cache, safety_fn=safety or (lambda m, usd=None: (True, "inline")),
                             facts_fn=lambda m: (18, 10 ** 27), host=host, gap_s=0.25,
                             stats_pacer=lane.WarmStatsPacer(1.0, clock=clock), clock=clock,
                             log=lambda *_: None, **kw)


def test_the_warmer_verdicts_the_most_urgent_coin_on_the_inline_host(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)
    cache, host, clock = _cache(), Host(), Clock()
    assert _warmer(cache, host, clock).step(1000.0) == 0.25
    assert cache._verdicts["0xa"][:2] == (True, "inline") and "0xb" not in cache._verdicts


def test_off_cached_only_the_warmer_never_reads(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", False, raising=False)
    cache, called = _cache(), []
    w = _warmer(cache, Host(), Clock(), safety=lambda m, usd=None: called.append(m) or (True, None))
    assert w.step(1000.0) == 1.0 and called == []


def test_a_429_pauses_the_warmer_ten_minutes_and_counts_it(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)
    cache, host, clock = _cache(), Host(), Clock()

    def limited(m, usd=None):
        host.counts["http_429"] += 1
        return False, "code read failed: 429"
    w = _warmer(cache, host, clock, safety=limited)
    w.step(1000.0)
    assert w.counts["pauses"] == 1 and "0xa" not in cache._verdicts   # no unsafe verdict cached
    clock.t += 599.0
    assert w.step(1000.0) == 1.0 and w.counts["verdicts"] == 1         # still paused
    clock.t += 2.0
    w.safety_fn_raw = lambda m, usd=None: (True, "inline")
    assert w.step(1000.0) == 0.25


def test_a_quota_answer_pauses_like_a_429(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)
    cache, clock = _cache(), Clock()

    def quota(m, usd=None):
        raise RuntimeError("rpc error -32005: monthly request quota exceeded")
    w = _warmer(cache, Host(), clock, safety=quota)
    w.step(1000.0)
    assert w.counts["pauses"] == 1 and w.paused_until == clock.t + 600.0


def test_a_coin_in_flight_is_skipped_by_the_second_worker():
    cache = _cache()
    started, release = threading.Event(), threading.Event()

    def slow(m, usd=None):
        started.set()
        release.wait(2.0)
        return True, "slow"
    t = threading.Thread(target=cache.run_one, args=("0xa",), kwargs=dict(now=1000.0, safety_fn=slow))
    t.start()
    assert started.wait(2.0)
    assert cache.next_due(1000.0) == "0xb"                            # 0xa is in flight
    assert cache.run_one("0xa", now=1000.0) is None                   # a racing second run returns
    release.set()
    t.join()
    assert cache._verdicts["0xa"][1] == "slow"


def test_stats_are_read_only_when_due_and_paced(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_CACHED_ONLY", True, raising=False)
    reads = []
    cache, clock = _cache(stats_fn=lambda m: reads.append(m) or {"price": 1.0}), Clock()
    w = _warmer(cache, Host(), clock)
    w.step(1000.0)                                     # 0xa: stats absent, read
    cache._verdicts.pop("0xb", None)
    w.step(1000.0)                                     # 0xb inside the 1 s stats gap: not read
    assert reads == ["0xa"]
```

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest tests/test_memecoin_rh_inline_warmer.py -q -p no:cacheprovider`

Expected: FAIL (`InlineWarmer` missing; `run_one() got an unexpected keyword argument 'safety_fn'`).

- [ ] **Step 3: Implement**

Settings (next to the cached-only knob):

```python
# The 4663 inline host's idle warmer (spec 2026-10-14 rh-fast-cached-only rev 2 §3.6). Runs only
# while MEMECOIN_RH_FAST_CACHED_ONLY is on (the fill path then makes no inline read).
MEMECOIN_RH_FAST_INLINE_WARM = os.getenv(
    "MEMECOIN_RH_FAST_INLINE_WARM", "1").strip().lower() in ("1", "on", "true", "yes")
MEMECOIN_RH_FAST_INLINE_WARM_GAP_S = float(os.getenv("MEMECOIN_RH_FAST_INLINE_WARM_GAP_S", "0.25"))
```

`background_safety`: make `rotation` optional.

```python
    def failures():
        rotation_part = ((sum(s["quota"] + s["transport"] for s in rotation.stats()[1]),
                          rotation.provider_errors()) if rotation is not None else ())
        return (*rotation_part, paced.counts["http_429"])
```

`SafetyCache.__init__`: `self._inflight = set()`.

`next_due`: add `and mint not in self._inflight` to the `due` comprehension's condition.

`run_one(self, mint, *, now, safety_fn=None, facts_fn=None, stats=True)`:
- First lines, before any work:

  ```python
          with self._lock:
              if mint in self._inflight:
                  return None
              self._inflight.add(mint)
          try:
              return self._run_one(mint, now=now, safety_fn=safety_fn or self.safety_fn,
                                   facts_fn=facts_fn or self.facts_fn, stats=stats)
          finally:
              with self._lock:
                  self._inflight.discard(mint)
  ```

- Rename today's body to `_run_one`, using the passed `safety_fn` and `facts_fn` in place of
  `self.safety_fn` / `self.facts_fn`, and `if stats and self.stats_fn is not None:` for the stats
  block.

The class:

```python
_QUOTA = re.compile(r"429|quota|rate.?limit|too many requests|limit exceeded", re.I)


class InlineWarmer:
    """The safety cache's second worker, on the inline host while it is idle (spec 2026-10-14
    rh-fast-cached-only rev 2 §3.6; operator via Alpha: 'Go, it's unlimited / plenty'). Idle by
    construction: it reads only while MEMECOIN_RH_FAST_CACHED_ONLY is on, when the fill path makes
    no inline read. The first 429 or quota answer pauses it `pause_s` (counted). A provider failure
    never caches a verdict (background_safety). Stats only when absent, through the shared WARM_STATS pacer:
    DexScreener's 429 stops every lane stats call and the funded breaker shares that provider."""

    def __init__(self, cache, *, safety_fn, facts_fn, host, gap_s, stats_pacer=None, pause_s=600.0,
                 clock=time.monotonic, log=print):
        self.cache, self.facts_fn, self.host = cache, facts_fn, host
        self.safety_fn_raw = safety_fn
        self.stats_pacer = stats_pacer or WARM_STATS            # shared with every lane's warmer
        self.gap_s, self.pause_s, self.clock, self.log = float(gap_s), float(pause_s), clock, log
        self.paused_until, self._quota = 0.0, False
        self.counts = collections.Counter()

    def _safety(self, mint, usd=None):
        try:
            ok, reason = self.safety_fn_raw(mint, usd=usd)
        except Exception as exc:
            self._quota = self._quota or bool(_QUOTA.search(str(exc)))
            raise
        if not ok and _QUOTA.search(str(reason or "")):
            self._quota = True
            raise ResearchPaused("inline host quota: %s" % str(reason)[:80])
        return ok, reason

    def step(self, now):
        if not lane_cfg.MEMECOIN_RH_FAST_CACHED_ONLY or self.clock() < self.paused_until:
            return 1.0
        if self.host.paused() or self.host.dead():
            return 1.0
        mint = self.cache.next_due(now)
        if mint is None:
            return 1.0
        stats = self.cache.stats(mint, now=now) is None and self.stats_pacer.take()
        before, self._quota = self.host.counts["http_429"], False
        checked = background_safety(self._safety, None, self.host)
        rec = self.cache.run_one(mint, now=now, safety_fn=checked, facts_fn=self.facts_fn, stats=stats)
        self.counts["verdicts"] += 1
        self.counts["passes" if rec and rec[0] else "refusals" if rec else "errors"] += 1
        if self.host.counts["http_429"] > before or self._quota:
            self.paused_until = self.clock() + self.pause_s
            self.counts["pauses"] += 1
            self.log("[rh-fast-lane] inline warmer paused %.0fs after a %s" % (
                self.pause_s, "429" if self.host.counts["http_429"] > before else "quota answer"))
        return self.gap_s
```

Add `import re` to the module's imports (it has none today).

`build_lane`, inside the `elif inline_url:` branch, after `inline = dict(...)`:

```python
        inline_warm = (InlineWarmer(None, safety_fn=safety_on(inline_http, inline_url, path="inline_warm"),
                                    facts_fn=lambda mint: facts(mint, http=inline_http, host=inline_url),
                                    host=inline_paced, gap_s=cfg.MEMECOIN_RH_FAST_INLINE_WARM_GAP_S, log=log)
                       if cfg.MEMECOIN_RH_FAST_INLINE_WARM else None)
```

- Initialise `inline_warm = None` beside `inline, inline_paced = None, None`.
- After `cache = SafetyCache(...)`: `if inline_warm is not None: inline_warm.cache = cache`.
- Return `"inline_warm": inline_warm` in the lane dict.

`start_lane`, beside `safety_loop`:

```python
    def inline_warm_loop():
        warm = lane["inline_warm"]
        while not stop.is_set():
            try:
                wait = warm.step(time.time())
            except Exception:
                logger.exception("rh fast lane: inline warmer step failed (keeps running)")
                wait = 1.0
            stop.wait(wait)
```

and after the threads list is built: `if lane.get("inline_warm") is not None:
threads.append(threading.Thread(target=inline_warm_loop, name="rh-fast-inline-warm", daemon=True))`.

`write_heartbeat`:
- add `("inline_warm", "TEXT")` to its columns and the INSERT;
- its value is `json.dumps(dict(warm.counts, paused_until=warm.paused_until), sort_keys=True)` when
  `getattr(dispatch, "inline_warm", None)` is set, else NULL;
- set `dispatch.inline_warm = lane["inline_warm"]` in `start_lane`.

- [ ] **Step 4: Green; the covering set; commit**

```bash
files5=$(grep -l "SafetyCache\|background_safety\|run_one\|memecoin_rh_fast_lane" tests/*.py | grep -v watchdog | tr '\n' ' '); python3 -m pytest $files5 tests/test_memecoin_rh_inline_warmer.py -q -n 4 -p no:cacheprovider; rc=$?; python3 -m pytest tests/test_memecoin_rh_inline_warmer.py -q -p no:cacheprovider; rc2=$?; [ $rc -eq 0 ] && [ $rc2 -eq 0 ] && git add sniper/settings/memecoin.py sniper/services/memecoin_rh_fast_lane.py tests/test_memecoin_rh_inline_warmer.py && git commit -m "feat(4663 lane): warm the safety cache on the idle inline host"
```

---

### Task 6: Coverage report, before and after

**Files:**
- Create: `scripts/research/rh_fast_cache_coverage.py`
- Test: `tests/test_memecoin_rh_fast_cached_only.py` (append)

- [ ] **Step 1: Failing test**

```python
def test_the_coverage_report_counts_signals_by_cache_state(tmp_path):
    import sqlite3
    from scripts.research.rh_fast_cache_coverage import coverage
    db = sqlite3.connect(str(tmp_path / "l.db"))
    db.execute("CREATE TABLE rh_fast_lane_events (mode TEXT, reason TEXT, safety_path TEXT, written_at TEXT)")
    rows = [("live", "filled", "cache", "2026-10-15T01:00:00"),
            ("live", "no_safety_verdict", None, "2026-10-15T02:00:00"),
            ("live", "safety_stale", None, "2026-10-15T03:00:00"),
            ("live", "hook:filled:", "inline", "2026-10-15T04:00:00"),
            ("runner_shadow", "filled", "cache", "2026-10-15T05:00:00")]
    db.executemany("INSERT INTO rh_fast_lane_events VALUES (?,?,?,?)", rows)
    assert coverage(db) == {"2026-10-15": {"cache": 1, "inline": 1, "no_safety_verdict": 1,
                                           "safety_stale": 1}}
```

- [ ] **Step 2: Implement**

```python
"""Signals per UTC day by safety-cache state at the 4663 fast lane's safety step, live mode only
(spec 2026-10-14 rh-fast-cached-only §3.5). Read-only.

    python3 scripts/research/rh_fast_cache_coverage.py [db_path]
"""
import sqlite3
import sys


def coverage(db):
    out = {}
    for day, path, reason in db.execute(
            "SELECT substr(written_at, 1, 10), safety_path, reason FROM rh_fast_lane_events "
            "WHERE mode='live' AND (safety_path IN ('cache', 'inline') "
            "OR reason IN ('no_safety_verdict', 'safety_stale'))"):
        key = path if path in ("cache", "inline") else reason
        slot = out.setdefault(day, {})
        slot[key] = slot.get(key, 0) + 1
    return out


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/memecoin_rh_fast_lane.db"
    with sqlite3.connect("file:%s?mode=ro" % path, uri=True) as db:
        for day, counts in sorted(coverage(db).items()):
            print(day, " ".join("%s=%d" % kv for kv in sorted(counts.items())))
```

Make sure `scripts/research/__init__.py` exists, so `from scripts.research...` imports, as the
other research scripts' tests do. If they import differently, follow their pattern.

- [ ] **Step 3: Green, commit; run it read-only for the "before" baseline and send it to Alpha**

```bash
python3 -m pytest tests/test_memecoin_rh_fast_cached_only.py -q -p no:cacheprovider; rc=$?; [ $rc -eq 0 ] && git add scripts/research/rh_fast_cache_coverage.py tests/test_memecoin_rh_fast_cached_only.py && git commit -m "research: 4663 fast-lane signals by safety-cache state"
python3 scripts/research/rh_fast_cache_coverage.py /home/bigpapi/Desktop/Sniper/data/memecoin_rh_fast_lane.db
```

---

### Final: whole-branch check, then Gamma

```bash
files=$(grep -l "memecoin_rh_fast_lane\|memecoin_real_runner\|rh_fast_entry\|SafetyCache" tests/*.py | grep -v watchdog | tr '\n' ' '); python3 -m pytest $files -q -n 4 -p no:cacheprovider; echo rc=$?
python3 scripts/validate_schema_refs.py --files docs/superpowers/specs/2026-10-14-rh-fast-cached-only-design.md docs/superpowers/plans/2026-10-14-rh-fast-cached-only.md
```

Send Gamma the branch, SHAs, the covering-set results, the mutants proven red, and the deploy steps
(the knobs at their defaults; coincurve as Task 4's operator commands).

**Live checks after deploy** (Alpha deploys):
- the first 20 admitted sends: `feed_to_send_ms` p90 <= 30, `accepted_by = '4663:sequencer'` on most;
- the `inline_warm` heartbeat counters moving with `pauses = 0`;
- the coverage report, before vs after.
