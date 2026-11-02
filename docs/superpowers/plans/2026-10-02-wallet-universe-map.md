# Wallet Universe Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show authoritative roster state and chain-specific supporting evidence, and make Map actions verifiable on Armed.

**Architecture:** Repair the existing funnel overlay; extend the existing offline producer with a versioned per-chain projection. The browser renders a focused neighborhood and shared-coin matrix, rather than an all-wallet force graph. Existing authority endpoints remain the only money writers.

**Tech Stack:** Flask, SQLite read-only handles, Python JSON materialization, existing JavaScript/CSS and test harnesses.

**Spec:** `docs/superpowers/specs/2026-10-02-wallet-universe-design.md`.

## Global Constraints

- No extra synchronous call/lock/retry is allowed on the entry path.
- No DexScreener prices may enter fill evidence.
- Unknown stays unknown. No automatic arming or filter exemptions.
- No new dependency, execution engine, production DB mutation or service activation.
- Source proof, configured authority, effective authority, subscription acknowledgement and last observation are separate fields.
- Reuse `db_memecoin.set_wallet_chain_armed`; preserve other chains and existing pause/exclusion semantics.

## Review Focus

1. Wallet absent from every saved list remains visible — Task 1.
2. Pause/exclusion and stale authority cannot show effective funded status — Task 1.
3. Missing/corrupt snapshot or overlay failure disables uncertain money controls — Tasks 1/3.
4. Alias/cross-chain edge data never leak across selected chains — Task 2.
5. Timeout after an arm POST requires readback, never another money POST — Task 3.

---

## File responsibilities

- `sniper/services/memecoin_wallet_map.py`: existing overlay plus pure chain projection; reuse graph readers and identity rules.
- `sniper/routes/api_memecoin.py`: bounded file reads/live overlay and universe GET; retain existing authority endpoints.
- `scripts/research/wallet_follow_funnel.py`: offline build and atomic publication beside existing funnel; no new daemon.
- `static/wallet_map.js`, `static/app.js`, `templates/index.html`, `static/styles.css`: focused evidence UI, source badges and Armed navigation.
- Existing Map/action/funnel/UI tests plus `tests/test_wallet_universe.py`: behavioral checks.

### Task 1: Live roster truth independent of cached membership

**Files:** Modify `sniper/services/memecoin_wallet_map.py:funnel_live_overlay`, `sniper/routes/api_memecoin.py:wallet_map_funnel`; test `tests/test_wallet_map_actions.py`, `tests/test_wallet_follow_funnel.py`.

**Interfaces:** Consumes `funnel_live_overlay(body, conn, cfg, now_iso) -> dict`, `m.wallet_armed_chains(conn, uid) -> list[str]`, `m.real_approved_user_ids(conn, network_id=chain) -> set[str]`. Produces the same funnel response with additive row fields `effective_chains: list[str]`, `measurement_state: str` and stamp `authority_state: str`; existing configured `armed` remains a list. States: authority `ok|unavailable`; measurement `ok|not_generated|unreadable`.

- [x] **Step 1: Add these red regressions to the existing isolated client fixture.**

```python
def test_missing_file_still_shows_live_arm(client):
    cl, conn, token, tmp_path = client
    m.add_wallet(conn, 'new-live', 'new', '')
    m.set_wallet_chain_armed(conn, 'new-live', 'robinhood', True, force=True)
    body = cl.get('/api/memecoin/wallet-map/funnel').get_json()
    row = next(r for r in body['funnel'] if r['uid'] == 'new-live')
    assert row['armed'] == ['4663']
    assert row['effective_chains'] == ['4663']
    assert row['paper_ret'] is None and row['measurement_state'] == 'not_generated'
    assert body['stamp']['authority_state'] == 'ok'
```

Add variants with an empty valid file, corrupt JSON, overlay read exception, excluded wallet and paused leftover configured arm. Force a leftover paused configuration only in the fixture; assert configured chains visible but effective chains empty and stage not `armed`/`trial`. An overlay exception must retain measurement diagnostics while marking authority unavailable, without asserting cached authority is current.

- [x] **Step 2: Run the new test red.** `python3 -m pytest tests/test_wallet_map_actions.py::test_missing_file_still_shows_live_arm -q`; expected missing `funnel`/row. Run the audit checker with `--require-live-overlay` and retain its failing assertion.
- [x] **Step 3: Repair the shared overlay and route.** Start with an empty body on file read failure, retain its typed measurement stamp, and always attempt a live roster read. Use a request-owned read-only connection in production; the existing injected fixture connection is supported without closing it. Include every non-excluded live roster uid exactly once after saved rows and candidate promotion. For new rows use null measurement counts/returns, not zero. Derive effective membership from the same `real_approved_user_ids` predicate as Armed and use it for stage selection; paused status dominates an open trial. Mark proof-read failure unavailable rather than “no proof.” Core insertion:

