# Wallet Map slice 3 (flow change around a source buy): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline; Lambda spawns
> no subagents on this host, operator rule 2026-08-14). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The hourly graph job measures, for each Solana source buy, other traders' net SOL flow in the
60 s after the buy against the 60 s before it. It stores one row per read and one row per wallet with a
pre-registered verdict. The Wallet Map shows the result as its default bubble size.

**Architecture:**
- The job side is a new section of `scripts/research/wallet_link_graph.py`. It reuses the slot-read
  queue (`memecoin_wallet_slot_reads`), the research-key fetch (`helius_slot_fetch`), the decoder
  (`_default_trade`) and `binomial_tail`. The section has pure functions (`impact_read`,
  `read_impacts`, `impact_units`, `wallet_impacts`), thin fetchers, and one orchestrator
  (`impact_pass`) that `run()` calls between the slot reads and the funder reads.
- The map side reads the new per-wallet table read-only in `sniper/services/memecoin_wallet_map.py`,
  and draws it in `static/wallet_map.js`.

**Tech stack:** Python 3.13 stdlib (`sqlite3`, `statistics`), pytest, the Helius
`getTransactionsForAddress` method on the research key (`funded_exit_truth.rpc`), vanilla JS and d3
(already vendored), node for the UI tests.

**Spec:** `docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md` (45c9385ae,
Gamma CLEAN 2026-09-30). The parent spec is `docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md`.

## Global constraints

- **Method:** `impact-v1:w60:sol:first-buy:det-origin:age60:show10:test20:cens25:loo-median:bh05`.
- **Window and cap:** ±60 s around t0. The window read takes at most 10 pages of 100, succeeded
  transactions only, ascending.
- **Age probe:** 1 call, ascending, limit 1, no filter. `launch` means `age_s <= 60`.
- **Budget:** `IMPACT_BUDGET = 300` calls per run, with a worst case of 11 calls per read. Every Helius
  call in the run stays under `SLOT_BUDGET_MAX = 2000`. The CLI flag is `--impact-budget`.
- **Verdicts:** stats show from n >= 10, the test runs from n >= 20, a wallet whose `censored_share` is
  over 0.25 is `unknown`, and Benjamini-Hochberg runs at q = 0.05 over the tested wallets.
- **Which reads count:** detection-fed units only in `n`, the stats, the pooled median and the test.
  Fill-fed units are reported apart.
- **Where the job reads:** research key only (`pick_research_url`, reached through
  `funded_exit_truth.rpc`), and only while the Solana book is flat (`solana_book_open` is false before
  every call).
- **No funded import:** nothing in the job section or the map imports `evm_real_seams`,
  `solana_real_seams`, Jupiter or price helpers.
- **Web access:** the web opens the graph DB `mode=ro`. The web makes no chain call.
- **Wording:** the UI says "flow change after vs before", never "moves the market".
- **Git:** Lambda works on branch `lambda/wallet-map-impact` in worktree `~/.cache/lambda/wt-map`.
  - Commit by explicit pathspec only.
  - Write commit messages with `-F -` and a quoted heredoc; no backticks inside double quotes.
  - Never push, never deploy. Alpha merges and deploys: the job ships with the memecoin runtime
    release (the cron runs from `.runtime/memecoin/current`), the map with the web release.
- **Writes:** Lambda never writes `data/`, `.runtime/`, `.env` or the production graph DB. The smoke run
  uses a copy in the scratchpad.
- **Tests:**
  - pytest runs with `-n 6` at most.
  - Gate every claim on the exit code (`echo $?`), never on a tail of the output.
  - Prove every new test red on the parent first, and kill a mutant for every claim.
- **Build fold (Gamma LOW, not in the spec text):** an empty age probe retries at most
  `PROBE_EMPTY_MAX = 3` runs, then the read is stored as censored status `probe_empty`, and
  `probe_empty` counts in `censored_share`. The read row carries `empty_probes`.

## File map

| File | Change | Responsibility |
|---|---|---|
| `scripts/research/wallet_link_graph.py` | modify | Constants and pure read core; fetchers (`helius_slot_fetch` gains `window` and `pages`; new `helius_first_tx_time`, `helius_impact_window`); `read_impacts`; units and ledger helpers; storage (`IMPACT_SCHEMA`); aggregation (`wallet_impacts`, `_bh`, `_k_needed`, `write_impacts`); `impact_pass`; `run()` wiring and `--impact-budget` |
| `tests/test_wallet_link_graph_impact.py` | create | Every job-side check from the spec's "Checks left behind" |
| `sniper/services/memecoin_wallet_map.py` | modify | Read `memecoin_wallet_impact`; `stamp.impact`; `node.impact` |
| `tests/test_memecoin_wallet_map.py` | modify | Missing table, stranger null, verdict pass-through, schema drift |
| `static/wallet_map.js` | modify | `defaultSizeBy`, `nodeStyle(n, sizeBy)`, `impactText`, size-by select, hover lines, mover ring, stamp |
| `static/styles.css` | modify | `.wm-node.wm-mover` ring |
| `tests/test_memecoin_wallet_map_ui.py` | modify | Size-by default, negative median radius, wording, verdict text |

---

### Task 1: Pure per-read core (`impact_read`)

**Files:**
- Modify: `scripts/research/wallet_link_graph.py`. Append a new section after `shared_funder_edges`,
  before `BRAIN_SECTION`.
- Create: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Produces:
  - constants `IMPACT_METHOD`, `IMPACT_WINDOW_S`, `IMPACT_PAGES`, `IMPACT_CALLS_MAX`, `IMPACT_BUDGET`,
    `LAUNCH_AGE_S`, `PROBE_EMPTY_MAX`, `SHOW_N`, `TEST_N`, `CENSORED_MAX`, `BH_Q`, `CENSORED`;
  - `impact_read(txs, source_sig, t0, mint, *, excluded, ours, trade_fn=None, sig_fn=None) -> dict`.
    The status is `ok`, `not_found`, `clock_mismatch` or `non_sol`. When ok, the dict also holds
    `net_before`, `net_after`, `delta_sol`, `buyers_before`, `buyers_after`, `tx_before`, `tx_after`,
    `undecoded_before` and `undecoded_after`.

- [ ] **Step 1: Write the failing tests**

```python
"""Wallet Map slice 3: flow change around a source buy (spec
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md, Gamma CLEAN 45c9385ae).
Pure: page fixtures, no network."""
import sqlite3

from scripts.research import wallet_link_graph as g

MINT = "Mint111"
T0 = 1_790_000_000


def _tx(sig, slot, index, t, trader=None, side="buy", sol=1.0, unit="SOL"):
    return {"sig": sig, "slot": slot, "transactionIndex": index, "blockTime": t,
            "trade": ({"trader": trader, "side": side, "amount": sol, "unit": unit} if trader else None)}


def _trade(tx, mint):
    return tx["trade"]


def _sig(tx):
    return tx["sig"]


SRC = _tx("src", 100, 7, T0, "A")


def _read(txs, excluded=(), ours=()):
    return g.impact_read(txs, "src", T0, MINT, excluded=set(excluded), ours=set(ours),
                         trade_fn=_trade, sig_fn=_sig)


def test_before_and_after_split_by_chain_order_inside_the_source_second():
    txs = [SRC, _tx("b1", 90, 1, T0 - 30, "B", sol=2.0), _tx("same_early", 100, 3, T0, "C", sol=1.0),
           _tx("same_late", 100, 9, T0, "D", sol=5.0), _tx("a_sell", 120, 2, T0 + 20, "E", side="sell", sol=1.5),
           _tx("too_early", 50, 1, T0 - 61, "F", sol=9.0), _tx("too_late", 200, 1, T0 + 61, "G", sol=9.0),
           _tx("undec", 110, 1, T0 + 5)]
    r = _read(txs)
    assert r["status"] == "ok"
    assert (r["net_before"], r["buyers_before"], r["tx_before"], r["undecoded_before"]) == (3.0, 2, 2, 0)
    assert (r["net_after"], r["buyers_after"], r["tx_after"], r["undecoded_after"]) == (3.5, 1, 3, 1)
    assert r["delta_sol"] == 0.5


def test_our_fills_the_sources_other_address_and_its_own_trader_are_not_market_flow():
    txs = [SRC, _tx("our_exit", 110, 1, T0 + 3, "US", side="sell", sol=3.0),
           _tx("alt", 111, 1, T0 + 4, "A2", sol=4.0), _tx("again", 112, 1, T0 + 5, "A", sol=2.0)]
    r = _read(txs, excluded={"A2"}, ours={"our_exit"})
    assert (r["status"], r["net_after"], r["buyers_after"], r["tx_after"]) == ("ok", 0.0, 0, 2)


def test_a_non_sol_trade_in_either_window_censors_the_read():
    assert _read([SRC, _tx("u", 90, 1, T0 - 5, "B", unit="USDC")])["status"] == "non_sol"
    assert _read([SRC, _tx("u", 110, 1, T0 + 5, "B", unit=None)])["status"] == "non_sol"


def test_a_missing_source_or_a_clock_mismatch_is_censored():
    assert _read([_tx("x", 100, 1, T0, "B")]) == {"status": "not_found"}
    assert _read([_tx("src", 100, 7, T0 + 1, "A")]) == {"status": "clock_mismatch"}


def test_empty_windows_are_a_known_zero_and_a_missing_amount_is_undecoded():
    r = _read([SRC])
    assert (r["status"], r["net_before"], r["net_after"], r["delta_sol"]) == ("ok", 0.0, 0.0, 0.0)
    r = _read([SRC, _tx("x", 110, 1, T0 + 2, "B", sol=None)])
    assert (r["net_after"], r["undecoded_after"], r["tx_after"]) == (0.0, 1, 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_wallet_link_graph_impact.py -q -p no:cacheprovider; echo $?`
