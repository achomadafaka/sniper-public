"""DB layer for the HL real copy leg: hl_real_intents (write-ahead order log),
hl_real_fills (execution results), and hl_meta keys (deployment epoch, breaker latch).

All access to these tables goes through this module. Tables are created by
intel_store.ensure_schema. Spec: docs/superpowers/specs/2026-07-17-hl-real-copy-leg-design.md
"""
from __future__ import annotations

import sqlite3

_DEPLOY_ID_KEY = "hl_real_deployment_id"
_EPOCH_KEY = "hl_real_epoch_ms"
_BREAKER_KEY = "hl_real_breaker"

# Intent lifecycle. 'skipped' and 'dry_run' are terminal, recorded without submission.
INTENT_STATES = ("intended", "skipped", "dry_run", "submitted",
                 "partial", "filled", "rejected", "ambiguous")


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM hl_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str, now_ms: int) -> None:
    conn.execute("INSERT OR REPLACE INTO hl_meta(key, value, updated_ts_ms) VALUES (?,?,?)",
                 (key, value, now_ms))
    conn.commit()


def _meta_del(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("DELETE FROM hl_meta WHERE key=?", (key,))
    conn.commit()


def get_or_create_deployment(conn: sqlite3.Connection, *, now_ms: int) -> dict:
    """First call stamps the deployment (id + epoch). Later calls return the stored one —
    the epoch gates eligibility so pre-existing paper opens are never adopted."""
    did, epoch = _meta_get(conn, _DEPLOY_ID_KEY), _meta_get(conn, _EPOCH_KEY)
    if did is None or epoch is None:
        did = f"dep-{now_ms:x}"
        epoch = str(now_ms)
        _meta_set(conn, _DEPLOY_ID_KEY, did, now_ms)
        _meta_set(conn, _EPOCH_KEY, epoch, now_ms)
    return {"deployment_id": did, "epoch_ms": int(epoch)}


def record_intent(conn, *, cloid: str, deployment_id: str, copy_id: int, leg: str,
                  coin: str, side: str, requested_usd, requested_sz,
                  state: str, reason, now_ms: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO hl_real_intents(cloid, deployment_id, copy_id, leg, coin, side, "
        "requested_usd, requested_sz, state, reason, created_ts_ms, updated_ts_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cloid, deployment_id, copy_id, leg, coin, side, requested_usd, requested_sz,
         state, reason, now_ms, now_ms))
    conn.commit()


def set_intent_state(conn, cloid: str, state: str, *, reason=None, now_ms: int) -> None:
    conn.execute(
        "UPDATE hl_real_intents SET state=?, reason=COALESCE(?, reason), updated_ts_ms=? "
        "WHERE cloid=?",
        (state, reason, now_ms, cloid))
    conn.commit()


def intents_in(conn, states: list[str]) -> list[dict]:
    ph = ",".join("?" * len(states))
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM hl_real_intents WHERE state IN ({ph}) ORDER BY created_ts_ms", states)]


def intent_for(conn, copy_id: int, leg: str) -> dict | None:
    row = conn.execute("SELECT * FROM hl_real_intents WHERE copy_id=? AND leg=?",
                       (copy_id, leg)).fetchone()
    return dict(row) if row else None


def record_fill(conn, *, cloid: str, copy_id: int, leg: str,
                fill_px, fill_sz, filled_ts_ms) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hl_real_fills(cloid, copy_id, leg, fill_px, fill_sz, "
        "fee_usd, filled_ts_ms, realized_usd, paper_twin_pnl_bps, real_net_bps, drift_bps) "
        "VALUES (?,?,?,?,?, (SELECT fee_usd FROM hl_real_fills WHERE cloid=?), ?, "
        "(SELECT realized_usd FROM hl_real_fills WHERE cloid=?), "
        "(SELECT paper_twin_pnl_bps FROM hl_real_fills WHERE cloid=?), "
        "(SELECT real_net_bps FROM hl_real_fills WHERE cloid=?), "
        "(SELECT drift_bps FROM hl_real_fills WHERE cloid=?))",
        (cloid, copy_id, leg, fill_px, fill_sz, cloid, filled_ts_ms,
         cloid, cloid, cloid, cloid))
    conn.commit()


def finalize_close(conn, *, cloid: str, realized_usd, paper_twin_pnl_bps,
                   real_net_bps, drift_bps) -> None:
    conn.execute(
        "UPDATE hl_real_fills SET realized_usd=?, paper_twin_pnl_bps=?, real_net_bps=?, "
        "drift_bps=? WHERE cloid=?",
        (realized_usd, paper_twin_pnl_bps, real_net_bps, drift_bps, cloid))
    conn.commit()


def set_fill_fee(conn, cloid: str, fee_usd: float) -> None:
    conn.execute("UPDATE hl_real_fills SET fee_usd=? WHERE cloid=?", (fee_usd, cloid))
    conn.commit()


def leg_open_positions(conn) -> list[dict]:
    """Open-leg intents that landed (filled/partial) with no landed close intent yet —
    the leg-owned real book. Fill px/sz joined in for accounting + reconcile."""
    return [dict(r) for r in conn.execute(
        "SELECT i.copy_id, i.coin, i.side, i.cloid, f.fill_px, f.fill_sz "
        "FROM hl_real_intents i JOIN hl_real_fills f ON f.cloid = i.cloid "
        "WHERE i.leg='open' AND i.state IN ('filled','partial') "
        "AND NOT EXISTS (SELECT 1 FROM hl_real_intents c "
        "                WHERE c.copy_id=i.copy_id AND c.leg='close' "
        "                AND c.state IN ('filled','partial')) "
        "ORDER BY i.created_ts_ms")]


def day_realized_usd(conn, day_utc: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_usd), 0) FROM hl_real_fills "
        "WHERE leg='close' AND realized_usd IS NOT NULL "
        "AND date(filled_ts_ms/1000, 'unixepoch') = ?",
        (day_utc,)).fetchone()
    return float(row[0])


