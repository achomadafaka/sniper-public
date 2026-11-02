# Crypto5m Direction-State Scalper v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the crypto5m scalper from entering and exiting on micro-noise by adding a memory/confirmation layer on entry and a gap-immune exit.

**Architecture:** Two small pure units inserted into the existing live flow. `direction_state.DirectionState` carries memory across ticks and confirms a direction only when the read persists, agrees across horizons, and agrees with tape order flow. `scalp_trader` enters only on a confirmed read and replaces the tight price-stop with persistence-based exits (patience stop / persistent flip / wide catastrophe cap). `direction.directional_thesis` is unchanged; the runner's deferred-fill + slip logging is unchanged.

**Tech Stack:** Python 3.13, pytest, sqlite3 (the separate paper DB `data/crypto5m/active_trader_v0.sqlite`), Flask (UI route), vanilla JS (`static/app.js`).

## Global Constraints

- Paper only. No real orders. No change to any other engine.
- `sniper/algos/polymarket_crypto5m/direction.py` (`directional_thesis`) is UNCHANGED.
- Pure decision logic stays pure and unit-tested; all I/O stays in the runner.
- Engine runs from repo root via the watchdog; `python3` = `/home/bigpapi/.pyenv/versions/3.13.0/bin/python3`.
- Constants are explicit module-level values (matches the current engine); tune later on the LIVE forward run, never on the touch-optimistic replay.
- Run the focused suite after each task: `python3 -m pytest tests/test_crypto5m_direction_state.py tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py tests/test_crypto5m_live_desk.py -q`
- Source of truth: `docs/superpowers/specs/2026-06-21-crypto5m-direction-state-design.md`.

---

## File Structure

- Create: `sniper/algos/polymarket_crypto5m/direction_state.py` — the memory/confirmation layer (`DirectionState`).
- Create: `tests/test_crypto5m_direction_state.py` — unit tests for confirmation.
- Modify: `sniper/algos/polymarket_crypto5m/scalp_trader.py` — confirmed-only entry; PATIENCE exit; `update_exit_state`; `Position` gains `adverse_since`/`flip_since`.
- Modify: `tests/test_crypto5m_direction_scalp.py` — replace the old entry-gate tests with confirm-based + PATIENCE-exit tests.
- Modify: `scripts/run_polymarket_crypto5m_active_trader.py` — instantiate `DirectionState`, call `update_exit_state`, gate entry on `confirm`, persist `entry_persistence`.
- Modify: `tests/test_crypto5m_active_runner.py` — entry fires on confirmation (not first spike); `entry_persistence` recorded.
- Modify: `sniper/routes/api_crypto5m.py` — pass confirmation fields through `/api/crypto5m/active`.
- Modify: `static/app.js` + `templates/index.html` — show the confirmation state; bump cache version.

---

### Task 1: `direction_state` memory/confirmation layer

**Files:**
- Create: `sniper/algos/polymarket_crypto5m/direction_state.py`
- Test: `tests/test_crypto5m_direction_state.py`

**Interfaces:**
- Consumes: a `thesis` dict from `direction.directional_thesis` (keys: `side` in {"Up","Down",None}, `conviction` float, `votes` dict with `order_flow` in {-1,0,1,None}); a `desk` dict (keys `ret_15s`, `ret_30s` floats or None); `now` int (unix seconds).
- Produces: `DirectionState(persist_ticks=3, persist_window_s=12, enter_conviction=0.50)` with `.update(thesis, desk, now) -> dict` returning keys: `confirmed` (bool), `confirmed_side` ("Up"|"Down"|None), `read_side` ("Up"|"Down"|None), `persistence_ticks` (int), `horizons_agree` (bool), `flow_agree` (bool), `conviction` (float).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_crypto5m_direction_state.py
"""Memory/confirmation layer: confirm a direction only when it PERSISTS, agrees across horizons, and the
tape order flow agrees — so the scalper commits on sustained moves, not micro-wiggles."""
from sniper.algos.polymarket_crypto5m.direction_state import DirectionState