Expected: exit 1, `AttributeError: module ... has no attribute 'impact_read'`.

- [ ] **Step 3: Write the implementation.** Add `import statistics` to the imports at the top, then
  add the new section:

```python
# ---------------------------------------------------------------- flow change around a source buy
# Wallet Map slice 3 (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3-impact-design.md,
# Gamma CLEAN 45c9385ae). One read per source buy (the first ok slot read per source and mint): other
# traders' net SOL flow 60 s after minus 60 s before, split by chain order. Descriptive, not causal;
# nothing here arms, sizes, admits or exits. Research key only, and only while the Solana book is flat.
IMPACT_METHOD = "impact-v1:w60:sol:first-buy:det-origin:age60:show10:test20:cens25:loo-median:bh05"
IMPACT_WINDOW_S = 60
IMPACT_PAGES = 10
IMPACT_CALLS_MAX = 1 + IMPACT_PAGES      # the age probe plus the window pages: a read's worst case
IMPACT_BUDGET = 300                      # Helius calls per run for these reads
LAUNCH_AGE_S = 60                        # the mint's first transaction this close to t0: a launch read
PROBE_EMPTY_MAX = 3                      # Gamma LOW (build): an index gap is retried 3 runs, then censored
SHOW_N, TEST_N, CENSORED_MAX, BH_Q = 10, 20, 0.25, 0.05
CENSORED = ("truncated", "not_found", "clock_mismatch", "non_sol", "probe_empty")


def impact_read(txs, source_sig, t0, mint, *, excluded, ours, trade_fn=None, sig_fn=None):
    """Other traders' net SOL flow in the IMPACT_WINDOW_S before and after the source transaction, split
    by chain order (slot, transactionIndex). Status ok, not_found, clock_mismatch or non_sol; the numbers
    exist only when ok. `excluded` holds the source's addresses and ours (the source transaction's own
    trader is added here); a transaction in `ours` (our fill signatures) is dropped entirely."""
    trade_fn, sig_fn = trade_fn or _default_trade, sig_fn or _default_sig
    src = next((t for t in txs if sig_fn(t) == source_sig), None)
    if src is None:
        return {"status": "not_found"}
    if src.get("blockTime") != t0:
        return {"status": "clock_mismatch"}

    def order(t):
        return (t.get("slot") or 0, t.get("transactionIndex") or 0)
    mine = trade_fn(src, mint)
    skip = set(excluded) | ({mine["trader"]} if mine else set())
    net, buyers = {"before": 0.0, "after": 0.0}, {"before": set(), "after": set()}
    n_tx, undecoded = {"before": 0, "after": 0}, {"before": 0, "after": 0}
    for t in txs:
        sig, bt = sig_fn(t), t.get("blockTime")
        if sig == source_sig or sig in ours or bt is None:
            continue
        if order(t) < order(src) and bt >= t0 - IMPACT_WINDOW_S:
            side = "before"
        elif order(t) > order(src) and bt <= t0 + IMPACT_WINDOW_S:
            side = "after"
        else:
            continue
        n_tx[side] += 1
        tr = trade_fn(t, mint)
        if tr is None:
            undecoded[side] += 1
            continue
        if tr.get("unit") != "SOL":
            return {"status": "non_sol"}
        if tr["trader"] in skip:
            continue
        amount = tr.get("amount", tr.get("sol"))
        if amount is None:
            undecoded[side] += 1                  # a trade without a size is not a zero
            continue
        if tr["side"] == "buy":
            net[side] += float(amount)
            buyers[side].add(tr["trader"])
        else:
            net[side] -= float(amount)
    return {"status": "ok", "net_before": round(net["before"], 9), "net_after": round(net["after"], 9),
            "delta_sol": round(net["after"] - net["before"], 9),
            "buyers_before": len(buyers["before"]), "buyers_after": len(buyers["after"]),
            "tx_before": n_tx["before"], "tx_after": n_tx["after"],
            "undecoded_before": undecoded["before"], "undecoded_after": undecoded["after"]}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_wallet_link_graph_impact.py -q -p no:cacheprovider; echo $?`
Expected: 5 passed, exit 0.

- [ ] **Step 5: Kill the mutants.** Copy the file aside first, then apply each mutant with `sed`, run
  the file, and restore. Each run must exit 1.
  - Split by blockTime: replace `order(t) < order(src)` with `bt < t0` and `order(t) > order(src)` with
    `bt > t0`.
  - Excluded ignored: replace `skip = set(excluded) | ` with `skip = `.
  - `ours` ignored: delete ` or sig in ours`.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): impact_read, net SOL flow around a source buy by chain order (slice 3 task 1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 2: Fetchers (window read and age probe)

**Files:**
- Modify: `scripts/research/wallet_link_graph.py`: `helius_slot_fetch` (`window`, `pages` kwargs), and
  new `helius_impact_window` and `helius_first_tx_time` next to it.
- Test: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Consumes: `ReadStop`, `_NoStop`, `SLOT_PAGES`, `IMPACT_WINDOW_S`, `IMPACT_PAGES`.
- Produces:
  - `helius_slot_fetch(det, *, rpc=None, flat_fn=..., window=None, pages=SLOT_PAGES) -> (txs, calls, truncated)`.
    Its existing behaviour is unchanged when `window` and `pages` are omitted.
  - `helius_impact_window(mint, t0, *, rpc=None, flat_fn=...) -> (txs, calls, truncated)`.
  - `helius_first_tx_time(mint, *, rpc=None, flat_fn=...) -> (block_time or None, 1)`. It raises
    `ReadStop` on quota, HTTP, RPC, timeout, or a book that is open.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _page(n, token, t=T0):
    return {"data": [{"slot": 5, "transactionIndex": i, "blockTime": t} for i in range(n)], "paginationToken": token}


def test_the_window_read_filters_plus_minus_60_s_and_caps_at_10_pages():
    seen = []

    def rpc(method, params):
        seen.append((params[0], params[1]["filters"], params[1]["sortOrder"], params[1]["limit"]))
        return _page(100, "more")
    txs, used, truncated = g.helius_impact_window(MINT, T0, rpc=rpc)
    assert (len(txs), used, truncated) == (1000, 10, True)
    assert seen[0] == (MINT, {"status": "succeeded", "blockTime": {"gte": T0 - 60, "lte": T0 + 60}}, "asc", 100)


def test_the_slot_read_keeps_its_three_page_cap():
    txs, used, truncated = g.helius_slot_fetch({"mint": MINT, "block_time": 1}, rpc=lambda m, p: _page(100, "more"))
    assert (used, truncated) == (g.SLOT_PAGES, True)


def test_the_age_probe_reads_the_mints_first_transaction():
    seen = []

    def rpc(method, params):
        seen.append(params[1])
        return {"data": [{"blockTime": T0 - 500}]}
    assert g.helius_first_tx_time(MINT, rpc=rpc) == (T0 - 500, 1)
    assert seen[0]["sortOrder"] == "asc" and seen[0]["limit"] == 1 and "filters" not in seen[0]
    assert g.helius_first_tx_time(MINT, rpc=lambda m, p: {"data": []}) == (None, 1)


def test_the_age_probe_stops_on_a_failed_call_or_an_open_book():
    def boom(method, params):
        raise OSError("down")
    for kw in ({"rpc": boom}, {"rpc": lambda m, p: {"data": []}, "flat_fn": lambda: False}):
        try:
            g.helius_first_tx_time(MINT, **kw)
        except g.ReadStop:
            continue
        raise AssertionError("no ReadStop for %r" % kw)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_wallet_link_graph_impact.py -q -p no:cacheprovider; echo $?`
Expected: exit 1, `AttributeError ... helius_impact_window`.

- [ ] **Step 3: Write the implementation.** In `helius_slot_fetch`, change the signature and the two
  lines that build `window` and loop:

```python
def helius_slot_fetch(det, *, rpc=None, flat_fn=lambda: True, window=None, pages=SLOT_PAGES):
```

```python
    # By slot when it is known (a fill's exact source signature), else by the detection's blockTime;
    # a caller's own `window` (slice 3: +-60 s) replaces both.
    if window is None:
        window = ({"slot": {"gte": int(det["slot"]), "lte": int(det["slot"])}} if det.get("slot") is not None
                  else {"blockTime": {"gte": int(det["block_time"]), "lte": int(det["block_time"])}})
```

```python
    for _ in range(pages):
```

Add after `helius_slot_fetch`:

```python
def helius_impact_window(mint, t0, *, rpc=None, flat_fn=lambda: True):
    """Slice 3: the mint's succeeded transactions with blockTime in t0 +- 60 s, ascending, at most
    IMPACT_PAGES pages; (txs, calls, truncated)."""
    return helius_slot_fetch({"mint": mint}, rpc=rpc, flat_fn=flat_fn, pages=IMPACT_PAGES,
                             window={"blockTime": {"gte": int(t0) - IMPACT_WINDOW_S,
                                                   "lte": int(t0) + IMPACT_WINDOW_S}})


def helius_first_tx_time(mint, *, rpc=None, flat_fn=lambda: True):
    """Slice 3 age probe (Gamma L1): the blockTime of the mint's FIRST transaction (ascending, limit 1,
    no filter), or None on an empty page (an index gap, never a launch); (block_time, 1)."""
    stop = _NoStop
    if rpc is None:
        sys.path.insert(0, str(REPO / "scripts" / "research"))
        import funded_exit_truth as fet         # the research key; refuses a funded key (pick_research_url)
        rpc, stop = fet.rpc, fet.Stop
    if not flat_fn():
        raise ReadStop("solana book open mid-read")
    opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 1, "encoding": "json",
            "maxSupportedTransactionVersion": 1}
    try:
        res = rpc("getTransactionsForAddress", [mint, opts]) or {}
    except stop as exc:
        raise ReadStop(str(exc)[:160])
    except (TimeoutError, OSError) as exc:
        raise ReadStop("%s: %s" % (type(exc).__name__, str(exc)[:120]))
    page = res.get("data") or []
    bt = page[0].get("blockTime") if page else None
    return (None if bt is None else int(bt)), 1
