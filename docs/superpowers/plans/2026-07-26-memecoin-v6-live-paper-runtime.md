# Memecoin V6 Live-Paper Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open realistic V6 paper fills from accepted live signals, feed production-shaped Solana and 4663 observations to V6, and model V6-directed exits without signing or broadcasting.

**Architecture:** A separate default-OFF worker reads accepted signals from the existing memecoin ledger through a read-only connection, writes only the dedicated V6 paper database, and owns entry/decision/exit processing. Chain adapters return quote or observation facts only; only `memecoin_v6_engine.step()` chooses post-fill actions.

**Tech Stack:** Python 3, SQLite read-only/read-write split, existing Jupiter/EVM quote seams, Solana tape and EVM tick/state capture, pytest, shell watchdog.

## Global Constraints

- Complete and review `2026-07-26-memecoin-v6-live-paper-foundation.md` first.
- Read `docs/superpowers/specs/2026-07-26-memecoin-v6-live-paper-engine-design.md` first.
- Work in an isolated worktree created with `superpowers:using-git-worktrees`.
- Use RED-first TDD and one logical commit per task.
- Default OFF under `MEMECOIN_V6_PAPER_ENABLED=0`.
- Source memecoin and tape/state databases are read-only; only `memecoin_v6_paper.sqlite` is writable.
- No signing, approval, broadcast, reservation, balance, funded exposure, or production-policy mutation is reachable.
- Entries require fresh executable route quotes; no source-price, DexScreener, GeckoTerminal, or chart-mid fallback.
- V6 is never called before entry fill persistence commits.
- Each `DERISK`/`EXIT_FULL` uses a fresh executable sell quote for the exact atomic quantity.
- Quote failure records `action_unfilled`; there is no fallback close.
- Do not run live quote, RPC, worker, watchdog, or service activation commands without new explicit operator approval.

## Locked Runtime Interfaces

```python
@dataclass(frozen=True)
class AcceptedSignal:
    source_event_id: int
    origin_event_id: str
    network_id: str
    mint: str
    ticker: str | None
    source_user_id: str
    source_handle: str | None
    signal_source: str
    signal_at: str
    received_at: str
    clock_domain: str
    their_price: Decimal | None
    their_market_cap: Decimal | None
    their_size_usd: Decimal | None


@dataclass(frozen=True)
class ModeledLiveFill:
    network_id: str
    mint: str
    side: str
    token_qty_atomic: int
    token_decimals: int
    quote_in_atomic: int
    quote_in_asset: str
    quote_in_decimals: int
    quote_out_atomic: int
    quote_out_asset: str
    quote_out_decimals: int
    unit_price: str
    observed_at: str
    quote_provider: str
    route_binding: Mapping[str, object]
    quote_payload: Mapping[str, object]
    quote_fingerprint: str
    costs: Mapping[str, object]
    net_pnl_computable: bool
    fill_kind: str = "modeled_live_quote"


@dataclass(frozen=True)
class CycleResult:
    signals_seen: int
    entries_opened: int
    positions_advanced: int
    holds: int
    fills_applied: int
    actions_unfilled: int
    errors: tuple[str, ...]
    source_cursor: int


def quote_entry(signal: AcceptedSignal, notional_usd: Decimal, *, http=None) -> ModeledLiveFill: ...
def quote_exit(position: Mapping[str, object], qty_atomic: int, *, http=None) -> ModeledLiveFill: ...
def accepted_signals(conn: sqlite3.Connection, after_id: int, limit: int) -> list[AcceptedSignal]: ...
def build_observation(position: Mapping[str, object], *, now_iso: str,
                      tape_conn: sqlite3.Connection,
                      state_conn: sqlite3.Connection,
                      quote_fn=quote_exit) -> V6Observation: ...
def run_cycle(source_conn: sqlite3.Connection, v6_conn: sqlite3.Connection, *,
              now_fn=real_utc_now_iso, entry_quote_fn=quote_entry,
              exit_quote_fn=quote_exit, observation_fn=build_observation) -> CycleResult: ...
```

## File Map

- Create `sniper/services/memecoin_v6_paper_quotes.py`: quote-only modeled fills.
- Modify `sniper/execution/evm_v4_quote.py`: pure V4 amount-out seam.
- Create `sniper/services/memecoin_v6_paper_signals.py`: read-only accepted-signal query.
- Create `sniper/services/memecoin_v6_observations.py`: frozen point-in-time observations.
- Modify the Solana tape target seam to accept V6-held routes when enabled; 4663 remains on its existing unfiltered whole-chain tape.
- Create `sniper/services/memecoin_v6_paper_runner.py`: one-cycle lifecycle orchestration.
- Create `scripts/memecoin_v6_paper_worker.py` and `scripts/memecoin_v6_paper_watchdog.sh`.