def _thesis(side, conv=0.6, ofi_vote=1):
    sign = 1 if side == "Up" else (-1 if side == "Down" else 0)
    return {"side": side, "conviction": conv, "score": 0.6 * sign,
            "votes": {"order_flow": (ofi_vote if side else None)}}


def _desk(ret15=0.0006, ret30=0.0005):
    return {"ret_15s": ret15, "ret_30s": ret30}


def test_sustained_move_confirms_after_persist_ticks():
    ds = DirectionState(persist_ticks=3, enter_conviction=0.5)
    r1 = ds.update(_thesis("Up"), _desk(), 100)
    r2 = ds.update(_thesis("Up"), _desk(), 102)
    assert not r1["confirmed"] and not r2["confirmed"]      # not yet persistent
    r3 = ds.update(_thesis("Up"), _desk(), 104)
    assert r3["confirmed"] and r3["confirmed_side"] == "Up" and r3["persistence_ticks"] >= 3


def test_wiggle_never_confirms():
    ds = DirectionState(persist_ticks=3)
    sides = ["Up", "Down", "Up", "Down", "Up"]
    res = [ds.update(_thesis(s), _desk(ret15=(0.0006 if s == "Up" else -0.0006),
                                       ret30=(0.0005 if s == "Up" else -0.0005)), 100 + 2 * i)
           for i, s in enumerate(sides)]
    assert not any(r["confirmed"] for r in res)             # alternating sign never persists


def test_single_spike_does_not_confirm():
    ds = DirectionState(persist_ticks=3)
    ds.update(_thesis(None), _desk(), 100)
    r = ds.update(_thesis("Up", conv=0.9), _desk(), 102)    # one strong tick is not memory
    assert not r["confirmed"] and r["persistence_ticks"] == 1


def test_horizon_disagreement_blocks_confirmation():
    ds = DirectionState(persist_ticks=2)
    # read Up persists, but the long horizon (ret_30s) points Down -> not a real trend
    ds.update(_thesis("Up"), _desk(ret15=0.0006, ret30=-0.0005), 100)
    r = ds.update(_thesis("Up"), _desk(ret15=0.0006, ret30=-0.0005), 102)
    assert not r["confirmed"] and r["horizons_agree"] is False


def test_order_flow_disagreement_blocks_confirmation():
    ds = DirectionState(persist_ticks=2)
    ds.update(_thesis("Up", ofi_vote=-1), _desk(), 100)
    r = ds.update(_thesis("Up", ofi_vote=-1), _desk(), 102)  # tape flow opposes the read
    assert not r["confirmed"] and r["flow_agree"] is False