```

- [ ] **Step 4: Run the new file and the slot tests.** The slot tests check that `helius_slot_fetch`
  still behaves as before.

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_wallet_link_graph_impact.py tests/test_wallet_link_graph_slots.py -q -p no:cacheprovider; echo $?`
Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - `pages=IMPACT_PAGES` changed to `pages=SLOT_PAGES`;
  - `"sortOrder": "asc", "limit": 1` changed to `"sortOrder": "desc", "limit": 1`.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): +-60 s window read (10 pages) and first-transaction age probe (slice 3 task 2)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 3: One read per unit (`read_impacts`)

**Files:**
- Modify: `scripts/research/wallet_link_graph.py` (after `impact_read`).
- Test: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Consumes: `impact_read`, `ReadStop`, `IMPACT_CALLS_MAX`, `LAUNCH_AGE_S`, `PROBE_EMPTY_MAX`.
- Produces:
  - `read_impacts(units, *, fetch_age, fetch_window, budget, flat_fn, excluded_for, ours, trade_fn=None, sig_fn=None) -> (reads, calls, why)`.
  - A unit is `{signature, source_user_id, mint, origin, t0, empty_probes}`.
  - `fetch_age(unit) -> (block_time | None, calls)`.
  - `fetch_window(unit) -> (txs, calls, truncated)`.
  - `excluded_for(uid) -> set`.
  - Each read is the unit's fields plus `network_id='solana'`, `status`, `age_s`, `calls`,
    `empty_probes`, and, when ok, the `impact_read` numbers.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _unit(sig="src", t0=T0, origin="detection", empties=0):
    return {"signature": sig, "source_user_id": "uA", "mint": MINT, "origin": origin, "t0": t0,
            "empty_probes": empties}


def _run(units, *, first=T0 - 600, window=None, truncated=False, budget=100, flat=lambda: True, stop=None):
    windows = []

    def fetch_age(u):
        return first, 1

    def fetch_window(u):
        windows.append(u["signature"])
        if stop:
            raise g.ReadStop(stop)
        return (window if window is not None else [SRC, _tx("b", 110, 1, T0 + 2, "B", sol=2.0)]), 1, truncated
    reads, calls, why = g.read_impacts(units, fetch_age=fetch_age, fetch_window=fetch_window, budget=budget,
                                       flat_fn=flat, excluded_for=lambda uid: set(), ours=set(),
                                       trade_fn=_trade, sig_fn=_sig)
    return reads, calls, why, windows


def test_a_mint_at_most_60_s_old_is_a_launch_read_with_no_window_call():
    reads, calls, why, windows = _run([_unit()], first=T0 - 60)
    assert (reads[0]["status"], reads[0]["age_s"], calls, windows) == ("launch", 60, 1, [])
    reads, calls, why, windows = _run([_unit()], first=T0 - 61)
    assert (reads[0]["status"], reads[0]["delta_sol"], calls, windows) == ("ok", 2.0, 2, ["src"])