---

### Task 1: Build Quote-Only Solana and 4663 Fill Adapters

**Files:**
- Create: `sniper/services/memecoin_v6_paper_quotes.py`
- Modify: `sniper/execution/evm_v4_quote.py`
- Test: `tests/test_memecoin_v6_paper_quotes.py`

**Interfaces:**
- Consumes: `jupiter_entry_quote`, `jupiter_exit_quote`, `token_decimals`, `usd_to_quote_atomic`, current V2/V3/V4 quote seams, `CHAIN_QUOTE`, `erc20_decimals`, and `quote_usd_price`.
- Produces: `ModeledLiveFill`, `QuoteUnavailable`, `QuoteStale`, `quote_entry`, and `quote_exit`.

- [ ] **Step 1: Write failing quote exactness and safety tests**

```python
def test_entry_has_no_price_fallback():
    with pytest.raises(QuoteUnavailable):
        quote_entry(sol_signal(), Decimal("25"),
                    jupiter_entry_fn=lambda *a, **k: None)


def test_exit_quotes_exact_requested_atomic_quantity():
    seen = []
    fill = quote_exit(sol_position(), 412_345,
                      jupiter_exit_fn=lambda mint, amount, pct, **kw:
                          seen.append(amount) or sol_quote(amount))
    assert seen == [412_345]
    assert fill.token_qty_atomic == 412_345


def test_4663_quote_never_loads_account_or_builds_transaction(monkeypatch):
    monkeypatch.setattr("sniper.execution.evm_wallet.load_account",
                        lambda *a, **k: pytest.fail("account loaded"))
    fill = quote_entry(evm_signal(), Decimal("25"), evm_quote_fn=fake_amount_out)
    assert "calldata" not in fill.quote_payload
    assert "permit" not in fill.quote_payload
```

Also test corrected-aware timestamps, staleness rejection, Solana decimals, 4663 WETH/USD conversion, positive atomic amounts, route binding, stable fingerprints, and the fixed fill label.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_quotes.py -q
```

- [ ] **Step 3: Extract the pure V4 amount-out seam**

```python
def v4_quote_amount_out(chain, sell_token, buy_token, amount_atomic, *,
                        rpc_url=None, http=None, gt_http=None):
    """Return quote evidence only; never require an account, permit, or signature."""
```

Reuse current pool discovery and V4Quoter `eth_call`. Make existing `dex_v4_quote()` call this helper before its funded transaction branch. A test must make every signing/permit helper fail while the pure quote still succeeds.

- [ ] **Step 4: Normalize and strip quote evidence**

Use decimal arithmetic, exact atomic amounts, and one `_normalize_fill()` helper. Persist only provider, quote id, router/quoter, pool/vault, token in/out, fee tier, protocol version, block/slot, amounts, and observed time. Reject route absence, stale quotes, unresolved decimals, or non-positive outputs.

- [ ] **Step 5: Implement both chains**

Solana entry uses `jupiter_entry_quote`; Solana exit uses `jupiter_exit_quote` for exact quantity. 4663 entry converts USD to chain quote-token atomic units, evaluates current verified V3/V2/pure-V4 routes, and chooses the best valid amount-out. 4663 exit sells exact token atomic quantity to `CHAIN_QUOTE[4663]`. A fixed nonzero paper-only address may encode quote calls; no key/account may load.

- [ ] **Step 6: Add behavioral and source guards**

Monkeypatch signing, approval, send, reservation, and real-position functions to fail. Also reject these strings in the new module source: `load_account`, `private_key`, `approve`, `sign_`, `send_raw_transaction`, `broadcast`, `reserve_entry`, `open_real_position`.

- [ ] **Step 7: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_quotes.py -q
python3 -m pytest $(rg --files tests | rg 'memecoin.*v4.*quote' | tr '\n' ' ') -q
git add sniper/services/memecoin_v6_paper_quotes.py sniper/execution/evm_v4_quote.py tests/test_memecoin_v6_paper_quotes.py
git commit -m "feat: add quote-only v6 paper fills"
```

If no existing V4 quote test file is found, record that and omit only the second pytest command.

### Task 2: Read Accepted Signals Without a Funded-Runner Hook

**Files:**
- Create: `sniper/services/memecoin_v6_paper_signals.py`
- Test: `tests/test_memecoin_v6_paper_signals.py`

**Interfaces:**
- Consumes: `memecoin_events` and `memecoin_wallets` through SQLite `mode=ro`.
- Produces: immutable `AcceptedSignal` and `accepted_signals(conn, after_id, limit)`.