```python
for uid, wallet in roster.items():
    if wallet['excluded'] or uid in seen:
        continue
    promote(uid, wallet['handle'], None)
    rows[-1].update(paper_n=None, gated_n=None, funded_n=None,
                    measurement_state='not_generated')
body.setdefault('stamp', {})['authority_state'] = 'ok'
```

Extend existing-row construction with `effective_chains`; do not mutate the DB. On overlay failure stamp `authority_state='unavailable'`; disable UI actions in Task 3. Update the older route fixture expectation: missing measurement file may coexist with successful live authority.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_map_actions.py tests/test_wallet_follow_funnel.py tests/test_memecoin_wallet_map.py -q` and `python3 docs/audits/2026-10-02-check-wallet-map.py --require-live-overlay`. All must pass.
- [x] **Step 5: Commit.** `git add sniper/services/memecoin_wallet_map.py sniper/routes/api_memecoin.py tests/test_wallet_map_actions.py tests/test_wallet_follow_funnel.py && git commit -m 'fix(wallet-map): overlay complete live roster authority'`.

### Task 2: Offline per-chain evidence projection

**Files:** Modify `sniper/services/memecoin_wallet_map.py`, `scripts/research/wallet_follow_funnel.py`, `sniper/routes/api_memecoin.py`; create `tests/test_wallet_universe.py`.

**Interfaces:** Produces `universe_projection(*, chain: str, wallets: list[dict], edges: list[dict], generated_at: str, graph_state: str) -> dict` in `memecoin_wallet_map.py`. Input wallets have `uid`, `chain`, `coverage`, `paper`, `funded`, `strict_policy`, `setups`; edge facts have `chain`, `src_uid`, `dst_uid`, `kind`, `coins`, `coverage`, `provenance`. Output `{version: 1, chain, generated_at, graph_state, wallets, edges}`. `graph_state` is `ok|not_collected|unavailable|partial`. Produces `publish_universe(path: Path, body: dict) -> None` in the existing producer and GET `/api/memecoin/wallet-map/universe?chain=<canonical-id>`. Each wallet coverage field is independently `{state, value, as_of, source}`; unknown has null value. Chain projection is additive to existing funnel format.

- [x] **Step 1: Write the pure projection and atomic publication tests.**

```python
def test_projection_never_pools_chains():
    from sniper.services.memecoin_wallet_map import universe_projection
    out = universe_projection(chain='robinhood', generated_at='2026-10-02T00:00:00+00:00',
        wallets=[{'uid': 'A', 'chain': '4663'}, {'uid': 'A', 'chain': 'solana'}],
        edges=[{'chain': 'solana', 'src_uid': 'A', 'dst_uid': 'B', 'kind': 'slot_follower'}],
        graph_state='not_collected')
    assert out['chain'] == '4663'
    assert len(out['wallets']) == 1 and out['edges'] == []
    assert out['graph_state'] == 'not_collected'
```

Add tests for case-sensitive Solana identities, aliases, duplicate wallet-chain rows, unknown versus known zero, undecoded/truncated graph coverage, shared-funder/co-buy direction `association` versus chronological `ordered`, and publication failure preserving prior JSON. Mock `os.replace` to fail and assert the old file content is unchanged. GET unknown chain returns 400; missing/corrupt universe file returns typed gaps while live authority still comes from Task 1.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_universe.py -q`; expected new interface import failure.
- [x] **Step 3: Build the projection from existing offline facts.** Reuse `wallet_follow_candidates.paper_and_funded`, `gated_view`, existing graph readers, proof helpers and strict-selector readouts. Keep survivor summaries explicitly separate from strict cohorts and relabel gated data `partial feasibility`. Union roster, proven candidates and identified measured neighbors; do not invent wallet ownership. Record each source's clock and coverage; independent DB reads are not an atomic cross-DB snapshot. Include exact supporting coin/event evidence or a typed missing-provenance reason. Derive chain choices from `memecoin_chains`, never a new fixed list. Projection core:

```python
from sniper.services.memecoin_chains import canonical_chain_id
chain = canonical_chain_id(chain)
if chain is None:
    raise ValueError('unknown chain')
selected = [w for w in wallets if canonical_chain_id(w['chain']) == chain]
links = [e for e in edges if canonical_chain_id(e['chain']) == chain]
return dict(version=1, chain=chain, generated_at=generated_at,
            graph_state=graph_state, wallets=selected, edges=links)
```