def test_an_empty_age_probe_is_unread_then_probe_empty_after_three_runs():
    reads, *_ = _run([_unit(empties=0)], first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("unread", 1)
    reads, *_ = _run([_unit(empties=2)], first=None)
    assert (reads[0]["status"], reads[0]["empty_probes"]) == ("probe_empty", 3)


def test_truncation_wins_over_a_missing_source():
    reads, *_ = _run([_unit()], window=[_tx("other", 90, 1, T0 - 50, "B")], truncated=True)
    assert reads[0]["status"] == "truncated"


def test_a_read_starts_only_when_its_worst_case_fits_and_the_book_is_flat():
    assert _run([_unit()], budget=10)[:3] == ([], 0, "budget")
    assert len(_run([_unit()], budget=11)[0]) == 1
    assert _run([_unit()], flat=lambda: False)[:3] == ([], 0, "solana book open")


def test_a_failed_call_records_the_read_unread_and_ends_the_pass():
    reads, calls, why, _ = _run([_unit("src"), _unit("second")], stop="HTTP 429 max usage")
    assert [r["status"] for r in reads] == ["unread"] and why.startswith("stopped: HTTP 429")
```

- [ ] **Step 2: Run the tests to verify they fail.** Run the same file. Expected: exit 1,
  `AttributeError ... read_impacts`.

- [ ] **Step 3: Write the implementation**

```python
def read_impacts(units, *, fetch_age, fetch_window, budget, flat_fn, excluded_for, ours, trade_fn=None,
                 sig_fn=None):
    """(reads, calls, why stopped). Per unit: the age probe first -- an empty page is unread (index gap),
    probe_empty after PROBE_EMPTY_MAX empty runs; age <= LAUNCH_AGE_S is launch and skips the window.
    Then the window: truncated wins over not_found (a cut page can miss the source). A read starts only
    if its worst case fits the budget and while the Solana book is flat; a ReadStop records the read
    unread and ends the pass (a failed call's quota use is not counted)."""
    reads, calls = [], 0
    for u in units:
        if calls + IMPACT_CALLS_MAX > budget:
            return reads, calls, "budget"
        if not flat_fn():
            return reads, calls, "solana book open"
        base = {k: u[k] for k in ("signature", "source_user_id", "mint", "origin", "t0")}
        base.update(network_id="solana", empty_probes=int(u.get("empty_probes") or 0))
        used = 0
        try:
            first, n = fetch_age(u)
            used += n
            if first is None:
                empties = base["empty_probes"] + 1
                reads.append(dict(base, status="probe_empty" if empties >= PROBE_EMPTY_MAX else "unread",
                                  empty_probes=empties, calls=used))
                calls += used
                continue
            age = int(u["t0"]) - int(first)
            if age <= LAUNCH_AGE_S:
                reads.append(dict(base, status="launch", age_s=age, calls=used))
                calls += used
                continue
            txs, n, truncated = fetch_window(u)
            used += n
        except ReadStop as exc:
            reads.append(dict(base, status="unread", calls=used))
            return reads, calls + used, "stopped: %s" % exc
        calls += used
        if truncated:
            reads.append(dict(base, status="truncated", age_s=age, calls=used))
            continue
        reads.append(dict(base, age_s=age, calls=used,
                          **impact_read(txs, u["signature"], int(u["t0"]), u["mint"],
                                        excluded=excluded_for(u["source_user_id"]), ours=ours,
                                        trade_fn=trade_fn, sig_fn=sig_fn)))
    return reads, calls, "done"
```

- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - `age <= LAUNCH_AGE_S` changed to `age < LAUNCH_AGE_S`;
  - `calls + IMPACT_CALLS_MAX > budget` changed to `calls + 1 > budget`;
  - the `if truncated:` block moved after the `impact_read` append (so the truncated read runs through
    `impact_read`): delete the three `if truncated` lines.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): read_impacts: age probe, launch, probe_empty, truncation first, worst-case budget (slice 3 task 3)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 4: Units, origin, t0, exclusions and storage

**Files:**
- Modify: `scripts/research/wallet_link_graph.py` (after `read_impacts`).
- Test: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Consumes: `_ts`, `graph_connection`, `IMPACT_METHOD`.
- Produces:
  - `detection_block_times(ledger, sigs) -> {sig: int}`;
  - `impact_units(slot_reads, block_times) -> [unit]`, where a unit has `signature`, `source_user_id`,
    `mint`, `origin`, `t0`, `slot` and `idx`;
  - `our_fill_sigs(ledger) -> set`;
  - `excluded_traders(owners, own_addresses) -> callable(uid) -> set`;
  - `IMPACT_SCHEMA`, `READ_COLS`, `save_impact_reads(path, reads, *, read_at)`,
    `load_impact_reads(path) -> {sig: read}`;
  - `pending_impacts(units, done) -> [unit with empty_probes]`, detection-fed first, newest t0 first.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _slot(sig, uid, mint, detected_at, status="ok", slot=5, idx=1):
    return {"signature": sig, "source_user_id": uid, "network_id": "solana", "mint": mint, "status": status,
            "detected_at": detected_at, "slot": slot, "index": idx}


def test_units_take_the_first_buy_by_chain_time_with_origin_and_t0():
    reads = [_slot("d1", "uA", "M1", "2026-09-30T00:00:03+00:00"),        # detection: detected_at is HOST time
             _slot("d0", "uA", "M1", "2026-09-30T00:00:05+00:00"),
             _slot("f1", "uA", "M2", "2026-09-30T00:00:10+00:00"),        # fill-fed: detected_at is chain time
             _slot("bad", "uB", "M3", "2026-09-30T00:00:10+00:00", status="not_found")]
    base = int(g._ts("2026-09-30T00:00:00+00:00"))
    units = {u["signature"]: u for u in g.impact_units(reads, {"d1": base + 2, "d0": base + 1})}
    assert set(units) == {"d0", "f1"}                                     # d0 is earlier by chain time
    assert (units["d0"]["origin"], units["d0"]["t0"]) == ("detection", base + 1)
    assert (units["f1"]["origin"], units["f1"]["t0"]) == ("fill", base + 10)


def test_detection_block_times_skip_null_and_our_fill_sigs_include_exits():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
              "detected_at, created_at) VALUES ('s1','uA','a','M',100,'t','t'), ('s2','uA','a','M',NULL,'t','t')")
    assert g.detection_block_times(c, ["s1", "s2", "s3"]) == {"s1": 100}
    c.execute("INSERT INTO memecoin_positions (id, source_user_id, mint, network_id, execution_mode, is_open) "
              "VALUES (1,'uA','M','solana','real',0), (2,'uA','E','4663','real',0)")
    c.execute("INSERT INTO memecoin_fills (position_id, kind, tx_sig) VALUES (1,'entry','in'), (1,'exit','out'), "
              "(2,'entry','evm')")
    assert g.our_fill_sigs(c) == {"in", "out"}


def test_excluded_traders_hold_every_source_address_the_pump_id_and_ours():
    ex = g.excluded_traders({"A1": "uA", "A2": "uA", "B1": "uB"}, {"OURS"})
    assert ex("uA") == {"A1", "A2", "OURS"}
    assert ex("pump:PumpAddr") == {"PumpAddr", "OURS"}


def test_impact_reads_persist_and_only_unread_ones_are_pending(tmp_path):
    graph = tmp_path / "graph.db"
    base = {"source_user_id": "uA", "network_id": "solana", "mint": "M", "t0": 1, "calls": 1}
    g.save_impact_reads(graph, [dict(base, signature="ok1", origin="detection", status="ok", delta_sol=1.5),
                                dict(base, signature="un1", origin="fill", status="unread", empty_probes=2)],
                        read_at="t")
    done = g.load_impact_reads(graph)
    assert done["ok1"]["delta_sol"] == 1.5 and done["un1"]["empty_probes"] == 2
    units = [{"signature": s, "origin": o, "t0": t} for s, o, t in
             (("ok1", "detection", 5), ("un1", "fill", 9), ("new_old", "detection", 1), ("new_new", "detection", 7))]
    pend = g.pending_impacts(units, done)
    assert [u["signature"] for u in pend] == ["new_new", "new_old", "un1"]
    assert pend[2]["empty_probes"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail.** Same command. Expected: exit 1,
  `AttributeError ... impact_units`.

- [ ] **Step 3: Write the implementation**

```python
IMPACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_impact_reads (
    signature TEXT PRIMARY KEY,
    source_user_id TEXT,
    network_id TEXT,
    mint TEXT,
    origin TEXT NOT NULL,
    t0 INTEGER,
    age_s INTEGER,
    status TEXT NOT NULL,
    delta_sol REAL,
    net_before REAL,
    net_after REAL,
    buyers_before INTEGER,
    buyers_after INTEGER,
    tx_before INTEGER,
    tx_after INTEGER,
    undecoded_before INTEGER,
    undecoded_after INTEGER,
    empty_probes INTEGER NOT NULL DEFAULT 0,
    calls INTEGER,
    method_version TEXT NOT NULL,
    read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_impact (
    source_user_id TEXT NOT NULL,
    network_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    median_delta_sol REAL,
    p25_delta_sol REAL,
    p75_delta_sol REAL,
    share_positive REAL,
    median_delta_buyers REAL,
    median_undecoded_share REAL,
    censored TEXT NOT NULL,
    censored_share REAL,
    n_fill INTEGER NOT NULL,
    median_delta_sol_fill REAL,
    k INTEGER,
    k_needed INTEGER,
    p_value REAL,
    q_value REAL,
    verdict TEXT NOT NULL,
    verdict_reason TEXT,
    window_from TEXT,
    computed_at TEXT NOT NULL,
    method_version TEXT NOT NULL,
    PRIMARY KEY (source_user_id, network_id, method_version)
);
"""
READ_COLS = ("signature", "source_user_id", "network_id", "mint", "origin", "t0", "age_s", "status",
             "delta_sol", "net_before", "net_after", "buyers_before", "buyers_after", "tx_before", "tx_after",
             "undecoded_before", "undecoded_after", "empty_probes", "calls")


def detection_block_times(ledger, sigs):
    """{signature: block_time} of watcher detections with a chain time; 500 per query on the unique
    signature index."""
    out, sigs = {}, list(sigs)
    for i in range(0, len(sigs), 500):
        chunk = sigs[i:i + 500]
        out.update({r[0]: int(r[1]) for r in ledger.execute(
            "SELECT signature, block_time FROM onchain_wallet_detections WHERE signature IN (%s) "
            "AND block_time IS NOT NULL" % ",".join("?" * len(chunk)), chunk)})
    return out


def impact_units(slot_reads, block_times):
    """One unit per (source_user_id, mint): its first ok Solana slot read by chain time. origin
    'detection' when the watcher saw the signature with a chain time (our gates did not select it),
    else 'fill'. t0: the detection's block_time; else detected_at, which only fill-fed reads reach and
    which they store as the page blockTime (a detection-fed detected_at is HOST time, never t0)."""
    best = {}
    for r in slot_reads:
        if r["status"] != "ok" or r.get("network_id") != "solana" or not r.get("detected_at"):
            continue
        sig = r["signature"]
        det = block_times.get(sig)
        u = {"signature": sig, "source_user_id": r["source_user_id"], "mint": r["mint"],
             "origin": "detection" if det is not None else "fill",
             "t0": int(det) if det is not None else int(_ts(r["detected_at"])),
             "slot": r.get("slot") or 0, "idx": r.get("index") or 0}
        key = (u["source_user_id"], u["mint"])
        if key not in best or (u["t0"], u["slot"], u["idx"]) < (best[key]["t0"], best[key]["slot"], best[key]["idx"]):
            best[key] = u
    return list(best.values())


def our_fill_sigs(ledger):
    """Every Solana fill signature of ours, entries AND exits: our own sells never read as other
    traders' flow (our_entry_sigs covers entries only)."""
    return {r[0] for r in ledger.execute(
        "SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE p.network_id='solana' AND f.tx_sig IS NOT NULL")}


def excluded_traders(owners, own_addresses):
    """uid -> the addresses whose trades are never market flow around that source's buy: its verified
    Solana addresses (solana_owners), its pump:<address> id, and our own addresses."""
    by_uid = collections.defaultdict(set)
    for address, uid in owners.items():
        by_uid[uid].add(address)
    own = set(own_addresses)
    return lambda uid: by_uid.get(uid, set()) | ({uid[5:]} if uid.startswith("pump:") else set()) | own


def _impact_db(path):
    conn = graph_connection(path)
    conn.executescript(IMPACT_SCHEMA)
    return conn


def save_impact_reads(path, reads, *, read_at):
    conn = _impact_db(path)
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO memecoin_wallet_impact_reads (%s, method_version, read_at) VALUES (%s)"
                % (", ".join(READ_COLS), ",".join("?" * (len(READ_COLS) + 2))),
                [tuple(int(r.get(c) or 0) if c == "empty_probes" else r.get(c) for c in READ_COLS)
                 + (IMPACT_METHOD, read_at) for r in reads])
    finally:
        conn.close()


def load_impact_reads(path):
    conn = _impact_db(path)
    try:
        rows = conn.execute("SELECT %s FROM memecoin_wallet_impact_reads WHERE method_version=?"
                            % ", ".join(READ_COLS), (IMPACT_METHOD,)).fetchall()
    finally:
        conn.close()
    return {r[0]: dict(zip(READ_COLS, r)) for r in rows}


def pending_impacts(units, done):
    """Units with no final read (an unread one is retried with its empty-probe count), detection-fed
    first, then newest t0 first."""
    out = []
    for u in units:
        prior = done.get(u["signature"])
        if prior is not None and prior["status"] != "unread":
            continue
        out.append(dict(u, empty_probes=(prior or {}).get("empty_probes") or 0))
    return sorted(out, key=lambda u: (u["origin"] != "detection", -u["t0"]))
```

- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - add ` AND f.kind='entry'` to the `our_fill_sigs` SQL;
  - in `impact_units`, set `"t0"` from `_ts(r["detected_at"])` always;
  - in `pending_impacts`, sort with key `-u["t0"]` only.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): impact units (first buy, origin, chain t0), exclusions, impact read storage (slice 3 task 4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 5: Per-wallet aggregation and the mover test

**Files:**
- Modify: `scripts/research/wallet_link_graph.py` (after `pending_impacts`).
- Test: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Consumes: `binomial_tail`, `_pct`, `SHOW_N`, `TEST_N`, `CENSORED_MAX`, `BH_Q`, `CENSORED`, `_impact_db`.
- Produces:
  - `_bh(pvalues) -> [q]`;
  - `_k_needed(n, m) -> int | None`;
  - `wallet_impacts(reads, *, window_from, computed_at) -> [row]`, where a row's keys are the
    `memecoin_wallet_impact` columns without `method_version`;
  - `write_impacts(path, rows) -> int`.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _r(uid, delta, status="ok", origin="detection"):
    return {"source_user_id": uid, "origin": origin, "status": status, "delta_sol": delta if status == "ok" else None,
            "buyers_before": 1, "buyers_after": 2, "tx_before": 2, "tx_after": 2,
            "undecoded_before": 0, "undecoded_after": 1}


def _rows(reads):
    return {r["source_user_id"]: r for r in g.wallet_impacts(reads, window_from="w", computed_at="c")}


def test_verdict_bands_by_n():
    reads = [_r("n9", 1.0)] * 9 + [_r("n19", 1.0)] * 19 + [_r("n20", 1.0)] * 20 + [_r("pool", 0.0)] * 30
    rows = _rows(reads)
    assert (rows["n9"]["verdict"], rows["n9"]["verdict_reason"], rows["n9"]["median_delta_sol"]) == ("unknown", "n", None)
    assert (rows["n19"]["verdict"], rows["n19"]["median_delta_sol"], rows["n19"]["p_value"]) == ("untested", 1.0, None)
    assert rows["n20"]["verdict"] in ("mover", "not_distinguished") and rows["n20"]["k"] == 20


def test_censored_share_over_a_quarter_is_unknown():
    reads = ([_r("hi", 1.0)] * 20 + [_r("hi", None, "truncated")] * 7
             + [_r("lo", 1.0)] * 20 + [_r("lo", None, "non_sol")] * 6
             + [_r("lo", None, "launch")] * 9 + [_r("lo", None, "unread")] * 9 + [_r("pool", 0.0)] * 30)
    rows = _rows(reads)
    assert (rows["hi"]["verdict"], rows["hi"]["verdict_reason"], rows["hi"]["censored_share"]) == ("unknown", "censored", 0.2593)
    assert rows["lo"]["censored_share"] == 0.2308 and rows["lo"]["verdict"] in ("mover", "not_distinguished")
    assert rows["lo"]["censored"] == {"non_sol": 6, "launch": 9}


def test_fill_fed_reads_are_reported_apart_and_never_move_the_median():
    reads = [_r("x", 1.0)] * 20 + [_r("y", 0.0)] * 20 + [_r("z", 100.0, origin="fill")] * 20
    rows = _rows(reads)
    assert rows["x"]["k"] == 20                          # M = y's 0.0; z's fills would make it 50
    assert (rows["z"]["n"], rows["z"]["verdict"], rows["z"]["n_fill"], rows["z"]["median_delta_sol_fill"]) == (0, "unknown", 20, 100.0)


def test_the_roster_median_leaves_the_wallet_out():
    rows = _rows([_r("x", 5.0)] * 20 + [_r("y", 2.0)] * 10)
    assert (rows["x"]["k"], rows["x"]["verdict"]) == (20, "mover")    # pooled with itself M = 5.0 and k = 0


def test_bh_and_k_needed_by_hand():
    q = g._bh([0.01, 0.04, 0.03])
    assert [round(x, 6) for x in q] == [0.03, 0.04, 0.04]
    assert (g._k_needed(20, 23), g._k_needed(20, 8), g._k_needed(10, 23)) == (17, 16, 10)


def test_impact_rows_are_rewritten_per_run(tmp_path):
    graph = tmp_path / "graph.db"
    rows = g.wallet_impacts([_r("x", 1.0)] * 3, window_from="w", computed_at="c1")
    assert g.write_impacts(graph, rows) == 1 and g.write_impacts(graph, rows) == 1
    conn = sqlite3.connect(graph)
    assert conn.execute("SELECT COUNT(*), MAX(verdict) FROM memecoin_wallet_impact").fetchone() == (1, "unknown")
```

- [ ] **Step 2: Run the tests to verify they fail.** Same command. Expected: exit 1,
  `AttributeError ... wallet_impacts`.

- [ ] **Step 3: Write the implementation**

```python
IMPACT_ROW_COLS = ("source_user_id", "network_id", "n", "median_delta_sol", "p25_delta_sol", "p75_delta_sol",
                   "share_positive", "median_delta_buyers", "median_undecoded_share", "censored", "censored_share",
                   "n_fill", "median_delta_sol_fill", "k", "k_needed", "p_value", "q_value", "verdict",
                   "verdict_reason", "window_from", "computed_at")


def _bh(pvalues):
    """Benjamini-Hochberg adjusted q per position (step-up), capped at 1."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [None] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def _k_needed(n, m):
    """The smallest k whose one-sided binomial tail passes a rank-1 BH bar (BH_Q / m)."""
    return next((k for k in range(n + 1) if binomial_tail(k, n, 0.5) <= BH_Q / m), None)


def wallet_impacts(reads, *, window_from, computed_at):
    """Per-wallet rows from the current units' reads (spec 'Per wallet' and 'Mover test'). n, the stats,
    the roster median and the test use detection-fed ok reads only; fill-fed reads are reported apart."""
    by = collections.defaultdict(lambda: {"det": [], "fill": [], "buyers": [], "undec": [],
                                          "counts": collections.Counter()})
    for r in reads:
        w = by[r["source_user_id"]]
        if r["origin"] == "fill":
            if r["status"] == "ok":
                w["fill"].append(r["delta_sol"])
            continue
        if r["status"] == "ok":
            w["det"].append(r["delta_sol"])
            w["buyers"].append(r["buyers_after"] - r["buyers_before"])
            n_tx = (r["tx_before"] or 0) + (r["tx_after"] or 0)
            if n_tx:
                w["undec"].append(((r["undecoded_before"] or 0) + (r["undecoded_after"] or 0)) / n_tx)
        elif r["status"] != "unread":
            w["counts"][r["status"]] += 1
    rows = []
    for uid, w in sorted(by.items()):
        n, d = len(w["det"]), w["det"]
        cens = sum(w["counts"][s] for s in CENSORED)
        share = cens / (cens + n) if cens + n else None
        row = dict.fromkeys(IMPACT_ROW_COLS)
        row.update(source_user_id=uid, network_id="solana", n=n, censored=dict(w["counts"]),
                   censored_share=None if share is None else round(share, 4), n_fill=len(w["fill"]),
                   median_delta_sol_fill=(round(statistics.median(w["fill"]), 6) if len(w["fill"]) >= SHOW_N
                                          else None),
                   verdict="unknown", window_from=window_from, computed_at=computed_at)
        if n >= SHOW_N:
            row.update(median_delta_sol=round(statistics.median(d), 6), p25_delta_sol=_pct(d, 0.25),
                       p75_delta_sol=_pct(d, 0.75), share_positive=round(sum(x > 0 for x in d) / n, 4),
                       median_delta_buyers=statistics.median(w["buyers"]),
                       median_undecoded_share=round(statistics.median(w["undec"]), 4) if w["undec"] else None)
        if n < SHOW_N:
            row["verdict_reason"] = "n"
        elif share is not None and share > CENSORED_MAX:
            row["verdict_reason"] = "censored"
        else:
            row["verdict"] = "untested"
        rows.append(row)
    tested = []
    for row in rows:
        others = [x for uid, w in by.items() if uid != row["source_user_id"] for x in w["det"]]
        if row["verdict"] == "untested" and row["n"] >= TEST_N and others:
            mine, cut = by[row["source_user_id"]]["det"], statistics.median(others)
            row["k"] = sum(x > cut for x in mine)
            row["p_value"] = binomial_tail(row["k"], len(mine), 0.5)
            tested.append(row)
    for row, q in zip(tested, _bh([r["p_value"] for r in tested])):
        row["k_needed"] = _k_needed(row["n"], len(tested))
        row["q_value"] = round(q, 6)
        row["verdict"] = "mover" if q <= BH_Q else "not_distinguished"
    return rows


def write_impacts(path, rows):
    """Replace this method version's per-wallet rows in one transaction; returns the row count."""
    conn = _impact_db(path)
    try:
        with conn:
            conn.execute("DELETE FROM memecoin_wallet_impact WHERE method_version=?", (IMPACT_METHOD,))
            conn.executemany(
                "INSERT INTO memecoin_wallet_impact (%s, method_version) VALUES (%s)"
                % (", ".join(IMPACT_ROW_COLS), ",".join("?" * (len(IMPACT_ROW_COLS) + 1))),
                [tuple(json.dumps(r[c]) if c == "censored" else r[c] for c in IMPACT_ROW_COLS) + (IMPACT_METHOD,)
                 for r in rows])
        return len(rows)
    finally:
        conn.close()
```

- [ ] **Step 4: Run the tests to verify they pass.** Same command. Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - leave-one-out off: change `if uid != row["source_user_id"]` to `if True`;
  - fill-fed pooled: change `if r["origin"] == "fill":` to `if False:`;
  - test floor at 10: change `row["n"] >= TEST_N` to `row["n"] >= SHOW_N`;
  - no step-up in `_bh`: change `running = min(running, pvalues[i] * m / rank)` to
    `running = pvalues[i] * m / rank`;
  - censored share off: change `share > CENSORED_MAX` to `share > 1.0`.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): per-wallet flow-change rows, leave-one-out median binomial test with BH (slice 3 task 5)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 6: `impact_pass` and `run()` wiring

