"""Bounded, local, read-only evidence for the RISK watcher; never an execution-path reader.

The caller supplies committed capture proof in host time. One venue is selected, never a curve/AMM price splice.
Unknown source history stays unknown; neither heartbeat activity nor USD amounts manufacture native quote facts.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_ps_losses as PSL
from sniper.services import memecoin_desk_risk as R
from sniper.util import clock


MAX_ROWS = 20_000
MAX_LOOKBACK_S = 3600.0
SOL_QUOTE = "11111111111111111111111111111111"  # universe_worker.quote_facts()'s explicit native SOL label


class TapeUnreadable(Exception):
    pass


def _ro(path):
    if not path.is_file():
        raise TapeUnreadable("tape_missing")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)


def _days(lo, hi):
    day = datetime.fromtimestamp(lo, timezone.utc).date()
    last = datetime.fromtimestamp(math.nextafter(hi, -math.inf), timezone.utc).date()
    while day <= last:
        yield day.isoformat()
        day = day.fromordinal(day.toordinal() + 1)


def _curve(desk_dir, mint, lo, hi):
    rows = []
    columns = "received_at, signature, is_buy, quote_atomic, actor, virtual_quote, virtual_token, real_quote, quote_mint"
    for day in _days(lo, hi):
        with closing(_ro(Path(desk_dir) / "swaps" / (day + ".db"))) as c:
            c.row_factory = sqlite3.Row
            rows.extend(dict(r) for r in c.execute(
                "SELECT " + columns + " FROM swaps WHERE mint=? AND received_at>=? AND received_at<? "
                "ORDER BY received_at LIMIT ?", (mint, lo, hi, MAX_ROWS + 1 - len(rows))))
        if len(rows) > MAX_ROWS:
            raise TapeUnreadable("row_limit")
    return sorted(rows, key=lambda r: r["received_at"])


def _graduated(desk_dir, mint, pool, lo, hi):
    path = PT.tape_path(desk_dir)
    if not path.exists():
        return False
    with closing(_ro(path)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ps_grads'").fetchone():
            return False
        return c.execute("SELECT 1 FROM ps_grads WHERE pool=? AND mint=? AND received_at>=? AND received_at<? LIMIT 1",
                         (pool, mint, lo, hi)).fetchone() is not None


def _ps_cut_slot(desk_dir, hi):
    slots=[]
    day=datetime.fromtimestamp(hi,timezone.utc).date()
    for d in (day,day.fromordinal(day.toordinal()-1)):
        path=Path(desk_dir)/'blocks'/(d.isoformat()+'.db')
        if not path.is_file():
            continue
        with closing(_ro(path)) as c:
            row=c.execute('SELECT slot FROM blocks WHERE received_at<? ORDER BY slot DESC LIMIT 1',(hi,)).fetchone()
        if row is not None:
            if type(row[0]) is not int or row[0]<=0:
                raise TapeUnreadable('ps_block_cut_unknown')
            slots.append(row[0])
    if not slots:
        raise TapeUnreadable('ps_block_cut_unknown')
    return max(slots)+1


def _ps(desk_dir, mint, pool, lo, hi, sol_usd, *, base_decimals=6, integrity=False):
    path = PT.tape_path(desk_dir)
    if not path.exists():
        return [], []
    cut_slot=_ps_cut_slot(desk_dir,hi) if integrity else None
    with closing(_ro(path)) as c:
        # PT's normalization fetches all matching rows: prove its input is bounded before calling it.
        identities = PT.window_rows(c, "mint,quote_mint", pool, lo, hi, limit=MAX_ROWS + 1)
        if len(identities) > MAX_ROWS:
            raise TapeUnreadable("row_limit")
        # PT omits explicit identity conflicts. A dropped swap is missing evidence,
        # never proof of zero activity; NULL labels still inherit the canonical PDA.
        if any(m not in (None, mint) or q not in (None, PT.WSOL_MINT) for m, q in identities):
            raise TapeUnreadable("ps_identity_unknown")
        if not identities:
            return [], []
    try:
        # Existing RISK cohorts keep their conservative global decoder policy. New chart/facts cohorts opt into scope.
        losses = PSL.losses(desk_dir, pool, lo, hi, max_rows=MAX_ROWS, scoped=integrity)
    except PSL.LossUnreadable as exc:
        raise TapeUnreadable('row_limit' if 'row_limit' in str(exc) else str(exc)) from None
    rows, gaps = PT.load_ps_swaps(desk_dir, mint, lo, hi, sol_usd, with_gaps=True,
                                    base_decimals=base_decimals,integrity=integrity,cut_slot=cut_slot)
    if not rows:
        raise TapeUnreadable("ps_identity_unknown")
    gaps = list(gaps) + losses
    gaps += [r["received_at"] for r in rows if r.get("price_only")]
    return [r for r in rows if not r.get("price_only")], gaps


def _proof(proof_fn, desk_dir, now, venue, lookback, *, pool=None, integrity=False):
    proof = proof_fn(desk_dir, now, venues=(venue,), window_s=0 if integrity and venue=='ps' else lookback,
                     **({'pool':pool,'integrity':True} if integrity and venue=='ps' else {}))
    if not isinstance(proof, dict):
        return None
    start, end = proof.get("from"), proof.get("through")
    if not all(R._finite_number(v) for v in (start, end)) or not 0 <= start <= end <= now or now - end > 5:
        return None
    return {"from": start, "through": end}


def evidence_of(pos, *, desk_dir, now, sol_usd, fill_sigs_of, sb, proof_fn):
    """Measured facts in SOL/token and SOL flow; all connections close before return. `now` and proof use host seconds.

    fill_sigs_of(mint) must return a complete nonempty verified own-fill list for this open position. Global gaps/stop spans are
    the proof_fn's responsibility; this reader additionally clips coverage past canonical/unknown-pool decode and continuity losses.
    """
    opened, entry, venue = None, None, None

    def blind(reason):
        ev = R.risk_evidence([], as_of=now, opened_ts=opened, entry_price=entry, sb=sb)
        ev.update(fresh=False, venue=venue)
        ev["unknown"] = sorted(set(ev["unknown"] + [reason]))
        return ev

    try:
        if not R._finite_number(now) or now < R.SB_LATE_S:
            return blind("clock_unknown")
        mint = pos["mint"]
        offset = clock.offset_s()
        opened = R._ts(pos["opened_at"]) + offset
        if not isinstance(mint, str) or not mint or not all(R._finite_number(v) for v in (offset, opened)) or not 0 <= opened <= now:
            return blind("position_unknown")
        ps_inputs = pos.get("desk_ps_inputs")
        if ps_inputs is None and "desk_base_decimals" in pos:
            ps_inputs = "pumpswap-facts-v1"  # Older strict position shapes cannot become legacy evidence.
        if ps_inputs not in (None, "pumpswap-facts-v1", "pumpswap-facts-v2"):
            return blind("ps_inputs_version_unknown")
        integrity = ps_inputs is not None
        entry = R.fill_baseline(pos, sol_usd)
        try:
            raw_sigs = fill_sigs_of(mint) if fill_sigs_of is not None else ()
            sigs = tuple(raw_sigs) if isinstance(raw_sigs, (tuple, list, set, frozenset)) else ()
        except Exception:
            return blind("ours_unknown")
        if not sigs or not all(isinstance(s, str) and s.strip() for s in sigs):
            return blind("ours_unknown")
        # A since-fill anchor must precede the fill; clipping at opened drops it.
        # Search only the retained hour, then clip reads to the proven venue run.
        lo = now - MAX_LOOKBACK_S
        lookback = now - lo
        curve_proof = _proof(proof_fn, desk_dir, now, "curve", lookback)
        pool = PT.canonical_pool(mint)
        base_decimals = pos.get('desk_base_decimals') if integrity else 6
        ps_proof = _proof(proof_fn, desk_dir, now, "ps", lookback,pool=pool,integrity=integrity)
        rows, losses = [], []
        if ps_proof is not None:
            ps_lo = max(lo, ps_proof["from"])
            ps_cut = math.floor(min(now - R.SB_LATE_S, ps_proof["through"]))
            rows, losses = _ps(desk_dir, mint, pool, ps_lo, ps_cut,
                               sol_usd if R._finite_number(sol_usd) and sol_usd > 0 else None,
                               base_decimals=base_decimals,integrity=integrity)
        if rows:
            venue, proof, lo, cut = "ps", ps_proof, ps_lo, ps_cut
        else:
            if curve_proof is None:
                return blind("capture_unproven")
            venue, proof = "curve", curve_proof
            lo = max(lo, proof["from"])
            cut = math.floor(min(now - R.SB_LATE_S, proof["through"]))
            rows = _curve(desk_dir, mint, lo, cut)
            if any(r["quote_mint"] != SOL_QUOTE for r in rows):
                return blind("curve_quote_unknown")
            grad = _graduated(desk_dir, mint, pool, lo, cut)
            if rows:
                real = rows[-1]["real_quote"]
                if type(real) is not int or real < 0:
                    if not grad:
                        return blind("curve_state_unknown")
                elif real >= PT.GRADUATION_REAL_SOL:
                    grad = True
            if grad:
                venue = "ps"
                return blind("graduated_no_ps")
            if not rows:
                return blind("venue_unproven")
        if len(rows) > MAX_ROWS:
            return blind("row_limit")
        coverage = {"from": max(lo, proof["from"]), "through": proof["through"]}
        if any(not R._finite_number(at) or not lo <= at < cut for at in losses):
            return blind("loss_time_unknown")
        if losses:
            coverage["from"] = max(coverage["from"], math.nextafter(max(losses), math.inf))
        ev = R.risk_evidence(rows, as_of=now, opened_ts=opened, entry_price=entry, sb=sb,
                             coverage=coverage, ours_sigs=frozenset(sigs), ours_complete=True)
        ev.update(fresh=True, venue=venue)
        return ev
    except TapeUnreadable as exc:
        return blind(str(exc))
    except Exception as exc:
        return blind("reader_error:" + type(exc).__name__)
