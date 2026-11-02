# Hyperliquid Copy Coin-Key Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make paper mirror reconciliation compare target, book, and mark coin keys case-insensitively without changing canonical symbols or restarting the live runner.

**Architecture:** `copy_sim.mirror_actions()` builds collision-aware comparison indexes for all three input mappings. Comparisons use `casefold()` keys; actions retain target spelling for open/hold and stored book spelling for close. Ambiguous normalized collisions fail closed for only the affected key.

**Tech Stack:** Python 3.13, pure functions, pytest.

## Global Constraints

- This branch is `fix/hl-copy-coin-key-normalization`; no Edge Desk/API/template/JS/CSS change belongs here.
- Normalize for comparison only. Never pass a synthesized uppercased symbol to the exchange adapter.
- Do not mutate `target_positions`, `our_book`, or `marks`.
- Do not change entry policy, alt-short taxonomy, costs, sizing, persistence, or schemas.
- Ambiguous case-insensitive collisions emit no open, close, or hold action for that comparison key.
- Do not restart `run_hl_copy.py`; the fix remains dormant in live state until separately authorized.

---

### Task 1: Normalize all mirror-action mapping comparisons

**Files:**
- Modify: `sniper/hyperlive/copy_sim.py:20-45`
- Modify: `tests/test_hyperlive_copy_sim.py`

**Interfaces:**
- Keeps: `mirror_actions(target_positions: dict, our_book: dict, marks: dict, fee_bps: float = FEE_BPS) -> dict`.
- Adds private: `_coin_key(coin: object) -> str`.
- Adds private: `_coin_index(mapping: dict) -> tuple[dict[str, tuple[str, object]], set[str]]`.
- Output symbols: target spelling for `open`/`hold`; book spelling for `close`.

- [ ] **Step 1: Add failing tests for target/book and mark normalization**

```python
def test_mirror_actions_matches_target_book_and_marks_case_insensitively():
    target = {"kPEPE": {"side": "short"}}
    book = {"KPEPE": {"side": "short", "entry_mark": 10.0}}
    marks = {"KPEPE": 9.0}

    result = CS.mirror_actions(target, book, marks)

    assert result == {
        "open": [],
        "close": [],
        "hold": [{"coin": "kPEPE", "side": "short"}],
    }

def test_mirror_actions_closes_mixed_case_book_with_mixed_case_mark():
    result = CS.mirror_actions(
        {},
        {"kSHIB": {"side": "short", "entry_mark": 10.0}},
        {"KSHIB": 9.0},
    )
    assert result["close"][0]["coin"] == "kSHIB"
    assert result["close"][0]["exit_mark"] == 9.0

def test_mirror_actions_opens_with_target_spelling():
    result = CS.mirror_actions(
        {"kBONK": {"side": "short"}}, {}, {"KBONK": 2.0}
    )
    assert result["open"] == [{"coin": "kBONK", "side": "short", "entry_mark": 2.0}]
```

- [ ] **Step 2: Run the three tests and confirm RED**

Run: `python3 -m pytest tests/test_hyperlive_copy_sim.py -k 'case or spelling' -q`

Expected: hold/open/close assertions fail because current code uses exact dictionary keys.

- [ ] **Step 3: Add failing side-flip, native-uppercase, collision, and immutability tests**

```python
def test_mirror_actions_side_flip_preserves_each_source_spelling():
    result = CS.mirror_actions(
        {"KPEPE": {"side": "short"}},
        {"kPEPE": {"side": "long", "entry_mark": 10.0}},
        {"KPEPE": 9.0},
    )
    assert result["open"][0]["coin"] == "KPEPE"
    assert result["close"][0]["coin"] == "kPEPE"

def test_mirror_actions_keeps_native_uppercase_ticker():
    result = CS.mirror_actions({"KAITO": {"side": "long"}}, {}, {"KAITO": 1.5})
    assert result["open"][0]["coin"] == "KAITO"

def test_mirror_actions_case_collision_fails_closed_for_ambiguous_key():
    target = {"kPEPE": {"side": "short"}, "KPEPE": {"side": "short"}}
    result = CS.mirror_actions(target, {}, {"KPEPE": 9.0})
    assert result == {"open": [], "close": [], "hold": []}

def test_mirror_actions_does_not_mutate_inputs():
    target = {"kFLOKI": {"side": "short"}}
    book = {"KFLOKI": {"side": "long", "entry_mark": 10.0}}
    marks = {"KFLOKI": 9.0}
    before = copy.deepcopy((target, book, marks))
    CS.mirror_actions(target, book, marks)
    assert (target, book, marks) == before
```

- [ ] **Step 4: Run the expanded tests and confirm RED**

Run: `python3 -m pytest tests/test_hyperlive_copy_sim.py -q`

Expected: new normalization tests fail; existing exact-case tests remain green.

- [ ] **Step 5: Implement collision-aware indexes**

```python
def _coin_key(coin: object) -> str:
    return str(coin or "").casefold()

def _coin_index(mapping: dict) -> tuple[dict[str, tuple[str, object]], set[str]]:
    index: dict[str, tuple[str, object]] = {}
    ambiguous: set[str] = set()
    for original, value in mapping.items():
        key = _coin_key(original)
        if not key:
            continue
        if key in index:
            index.pop(key, None)
            ambiguous.add(key)
        elif key not in ambiguous:
            index[key] = (str(original), value)
    return index, ambiguous
```

- [ ] **Step 6: Use indexes across all three mappings**

At function entry, build indexes and one unsafe set:

```python
target_index, target_ambiguous = _coin_index(target_positions)
book_index, book_ambiguous = _coin_index(our_book)
mark_index, mark_ambiguous = _coin_index(marks)
unsafe = target_ambiguous | book_ambiguous | mark_ambiguous
```

Iterate the original target mapping for open/hold order and original book mapping for close order. For each original symbol, skip when its comparison key is unsafe. Resolve counterpart and mark values from indexes. Keep existing fee and PnL math unchanged.

- [ ] **Step 7: Run focused and runner-adjacent tests**

Run:

```bash
python3 -m pytest tests/test_hyperlive_copy_sim.py tests/test_run_hl_copy_capture.py tests/test_run_hl_copy_selection.py -q
```

Expected: all pass.

- [ ] **Step 8: Run hygiene checks**

Run: `git diff --check`

Expected: no output.

- [ ] **Step 9: Commit the isolated fix**

```bash
git add sniper/hyperlive/copy_sim.py tests/test_hyperlive_copy_sim.py
git commit -m "fix(hl): normalize mirror comparison keys"
```

### Task 2: Verify dormant deployment state

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes: committed pure-function fix.
- Produces: evidence that live `run_hl_copy.py` was not restarted or activated.

- [ ] **Step 1: Record the live runner PID/start time without mutation**

Run: `ps -eo pid,lstart,args | rg 'scripts/run_hl_copy.py' | rg -v 'rg '`

Expected: the existing production process remains unchanged.

- [ ] **Step 2: Confirm branch separation**

Run: `git diff --name-only main...HEAD`

Expected: only `sniper/hyperlive/copy_sim.py`, `tests/test_hyperlive_copy_sim.py`, and this plan are present.

- [ ] **Step 3: Prepare Knowledge Sync Gate evidence**

State `Knowledge delta: bugfix-only. The comparison fix preserves symbols and has no runtime effect until restart.` Do not push, open a PR, merge, restart services, close rows, or activate funded execution.