**Files:**
- Modify: `scripts/research/wallet_link_graph.py`: `impact_pass` after `write_impacts`, plus `run()` and
  `main()`.
- Test: `tests/test_wallet_link_graph_impact.py`.

**Interfaces:**
- Consumes: everything above, plus `load_reads`, `solana_owners`, and
  `sniper.services.memecoin_wallet_map.own_address_set`.
- Produces: `impact_pass(ledger, graph_path, since_iso, *, budget, flat_fn, own_addresses, fetch_age=None, fetch_window=None, now_iso=None) -> {reads_new, calls, stop, units, wallets}`.
  - The CLI gains `--impact-budget` (default `IMPACT_BUDGET`).
  - The run note gains `impact_reads_new`, `impact_stop` and `impact_calls`.

- [ ] **Step 1: Write the failing test** (append):

```python
def test_impact_pass_reads_pending_units_then_rewrites_the_wallet_rows(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    graph = tmp_path / "graph.db"
    monkeypatch.setattr(g, "solana_owners", lambda ledger: {})
    ledger = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, mint, block_time, "
                   "detected_at, created_at) VALUES ('src', 'uA', 'a', ?, ?, 't', 't')", (MINT, T0))
    g.save_reads(graph, [dict(_slot("src", "uA", MINT, "2026-09-30T00:00:01+00:00"), companions=[]),
                         dict(_slot("fsrc", "uB", MINT, g._iso(T0)), companions=[])], read_at="t")
    ages, windows = [], []

    def fetch_age(u):
        ages.append(u["signature"])
        return T0 - 600, 1

    def fetch_window(u):
        windows.append(u["signature"])
        src = dict(SRC, sig=u["signature"])
        return [src, _tx("b", 110, 1, T0 + 2, "B", sol=2.0)], 1, False
    kw = dict(budget=100, flat_fn=lambda: True, own_addresses=set(), fetch_age=fetch_age, fetch_window=fetch_window,
              now_iso="2026-09-30T12:00:00+00:00")
    out = g.impact_pass(ledger, graph, "2026-09-16T00:00:00+00:00", trade_fn=_trade, sig_fn=_sig, **kw)
    assert (out["reads_new"], out["calls"], out["units"], out["wallets"]) == (2, 4, 2, 2)
    assert ages == ["src", "fsrc"]                                        # detection-fed first
    again = g.impact_pass(ledger, graph, "2026-09-16T00:00:00+00:00", trade_fn=_trade, sig_fn=_sig,
                          **dict(kw, budget=0))
    assert (again["reads_new"], again["stop"], again["wallets"]) == (0, "skipped (budget 0)", 2)
    reads = g.load_impact_reads(graph)
    assert (reads["src"]["origin"], reads["fsrc"]["origin"], reads["src"]["delta_sol"]) == ("detection", "fill", 2.0)
```