- [ ] **Step 1: Write failing eligibility tests**

Assert the query applies exactly:

```sql
e.id > :after_id
AND e.side = 'buy'
AND e.action = 'opened'
AND lower(trim(e.network_id)) IN ('solana','4663')
AND w.paused = 0
AND w.excluded = 0
```

Join on source user id. Exclude sells, candidate/stale/skipped/ignored rows, unsupported/null chains, paused/excluded wallets, and cursor-bound rows. Return ascending event id with a bounded limit.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_signals.py -q
```

- [ ] **Step 3: Implement immutable signal mapping**

Carry source event/fomo ids, normalized network, mint/ticker, source user/handle, signal source, their price/mcap/size as context only, corrected signal/received times, and clock domain. Context prices never enter quote fallback.

- [ ] **Step 4: Prove read-only behavior**

Open the fixture with `mode=ro`, call `accepted_signals`, and assert an insert fails. Do not modify `memecoin_runner.run_once`: polling committed `action='opened'` avoids funded-entry latency and preserves runner at-least-once behavior.

- [ ] **Step 5: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_signals.py -q
git add sniper/services/memecoin_v6_paper_signals.py tests/test_memecoin_v6_paper_signals.py
git commit -m "feat: read accepted signals for v6 paper"
```

### Task 3: Materialize Point-in-Time Observations and Capture Targets

**Files:**
- Create: `sniper/services/memecoin_v6_observations.py`
- Modify: `sniper/services/memecoin_solana_tape.py`
- Modify: `scripts/memecoin_sol_tape_worker.py`
- Test: `tests/test_memecoin_v6_observations.py`
- Test: `tests/test_memecoin_sol_tape_worker.py`

**Interfaces:**
- Consumes: open V6 positions, entry route binding, existing tape/state readers, `quote_exit`, and `make_observation`.
- Produces: `build_observation(...)` and `v6_capture_positions(v6_conn, network_id)`.

- [ ] **Step 1: Write failing point-in-time tests**

Assert the payload freezes whole-bag executable quote/proceeds/freshness; remaining quantity/cost/realized PnL; executable high-water; held time/drawdown; break/prior-low; volatility/chain p90; buy/sell counts, arrival rate, buy share, net flow; route/sellability; LP/depth; tape completeness/provenance; source-wallet state/tier; network/block/capture version. No row newer than `now_iso` may be selected. Missing facts remain `None` and appear in `missing_fields`.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_observations.py -q
```

- [ ] **Step 3: Implement a chain-neutral payload builder**

Chain adapters return facts only. `build_observation()` merges them, records provenance, calls the whole-bag exit quote once, and fingerprints the canonical payload. It must not import V5, `exit_engine`, `memecoin_real_runner`, or legacy bracket functions.

- [ ] **Step 4: Inject V6-held capture targets**

Add `extra_positions=()` to `solana_tape_pass(...)`. When V6 paper is enabled, `scripts/memecoin_sol_tape_worker.py` opens the V6 DB with `connect_existing()` and adds exact Solana mint/vault identities. Disabled or missing-V6-DB behavior stays byte-identical and the V6 connection remains read-only. Do not add a 4663 target filter: `memecoin_tick_stream.py` already ingests chain 4663 unfiltered, so V6 observations read that shared tape by exact route/pool identity.

- [ ] **Step 5: Prove missing capture is engine evidence**

Pass an incomplete observation to `step()` and assert `HOLD` with `data_insufficient:*`. The observation adapter itself must never choose an action.

- [ ] **Step 6: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_observations.py tests/test_memecoin_sol_tape_worker.py -q
git add sniper/services/memecoin_v6_observations.py sniper/services/memecoin_solana_tape.py scripts/memecoin_sol_tape_worker.py tests/test_memecoin_v6_observations.py tests/test_memecoin_sol_tape_worker.py
git commit -m "feat: feed live chain observations to v6 paper"
```

Also run `tests/test_memecoin_tick_stream.py` to preserve the existing unfiltered-4663 contract.

### Task 4: Orchestrate Entry, Decision, and Modeled Exit Lifecycles

**Files:**
- Create: `sniper/services/memecoin_v6_paper_runner.py`
- Test: `tests/test_memecoin_v6_paper_runner.py`

**Interfaces:**
- Consumes: all foundation and Runtime Tasks 1-3 interfaces.
- Produces: immutable `CycleResult` and `run_cycle(...)`.

- [ ] **Step 1: Write failing lifecycle and order tests**

Assert: signal read; entry quote; entry fill commit; only then `step`; HOLD calls `apply_hold_decision` and has no fill; DERISK quotes/applies exact quantity; EXIT_FULL closes remainder; restart resolves `unapplied_decisions()` before creating another observation/decision; redelivery duplicates nothing. Use call-order spies and assert `step` is absent when the entry quote fails.

