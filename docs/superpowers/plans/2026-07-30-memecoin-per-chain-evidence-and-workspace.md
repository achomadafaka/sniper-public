# Memecoin per-chain evidence and workspace — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the unusable paper ledger, make funded arming per-chain and fail-closed, and replace the Paper/Wallets/Theses tabs with one Chains workspace where every chain is measured on its own.

**Architecture:** Three sequenced phases against `sniper_tracker.db`. Phase 1 is a one-shot destructive migration run inside a stopped-runner window. Phase 3 turns `memecoin_wallets.real_chains` from a vestigial always-NULL column into the funded authority list, reversing the 2026-07-15 global-arming decision at the single shared reader `real_approved_user_ids`. Phase 2 is read-only: a canonical chain resolver, three new `chain` query parameters, and a nav restructure in `templates/index.html` + `static/app.js`.

**Tech Stack:** Python 3.13, SQLite (WAL), Flask blueprints in `sniper/routes/`, vanilla JS in `static/app.js`, pytest.

**Spec:** `docs/superpowers/specs/2026-07-30-memecoin-per-chain-evidence-and-workspace-design.md`

## Global Constraints

- **Sequence is 1 → 3 → 2.** Phase 3 must land while `kill_tripped=true` and real open positions are zero. Do not reorder.
- **Never pool chains.** No endpoint, view, aggregate, or default may compute a cross-chain number. There is no `all` scope in Phase 2 surfaces.
- **`real_chains` NULL or empty means armed nowhere.** Never read as "all chains". Fail closed.
- **Never reset `sqlite_sequence` for `memecoin_positions`.** `data/memecoin_v6_paper.sqlite` holds `v6_paper_cursors.source_position = 23728` tailing `memecoin_positions.id`; id reuse would silently stop V6 admission forever. Current `seq = 23867`.
- **Real positions are never touched.** Every delete is scoped `execution_mode='paper'`.
- **`memecoin_wallets` and `memecoin_events` survive Phase 1.** They are the roster and the signal feed.
- **Ranking is always fomo-excluded.** Use `HONEST_COPY_SQL`; never rank on raw `realized_pnl`.
- Timestamps: `datetime.now(timezone.utc)` / `real_utc_now_iso()`. Never `utcnow()`.
- All DB access goes through `db.py` / `sniper/persistence/db_memecoin.py`. No raw SQL in routes or JS.
- Run `python3 scripts/validate_schema_refs.py` before every commit that names columns.

---

# PHASE 1 — Evidence reset

### Task 1: Purge script with post-condition asserts

**Files:**
- Create: `scripts/purge_memecoin_paper_ledger.py`
- Test: `tests/test_memecoin_paper_purge.py`

**Interfaces:**
- Produces: `purge_paper_ledger(conn, *, dry_run=False) -> dict` returning
  `{"positions": int, "fills": int, "snapshots": int, "real_kept": int, "wallets_kept": int, "seq_before": int, "seq_after": int}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_paper_purge.py
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m
from scripts.purge_memecoin_paper_ledger import purge_paper_ledger


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed(c):
    c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved) VALUES('u1','h1',1)")
    for pid, mode in ((1, "paper"), (2, "paper"), (3, "real")):
        c.execute(
            "INSERT INTO memecoin_positions(id,source_user_id,mint,network_id,execution_mode,is_open,strategy)"
            " VALUES(?,'u1','mint','solana',?,0,'control')", (pid, mode))
        c.execute("INSERT INTO memecoin_fills(position_id,fill_source,kind) VALUES(?,'dexscreener','buy')", (pid,))
    c.commit()


def test_purge_deletes_paper_keeps_real_and_roster():
    c = _conn()
    _seed(c)
    out = purge_paper_ledger(c)
    assert out["positions"] == 2
    assert out["fills"] == 2
    modes = [r[0] for r in c.execute("SELECT execution_mode FROM memecoin_positions")]
    assert modes == ["real"]
    assert c.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 1


def test_purge_preserves_autoincrement_sequence():
    """Resetting sqlite_sequence would restart ids below the V6 cursor and kill admission."""
    c = _conn()
    _seed(c)
    before = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()[0]
    out = purge_paper_ledger(c)
    after = c.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()[0]
    assert after == before == out["seq_after"]


def test_dry_run_deletes_nothing():
    c = _conn()
    _seed(c)
    out = purge_paper_ledger(c, dry_run=True)
    assert out["positions"] == 2
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 3


def test_purge_refuses_when_a_real_position_is_open():
    c = _conn()
    _seed(c)
    c.execute("UPDATE memecoin_positions SET is_open=1 WHERE execution_mode='real'")
    c.commit()
    with pytest.raises(RuntimeError, match="real position"):
        purge_paper_ledger(c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_paper_purge.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.purge_memecoin_paper_ledger'`

- [ ] **Step 3: Write the script**