(`impact_pass` takes `trade_fn` and `sig_fn` keyword arguments, which pass through to `read_impacts`
for tests. Production leaves them as None.)

- [ ] **Step 2: Run the test to verify it fails.** Same command. Expected: exit 1,
  `AttributeError ... impact_pass`.

- [ ] **Step 3: Write the implementation**

```python
def impact_pass(ledger, graph_path, since_iso, *, budget, flat_fn, own_addresses, fetch_age=None,
                fetch_window=None, now_iso=None, trade_fn=None, sig_fn=None):
    """Slice 3, once per run: read pending source buys (detection-fed first, newest first) within
    `budget`, then rewrite the per-wallet rows from every current unit's stored read."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    slot_reads = load_reads(graph_path, since_iso)
    units = impact_units(slot_reads, detection_block_times(
        ledger, [r["signature"] for r in slot_reads if r["status"] == "ok"]))
    done = load_impact_reads(graph_path)
    new, calls, why = [], 0, "skipped (budget 0)"
    if budget > 0:
        new, calls, why = read_impacts(
            pending_impacts(units, done), budget=budget, flat_fn=flat_fn,
            excluded_for=excluded_traders(solana_owners(ledger), own_addresses), ours=our_fill_sigs(ledger),
            fetch_age=fetch_age or (lambda u: helius_first_tx_time(u["mint"], flat_fn=flat_fn)),
            fetch_window=fetch_window or (lambda u: helius_impact_window(u["mint"], u["t0"], flat_fn=flat_fn)),
            trade_fn=trade_fn, sig_fn=sig_fn)
        save_impact_reads(graph_path, new, read_at=now_iso)
        done = load_impact_reads(graph_path)
    rows = wallet_impacts([done[u["signature"]] for u in units if u["signature"] in done],
                          window_from=since_iso, computed_at=now_iso)
    write_impacts(graph_path, rows)
    return {"reads_new": len(new), "calls": calls, "stop": why, "units": len(units), "wallets": len(rows)}
```

In `run()`, replace the funder block's opening:

```python
        funder_calls, funder_why, funder_new = 0, "skipped (budget 0)", 0
        f_budget = max(0, min(args.funder_budget, SLOT_BUDGET_MAX - slot_calls))
```

with:

```python
        from sniper.services.memecoin_wallet_map import own_address_set
        impact = impact_pass(ledger, graph_path, since, flat_fn=flat,
                             budget=max(0, min(args.impact_budget, SLOT_BUDGET_MAX - slot_calls)),
                             own_addresses=own_address_set(os.environ.get("MEMECOIN_OWN_ADDRESSES", "")))
        slot_calls += impact["calls"]
        funder_calls, funder_why, funder_new = 0, "skipped (budget 0)", 0
        f_budget = max(0, min(args.funder_budget, SLOT_BUDGET_MAX - slot_calls))
```

In the runs-row note format string, append ` impact_reads_new=%d impact_stop=%s impact_calls=%d`, and
append `impact["reads_new"], impact["stop"], impact["calls"]` to its tuple. Add the same three keys to
the final `print(json.dumps({...}))`.

In `main()`, add:

```python
    ap.add_argument("--impact-budget", type=int, default=IMPACT_BUDGET,
                    help="Helius calls for slice-3 flow-change reads this run (after the slot reads; 0 skips)")
```

- [ ] **Step 4: Run the whole graph test family to verify it passes.** Also confirm the CLI parses.

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_wallet_link_graph_impact.py tests/test_wallet_link_graph.py tests/test_wallet_link_graph_slots.py tests/test_wallet_link_graph_funders.py tests/test_wallet_link_graph_brain.py -q -p no:cacheprovider -n 6; echo $?`
Expected: exit 0.

Run: `cd ~/.cache/lambda/wt-map && python3 scripts/research/wallet_link_graph.py --help | grep -c impact-budget`
Expected: `1`.

- [ ] **Step 5: Kill the mutant.** Change `if budget > 0:` to `if True:`. The `again` assertion must fail
  (exit 1).

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add scripts/research/wallet_link_graph.py tests/test_wallet_link_graph_impact.py && git commit -q -F - <<'EOF'
feat(wallet-graph): impact_pass in the hourly run between slot and funder reads, --impact-budget 300 (slice 3 task 6)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 7: Map API reads the per-wallet rows

**Files:**
- Modify: `sniper/services/memecoin_wallet_map.py` (`build_wallet_map`).
- Test: `tests/test_memecoin_wallet_map.py`.

**Interfaces:**
- Consumes: the `memecoin_wallet_impact` table; its schema comes from `IMPACT_SCHEMA` in Task 4.
- Produces:
  - `stamp["impact"]`: `None`, or `{method_version, computed_at, window_from, wallets_known}`;
  - `stamp["impact_error"]`: `None` or `"impact_unreadable"`;
  - every node gets `impact`: `None`, or `{n, median_delta_sol, p25, p75, share_positive,
    median_delta_buyers, median_undecoded_share, censored, censored_share, n_fill,
    median_delta_sol_fill, k, k_needed, q_value, verdict, verdict_reason}`.

- [ ] **Step 1: Write the failing tests** (append):

```python
def _impact(g, rows):
    from scripts.research import wallet_link_graph as wlg
    g.executescript(wlg.IMPACT_SCHEMA)          # the job's own schema: a column drift fails here
    for r in rows:
        g.execute("INSERT INTO memecoin_wallet_impact (source_user_id, network_id, n, median_delta_sol, "
                  "p25_delta_sol, p75_delta_sol, censored, censored_share, n_fill, k, k_needed, q_value, verdict, "
                  "verdict_reason, window_from, computed_at, method_version) "
                  "VALUES (?,'solana',?,?,?,?,?,?,?,?,?,?,?,?,'w','2026-09-30T09:40:00+00:00','impact-v1')",
                  (r["uid"], r["n"], r.get("med"), r.get("p25"), r.get("p75"), json.dumps(r.get("cens", {})),
                   r.get("share"), r.get("n_fill", 0), r.get("k"), r.get("k_needed"), r.get("q"), r["verdict"],
                   r.get("reason")))