def test_weak_conviction_blocks_confirmation():
    ds = DirectionState(persist_ticks=2, enter_conviction=0.5)
    ds.update(_thesis("Up", conv=0.2), _desk(), 100)
    r = ds.update(_thesis("Up", conv=0.2), _desk(), 102)
    assert not r["confirmed"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_direction_state.py -q`
Expected: FAIL with `ModuleNotFoundError: ... direction_state`.

- [ ] **Step 3: Write the implementation**

```python
# sniper/algos/polymarket_crypto5m/direction_state.py
"""Memory/confirmation layer for the crypto5m scalper — the missing 'distinguish a real direction from a
market micro-adjustment'.

`direction.directional_thesis` gives an INSTANTANEOUS read each tick; it has no memory, so the scalper used
to commit and un-commit on noise. DirectionState sits between the thesis and the scalper and confirms a
direction only when it is REAL: the read sign has PERSISTED across ticks (memory), a short and a longer
horizon AGREE (a trend shows at multiple scales; a wiggle at one), the tape ORDER FLOW agrees, and the read
clears the conviction bar. Pure + unit-tested; the runner holds one instance and calls .update() each tick.
"""
from __future__ import annotations

DEFAULT_PERSIST_TICKS = 3          # consecutive same-sign updates (~6s at a 2s poll) = memory
DEFAULT_PERSIST_WINDOW_S = 12      # only remember this far back (bounds the history)
DEFAULT_ENTER_CONVICTION = 0.50    # strength floor (matches scalp_trader's gate)


def _sign(side):
    return 1 if side == "Up" else (-1 if side == "Down" else 0)


def _agree(side_sign, *vals):
    """True iff every PRESENT value shares the side's sign (None values are ignored, but at least one must
    be present)."""
    present = [v for v in vals if v is not None]
    return bool(present) and all((v > 0) == (side_sign > 0) for v in present)


class DirectionState:
    def __init__(self, persist_ticks=DEFAULT_PERSIST_TICKS, persist_window_s=DEFAULT_PERSIST_WINDOW_S,
                 enter_conviction=DEFAULT_ENTER_CONVICTION):
        self.persist_ticks = persist_ticks
        self.persist_window_s = persist_window_s
        self.enter_conviction = enter_conviction
        self._hist = []                                  # list of (ts, side) most-recent last

    def _run_length(self, side):
        """How many consecutive most-recent updates share `side` (the memory depth of the current read)."""
        if not side:
            return 0
        n = 0
        for _, s in reversed(self._hist):
            if s == side:
                n += 1
            else:
                break
        return n

    def update(self, thesis, desk, now) -> dict:
        side = thesis.get("side")
        self._hist.append((now, side))
        self._hist = [(t, s) for (t, s) in self._hist if t >= now - self.persist_window_s]
        sign = _sign(side)
        persistence = self._run_length(side)
        horizons_agree = _agree(sign, desk.get("ret_15s"), desk.get("ret_30s")) if side else False
        of_vote = (thesis.get("votes") or {}).get("order_flow")
        flow_agree = of_vote is not None and of_vote == sign and sign != 0
        conviction = float(thesis.get("conviction") or 0.0)
        confirmed = bool(side and persistence >= self.persist_ticks and horizons_agree
                         and flow_agree and conviction >= self.enter_conviction)
        return {"confirmed": confirmed, "confirmed_side": side if confirmed else None,
                "read_side": side, "persistence_ticks": persistence, "horizons_agree": horizons_agree,
                "flow_agree": flow_agree, "conviction": conviction}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_direction_state.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/direction_state.py tests/test_crypto5m_direction_state.py
git commit -m "feat(crypto5m): direction-state memory layer — confirm persistence + multi-horizon + flow"
```

---

### Task 2: `scalp_trader` — confirmed-only entry + PATIENCE exit

**Files:**
- Modify: `sniper/algos/polymarket_crypto5m/scalp_trader.py`
- Test: `tests/test_crypto5m_direction_scalp.py`

**Interfaces:**
- Consumes: `confirm` dict from `DirectionState.update` (Task 1); `Position` with new `adverse_since`/`flip_since` fields.
- Produces: `decide(*, thesis, book, position, now_ts, seconds_left, stake_usdc=10.0, confirm=None) -> dict`; `update_exit_state(position, book, thesis, now_ts) -> None` (mutates `position.adverse_since`/`flip_since`, mirroring how the runner updates `peak_bid`); constants `PATIENCE_STOP=0.10`, `PATIENCE_S=12`, `FLIP_PERSIST_S=6`, `CATASTROPHE=0.25`.

- [ ] **Step 1: Update the exit/entry constants and `Position`**

In `scalp_trader.py`, replace the stop constants block:

```python
# remove: STOP_SPREAD_MULT / STOP_FLOOR (tight price-stop is the bleed — gaps past it)
# EXIT philosophy v2: ride while the direction holds; the binary GAPS 5-12c per 2s poll, so a tight
# price-stop bails into the gap and 95% of the time the price recovers (median +26c). Exits are now
# PERSISTENCE-based and gap-immune.
TRAIL_SPREAD_MULT = 1.5     # lock the win once the bid gives back > this many spreads from its peak
TRAIL_FLOOR = 0.005
PATIENCE_STOP = 0.10        # an adverse move only counts once it's this deep...
PATIENCE_S = 12             # ...AND has PERSISTED this long (a 2s gap that recovers is ignored)
FLIP_PERSIST_S = 6          # a reversal must HOLD this long to count (not a single-tick opposite read)
CATASTROPHE = 0.25          # hard cut only on a genuine collapse
HOLD_SECONDS = 40
TAKE_PROFIT_PRICE = 0.90
MIN_SECONDS_LEFT = 20
MAX_ENTRY_PRICE = 0.92
MIN_ENTRY_PRICE = 0.08
EXIT_FLIP_CONVICTION = 0.15  # min conviction for an opposite read to START the flip timer
```

Add fields to `Position`:

```python
    adverse_since: int | None = None   # ts the bid first went > PATIENCE_STOP against us (runner updates)
    flip_since: int | None = None      # ts the read first flipped against us (runner updates)
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_crypto5m_direction_scalp.py`; also UPDATE the old entry tests)

Replace `test_enters_on_strong_read_at_market`, `test_waits_on_weak_read`, `test_scalper_vetoes_contra_flow_entry`, `test_scalper_enters_when_flow_agrees` (these tested the OLD instantaneous gate, now owned by direction_state) with:

```python
CONFIRMED_UP = {"confirmed": True, "confirmed_side": "Up", "persistence_ticks": 3}
NOT_CONFIRMED = {"confirmed": False, "confirmed_side": None, "persistence_ticks": 1}


def test_enters_only_on_confirmed_direction():
    a = S.decide(thesis=STRONG_UP, book=_book(up_ask=0.50), position=None, now_ts=100,
                 seconds_left=200, confirm=CONFIRMED_UP)
    assert a["action"] == "enter" and a["side"] == "Up" and a["price"] == 0.50


def test_waits_when_not_confirmed():
    a = S.decide(thesis=STRONG_UP, book=_book(), position=None, now_ts=100,
                 seconds_left=200, confirm=NOT_CONFIRMED)
    assert a["action"] == "wait" and "confirmed" in a["reason"]


def test_patience_stop_ignores_a_gap_but_cuts_a_persistent_adverse_move():
    # adverse move present but only just started -> HOLD (gap-immune)
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=119)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.38), position=pos, now_ts=120, seconds_left=150)
    assert a["action"] == "hold"
    # same adverse move, now persisted >= PATIENCE_S -> CUT
    pos2 = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=100)
    a2 = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.38), position=pos2, now_ts=100 + S.PATIENCE_S,
                  seconds_left=150)
    assert a2["action"] == "exit" and a2["kind"] == "stop"