```python
#!/usr/bin/env python3
"""One-shot destructive purge of the paper ledger (spec 2026-07-30, Phase 1).

Every per-chain number computed before 2026-07-30 is a fill-model artifact: the
paper lab priced entries off a DexScreener mid and fell back to the fomo feed
where no pair existed, which on Robinhood 4663 produced 13,530 fomo-priced
entries and zero usable ones. This deletes that history so a clean evidence era
can start.

Real positions, the wallet roster and the raw event feed are never touched.
sqlite_sequence is never reset — see the module docstring test for why.
"""
import argparse
import sqlite3
import sys


def purge_paper_ledger(conn, *, dry_run=False):
    """Delete every paper position and its dependent rows. Returns a count report."""
    open_real = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=1").fetchone()[0]
    if open_real:
        raise RuntimeError(
            f"refusing to purge: {open_real} real position(s) still open")

    seq_before = _sequence(conn)
    paper_ids = [r[0] for r in conn.execute(
        "SELECT id FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper'")]
    fills = conn.execute(
        "SELECT COUNT(*) FROM memecoin_fills WHERE position_id IN "
        "(SELECT id FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper')"
    ).fetchone()[0]
    snapshots = _snapshot_count(conn)

    report = {
        "positions": len(paper_ids), "fills": fills, "snapshots": snapshots,
        "real_kept": conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real'").fetchone()[0],
        "wallets_kept": conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0],
        "seq_before": seq_before, "seq_after": seq_before,
    }
    if dry_run:
        return report

    # Dependency order: dependents first, parents last.
    conn.execute(
        "DELETE FROM memecoin_entry_snapshots WHERE position_id IN "
        "(SELECT id FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper')")
    conn.execute(
        "DELETE FROM memecoin_fills WHERE position_id IN "
        "(SELECT id FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper')")
    conn.execute(
        "DELETE FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper'")
    conn.commit()

    report["seq_after"] = _sequence(conn)
    if report["seq_after"] != seq_before:
        raise RuntimeError(
            f"sqlite_sequence changed {seq_before} -> {report['seq_after']}; "
            "id reuse would break the V6 position cursor")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions "
        "WHERE COALESCE(execution_mode,'paper')='paper'").fetchone()[0]
    if remaining:
        raise RuntimeError(f"purge incomplete: {remaining} paper rows remain")
    return report


def _sequence(conn):
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='memecoin_positions'").fetchone()
    return int(row[0]) if row else 0


def _snapshot_count(conn):
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memecoin_entry_snapshots WHERE position_id IN "
            "(SELECT id FROM memecoin_positions WHERE COALESCE(execution_mode,'paper')='paper')"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0        # column/table absent in older schemas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--apply", action="store_true", help="without this, dry-run only")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    report = purge_paper_ledger(conn, dry_run=not args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_paper_purge.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
python3 scripts/validate_schema_refs.py --files scripts/purge_memecoin_paper_ledger.py tests/test_memecoin_paper_purge.py
git add scripts/purge_memecoin_paper_ledger.py tests/test_memecoin_paper_purge.py
git commit -m "feat(memecoin): add the paper-ledger purge with sequence and real-position guards"
```

---

### Task 2: Execute the purge (operations)

**Files:** none — this is a runtime operation.

The cron watchdog at `* * * * * .runtime/memecoin/current/scripts/memecoin_watchdog.sh` respawns the runner within 60 seconds, so the cron line must be commented out for the window or the runner comes back mid-purge.

- [ ] **Step 1: Dry-run against the live DB**

```bash
python3 scripts/purge_memecoin_paper_ledger.py --db sniper_tracker.db
```
Expected: `positions` ≈ 23460, `real_kept` == 100, `wallets_kept` == 170, `seq_before` == 23867.

- [ ] **Step 2: Back up the database**

```bash
python3 -c "
import sqlite3, pathlib, time
src = sqlite3.connect('sniper_tracker.db')
out = pathlib.Path('data/backups') / f'sniper_tracker.pre-paper-purge.{int(time.time())}.db'
out.parent.mkdir(parents=True, exist_ok=True)
dst = sqlite3.connect(str(out)); src.backup(dst); dst.close(); src.close()
print('backup ->', out, out.stat().st_size)
"
```

- [ ] **Step 3: Pause the watchdog and stop the runner**

```bash
crontab -l > /tmp/crontab.before-purge
crontab -l | sed 's#^\(\* \* \* \* \* /usr/bin/env bash .*memecoin_watchdog.sh.*\)$#\#\1#' | crontab -
crontab -l | grep memecoin_watchdog.sh          # must show the line commented
kill -TERM "$(cat data/memecoin_runner.pid)"
until ! pgrep -f "memecoin/releases/.*/scripts/memecoin_runner_loop.py" >/dev/null; do sleep 2; done
echo "runner stopped"
```

- [ ] **Step 4: Re-check the funded preconditions after the stop**

```bash
python3 -c "
import sqlite3
c = sqlite3.connect('sniper_tracker.db'); c.row_factory = sqlite3.Row
print('open real     :', c.execute(\"SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1\").fetchone()[0])
print('pending resv  :', c.execute(\"SELECT COUNT(*) FROM memecoin_reservations WHERE status IN ('pending','unresolved')\").fetchone()[0])
"
```
Expected: both `0`. If either is non-zero, restore the crontab and stop.

- [ ] **Step 5: Apply the purge**

```bash
python3 scripts/purge_memecoin_paper_ledger.py --db sniper_tracker.db --apply
```
Expected: `[APPLIED] {... 'positions': 23460, 'seq_before': 23867, 'seq_after': 23867 ...}`

- [ ] **Step 6: Restore the watchdog and verify the runner returns**

```bash
crontab /tmp/crontab.before-purge
crontab -l | grep -c "^\* \* \* \* \* /usr/bin/env bash .*memecoin_watchdog.sh"   # expect 1
until pgrep -f "memecoin/releases/.*/scripts/memecoin_runner_loop.py" >/dev/null; do sleep 5; done
python3 -c "
import json, time
hb = json.load(open('data/memecoin_runner.heartbeat'))
print('heartbeat age', round(time.time() - hb['ts'], 1), 's  auth_ok', hb['auth_ok'])
"
```
Expected: heartbeat age < 30s, `auth_ok True`.

- [ ] **Step 7: Record the cutover**

```bash
git commit --allow-empty -m "chore(memecoin): paper evidence era 2 begins - ledger purged $(date -u +%FT%TZ)"
```

---

# PHASE 3 — Per-chain arming

### Task 3: Make `real_approved_user_ids` chain-aware

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:1775-1783` (`real_approved_user_ids`), `:386-389` (column comment)
- Modify: `tests/test_memecoin_real_approval_gate.py:205-215`, `tests/test_memecoin_unknown_chain_allowlist.py:83-86`
- Test: `tests/test_memecoin_per_chain_arming.py`

**Interfaces:**
- Produces: `real_approved_user_ids(conn, network_id=None) -> set[str]` — now filters. `network_id=None` returns the empty set.
- Produces: `wallet_armed_chains(conn, source_user_id) -> list[str]`
- Produces: `set_wallet_real_chains(conn, source_user_id, chains, *, actor="operator", reason="") -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_per_chain_arming.py
import sqlite3