def test_a_missing_impact_table_is_no_impact_not_an_error():
    out = _build(_graph([dict(type="follows", src="uA", dst="uB", su="uA", du="uB", n=6)]), _ledger())
    assert out["stamp"]["impact"] is None and out["stamp"]["error"] is None
    assert all(n["impact"] is None for n in out["nodes"])


def test_impact_rows_pass_through_and_strangers_have_none():
    g = _graph([dict(type="slot_follower", src="uA", dst="Stranger1", su="uA", n=4)])
    _impact(g, [dict(uid="uA", n=22, med=1.5, p25=0.2, p75=3.0, share=0.1, k=19, k_needed=17, q=0.001,
                     verdict="mover", cens={"truncated": 2}),
                dict(uid="uB", n=4, verdict="unknown", reason="n")])
    out = _build(g, _ledger())
    nodes = {n["id"]: n for n in out["nodes"]}
    a = nodes["uA"]["impact"]
    assert (a["verdict"], a["median_delta_sol"], a["p25"], a["p75"], a["k_needed"], a["censored"]) == (
        "mover", 1.5, 0.2, 3.0, 17, {"truncated": 2})
    assert nodes["uB"]["impact"]["verdict_reason"] == "n"          # an impact row puts the source on the map
    assert nodes["Stranger1"]["impact"] is None
    assert out["stamp"]["impact"] == {"method_version": "impact-v1", "computed_at": "2026-09-30T09:40:00+00:00",
                                      "window_from": "w", "wallets_known": 1}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:cacheprovider; echo $?`
Expected: exit 1, `KeyError: 'impact'`.

- [ ] **Step 3: Write the implementation.** In `build_wallet_map`:

  1. Add `"impact": None, "impact_error": None` to the initial `stamp` dict.
  2. Add module constants:

```python
_IMPACT_COLS = ("source_user_id", "n", "median_delta_sol", "p25_delta_sol", "p75_delta_sol", "share_positive",
                "median_delta_buyers", "median_undecoded_share", "censored", "censored_share", "n_fill",
                "median_delta_sol_fill", "k", "k_needed", "q_value", "verdict", "verdict_reason",
                "method_version", "computed_at", "window_from")
_IMPACT_RENAME = {"p25_delta_sol": "p25", "p75_delta_sol": "p75"}
```

  3. After the edge loop and the `stamp["coverage"]` line, before
     `ids.update(uid for uid, v in activity.items() if v)`, add:

```python
    # Slice 3 (spec 2026-09-30-memecoin-wallet-map-slice3-impact-design.md): the latest per-wallet
    # flow-change rows. No table yet = the job has not run slice 3: impact None, not an error.
    impact = {}
    try:
        imp_rows = graph.execute(
            "SELECT %s FROM memecoin_wallet_impact WHERE network_id IN (%s) AND computed_at = "
            "(SELECT MAX(computed_at) FROM memecoin_wallet_impact WHERE network_id IN (%s))"
            % (", ".join(_IMPACT_COLS), marks, marks), nets + nets).fetchall()
    except sqlite3.Error as exc:
        imp_rows = None
        if "no such table" not in str(exc):
            stamp["impact_error"] = "impact_unreadable"
    if imp_rows is not None:
        head = None
        for r in imp_rows:
            d = dict(zip(_IMPACT_COLS, r))
            head = head or d
            d["censored"] = _json_or_text(d["censored"])
            impact[d["source_user_id"]] = {_IMPACT_RENAME.get(k, k): v for k, v in d.items()
                                           if k not in ("source_user_id", "method_version", "computed_at",
                                                        "window_from")}
        stamp["impact"] = {"method_version": head and head["method_version"],
                           "computed_at": head and head["computed_at"],
                           "window_from": head and head["window_from"],
                           "wallets_known": sum(1 for v in impact.values() if v["verdict"] != "unknown")}
    ids.update(uid for uid in impact if uid in sources)
```

  4. Add `"impact": None` to the stranger node dict and `"impact": impact.get(nid)` to the source node
     dict.

- [ ] **Step 4: Run the map tests to verify they pass**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:cacheprovider; echo $?`
Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - delete `ids.update(uid for uid in impact if uid in sources)`;
  - change `v["verdict"] != "unknown"` to `True`.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add sniper/services/memecoin_wallet_map.py tests/test_memecoin_wallet_map.py && git commit -q -F - <<'EOF'
feat(wallet-map): the payload carries per-wallet flow change and an impact stamp (slice 3 task 7)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 8: UI (size-by, ring, hover, stamp)

**Files:**
- Modify: `static/wallet_map.js`, `static/styles.css` (next to `.wm-node.wm-unknown`).
- Test: `tests/test_memecoin_wallet_map_ui.py`.

**Interfaces:**
- Consumes: `stamp.impact` and `node.impact` from Task 7.
- Produces, exported on `WalletMapCore`:
  - `defaultSizeBy(stamp) -> 'impact' | 'activity'`;
  - `nodeStyle(n, sizeBy) -> {r, unknown, ring}`;
  - `impactText(imp) -> string`;
  - `SIZE_LABEL`.

- [ ] **Step 1: Write the failing tests** (append):

```python
def test_size_by_defaults_to_flow_change_only_when_a_wallet_is_known():
    got = _core("[null, {impact: null}, {impact: {wallets_known: 0}}, {impact: {wallets_known: 1}}].map(C.defaultSizeBy)")
    assert got == ["activity", "activity", "activity", "impact"]


def test_flow_change_radius_ring_and_unknown():
    st = _core("[{kind:'source', impact:{verdict:'untested', median_delta_sol:-3}},"
               " {kind:'source', impact:{verdict:'untested', median_delta_sol:4}},"
               " {kind:'source', impact:{verdict:'unknown', median_delta_sol:4}},"
               " {kind:'source', impact:{verdict:'mover', median_delta_sol:0}},"
               " {kind:'source', impact:null}].map(n => C.nodeStyle(n, 'impact'))")
    assert st[0]["r"] == 5 and st[0]["unknown"] is False                 # a negative median: base size, known
    assert abs(st[1]["r"] - (5 + 2 * 2.2)) < 1e-9
    assert st[2]["r"] == 5 and st[2]["unknown"] is True and st[4]["unknown"] is True
    assert st[3]["ring"] is True and st[1]["ring"] is False


def test_the_wording_is_flow_change_never_moves_the_market():
    assert _core("C.SIZE_LABEL.impact") == "flow change after vs before"
    assert "moves the market" not in Path("static/wallet_map.js").read_text().lower()


def test_verdict_text_carries_its_bar():
    t = _core("[{verdict:'mover', k:19, n:22, q_value:0.001}, {verdict:'not_distinguished', k:12, n:20, k_needed:16},"
              " {verdict:'untested'}, {verdict:'unknown', verdict_reason:'n'}, {verdict:'unknown', verdict_reason:'censored'},"
              " null].map(C.impactText)")
    assert "19 of 22" in t[0] and "this run only" in t[0]
    assert "16 needed" in t[1] and "not a mover" not in t[1]
    assert t[2] == "n < 20: not tested" and t[3] == "n < 10: unknown"
    assert t[4] == "censored share over 25%: unknown" and "unknown" in t[5]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_memecoin_wallet_map_ui.py -q -p no:cacheprovider; echo $?`
Expected: exit 1, `TypeError: C.defaultSizeBy is not a function` (in the node stderr).

- [ ] **Step 3: Write the implementation.** In `static/wallet_map.js`, replace `nodeStyle` with the code
  below and add the helpers next to it:

```js
  // Slice 3 (spec 2026-09-30-memecoin-wallet-map-slice3-impact-design.md). Descriptive, not causal.
  const IMPACT_R = 2.2;                           // px per sqrt(SOL): the spec's R
  const SIZE_LABEL = { activity: 'activity (distinct coins bought)', impact: 'flow change after vs before' };

  function defaultSizeBy(stamp) {
    return stamp && stamp.impact && stamp.impact.wallets_known > 0 ? 'impact' : 'activity';
  }

  // Unknown is drawn the size of a known 0, hollow, never smaller (Gamma LOW 3). A negative median
  // flow change is known: base size, value in the hover. A mover gets a ring.
  function nodeStyle(n, sizeBy) {
    if (sizeBy === 'impact') {
      const imp = n.impact;
      const known = !!imp && imp.verdict !== 'unknown' && imp.median_delta_sol != null;
      return { r: known ? 5 + Math.sqrt(Math.max(0, imp.median_delta_sol)) * IMPACT_R : 5,
               unknown: !known && n.kind === 'source', ring: !!imp && imp.verdict === 'mover' };
    }
    const known = n.activity != null;
    return { r: known ? 5 + Math.sqrt(n.activity) * 2.2 : 5, unknown: !known && n.kind === 'source', ring: false };
  }

  // The verdict line (Gamma M3, L3): not_distinguished carries the bar it missed; nothing reads as
  // "not a mover"; every tested verdict is this run only.
  function impactText(imp) {
    if (!imp) return 'flow change: unknown (no read)';
    if (imp.verdict === 'unknown') return imp.verdict_reason === 'censored' ? 'censored share over 25%: unknown' : 'n < 10: unknown';
    if (imp.verdict === 'untested') return 'n < 20: not tested';
    if (imp.verdict === 'mover') return `${imp.k} of ${imp.n} above the roster median, BH q ${imp.q_value}, this run only`;
    return `${imp.k} of ${imp.n} above the roster median; ${imp.k_needed} needed at this n, this run only`;
  }
```