Publish a bounded JSON file per chain from the existing offline command, not on web requests. Same-directory temp file, `json.dump(..., allow_nan=False)`, flush/fsync, `os.replace`, cleanup temp on error. Web loads that file and overlays only live authority/proof reads with owned read-only handles; it does not scan graph/history or call providers. No observed heartbeat means subscription `unknown`, even with proof. Maintain backwards readability of the existing funnel file.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_universe.py tests/test_wallet_follow_funnel.py tests/test_wallet_follow_candidates.py tests/test_wallet_follow_gate_matched.py tests/test_memecoin_wallet_map.py -q`. Generate a snapshot against fixture DBs, assert no writes to input DBs and no provider calls.
- [x] **Step 5: Commit.** `git add sniper/services/memecoin_wallet_map.py scripts/research/wallet_follow_funnel.py sniper/routes/api_memecoin.py tests/test_wallet_universe.py && git commit -m 'feat(wallet-map): materialize chain-specific wallet evidence'`.

### Task 3: Focused universe, coverage and action readback

**Files:** Modify `static/wallet_map.js`, `static/app.js`, `templates/index.html`, `static/styles.css`; test `tests/test_wallet_map_actions.py`, `tests/test_memecoin_wallet_map_ui.py`, `tests/test_memecoin_ui_render_runtime.py`.

**Interfaces:** Consumes Task 2 universe response and existing `runSteps(act, send, ask)`. Produces exported pure `armedUrl(chain: string) -> string`, `sharedCoinRows(edges: Array, ids: Array<string>) -> Array`, `authorityAfterReadback(uid: string, chain: string, body: Object) -> string` in `WalletMapCore`; readback result `armed|watch|unavailable`. URL `/?mc_view=wallets&mc_chain=<canonical-id>`; reuse main navigation for the Memecoin section. Update `_writeOperatorLocation` to preserve `_armedChain` when workspace is wallets rather than deleting `mc_chain`.

- [x] **Step 1: Add helper and browser regressions.**

```python
def test_armed_link_keeps_the_selected_chain():
    assert _actions("C.armedUrl('4663')") == '/?mc_view=wallets&mc_chain=4663'

def test_failed_authority_read_is_not_watch_only():
    assert _actions("C.authorityAfterReadback('A','4663',{stamp:{authority_state:'unavailable'}})") == 'unavailable'
```

Using the existing Playwright routed fixture, simulate a successful server arm followed by a network rejection of its response; assert one money POST, a GET readback, visible confirmed/unknown status, unchanged selected chain and working Armed link. Test denied POST, single explicit CSRF refresh, corrupted cached authority, malicious handles/evidence escaping and keyboard-only selection. Matrix test uses two wallets sharing one mint over several events and asserts distinct-coin count one, not event count.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_map_actions.py::test_armed_link_keeps_the_selected_chain tests/test_wallet_map_actions.py::test_failed_authority_read_is_not_watch_only -q`.
- [x] **Step 3: Render the decision view using existing UI patterns.** Searchable chain-local wallet list; selected neighborhood with ordered/association labels; expandable evidence and explicit omitted counts; shared-coin matrix; separate authority/proof/subscription/last-buy/paper badges. No new graph library. Replace the Armed J7-only coverage claim with actual source evidence or unknown. Money controls require live authority/proof status; retain existing preflight/confirmation. On ambiguous POST, read once and display the result, never replay. Pure helper core:

```javascript
function armedUrl(chain) {
  return '/?mc_view=wallets&mc_chain=' + encodeURIComponent(chain);
}
function authorityAfterReadback(uid, chain, body) {
  if (body?.stamp?.authority_state !== 'ok') return 'unavailable';
  const row = (body.funnel || []).find(r => r.uid === uid);
  if (!row) return 'unavailable';
  return (row.effective_chains || []).includes(chain) ? 'armed' : 'watch';
}
```

A confirmed configured-but-paused result remains a visible configured/effective mismatch. On chain change, ignore late responses for the previous chain. Render missing graph as `not collected`/`unavailable`, not “independent.”

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_map_actions.py tests/test_memecoin_wallet_map_ui.py tests/test_memecoin_ui_render_runtime.py -q`; `node --check static/wallet_map.js`; `node --check static/app.js`. Browser fixtures cover Map → exact-chain Armed, stale/error states, neighborhood and matrix. No real arm occurs.
- [x] **Step 5: Commit.** `git add static/wallet_map.js static/app.js templates/index.html static/styles.css tests/test_wallet_map_actions.py tests/test_memecoin_wallet_map_ui.py tests/test_memecoin_ui_render_runtime.py && git commit -m 'feat(wallet-map): show focused evidence and verify arm actions'`.

## Rollback and limits

Revert UI/projection commits without changing authority. Existing funnel format stays readable; unavailable versioned fields are labeled. No historical reconstruction, organic fill proof, causal edges or new EVM graph collection. Review Focus is pinned by Tasks 1–3; the approved spec's A/B requirements are covered.

## Completion rulings, 2026-10-02

The eight code tasks are delivered with review corrections. Named example tests were adapted to actual repository interfaces; the final focused behavioral union passed 389 tests. Source clocks and paced completion clocks are now explicit. Frozen policies are scoped to the selected setup/window and ambiguous identities disable hypothesis export. Current proof helpers include verified watch-only Pump profiles. Seven existing chain ledgers materialized to scratch without production writes.

Provider isolation is an activation prerequisite, not a completed live test: configured host separation and explicit capacity declaration are checked, but different URLs alone cannot prove independent provider/key/IP capacity. No live provider smoke, service activation, continuous-rail policy verdict, new cash/native route or funded latency improvement is claimed. Actual scratch snapshot size required a shared 16 MiB producer/reader bound.