import sniper.persistence.db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _armed(c, uid="u1", chains=None):
    m.add_wallet(c, uid, "h-" + uid)
    m.set_wallet_real_approved(c, uid, True, force=True)
    if chains is not None:
        m.set_wallet_real_chains(c, uid, chains)
    return c


def test_null_real_chains_is_armed_nowhere():
    """NULL must never be read as 'all chains' - that is the pooled behaviour being removed."""
    c = _armed(_conn())
    for nid in ("solana", "4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"NULL armed on {nid}"


def test_wallet_is_armed_only_on_its_listed_chains():
    c = _armed(_conn(), chains=["solana"])
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert m.real_approved_user_ids(c, network_id="4663") == set()
    assert m.real_approved_user_ids(c, network_id="8453") == set()


def test_missing_chain_fails_closed():
    """An authority caller that cannot name a chain gets nobody, not everybody."""
    c = _armed(_conn(), chains=["solana", "4663"])
    assert m.real_approved_user_ids(c) == set()
    assert m.real_approved_user_ids(c, network_id=None) == set()
    assert m.real_approved_user_ids(c, network_id="") == set()


def test_paused_and_excluded_still_override_chain_arming():
    c = _armed(_conn(), chains=["solana"])
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    c.execute("UPDATE memecoin_wallets SET paused=0, excluded=1 WHERE source_user_id='u1'")
    c.commit()
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_revoking_real_approved_disarms_every_chain():
    c = _armed(_conn(), chains=["solana", "4663"])
    m.set_wallet_real_approved(c, "u1", False)
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    assert m.real_approved_user_ids(c, network_id="4663") == set()


def test_wallet_armed_chains_roundtrip():
    c = _armed(_conn(), chains=["solana", "4663"])
    assert m.wallet_armed_chains(c, "u1") == ["4663", "solana"]
    m.set_wallet_real_chains(c, "u1", [])
    assert m.wallet_armed_chains(c, "u1") == []
    assert m.real_approved_user_ids(c, network_id="solana") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'set_wallet_real_chains'`

- [ ] **Step 3: Replace the reader and add the helpers**

Replace `real_approved_user_ids` at `sniper/persistence/db_memecoin.py:1775`:

```python
def real_approved_user_ids(conn, network_id=None):
    """Wallets whose BUYS may open a real position ON `network_id`.

    Arming is PER CHAIN (2026-07-30), reversing the 2026-07-15 global decision.
    `memecoin_wallets.real_chains` is the authority list: a comma-separated set of
    network ids. NULL or empty means armed NOWHERE — it is never read as "all
    chains", because a silent default is exactly the confusion this removes.

    A caller that cannot name a chain gets the empty set. The venue-context guard
    in make_real_hook answers "can we execute on this chain at all" (capability);
    this answers "is this wallet authorised here" (authority). They are different
    questions and the first never stood in for the second.
    """
    chain = str(network_id or "").strip().lower()
    if not chain:
        return set()
    out = set()
    for row in conn.execute(
            "SELECT source_user_id, real_chains FROM memecoin_wallets "
            "WHERE COALESCE(real_approved,0)=1 AND paused=0 AND COALESCE(excluded,0)=0"):
        if chain in _parse_real_chains(row["real_chains"]):
            out.add(row["source_user_id"])
    return out


def _parse_real_chains(raw):
    """'solana,4663' -> {'solana','4663'}. NULL/blank -> empty set (armed nowhere)."""
    return {p.strip().lower() for p in str(raw or "").split(",") if p.strip()}


def wallet_armed_chains(conn, source_user_id):
    """Sorted chains this wallet may open real positions on. Empty when armed nowhere."""
    row = conn.execute("SELECT real_chains FROM memecoin_wallets WHERE source_user_id=?",
                       (source_user_id,)).fetchone()
    return sorted(_parse_real_chains(row["real_chains"] if row else None))


def set_wallet_real_chains(conn, source_user_id, chains, *, actor="operator", reason="",
                           now_iso=None):
    """Set a wallet's per-chain authority list. Audited like any arming change."""
    if now_iso is None:
        now_iso = real_utc_now_iso()
    normalised = sorted({str(c).strip().lower() for c in (chains or []) if str(c).strip()})
    conn.execute("UPDATE memecoin_wallets SET real_chains=? WHERE source_user_id=?",
                 (",".join(normalised) or None, source_user_id))
    _audit_approval(conn, source_user_id, "set_chains", actor=actor,
                    reason=reason or f"chains={normalised or 'none'}",
                    stats=alpha_b_copy_stats(conn, source_user_id), forced=False,
                    now_iso=now_iso)
    conn.commit()
    return {"ok": True, "chains": normalised}
```

Replace the vestigial-column comment at `sniper/persistence/db_memecoin.py:386-389`:

```python
    # per-chain arming (2026-07-30): `real_chains` is the AUTHORITY list — the chains a wallet's
    # buys may open real positions on. NULL/empty = armed nowhere (fail-closed); it is never read
    # as "every chain". This reverses the 2026-07-15 global-arming decision, under which the column
    # was vestigial and always NULL. See real_approved_user_ids.
```

- [ ] **Step 4: Invert the two tests that encoded global arming**

In `tests/test_memecoin_real_approval_gate.py` replace `test_arming_is_global_across_every_chain`:

```python
def test_arming_is_per_chain_not_global():
    """Arming is PER CHAIN (2026-07-30). An approved wallet with no chain list is armed nowhere,
    and one listing solana is refused on every other chain."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_real_approved(c, "u1", True, force=True)
    for nid in ("solana", "4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"NULL armed on {nid}"
    m.set_wallet_real_chains(c, "u1", ["solana"])
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    for nid in ("4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"leaked onto {nid}"
```

In `tests/test_memecoin_unknown_chain_allowlist.py` replace the assertion loop at lines 83-86:

```python
    m.set_wallet_real_chains(conn, "u1", ["4663"])
    assert m.real_approved_user_ids(conn, network_id="4663") == {"u1"}
    # an unresolved chain is not an authority: it must not inherit 4663's arming
    assert m.real_approved_user_ids(conn, network_id="evm-unknown") == set()
    assert m.real_approved_user_ids(conn, network_id=None) == set()
```

- [ ] **Step 5: Run all three suites**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_real_approval_gate.py tests/test_memecoin_unknown_chain_allowlist.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin.py tests/test_memecoin_per_chain_arming.py \
        tests/test_memecoin_real_approval_gate.py tests/test_memecoin_unknown_chain_allowlist.py
git commit -m "feat(memecoin): make funded arming per-chain and fail-closed

Reverses the 2026-07-15 global-arming decision. real_chains becomes the
authority list; NULL means armed nowhere, never all chains."
```

---

### Task 4: Pass the chain at the funded entry gate

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py:2886`
- Test: `tests/test_memecoin_per_chain_arming.py` (append)

`chain` is already in scope at that line — it is bound at `:2879` from `identity.network_id` and validated against `contexts` immediately above. The fix is to pass it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_per_chain_arming.py
def test_entry_gate_refuses_a_wallet_not_armed_on_the_event_chain(monkeypatch):
    """A wallet armed on solana must be refused a 4663 entry with status not_approved."""
    import sniper.services.memecoin_real_runner as rr

    c = _armed(_conn(), chains=["solana"])
    seen = {}

    def _spy(conn, network_id=None):
        seen["network_id"] = network_id
        return m.real_approved_user_ids(conn, network_id=network_id)

    monkeypatch.setattr(rr.m, "real_approved_user_ids", _spy)
    assert rr.m.real_approved_user_ids(c, network_id="4663") == set()
    assert seen["network_id"] == "4663"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q -k entry_gate`
Expected: FAIL — the gate call passes no `network_id`.

- [ ] **Step 3: Pass the chain**

At `sniper/services/memecoin_real_runner.py:2886`:

```python
                # Authority is per chain (2026-07-30): being armed on solana does not
                # authorise a 4663 entry. `chain` is already resolved and venue-checked above.
                if ev.source_user_id not in m.real_approved_user_ids(conn, network_id=chain):
                    res = {"status": "not_approved", "chain": chain}
```

- [ ] **Step 4: Run the funded-path suites**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_entry_identity.py tests/test_memecoin_real_mcap_gate.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_real_runner.py tests/test_memecoin_per_chain_arming.py
git commit -m "feat(memecoin): scope the funded entry gate to the event's chain"
```

---

### Task 5: Make auto-demotion per chain

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py:2490-2515` (`check_wallet_demotion`)
- Test: `tests/test_memecoin_per_chain_arming.py` (append)

Today a wallet that loses on 4663 has `real_approved` revoked outright and is disarmed on Solana too — pooling through the back door, in the one place that moves real money.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_per_chain_arming.py
def test_demotion_on_one_chain_leaves_the_other_armed():
    """Losses on 4663 must not disarm solana."""
    import sniper.services.memecoin_real_runner as rr

    c = _armed(_conn(), chains=["solana", "4663"])
    rr.check_wallet_demotion(c, "u1", network_id="4663", alert_fn=lambda *a, **k: None)
    assert m.wallet_armed_chains(c, "u1") == ["solana"]
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert m.real_approved_user_ids(c, network_id="4663") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q -k demotion`
Expected: FAIL — `check_wallet_demotion() got an unexpected keyword argument 'network_id'`

- [ ] **Step 3: Scope the demotion**

In `check_wallet_demotion`, take `network_id`, gate on per-chain arming, and revoke only that chain:

```python
def check_wallet_demotion(conn, source_user_id, network_id=None, alert_fn=None):
    """Pre-registered auto-demote guardrail, scoped to ONE chain (2026-07-30).

    Losses on 4663 revoke 4663 only; solana arming survives. Revoking globally would
    reintroduce chain pooling in the one place that moves real money. When a wallet's
    last chain is revoked, real_approved is cleared too so the roster stays truthful.
    """
    chain = str(network_id or "").strip().lower()
    if not source_user_id or not chain:
        return False
    if source_user_id not in m.real_approved_user_ids(conn, network_id=chain):
        return False
    rec = m.wallet_real_record(conn, source_user_id, network_id=chain)
    loss_hit = (cfg.MEMECOIN_REAL_DEMOTE_LOSS_USD > 0
                and rec["realized_total"] <= -cfg.MEMECOIN_REAL_DEMOTE_LOSS_USD)
    consec_hit = (cfg.MEMECOIN_REAL_DEMOTE_CONSEC > 0
                  and rec["consecutive_losses"] >= cfg.MEMECOIN_REAL_DEMOTE_CONSEC)
    if not (loss_hit or consec_hit):
        return False
    remaining = [ch for ch in m.wallet_armed_chains(conn, source_user_id) if ch != chain]
    m.set_wallet_real_chains(
        conn, source_user_id, remaining, actor="auto_demote",
        reason=f"{chain}: cohort realized ${rec['realized_total']:+.2f} over "
               f"{rec['closed_count']} closes / {rec['consecutive_losses']} consecutive losses")
    if not remaining:
        m.set_wallet_real_approved(
            conn, source_user_id, False, actor="auto_demote",
            reason=f"last armed chain ({chain}) revoked")
```

Keep the existing alert block below unchanged, but include the chain in the message.

- [ ] **Step 4: Scope the cohort record**

`wallet_real_record` must accept `network_id` and filter its position query by it, so the demote cohort is per (wallet, chain) as the spec requires. Add the parameter in `sniper/persistence/db_memecoin.py` and append `AND network_id=?` to the query when it is supplied; a `None` chain keeps the current lifetime behaviour for non-authority readers.

- [ ] **Step 5: Update every call site of `check_wallet_demotion`**

```bash
grep -rn "check_wallet_demotion" --include=*.py sniper/ scripts/ | grep -v test
```
Pass the closing position's `network_id` at each. A call site that cannot supply one must not demote.

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_real_deploy.py tests/test_memecoin_real_deploy_routes.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add sniper/services/memecoin_real_runner.py sniper/persistence/db_memecoin.py tests/test_memecoin_per_chain_arming.py
git commit -m "fix(memecoin): demote a wallet on the losing chain only, never globally"
```

---

### Task 6: Classify the remaining lanes

**Files:**
- Modify: `sniper/services/memecoin_follow_activity.py:193,363`, `sniper/services/memecoin_runner.py:145,584`, `sniper/services/memecoin_follow_sync.py:144`, `sniper/persistence/db_memecoin.py:4572`, `sniper/routes/api_memecoin.py:362,379,1572`
- Test: `tests/test_memecoin_per_chain_arming.py` (append)

Each caller is authority, display, or historical stamp. Display lanes must show *which* chains, never a bare armed/not-armed, or the UI claims authority the runner would refuse.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_per_chain_arming.py
def test_display_lane_reports_chains_not_a_bare_boolean():
    """A wallet armed only on solana must not render as globally armed."""
    c = _armed(_conn(), chains=["solana"])
    rows = m.list_curated_with_stats(c)
    row = next(r for r in rows if r["source_user_id"] == "u1")
    assert row["armed_chains"] == ["solana"]
    assert row["armed"] is False, "bare `armed` must not claim authority the runner would refuse"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q -k display_lane`
Expected: FAIL — `KeyError: 'armed_chains'`

- [ ] **Step 3: Update the display lane**

At `sniper/persistence/db_memecoin.py:4572`, replace the global `armed_ids` read with a per-wallet chain list and emit both fields:

```python
    # `armed_chains` is the AUTHORITATIVE per-chain effective-arm truth (real_approved AND
    # not-paused AND not-excluded AND chain listed). Since 2026-07-30 arming is per chain, so a
    # bare boolean cannot express it — `armed` is kept as "armed on at least one chain" for
    # legacy readers and must never be used to decide whether an entry is allowed.
    out.append({
        ...
        "armed_chains": wallet_armed_chains(conn, uid),
        "armed": bool(wallet_armed_chains(conn, uid)),
    })
```

Apply the same treatment at `api_memecoin.py:362` and `:379` (`"real_approved"` becomes `"armed_chains"`), and at `:1572` report `allowlist_by_chain` instead of a single `allowlist_n`.

- [ ] **Step 4: Handle the roster lanes**

`memecoin_follow_activity.py:193` and `:363` build the fast-lane roster and the armed flag for *observation*, not authority — a wallet armed on any chain should still be observed. Replace `m.real_approved_user_ids(conn)` with a union across armed chains:

```python
    armed = {r["source_user_id"] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE COALESCE(real_approved,0)=1 "
        "AND paused=0 AND COALESCE(excluded,0)=0 AND COALESCE(real_chains,'')<>''")}
```

Do the same at `memecoin_runner.py:584` and `memecoin_follow_sync.py:144` (the fomo-follow sync must keep following a wallet armed on any chain). At `memecoin_runner.py:145` the telemetry context becomes `"real_armed_chains": m.wallet_armed_chains(conn, ev.source_user_id)`.

Leave `was_real_approved` in `memecoin_capture_v2.py` and `memecoin_shadow_signals.py` untouched — those record what was true when written.

- [ ] **Step 5: Run the lane suites**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py tests/test_memecoin_follow_sync.py tests/test_memecoin_real_approval_gate.py tests/test_memecoin_real_deploy.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
python3 scripts/validate_schema_refs.py
git add -u sniper/ tests/
git commit -m "feat(memecoin): report armed chains on every display and roster lane"
```

---

### Task 7: Arming controls per chain

**Files:**
- Modify: `sniper/routes/api_memecoin.py` (near `:313`, the existing `wallets/<uid>/real` route)
- Modify: `static/app.js` (traders table arming control)
- Test: `tests/test_memecoin_per_chain_arming.py` (append)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_per_chain_arming.py
def test_arm_route_requires_a_chain(client_and_conn):
    client, c = client_and_conn
    m.add_wallet(c, "u1", "h1")
    assert client.post("/api/memecoin/wallets/u1/real", json={"approved": True}).status_code == 400
    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"approved": True, "chains": ["solana"], "force": True})
    assert r.status_code == 200
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert m.real_approved_user_ids(c, network_id="4663") == set()
```

Reuse the app/conn fixture pattern already in `tests/test_memecoin_real_deploy_routes.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_per_chain_arming.py -q -k arm_route`
Expected: FAIL — arming succeeds without a chain.

- [ ] **Step 3: Require a chain list on the route**

In the `wallets/<uid>/real` POST handler, reject an approve with no chains:

```python
        chains = payload.get("chains")
        if payload.get("approved") and not chains:
            return jsonify({"error": "chains_required",
                            "detail": "arming is per chain; name at least one"}), 400
```
On success call `m.set_wallet_real_approved(...)` then `m.set_wallet_real_chains(conn, uid, chains, ...)`. A revoke needs no chains and clears the list.

- [ ] **Step 4: Add the control to the traders table**

In the chain tab's traders table, the `Armed?` cell renders an arm/disarm button **for that chain only**, posting `{"approved": true, "chains": [<current chain>]}` merged with the wallet's existing chains.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/test_memecoin_per_chain_arming.py -q
node --check static/app.js
git add sniper/routes/api_memecoin.py static/app.js tests/test_memecoin_per_chain_arming.py
git commit -m "feat(memecoin): arm and disarm wallets per chain from the chain tab"
```

---

# PHASE 2 — Chains workspace

### Task 8: Canonical chain resolver

**Files:**
- Create: `sniper/services/memecoin_chains.py`
- Test: `tests/test_memecoin_chains.py`

Today `paper-report` speaks `CHAIN_IDS` aliases (`robin`, `bsc`) while `wallet_recommendations` matches raw `network_id` (`4663`, `56`). Passing one vocabulary to the other returns the wrong chain's rows with no error.

**Interfaces:**
- Produces: `CHAINS: tuple[dict]` — ordered `(key, label, network_ids, funded)` records
- Produces: `resolve_chain(value) -> str | None` — any alias to canonical key
- Produces: `network_ids_for(key) -> tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_chains.py
import pytest

from sniper.services.memecoin_chains import CHAINS, network_ids_for, resolve_chain


def test_every_alias_resolves_to_one_canonical_key():
    for value in ("solana", "SOLANA", "sol"):
        assert resolve_chain(value) == "solana"
    for value in ("4663", "robin", "robinhood"):
        assert resolve_chain(value) == "robin"
    for value in ("56", "bsc"):
        assert resolve_chain(value) == "bsc"
    for value in ("8453", "base"):
        assert resolve_chain(value) == "base"


def test_there_is_no_all_scope():
    """Chains are never pooled - 'all' must not resolve."""
    assert resolve_chain("all") is None
    assert "all" not in {c["key"] for c in CHAINS}


def test_unresolved_is_its_own_bucket_not_a_chain():
    assert resolve_chain("evm-unknown") == "unresolved"
    assert next(c for c in CHAINS if c["key"] == "unresolved")["funded"] is False


def test_network_ids_are_disjoint_across_chains():
    seen = set()
    for chain in CHAINS:
        ids = set(network_ids_for(chain["key"]))
        assert not (ids & seen), f"{chain['key']} shares a network id with another chain"
        seen |= ids


def test_unknown_value_resolves_to_none():
    assert resolve_chain("dogecoin") is None
    with pytest.raises(KeyError):
        network_ids_for("dogecoin")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_chains.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.services.memecoin_chains'`

- [ ] **Step 3: Write the resolver**

```python
"""Canonical memecoin chain vocabulary (spec 2026-07-30).

One resolver so `robin`, `4663` and `Robinhood` cannot address different row sets
on different endpoints. There is deliberately NO "all" key: chains are never
pooled. `unresolved` is a data-quality bucket for positions whose chain could not
be identified — it is listed so those rows stay visible, and is never merged into
a real chain's numbers.
"""

CHAINS = (
    {"key": "solana", "label": "Solana", "network_ids": ("solana",), "funded": True},
    {"key": "base", "label": "Base", "network_ids": ("8453", "base"), "funded": True},
    {"key": "robin", "label": "Robinhood", "network_ids": ("4663", "robin"), "funded": True},
    {"key": "bsc", "label": "BSC", "network_ids": ("56", "bsc"), "funded": False},
    {"key": "eth", "label": "Ethereum", "network_ids": ("1", "eth", "ethereum"), "funded": False},
    {"key": "unresolved", "label": "Unresolved", "network_ids": ("evm-unknown",), "funded": False},
)

_ALIASES = {"sol": "solana", "robinhood": "robin"}
_BY_NETWORK_ID = {nid: c["key"] for c in CHAINS for nid in c["network_ids"]}
_BY_KEY = {c["key"]: c for c in CHAINS}


def resolve_chain(value):
    """Any alias, label or network id -> canonical key. Unknown or 'all' -> None."""
    v = str(value or "").strip().lower()
    if not v:
        return None
    if v in _BY_KEY:
        return v
    if v in _ALIASES:
        return _ALIASES[v]
    return _BY_NETWORK_ID.get(v)


def network_ids_for(key):
    """Network ids stored in memecoin_positions.network_id for a canonical key."""
    return _BY_KEY[key]["network_ids"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_chains.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_chains.py tests/test_memecoin_chains.py
git commit -m "feat(memecoin): add one canonical chain vocabulary with no pooled scope"
```

---

### Task 9: Chain-scope the arm matrix

**Files:**
- Modify: `sniper/persistence/db_memecoin.py:5615` (`trader_arm_matrix`)
- Modify: `sniper/routes/api_memecoin.py:1742` (`traders_arm_matrix`)
- Test: `tests/test_memecoin_arm_matrix_chain.py`

`trader_arm_matrix` already returns `shared_origins` per wallet — that is the `signals` count the spec asks for, so no new aggregate is needed. It already excludes fomo fills via `HONEST_COPY_SQL` and already sorts by total PnL.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_arm_matrix_chain.py
import sqlite3

import sniper.persistence.db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, pid, uid, net, arm, origin, pnl):
    c.execute(
        "INSERT INTO memecoin_positions(id,source_user_id,source_handle,mint,network_id,"
        "execution_mode,is_open,strategy,origin_event_id,realized_pnl,closed_at)"
        " VALUES(?,?,?,?,?,'paper',0,?,?,?,'2026-07-30T00:00:00+00:00')",
        (pid, uid, "h-" + uid, "mint" + str(pid), net, arm, origin, pnl))


def test_matrix_returns_only_the_requested_chain():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "solana", "control", "e1", 10.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 20.0)
    _pos(c, 3, "u1", "4663", "control", "e2", -5.0)
    _pos(c, 4, "u1", "4663", "alpha_b", "e2", -7.0)
    c.commit()

    sol = m.trader_arm_matrix(c, chain="solana")
    assert [w["total_pnl"] for w in sol["wallets"]] == [30.0]
    robin = m.trader_arm_matrix(c, chain="robin")
    assert [w["total_pnl"] for w in robin["wallets"]] == [-12.0]


def test_signals_counts_distinct_origin_events_not_positions():
    """Two arms on one event is ONE signal, not two."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _pos(c, 1, "u1", "solana", "control", "e1", 1.0)
    _pos(c, 2, "u1", "solana", "alpha_b", "e1", 2.0)
    c.commit()
    w = m.trader_arm_matrix(c, chain="solana")["wallets"][0]
    assert w["signals"] == 1
    assert w["total_closed"] == 2


def test_unknown_chain_raises_rather_than_pooling():
    c = _conn()
    try:
        m.trader_arm_matrix(c, chain="all")
    except (KeyError, ValueError):
        return
    raise AssertionError("'all' must not be an accepted scope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_arm_matrix_chain.py -q`
Expected: FAIL — `trader_arm_matrix() got an unexpected keyword argument 'chain'`

- [ ] **Step 3: Add the chain filter and the signals alias**

In `trader_arm_matrix(conn, mode="paper", min_n=3)` add `chain=None`, resolve it, and append the filter to the query:

```python
def trader_arm_matrix(conn, mode="paper", min_n=3, chain=None):
    """Honest wallet x arm matrix over each wallet's exact shared origin set, scoped to ONE chain.

    `chain` is a canonical key from sniper.services.memecoin_chains. There is no pooled scope:
    a value that does not resolve raises rather than silently returning every chain.
    """
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain

    chain_sql, chain_params = "", ()
    if chain is not None:
        key = resolve_chain(chain)
        if key is None:
            raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
        ids = network_ids_for(key)
        chain_sql = " AND network_id IN (%s)" % ",".join("?" * len(ids))
        chain_params = ids
```

Add `chain_sql` to the `WHERE` clause and `*chain_params` to the parameter tuple. Then expose the signal count alongside the existing field:

```python
        a["signals"] = len(shared)          # distinct origin events, not positions
```

- [ ] **Step 4: Pass the parameter through the route**

At `sniper/routes/api_memecoin.py:1742`:

```python
    @bp.get("/api/memecoin/traders/arm-matrix")
    def traders_arm_matrix():
        # Wallet x arm closed-copy PnL matrix (read-only), scoped to ONE chain. No pooled scope.
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required",
                            "detail": "chains are never pooled; name one"}), 400
        return jsonify(m.trader_arm_matrix(conn, chain=chain))
```

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/test_memecoin_arm_matrix_chain.py -q
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin.py sniper/routes/api_memecoin.py tests/test_memecoin_arm_matrix_chain.py
git commit -m "feat(memecoin): scope the wallet x arm matrix to one chain and expose signal counts"
```

---

### Task 10: Chain-scope open and closed positions

**Files:**
- Modify: `sniper/routes/api_memecoin.py:385-417`
- Modify: `sniper/persistence/db_memecoin.py` (`list_open_positions`, `list_closed_positions`, `closed_position_totals`)
- Test: `tests/test_memecoin_positions_chain_scope.py`

`positions/closed` returns the last 200 rows globally. Client-side filtering therefore gives each chain an arbitrary slice — in the current window those 200 are `4663:126, solana:64, 56:7, 8453:3`, so a Base tab would show 3 closed trades out of 1,147.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_positions_chain_scope.py
import sqlite3

import sniper.persistence.db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_closed_limit_applies_per_chain_not_globally():
    """A quiet chain must return ITS most recent rows, not its share of a global window."""
    c = _conn()
    for i in range(250):
        c.execute(
            "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
            "is_open,strategy,realized_pnl,closed_at) VALUES('u1',?,'solana','paper',0,"
            "'control',1.0,?)", (f"s{i}", f"2026-07-30T00:{i // 60:02d}:{i % 60:02d}+00:00"))
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
        "is_open,strategy,realized_pnl,closed_at) VALUES('u1','b1','8453','paper',0,"
        "'control',2.0,'2026-07-29T00:00:00+00:00')")
    c.commit()

    base = m.list_closed_positions(c, limit=200, chain="base")
    assert len(base) == 1 and base[0]["mint"] == "b1"
    assert len(m.list_closed_positions(c, limit=200, chain="solana")) == 200


def test_totals_are_scoped_to_the_chain():
    c = _conn()
    c.execute("INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
              "is_open,strategy,realized_pnl,closed_at) VALUES('u1','s','solana','paper',0,"
              "'control',10.0,'2026-07-30T00:00:00+00:00')")
    c.execute("INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
              "is_open,strategy,realized_pnl,closed_at) VALUES('u1','b','8453','paper',0,"
              "'control',99.0,'2026-07-30T00:00:00+00:00')")
    c.commit()
    assert m.closed_position_totals(c, chain="solana")["realized_pnl"] == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_positions_chain_scope.py -q`
Expected: FAIL — `list_closed_positions() got an unexpected keyword argument 'chain'`

- [ ] **Step 3: Add the parameter to all three readers**

Each takes `chain=None`, resolves via `resolve_chain`, raises `ValueError` on an unknown value, and appends `AND network_id IN (...)` **before** `ORDER BY closed_at DESC LIMIT ?` so the limit applies within the chain.

- [ ] **Step 4: Require the chain on both routes**

```python
    @bp.get("/api/memecoin/positions/open")
    def open_positions():
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required"}), 400
        positions = m.list_open_positions(conn, chain=chain)
        return jsonify({"positions": positions,
                        "marks": stored_position_marks(conn, positions, _now_iso())})
```
Apply the same guard to `positions/closed`, passing `chain` to both `list_closed_positions` and `closed_position_totals`.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m pytest tests/test_memecoin_positions_chain_scope.py -q
python3 scripts/validate_schema_refs.py
git add sniper/persistence/db_memecoin.py sniper/routes/api_memecoin.py tests/test_memecoin_positions_chain_scope.py
git commit -m "fix(memecoin): apply the closed-position limit per chain instead of globally"
```

---

### Task 11: Navigation restructure

**Files:**
- Modify: `static/app.js:3068-3088` (workspace + subtab markup), `:3305-3306` (`_WORKSPACES`, `_wsLast`), `:3323` (loader map)
- Test: manual — verified by the Task 12 screenshot

- [ ] **Step 1: Replace the workspace nav**

```html
          <div id="mc-workspaces" style="display:flex;gap:4px;margin-bottom:2px;overflow-x:auto;-webkit-overflow-scrolling:touch">
            <button class="mc-ws" data-workspace-nav="live">💰 Live</button>
            <button class="mc-ws" data-workspace-nav="chains">Chains</button>
            <button class="mc-ws" data-workspace-nav="discovery">Discovery</button>
            <button class="mc-ws" data-workspace-nav="radar">Radar</button>
          </div>
```

- [ ] **Step 2: Replace the sub-tab row**

Drop the `paper`, `wallets` and `theses` buttons. Chains gets three sub-tabs; Discovery has none.

```html
            <button class="mc-subtab" data-workspace="chains" data-subtab="chain-paper">Paper results</button>
            <button class="mc-subtab" data-workspace="chains" data-subtab="chain-open">Open trades</button>
            <button class="mc-subtab" data-workspace="chains" data-subtab="chain-traders">Traders</button>
            <button class="mc-subtab" data-workspace="discovery" data-subtab="discovery">Candidates</button>
```

- [ ] **Step 3: Update the workspace maps**

```javascript
    _WORKSPACES: {
      live: ['real'],
      chains: ['chain-paper', 'chain-open', 'chain-traders'],
      discovery: ['discovery'],
      radar: ['radar', 'activity', 'decisions'],
    },
    _wsLast: { live: 'real', chains: 'chain-paper', discovery: 'discovery', radar: 'radar' },
```
Point `chain-paper`/`chain-open`/`chain-traders` at their loaders in the `:3323` map and delete the `performance`, `closed`, `positions` and `theses` entries plus the now-unreachable `_loadTheses`.

- [ ] **Step 4: Default the chain selection**

`_chainFilter` currently defaults to `'all'`, which no longer exists. Default it to `'solana'` and delete `chainFilterChips`' all-chip. Every `_loadSection` call for a `chain-*` sub-tab passes `this._chainFilter` to its endpoint.

- [ ] **Step 5: Syntax check and commit**

```bash
node --check static/app.js
git add static/app.js
git commit -m "feat(memecoin): replace the Paper/Wallets/Theses tabs with Chains and Discovery"
```

---

### Task 12: Chain tab content, empty states, and deploy

**Files:**
- Modify: `static/app.js` (three chain loaders, dropped-wallet list, empty states)

- [ ] **Step 1: Render the chain selector with no pooled option**

```javascript
    _syncChainSelector() {
      const el = document.getElementById('mc-chain-filter');
      if (!el) return;
      // No 'all' chip: chains are never pooled. Funded chains are marked.
      el.innerHTML = MC_CHAINS.map(c =>
        `<button class="pill${c.key === this._chainFilter ? ' active' : ''}" data-mc-chain="${esc(c.key)}">
           ${esc(c.label)}${c.funded ? '<sup title="funded">*</sup>' : ''}</button>`).join('');
    },
```

- [ ] **Step 2: Render the traders table per the agreed columns**

Header is `Trader | signals | <arm columns present in this chain> | Total | Armed?`. Arm columns come from the response's `arms`, so a chain that never ran an arm grows no column. `momentum_a` renders `n/a — no tape` on `base`, `bsc` and `eth`. `Armed?` shows arming for the selected chain only.

- [ ] **Step 3: List dropped wallets below the table**

Wallets copied on the chain but absent from the matrix are shown with their reason (`only 1 arm`, `no shared origin`) so the roster is complete without making non-comparable numbers look comparable.

- [ ] **Step 4: Honest empty states**

```javascript
    _chainEmpty(label, counts) {
      return `<div class="text-muted" style="padding:10px 0">
        <div style="font-weight:600">${esc(label)} · no evidence in this era yet</div>
        <div style="font-size:11px">${counts.closed} closed · ${counts.wallets} wallets comparable
          · era started 2026-07-30</div></div>`;
    },
```
An empty chain is never filled by borrowing another chain's rows.

- [ ] **Step 5: Verify visually**

```bash
set -a && . <(grep -E "^SNIPER_DASH_(USER|PASS)=" .env) && set +a
python3 "$SCRATCH/shot_dash.py" "/" "$SCRATCH/chains.png" --wait 7 \
  --eval "document.querySelector('[data-venue=\"solana\"]')?.click()"
```
Read the PNG and confirm: four workspace buttons, no `All` chip, funded chains marked.

- [ ] **Step 6: Commit, merge and deploy**

```bash
node --check static/app.js
python3 -m pytest tests/test_memecoin_chains.py tests/test_memecoin_arm_matrix_chain.py \
        tests/test_memecoin_positions_chain_scope.py tests/test_memecoin_per_chain_arming.py \
        tests/test_memecoin_paper_purge.py -q
git add static/app.js && git commit -m "feat(memecoin): per-chain paper results, open trades and trader matrix"
gh pr create --base main --title "feat(memecoin): per-chain evidence and Chains workspace" --body "<KSG block>"
```
After merge, publish the web release: `git worktree add --detach .runtime/web/releases/$SHA $SHA`, swap `current`/`previous`, `kill -TERM` the server pid, wait for HTTP, confirm the served HTML.

---

## Self-review

**Spec coverage:** Phase 1 §Scope/§Procedure → Tasks 1-2. §"165 open positions" → deleted by Task 1, no close path, as specified. Phase 3 §Semantics → Task 3; §"Every lane" authority row → Task 4, display row → Task 6, historical-stamp row → Task 6 Step 4 (explicitly untouched); §"Auto-demotion" → Task 5; §Migration → Task 3 (NULL fails closed, so all 19 disarm without a data migration). Phase 2 §Navigation → Task 11; §"Content per chain" → Tasks 9, 10, 12; §"traders table" → Tasks 9, 12; §"API work" → Tasks 8-10; §"Empty and thin states" → Task 12. §Testing rows map to the tests in Tasks 1, 3-5, 8-10.

**Type consistency:** `resolve_chain`/`network_ids_for` are used identically in Tasks 9 and 10. `wallet_armed_chains` returns a sorted list in Tasks 3, 5, 6. `trader_arm_matrix` gains `chain=` in Task 9 and is consumed with the same name in Task 12. `check_wallet_demotion` gains `network_id=` in Task 5 and every call site is swept in the same task.

**Known gap:** Task 5 Step 4 changes `wallet_real_record` to accept `network_id`; its other callers keep the lifetime behaviour when it is omitted. Confirm with `grep -rn "wallet_real_record" --include=*.py sniper/ scripts/` during that task.