In `stampText`, before the closing semicolon of the returned string, append:

```js
      + (st.impact ? ` · flow change: ${st.impact.wallets_known} wallets · 14 d · method ${st.impact.method_version} · computed ${st.impact.computed_at}` : '')
```

Change the exports line to:

```js
  const WalletMapCore = { edgeStyle, edgeTestText, nodeStyle, stampText, filter, defaultSizeBy, impactText,
                          SIZE_LABEL, DEFAULT_KNOBS, STYLE };
```

Renderer changes (browser only):

- Add `sizeChosen: false` to `state`.
- In `tooltipNode`, before the `<code>` line, add `+ (n.kind === 'source' ? impactLines(n) : '')`,
  defined as:

```js
  function impactLines(n) {
    const imp = n.impact;
    if (!imp) return '<br>flow change: unknown (no read)';
    const pct = v => (v == null ? '—' : Math.round(v * 100) + '%');
    const c = Object.entries(imp.censored || {}).map(([k, v]) => `${k} ${v}`).join(', ') || 'none';
    return '<br><b>flow change after vs before</b> (not causal)'
      + (imp.median_delta_sol != null
        ? `<br>median ΔSOL ${imp.median_delta_sol} (IQR ${imp.p25} – ${imp.p75}) · n ${imp.n} · positive ${pct(imp.share_positive)}`
          + ` · Δbuyers ${imp.median_delta_buyers} · undecoded ${pct(imp.median_undecoded_share)}` : '')
      + `<br>censored ${pct(imp.censored_share)} (${esc(c)})`
      + `<br>our fills (selected by our gates): n ${imp.n_fill}`
      + (imp.median_delta_sol_fill != null ? `, median ΔSOL ${imp.median_delta_sol_fill}` : '')
      + `<br>${esc(impactText(imp))}`;
  }
```

- In `draw()`, replace `const size = n => nodeStyle(n).r;` with:

```js
    const sizeBy = state.knobs.sizeBy;
    const size = n => nodeStyle(n, sizeBy).r;
```

  and the node class expression's
  `+ (nodeStyle(d).unknown ? ' wm-unknown' : '')` with
  `+ (nodeStyle(d, sizeBy).unknown ? ' wm-unknown' : '') + (nodeStyle(d, sizeBy).ring ? ' wm-mover' : '')`.

- In `renderControls`, after the Activity window label, add
  `<label class="wm-knob">Size <select id="wm-size"><option value="impact">${esc(SIZE_LABEL.impact)}</option><option value="activity">${esc(SIZE_LABEL.activity)}</option></select></label>`,
  and a listener:

```js
    el.querySelector('#wm-size').addEventListener('change', ev => { state.knobs.sizeBy = ev.target.value; state.sizeChosen = true; redraw(); });
```

- In `load()`, after `state.payload = body;`, add:

```js
      if (!state.sizeChosen) {
        state.knobs.sizeBy = defaultSizeBy(body.stamp);
        const sel = document.getElementById('wm-size');
        if (sel) sel.value = state.knobs.sizeBy;
      }
```

In `static/styles.css`, after `.wm-source.wm-unknown`:

```css
.wm-node.wm-mover { stroke: #f5b041; stroke-width: 3.5; }
```

- [ ] **Step 4: Run the UI tests to verify they pass** (the existing `nodeStyle` test still passes, since
  its index argument is not `'impact'`)

Run: `cd ~/.cache/lambda/wt-map && python3 -m pytest tests/test_memecoin_wallet_map_ui.py -q -p no:cacheprovider; echo $?`
Expected: exit 0.

- [ ] **Step 5: Kill the mutants.** Each must exit 1:
  - `Math.max(0, imp.median_delta_sol)` changed to `Math.abs(imp.median_delta_sol)`;
  - `> 0 ? 'impact'` changed to `>= 0 ? 'impact'`.

- [ ] **Step 6: Commit**

```bash
cd ~/.cache/lambda/wt-map && git add static/wallet_map.js static/styles.css tests/test_memecoin_wallet_map_ui.py && git commit -q -F - <<'EOF'
feat(wallet-map): size by flow change after vs before, mover ring, verdict hover with its bar (slice 3 task 8)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
```

### Task 9: Verification, smoke read and handoff

**Files:** none new. Regenerated index files only if they changed.

- [ ] **Step 1: Run the covering set**

```bash
cd ~/.cache/lambda/wt-map && grep -rl "wallet_link_graph\|memecoin_wallet_map\|wallet_map.js" tests/ | sort | tr '\n' ' ' > /tmp/claude-1000/-home-bigpapi-Desktop-Sniper/b0bb3e4a-e84b-48df-bc17-11926708765f/scratchpad/cover.txt; python3 -m pytest $(cat /tmp/claude-1000/-home-bigpapi-Desktop-Sniper/b0bb3e4a-e84b-48df-bc17-11926708765f/scratchpad/cover.txt) -q -p no:cacheprovider -n 6; echo $?
```

Expected: exit 0. The set is the six graph and map test files; the route test for
`/api/memecoin/wallet-map` lives in `tests/test_memecoin_wallet_map.py`.

- [ ] **Step 2: Run the schema validator and refresh the index**

```bash
cd ~/.cache/lambda/wt-map && python3 scripts/validate_schema_refs.py; echo $?; python3 scripts/brain_index.py generate && git status --short index/
```

Expected: the validator exits 0. If `index/` files changed, commit them by explicit path in their own
commit.

- [ ] **Step 3: Measure the web payload latency.** Rule 5.2, measured p50 and p90, read-only, on the
  production graph DB and ledger, 20 builds:

```bash
cd ~/.cache/lambda/wt-map && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper python3 - <<'EOF'
import time, statistics
from sniper.services.memecoin_wallet_map import build_wallet_map, open_graph_readonly
from sniper.persistence.db_core import get_readonly_connection
ts = []
for _ in range(20):
    g = open_graph_readonly("/home/bigpapi/Desktop/Sniper/data/memecoin_wallet_graph.db")
    l = get_readonly_connection("/home/bigpapi/Desktop/Sniper/sniper_tracker.db")
    t = time.perf_counter()
    build_wallet_map(g, l, chain="solana", network_ids=("solana",), days=14, own_addresses=set(),
                     now_iso="2026-09-30T12:00:00+00:00")
    ts.append((time.perf_counter() - t) * 1000); g.close(); l.close()
ts.sort(); print("p50 %.1f ms p90 %.1f ms" % (statistics.median(ts), ts[17]))
EOF
```

- [ ] **Step 4: Smoke read on a scratch copy** (research key; the Solana book must be flat; the first
  impact read, allowed by Gamma CLEAN). It never writes the production graph DB:

```bash
S=/tmp/claude-1000/-home-bigpapi-Desktop-Sniper/b0bb3e4a-e84b-48df-bc17-11926708765f/scratchpad
cp /home/bigpapi/Desktop/Sniper/data/memecoin_wallet_graph.db $S/graph-smoke.db
cd ~/.cache/lambda/wt-map && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper python3 scripts/research/wallet_link_graph.py run --graph $S/graph-smoke.db --helius-budget 0 --funder-budget 0 --impact-budget 33; echo $?
sqlite3 "file:$S/graph-smoke.db?mode=ro" "SELECT origin, status, COUNT(*), ROUND(AVG(calls),1) FROM memecoin_wallet_impact_reads GROUP BY 1,2; SELECT verdict, COUNT(*) FROM memecoin_wallet_impact GROUP BY 1;"
rm -f $S/graph-smoke.db
```

Expected: exit 0; the printed JSON has `impact_reads_new` of 3 or fewer and `impact_calls` of 33 or
fewer. Every wallet row is `unknown` (n < 10 after 3 reads). Quote the output in the report. If the
book is open, the stop is `solana book open`: report that, and do not retry in a loop.

- [ ] **Step 5: Handoff**
  - Send Gamma the branch head SHA, the task SHAs, the covering-set exit code, the mutant list, the
    p50/p90, and the smoke output.
  - After Gamma CLEAN, send Alpha the SHA with the deploy split: the job goes with the memecoin
    runtime deploy (cron from `.runtime/memecoin/current`), and the map goes with the web deploy.
  - Record the handoff: `python3 scripts/brain_cli.py episode --role lambda --kind handoff_line "<what, SHA>"`.