def test_persistent_flip_exits_but_a_single_tick_flip_does_not():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, flip_since=110)
    hold = S.decide(thesis=FLIP_DOWN, book=_book(up_bid=0.49), position=pos, now_ts=112, seconds_left=150)
    assert hold["action"] == "hold"
    pos2 = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, flip_since=100)
    out = S.decide(thesis=FLIP_DOWN, book=_book(up_bid=0.49), position=pos2, now_ts=100 + S.FLIP_PERSIST_S,
                   seconds_left=150)
    assert out["action"] == "exit" and out["kind"] == "reversal"


def test_catastrophe_cap_cuts_a_collapse():
    pos = Position("Up", 0.50, 100, 20.0, peak_bid=0.50, adverse_since=None)
    a = S.decide(thesis=STRONG_UP, book=_book(up_bid=0.24), position=pos, now_ts=104, seconds_left=150)
    assert a["action"] == "exit" and a["kind"] == "stop"


def test_update_exit_state_sets_and_clears_timers():
    pos = Position("Up", 0.50, 100, 20.0)
    S.update_exit_state(pos, _book(up_bid=0.38), STRONG_UP, 130)   # 12c adverse -> timer starts
    assert pos.adverse_since == 130
    S.update_exit_state(pos, _book(up_bid=0.49), STRONG_UP, 132)   # recovered -> timer clears
    assert pos.adverse_since is None
    S.update_exit_state(pos, _book(up_bid=0.49), FLIP_DOWN, 134)   # opposite read -> flip timer starts
    assert pos.flip_since == 134
```

Keep `test_rides_while_direction_holds_no_fixed_cap`, `test_trailing_locks_after_giveback_from_peak`, `test_near_certain_locks`, `test_timeout_closes_a_stalled_scalp` (their `Position(...)` calls still work with the new optional fields). Keep the thesis-fusion and `direction_state` tests.

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_crypto5m_direction_scalp.py -q`
Expected: FAIL (`decide()` unexpected `confirm`; `update_exit_state` missing).

- [ ] **Step 4: Implement `update_exit_state` and rewrite `decide`'s entry + exit**