def refinalize_close(conn, close_cloid: str) -> None:
    """(Re)compute close accounting from stored fills + fees + the paper twin. Safe to
    call repeatedly — used at close-fill time and again after fee backfill so fees are
    never silently dropped from realized/drift."""
    from sniper.hyperlive import real_exec as RE     # pure module; no cycle risk
    ci = conn.execute("SELECT * FROM hl_real_intents WHERE cloid=?",
                      (close_cloid,)).fetchone()
    cf = conn.execute("SELECT * FROM hl_real_fills WHERE cloid=?",
                      (close_cloid,)).fetchone()
    if not ci or not cf or cf["fill_px"] is None:
        return
    oi = intent_for(conn, ci["copy_id"], "open")
    of = conn.execute("SELECT * FROM hl_real_fills WHERE cloid=?",
                      (oi["cloid"],)).fetchone() if oi else None
    paper = conn.execute("SELECT pnl_bps FROM hl_copy_book WHERE id=?",
                         (ci["copy_id"],)).fetchone()
    if not of or of["fill_px"] is None or paper is None:
        return
    fees = (of["fee_usd"] or 0.0) + (cf["fee_usd"] or 0.0)
    paper_bps = paper["pnl_bps"] or 0.0
    acct = RE.close_accounting(dict(of), dict(cf), fees_usd=fees,
                               paper_pnl_bps=paper_bps, side=ci["side"])
    finalize_close(conn, cloid=close_cloid, realized_usd=acct["realized_usd"],
                   paper_twin_pnl_bps=paper_bps, real_net_bps=acct["real_net_bps"],
                   drift_bps=acct["drift_bps"])


def breaker_latched(conn) -> str | None:
    """Returns the UTC day the breaker latched on, or None. Latch persists across
    restarts until an operator clears it (software-soft cap — see spec §7)."""
    return _meta_get(conn, _BREAKER_KEY)


def latch_breaker(conn, *, day_utc: str, now_ms: int) -> None:
    _meta_set(conn, _BREAKER_KEY, day_utc, now_ms)


def clear_breaker(conn) -> None:
    _meta_del(conn, _BREAKER_KEY)
