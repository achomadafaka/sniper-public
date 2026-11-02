"""Bounded read-only native-curve input for the forward exit observer; no quote/provider calls."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_risk as R
from sniper.services import memecoin_desk_risk_tape as RT
from sniper.util import clock

MAX_ROWS = RT.MAX_ROWS
MAX_LOOKBACK_S = RT.MAX_LOOKBACK_S
_RAW_FIELDS = ("received_at", "signature", "is_buy", "quote_atomic", "token_atomic", "actor",
               "virtual_quote", "virtual_token", "real_quote", "fee_bps", "creator_fee_bps",
               "cashback_fee_bps", "slot", "tx_index", "ordinal", "quote_mint", "curve")
_FEE_FIELDS = {"fee_bps", "creator_fee_bps", "cashback_fee_bps"}
_BUSY_FIELDS = ("close_claim_id", "close_sig", "close_pending_at", "derisk_claim_id")


def _aware(iso):
    value = datetime.fromisoformat(iso)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock_naive")
    return value.timestamp()


def _positive(value):
    return R._finite_number(value) and value > 0


def _quantity(pos, quote, corrected_now):
    if not isinstance(quote, dict):
        return None, "quote_missing"
    if (any(pos.get(k) for k in _BUSY_FIELDS)
            or (not pos.get("derisked_at") and (pos.get("derisk_sig") or pos.get("derisk_pending_at")))):
        return None, "position_busy"
    if pos.get("open_qty_known") is not True or not _positive(pos.get("open_qty")):
        return None, "residual_unknown"
    if (quote.get("position_id") != pos.get("id") or quote.get("mint") != pos.get("mint")
            or quote.get("network_id") != pos.get("network_id") or quote.get("seam") != "exit_decision"):
        return None, "quote_identity"
    try:
        quote_times = []
        for field in ("observed_at", "quoted_at"):
            quote_times.append(_aware(quote.get(field)))
            age = corrected_now - quote_times[-1]
            if not 0 <= age <= R.MARK_MAX_AGE_S:
                return None, "quote_clock"
        try:
            partial_times = [_aware(pos[k]) for k in ("derisked_at", "_exit_observer_partial_at")
                             if pos.get(k) is not None]
        except (TypeError, ValueError, OverflowError):
            return None, "partial_clock_unknown"
        if partial_times and min(quote_times) < max(partial_times):
            return None, "quote_before_partial"
        qty, expected, unit = (quote.get(k) for k in ("open_qty", "expected_usdc", "unit_price"))
        if (not all(_positive(v) for v in (qty, expected, unit))
                or not math.isclose(qty, pos["open_qty"], rel_tol=1e-12, abs_tol=0)
                or not math.isclose(unit, expected / qty, rel_tol=1e-12, abs_tol=0)):
            return None, "quote_size_or_value"
        evidence = json.loads(quote.get("quote_evidence_json"))
        atoms = evidence.get("input_atomic")
        if (not isinstance(atoms, str) or re.fullmatch(r"[1-9][0-9]{0,19}", atoms) is None
                or int(atoms) >= 2**64):
            return None, "atomic_quantity_unknown"
        # Compare at the retained REAL field's precision; never reconstruct atoms from that float.
        if float(Decimal(atoms).scaleb(-6)) != qty:
            return None, "atomic_quantity_mismatch"
        return atoms, None
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None, "quote_invalid"


def _rows(desk_dir, mint, lo, cut):
    rows = []
    for day in RT._days(lo, cut):
        with closing(RT._ro(Path(desk_dir) / "swaps" / (day + ".db"))) as conn:
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute("PRAGMA table_info(swaps)")}
            if not (set(_RAW_FIELDS) - _FEE_FIELDS) <= columns:
                raise RT.TapeUnreadable("curve_schema_unknown")
            fields = ",".join(k if k in columns else "NULL AS " + k for k in _RAW_FIELDS)
            # A retained day can contain much more than this mint's hour. Bound SQLite work too.
            steps = iter(range(500))
            conn.set_progress_handler(lambda: next(steps, None) is None, 1000)
            if conn.execute("SELECT 1 FROM swaps WHERE mint=? AND (received_at IS NULL "
                            "OR typeof(received_at) NOT IN ('integer','real') "
                            "OR received_at-received_at IS NULL) LIMIT 1", (mint,)).fetchone():
                raise RT.TapeUnreadable("curve_time_unknown")
            rows.extend(dict(r) for r in conn.execute(
                "SELECT " + fields + " FROM swaps WHERE mint=? AND received_at>=? AND received_at<? "
                "ORDER BY received_at LIMIT ?", (mint, lo, cut, MAX_ROWS + 1 - len(rows))))
        if len(rows) > MAX_ROWS:
            raise RT.TapeUnreadable("row_limit")
    return sorted(rows, key=lambda r: r["received_at"])


def _launch(desk_dir, mint, rows, lo, cut):
    if not rows:
        return None
    first = rows[0]
    for day in RT._days(lo, cut):
        path = Path(desk_dir) / "launches" / (day + ".db")
        if not path.is_file():
            continue
        with closing(RT._ro(path)) as conn:
            found = conn.execute("SELECT received_at,curve,slot,signature,creator FROM launches WHERE mint=? LIMIT 1", (mint,)).fetchone()
        if found is None:
            continue
        at, curve, slot, sig, creator = found
        if (R._finite_number(at) and lo <= at <= first["received_at"] < cut
                and isinstance(curve, str) and curve and curve == first["curve"]
                and type(slot) is int and type(first["slot"]) is int and slot <= first["slot"]
                and isinstance(sig, str) and sig.strip() and isinstance(creator, str) and creator.strip()):
            return first["received_at"]
    return None


def read_snapshot(pos, quote, *, desk_dir, now, fill_sigs_of, proof_fn):
    """Core input plus separate source/quote audit metadata. All core clocks are host receipt seconds.

    Own signatures must be a complete verified nonempty collection. Absent quote/launch proof does not
    manufacture quantities or creators; no live call or writable connection is opened here.
    """
    snapshot = dict(rows=[], cut=None, coverage_from=None, opened_at=None, residual_atomic=None,
                    own_signatures=[], own_complete=False, launch_at=None, quantity_reason=None,
                    source_reason=None, venue=None, exact_quote=quote, clocks={"host_now": now})
    try:
        offset = clock.offset_s()
        if not R._finite_number(now) or not R._finite_number(offset) or now < R.SB_LATE_S:
            raise RT.TapeUnreadable("clock_unknown")
        opened = _aware(pos.get("opened_at")) + offset
        snapshot["clocks"].update(offset_s=offset, corrected_now=now - offset)
        if not 0 <= opened <= now:
            raise RT.TapeUnreadable("position_clock_unknown")
        snapshot["opened_at"] = opened
        atoms, why = _quantity(pos, quote, now - offset)
        snapshot.update(residual_atomic=atoms, quantity_reason=why)
        try:
            sigs = fill_sigs_of(pos["mint"]) if fill_sigs_of is not None else None
            if (isinstance(sigs, (list, tuple, set, frozenset)) and sigs
                    and all(isinstance(s, str) and s.strip() for s in sigs)):
                snapshot.update(own_signatures=sorted(set(sigs)), own_complete=True)
        except Exception:
            pass
        mint = pos.get("mint")
        if pos.get("network_id") != "solana" or not isinstance(mint, str) or not mint:
            raise RT.TapeUnreadable("position_identity_unknown")
        proof = RT._proof(proof_fn, desk_dir, now, "curve", MAX_LOOKBACK_S)
        if proof is None:
            raise RT.TapeUnreadable("capture_unproven")
        lo = max(0, now - MAX_LOOKBACK_S, proof["from"])
        cut = math.floor(min(now - R.SB_LATE_S, proof["through"]))
        if lo >= cut:
            raise RT.TapeUnreadable("capture_unproven")
        snapshot.update(cut=cut, coverage_from=lo, venue="curve")
        snapshot["clocks"].update(committed_through=proof["through"], continuous_from=proof["from"])
        rows = _rows(desk_dir, mint, lo, cut)
        if not rows:
            raise RT.TapeUnreadable("venue_unproven")
        if any(r["quote_mint"] != RT.SOL_QUOTE for r in rows):
            raise RT.TapeUnreadable("curve_quote_unknown")
        real = rows[-1]["real_quote"]
        graduated = RT._graduated(desk_dir, mint, PT.canonical_pool(mint), now - MAX_LOOKBACK_S, cut)
        if graduated or (type(real) is int and real >= PT.GRADUATION_REAL_SOL):
            snapshot["venue"] = "ps"
            raise RT.TapeUnreadable("graduated_unsupported")
        if type(real) is not int or real < 0:
            raise RT.TapeUnreadable("curve_state_unknown")
        snapshot["rows"] = rows
        try:
            snapshot["launch_at"] = _launch(desk_dir, mint, rows, lo, cut)
        except Exception:
            snapshot["launch_at"] = None
        return snapshot
    except RT.TapeUnreadable as exc:
        reason = str(exc)
    except Exception as exc:
        reason = "reader_error:" + type(exc).__name__
    snapshot.update(rows=[], coverage_from=None, source_reason=reason)
    return snapshot