- [ ] **Step 2: Write simultaneous-chain and unfilled tests**

Open two Solana plus two 4663 positions under cap four, interleave observations, force one unfilled action, leave one decision unapplied, reconnect, resolve the pending decision first, and assert independent progress. The unfilled position remains open with unchanged inventory and `state_if_unfilled`.

- [ ] **Step 3: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_runner.py -q
```

- [ ] **Step 4: Implement entry processing**

Process accepted signals in ascending id. Enforce the independent capacity cap before quoting. Record quote/cap outcomes and advance the source cursor only after that outcome commits. Freeze policy document/hash into successful entry fills. Capacity and quote failure are entry-adapter outcomes, not V6 decisions.

- [ ] **Step 5: Implement post-fill processing**

At cycle start, drain `unapplied_decisions()` before observing any affected position. Append observation before decision and decision before action. For HOLD, call `apply_hold_decision`. For DERISK/EXIT_FULL, request the exact action quantity. `QuoteUnavailable`/`QuoteStale` call `record_action_unfilled`; a valid quote calls `apply_action_fill`. No branch may invoke another policy or synthetic close.

- [ ] **Step 6: Add fault isolation**

One expected quote failure must not block other positions. An invariant/schema error fails loudly and cannot advance that input's cursor. Test both cases.

- [ ] **Step 7: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_runner.py -q
git add sniper/services/memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_runner.py
git commit -m "feat: run v6 live-paper lifecycles"
```

### Task 5: Add the Default-OFF Worker and Watchdog

**Files:**
- Create: `scripts/memecoin_v6_paper_worker.py`
- Create: `scripts/memecoin_v6_paper_watchdog.sh`
- Test: `tests/test_memecoin_v6_paper_worker.py`

**Interfaces:**
- Consumes: settings, read-only source/tape/state connections, writable V6 connection, and `run_cycle`.
- Produces: `validate_startup`, `main`, `--once`, `--smoke-no-rpc`, and isolated watchdog behavior.

- [ ] **Step 1: Write failing startup tests**

Default OFF must exit before opening DB/network adapters. Enabled startup rejects a V6 path equal to production/tape paths, unsupported networks, invalid cap/interval, policy-hash mismatch on an open book, missing source DB, or unavailable required adapter. `--smoke-no-rpc` must use fixtures only.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_worker.py -q
```

- [ ] **Step 3: Implement validation and loop**

Resolve absolute paths, open source/tape/state in read-only mode, validate V6 schema/current policy hash, and log `V6 PAPER — NO FUNDS`. `--once` runs one cycle. Continuous mode emits structured health and waits interruptibly for the configured interval. SIGTERM/SIGINT exit cleanly.

- [ ] **Step 4: Implement isolated watchdog**

Use unique `memecoin_v6_paper` lock, pid, and log names. Never restart the funded runner. Do not add this worker to any deployed/pinned runtime manifest in this task.

- [ ] **Step 5: Prove funded surfaces are unreachable**

Monkeypatch reservation, real-position open, approval, signing, and broadcast functions to fail. Run a fixture cycle on both chains and assert none fire. Add a forbidden-import source guard for funded runner/executor modules.

- [ ] **Step 6: Run runtime verification and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_quotes.py tests/test_memecoin_v6_paper_signals.py tests/test_memecoin_v6_observations.py tests/test_memecoin_v6_paper_runner.py tests/test_memecoin_v6_paper_worker.py -q
python3 scripts/memecoin_v6_paper_worker.py --smoke-no-rpc --once
python3 -m compileall -q sniper/services/memecoin_v6_paper_quotes.py sniper/services/memecoin_v6_paper_signals.py sniper/services/memecoin_v6_observations.py sniper/services/memecoin_v6_paper_runner.py scripts/memecoin_v6_paper_worker.py
git diff --check
git add scripts/memecoin_v6_paper_worker.py scripts/memecoin_v6_paper_watchdog.sh tests/test_memecoin_v6_paper_worker.py
git commit -m "feat: add isolated v6 paper worker"
```

Do not run an enabled live smoke.

## Runtime Review Gate

- Accepted signals are read after upstream `action='opened'`; funded entry latency is unchanged.
- Source/tape/state connections are read-only; only the V6 book is writable.
- Quote adapters cannot sign, approve, broadcast, reserve, or load funded keys.
- Both chains use fresh executable quotes with exact atomic quantities.
- Missing quotes persist unfilled actions and open inventory.
- Multiple positions and restart idempotency pass.
- No live worker or RPC command was run.