Add the helper:

```python
def update_exit_state(position, book, thesis, now_ts):
    """Maintain the gap-immune exit timers on the position each tick (the runner calls this before decide(),
    just like it updates peak_bid). adverse_since = when the bid first went > PATIENCE_STOP against us;
    flip_since = when the computed direction first flipped against us. Cleared the moment the condition lifts,
    so a transient 2s gap can't trip the persistence timers."""
    sell_bid, _ = _bid_ask(book, position.side)
    if sell_bid is not None and 0.0 < sell_bid < 1.0:
        adverse = (sell_bid - position.entry_price) <= -PATIENCE_STOP
        position.adverse_since = (position.adverse_since or now_ts) if adverse else None
    opp = (thesis.get("side") and thesis.get("side") != position.side
           and thesis.get("conviction", 0) >= EXIT_FLIP_CONVICTION)
    position.flip_since = (position.flip_since or now_ts) if opp else None
```

Rewrite the entry block of `decide` (confirmed-only):

```python
    if position is None:
        if seconds_left < MIN_SECONDS_LEFT:
            return {"action": "wait", "reason": "too little time left to scalp"}
        conf = confirm or {}
        side = conf.get("confirmed_side")
        if not conf.get("confirmed") or side is None:
            return {"action": "wait", "score": thesis.get("score"),
                    "reason": f"no confirmed direction (persist {conf.get('persistence_ticks', 0)}, "
                              f"horizons={conf.get('horizons_agree')}, flow={conf.get('flow_agree')})"}
        _bid, ask = _bid_ask(book, side)
        if ask is None or not (MIN_ENTRY_PRICE < ask < MAX_ENTRY_PRICE):
            return {"action": "wait", "best_side": side,
                    "reason": f"confirmed {side} but ask {'—' if ask is None else f'{ask:.2f}'} unusable"}
        return {"action": "enter", "side": side, "price": ask, "kind": "entry",
                "shares": round(stake_usdc / ask, 4), "score": thesis.get("score"),
                "conviction": thesis.get("conviction"), "entry_prob": thesis.get("score"),
                "persistence": conf.get("persistence_ticks"),
                "reason": f"CONFIRMED {side} (held {conf.get('persistence_ticks')} ticks, horizons+flow agree); "
                          f"take @ {ask:.2f}, ride until the direction turns"}
```

Update the signature: `def decide(*, thesis, book, position, now_ts, seconds_left, stake_usdc=10.0, confirm=None):`

Rewrite the EXIT section (replace the tight-stop branch). The take-profit and trailing blocks stay; replace the stop with patience/flip/catastrophe:

```python
        if sell_bid >= TAKE_PROFIT_PRICE:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "take_profit",
                    "reason": f"near-certain — lock {side} @ {sell_bid:.2f} (+{100*gain:.1f}c)"}
        if peak_gain > 0 and gain <= peak_gain - trail:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "take_profit",
                    "reason": f"gave back {100*(peak_gain-gain):.1f}c from +{100*peak_gain:.1f}c peak — "
                              f"lock {side} @ {sell_bid:.2f} ({'+' if gain >= 0 else ''}{100*gain:.1f}c)"}
        # PATIENCE stop: cut only if the adverse move PERSISTED (gap-immune)
        if position.adverse_since is not None and (now_ts - position.adverse_since) >= PATIENCE_S:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "stop",
                    "reason": f"adverse {100*gain:.1f}c held {now_ts - position.adverse_since}s — cut {side} @ {sell_bid:.2f}"}
        # PERSISTENT flip: the direction turned AND stayed turned
        if position.flip_since is not None and (now_ts - position.flip_since) >= FLIP_PERSIST_S:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "reversal",
                    "reason": f"read flipped & held {now_ts - position.flip_since}s — bank {side} @ {sell_bid:.2f} "
                              f"({'+' if gain >= 0 else ''}{100*gain:.1f}c)"}
        if gain <= -CATASTROPHE:                                            # genuine collapse
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "stop",
                    "reason": f"collapse {100*gain:.1f}c (> {100*CATASTROPHE:.0f}c) — cut {side} @ {sell_bid:.2f}"}
```

The `trail` line stays as-is (`trail = max(TRAIL_FLOOR, TRAIL_SPREAD_MULT * spread) ...`). Remove the old `stop = max(STOP_FLOOR, ...)` line and the old reversal/stop branches. Keep the `held >= HOLD_SECONDS` timeout and the final `hold` return.

Remove the now-dead `ENTER_CONVICTION` / `MIN_AGREE` / `REQUIRE_ORDER_FLOW_AGREE` constants and their entry logic (confirmation now lives in `direction_state`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_crypto5m_direction_scalp.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add sniper/algos/polymarket_crypto5m/scalp_trader.py tests/test_crypto5m_direction_scalp.py
git commit -m "feat(crypto5m): confirmed-only entry + gap-immune PATIENCE exit (drop the tight stop)"
```

---

### Task 3: Wire the runner — DirectionState, exit-state, `entry_persistence`

**Files:**
- Modify: `scripts/run_polymarket_crypto5m_active_trader.py`
- Test: `tests/test_crypto5m_active_runner.py`

**Interfaces:**
- Consumes: `DirectionState` (Task 1), `update_exit_state`/`decide(confirm=...)` (Task 2).
- Produces: new column `entry_persistence` on `crypto5m_active_positions`; `confirmed_side`/`persistence_ticks`/`horizons_agree` in the state JSON.

- [ ] **Step 1: Add the import, the migration, and the DirectionState instance**

Add import near the others:

```python
from sniper.algos.polymarket_crypto5m import direction_state as DS          # noqa: E402
```

Add `entry_persistence` to the migration loop in `_connect`:

```python
    for col, typ in (("entry_prob", "REAL"), ("decision_price", "REAL"), ("fill_slip", "REAL"),
                     ("entry_conviction", "REAL"), ("entry_ofi", "REAL"), ("entry_persistence", "INTEGER")):
```

In `run()`, before the `while True:` loop:

```python
    dstate = DS.DirectionState()      # one instance — BTC direction persists across windows; ~6s memory
```

- [ ] **Step 2: Thread confirm + exit-state into the loop**

After `thesis = DIR.directional_thesis(...)`, add:

```python
            confirm = dstate.update(thesis, desk, now)
```

Where `peak_bid` is updated (inside `if pos is not None:`), also maintain the exit timers:

```python
                ST.update_exit_state(pos, book, thesis, now)
```

Change the `decide` call to pass `confirm`:

```python
            action = ST.decide(thesis=thesis, book=book, position=pos, now_ts=now,
                               seconds_left=desk.get("seconds_left", 0), stake_usdc=STAKE, confirm=confirm)
```

In the pending-intent block, record persistence:

```python
                pending[ws] = {"side": action["side"], "decision_price": action["price"],
                               "entry_prob": action.get("entry_prob"), "conviction": action.get("conviction"),
                               "ofi": desk.get("tape_ofi_10s"), "persistence": action.get("persistence"),
                               "reason": action.get("reason")}
```

In the fill block, pass it to `_open_position` (add a kwarg):

```python
                    _open_position(conn, _pos_key(ws, seq[ws]), ws, desk.get("condition_id"), pos,
                                   pend["reason"], now, decision_price=pend["decision_price"], fill_slip=slip,
                                   conviction=pend["conviction"], ofi=pend["ofi"], persistence=pend.get("persistence"))
```

Update `_open_position` signature + INSERT:

```python
def _open_position(conn, key, ws, cond, pos: Position, reason, now, *,
                   decision_price=None, fill_slip=None, conviction=None, ofi=None, persistence=None):
    conn.execute("""INSERT OR REPLACE INTO crypto5m_active_positions
        (position_key, campaign_id, window_start_ts, condition_id, side, entry_ts, entry_price, shares,
         entry_reason, entry_prob, decision_price, fill_slip, entry_conviction, entry_ofi, entry_persistence,
         status, persist_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
        (key, CAMPAIGN_ID, ws, cond, pos.side, pos.entry_ts, pos.entry_price, pos.shares,
         reason, pos.entry_prob, decision_price, fill_slip, conviction, ofi, persistence, now))
    conn.commit()
```

Add confirmation fields to `_write_state`'s `st` dict (pass `confirm` into `_write_state`):

```python
          "confirmed_side": (confirm or {}).get("confirmed_side"),
          "persistence_ticks": (confirm or {}).get("persistence_ticks"),
          "horizons_agree": (confirm or {}).get("horizons_agree"),
```

Update the `_write_state(...)` call and signature to take `confirm=confirm`.

- [ ] **Step 3: Update the runner test**

In `tests/test_crypto5m_active_runner.py`, the scripted desks must now produce a CONFIRMED read (persistence). Update `test_deferred_fill_records_slip` so the Up read is fed for enough ticks to confirm before the fill, and assert `entry_persistence` is recorded:

```python
def test_deferred_fill_records_slip(tmp_path, monkeypatch):
    ws = 1782000000
    # feed a SUSTAINED Up read so DirectionState confirms (>=3 ticks), THEN the fill at a moved touch.
    desks = iter([
        _desk(ws + 100, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick1 read Up
        _desk(ws + 102, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick2 still Up
        _desk(ws + 104, ws, up_ask=0.50, up_bid=0.49, ofi=0.6),   # tick3 confirmed -> SIGNAL
        _desk(ws + 106, ws, up_ask=0.52, up_bid=0.51, ofi=0.6),   # tick4 FILL @ 0.52, slip +0.02
        _desk(ws + 108, ws, up_ask=0.52, up_bid=0.52, ofi=0.6),   # tick5 hold
    ])
    monkeypatch.setattr(runner.LD, "build_live_desk", lambda *a, **k: next(desks))
    monkeypatch.setattr(runner.time, "sleep", lambda *_: None)
    conn = runner._connect(str(tmp_path / "t.sqlite"))
    runner.run(conn, poll_interval=0, max_iterations=5)
    r = dict(conn.execute("SELECT side, entry_price, decision_price, fill_slip, entry_persistence, status "
                          "FROM crypto5m_active_positions").fetchall()[0])
    assert r["side"] == "Up" and r["decision_price"] == 0.50 and r["entry_price"] == 0.52
    assert abs(r["fill_slip"] - 0.02) < 1e-9 and r["entry_persistence"] >= 3 and r["status"] == "open"
```

Ensure `_desk(...)` includes `ret_15s` and `ret_30s` with the same sign (already present: `ret_15s=0.0008`; add `ret_30s=0.0006`) so `horizons_agree` is True. Keep `test_no_signal_no_fill` (a weak read still never confirms).

- [ ] **Step 4: Run the runner test**

Run: `python3 -m pytest tests/test_crypto5m_active_runner.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full focused suite**

Run: `python3 -m pytest tests/test_crypto5m_direction_state.py tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py tests/test_crypto5m_live_desk.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_polymarket_crypto5m_active_trader.py tests/test_crypto5m_active_runner.py
git commit -m "feat(crypto5m): runner wires DirectionState + exit-state + persists entry_persistence"
```

---

### Task 4: Surface the confirmation state in the UI (display-only)

**Files:**
- Modify: `sniper/routes/api_crypto5m.py`
- Modify: `static/app.js`
- Modify: `templates/index.html`

**Interfaces:**
- Consumes: `confirmed_side`/`persistence_ticks`/`horizons_agree` in the state JSON (Task 3).
- Produces: the 5m Binary tab desk shows "read forming: Up held Ns" vs "CONFIRMED" — no behavior change.

- [ ] **Step 1: Pass the fields through the API**

In `sniper/routes/api_crypto5m.py`, find where `/api/crypto5m/active` builds its response from the state JSON and add the three fields to the returned object (next to the existing thesis/tape fields). Confirm the exact key names by reading the handler first; mirror the existing passthrough pattern.

- [ ] **Step 2: Render it in the desk**

In `static/app.js`, in the crypto5m active-desk renderer (the DIRECTIONAL READ panel), add a line: if `e.confirmed_side`, show `CONFIRMED {side}`; else show `read forming: {read_side or '—'} held {persistence_ticks||0} ticks` plus `horizons {✓/✗}`. Match the existing `_thesisHtml` style.

- [ ] **Step 3: Bump the cache version**

In `templates/index.html`, bump `app.js?v=NNN` to the next number; mirror it in any `CACHE`/version constant in `app.js` if one exists.

- [ ] **Step 4: Smoke-test the endpoint**

Run (engine must be running): `curl -s localhost:5000/api/crypto5m/active | python3 -m json.tool | grep -E "confirmed_side|persistence_ticks|horizons_agree"`
Expected: the three keys are present.

- [ ] **Step 5: Commit**

```bash
git add sniper/routes/api_crypto5m.py static/app.js templates/index.html
git commit -m "feat(crypto5m-ui): show the engine's direction-confirmation state on the 5m desk"
```

---

### Task 5: Regression replay, restart, and land

**Files:** none modified (verification + ops).

- [ ] **Step 1: Align the replay constants with the shipped engine**

Confirm `scripts/crypto5m_exit_replay.py` PATIENCE/PERSIST constants match the values in `scalp_trader.py` + `direction_state.py` (PATIENCE_STOP=0.10, PATIENCE_S=12, FLIP_PERSIST_S=6, CATASTROPHE=0.25, PERSIST_K=3). Fix any drift.

- [ ] **Step 2: Re-run the regression replay**

Run: `cp data/crypto5m_capture.sqlite /tmp/cap_snap.sqlite && cp data/crypto5m_capture.sqlite-wal /tmp/cap_snap.sqlite-wal 2>/dev/null; python3 scripts/crypto5m_exit_replay.py /tmp/cap_snap.sqlite`
Expected: `PERSIST+PATIENCE` row remains clearly the best (net ≫ today's `INSTANT+CURRENT`, lowest worst_window). If it regressed, STOP — a constant or logic drifted from the spec.

- [ ] **Step 3: Run the full focused suite**

Run: `python3 -m pytest tests/test_crypto5m_direction_state.py tests/test_crypto5m_direction_scalp.py tests/test_crypto5m_active_runner.py tests/test_crypto5m_live_desk.py tests/test_crypto5m_active_trader.py -q`
Expected: all pass.

- [ ] **Step 4: Restart the engine and verify it confirms before entering**

```bash
PID=$(pgrep -f run_polymarket_crypto5m_active_trader | head -1); kill "$PID" 2>/dev/null; sleep 2
bash scripts/crypto5m_watchdog.sh; sleep 6
pgrep -af run_polymarket_crypto5m_active_trader | grep -v grep
```
Then confirm the state carries the new fields:
`python3 -c "import sqlite3,json; c=sqlite3.connect('data/crypto5m/active_trader_v0.sqlite'); j=json.loads(c.execute('SELECT state_json FROM crypto5m_active_state WHERE id=1').fetchone()[0]); print({k:j.get(k) for k in ('confirmed_side','persistence_ticks','horizons_agree')})"`
Expected: the three keys print (values may be None/0 when no read is forming).

- [ ] **Step 5: Validate schema refs + land to main**

```bash
python3 scripts/validate_schema_refs.py
```
Then land to `origin/main` via the worktree cherry-pick pattern (`git worktree add /tmp/sniper-main origin/main`, cherry-pick the task commits, run validator + index freshness + the focused suite in the worktree, `git push origin HEAD:main`, `git worktree remove /tmp/sniper-main`). Leave the foreign lane-eval WIP untouched.

- [ ] **Step 6: Update memory**

Append the v2 outcome to `~/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/project-crypto5m-active-engine.md` and the [[finding-crypto5m-walkforward-tape-model]] follow-up: the live forward run (bucketed by `entry_persistence`) is now the arbiter; do not claim edge off the replay.

---

## Validation / success criteria

- All focused tests green; the replay's `PERSIST+PATIENCE` row stays clearly best.
- Engine restarted; the desk shows confirmation state; entries fire only after a confirmed (persistent) read.
- LIVE forward run is the arbiter: after ~150 trades, the slip-logged review bucketed by `entry_persistence` + exit kind shows the stop-bleed gone, take-profit win-rate preserved, net per-trade ≥ 0 after real fills. If live is negative despite the replay, investigate execution (touch-optimism) — do NOT re-tune on the replay.
