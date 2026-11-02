#!/usr/bin/env python3
"""Freeze arm-independent, conditional reserve outcomes; unknown evidence stays censored."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sniper.services.memecoin_desk_safety import MAYHEM_AGENT_IDS  # noqa: E402

HORIZONS = (60, 300)
DELAY_S, SILENCE_S = 1.5, 10.0
CLIP_USD, STRESS_PP = Decimal("25"), Decimal("2.5")
SOL_QUOTES = {"1" * 32, "So11111111111111111111111111111111111111112"}
COLUMNS = ("received_at", "slot", "tx_index", "ordinal", "signature", "mint", "actor",
           "quote_mint", "virtual_quote", "virtual_token", "real_quote", "real_token",
           "fee_bps", "creator_fee_bps", "cashback_fee_bps")


class Censor(ValueError):
    """A required observation is missing or incompatible with this model."""


@contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _epoch(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.timestamp()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def verify_fx(case, proof, result_json):
    """Verify the pre-label amendment against the original frozen result, not today's FX."""
    try:
        if (_hash(case["sheet"].encode()) != case["sheet_sha256"]
                or proof["call_id"] != case["call_id"]
                or proof["sheet_sha256"] != case["sheet_sha256"]
                or _hash(result_json.encode()) != proof["result_json_sha256"]):
            return None, "fx_provenance_mismatch"
        meta = json.loads(result_json)["asof"]["meta"]
        frozen = proof["asof"]
        if any(type(frozen.get(k)) not in (int, float) for k in ("sol_usd", "sol_age_s", "as_of")):
            return None, "fx_invalid"
        fx = Decimal(str(frozen["sol_usd"]))
        if not fx.is_finite() or fx <= 0:
            return None, "fx_invalid"
        if not math.isfinite(frozen["sol_age_s"]) or not 0 <= frozen["sol_age_s"] <= 120:
            return None, "fx_stale"
        # Original as_of is a binary float, call_started_at an ISO microsecond timestamp.
        if not math.isfinite(frozen["as_of"]) or frozen["as_of"] > _epoch(case["call_started_at"]) + 1e-6:
            return None, "fx_future"
        if any(meta.get(k) != v for k, v in frozen.items()):
            return None, "fx_provenance_mismatch"
        if not frozen.get("version") or not frozen.get("snapshot_hash"):
            return None, "fx_provenance_mismatch"
        return fx, None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None, "fx_unproven"


def _fees(state):
    if not isinstance(state.get("actor"), str) or not state["actor"]:
        raise Censor("fee_identity_unknown")
    if state.get("actor") in MAYHEM_AGENT_IDS:
        raise Censor("agent_fee_not_transferable")
    fees = [state.get(k) for k in ("fee_bps", "creator_fee_bps", "cashback_fee_bps")]
    if any(v is None for v in fees):
        raise Censor("fee_unknown")
    if any(type(v) is not int or not 0 <= v < 10_000 for v in fees) or sum(fees) >= 10_000:
        raise Censor("fee_invalid")
    return fees


def _state(state):
    quote = state.get("quote_mint")
    if quote is None or quote in ("", "unknown"):
        raise Censor("quote_unit_unknown")
    if quote not in SOL_QUOTES:
        raise Censor("non_sol_quote")
    values = [state.get(k) for k in ("virtual_quote", "virtual_token", "real_quote", "real_token")]
    if any(v is None for v in values):
        raise Censor("reserve_unknown")
    if any(type(v) is not int or not 0 <= v < 2**64 for v in values) or not all(values[:2]):
        raise Censor("reserve_invalid")
    if values[2] > values[0] or values[3] > values[1]:
        raise Censor("reserve_invalid")
    if values[3] == 0:
        raise Censor("curve_complete")
    return values


def _charges(amount, fees):
    return [(amount * rate + 9_999) // 10_000 for rate in fees]


def buy_exact(state, spend_atomic):
    """Pump exact-in integer rounding: separately ceil fees, price net minus one atom."""
    vq, vt, _rq, rt = _state(state)
    fees = _fees(state)
    if type(spend_atomic) is not int or not 0 < spend_atomic < 2**64:
        raise Censor("clip_invalid")
    net = spend_atomic * 10_000 // (10_000 + sum(fees))
    while net > 0 and net + sum(_charges(net, fees)) > spend_atomic:
        net -= 1
    if net <= 1:
        raise Censor("clip_too_small")
    tokens = vt * (net - 1) // (vq + net - 1)
    if tokens <= 0:
        raise Censor("clip_too_small")
    if tokens >= rt:
        raise Censor("entry_completes_curve")
    return {"spend_atomic": spend_atomic, "curve_in_atomic": net, "tokens_atomic": tokens,
            "fee_parts_atomic": _charges(net, fees)}


def sell_exact(state, entry):
    """Whole-quantity sell after applying our fixed entry reserve displacement."""
    vq, vt, rq, rt = _state(state)
    fees = _fees(state)
    tokens, net = entry["tokens_atomic"], entry["curve_in_atomic"]
    if vt <= tokens or rt <= tokens:
        raise Censor("own_displacement_invalid")
    # Own buy removed these tokens; returning them makes denominator exactly observed vt.
    gross = tokens * (vq + net) // ((vt - tokens) + tokens)
    if gross > rq + net:
        raise Censor("real_quote_short")
    charges = _charges(gross, fees)
    if gross - sum(charges) <= 0:
        raise Censor("sell_net_zero")
    return {"gross_atomic": gross, "out_atomic": gross - sum(charges), "fee_parts_atomic": charges}


def _order(row):
    return row["slot"], row.get("tx_index") or 0, row["ordinal"]


def _asof(rows, at):
    available = [r for r in rows if r["received_at"] <= at]
    if not available:
        raise Censor("state_missing")
    last_slot = max(r["slot"] for r in available)
    same_slot = [r for r in available if r["slot"] == last_slot]
    if (len({r["signature"] for r in same_slot}) > 1
            and any(r.get("tx_index") is None for r in same_slot)):
        raise Censor("ambiguous_slot_order")
    if len({(r.get("tx_index"), r["signature"]) for r in same_slot}) != len({r.get("tx_index") for r in same_slot}):
        raise Censor("ambiguous_slot_order")
    if len({(*_order(r), r["signature"]) for r in same_slot}) != len(same_slot):
        raise Censor("duplicate_state")
    if len({_order(r) for r in same_slot}) != len({r["signature"] + ":" + str(r["ordinal"]) for r in same_slot}):
        raise Censor("ambiguous_slot_order")
    selected = max(same_slot, key=_order)
    if at - selected["received_at"] >= SILENCE_S:
        raise Censor("coin_silence")
    return selected


def _endpoint(rows, gaps, entry, start, end):
    after = [r for r in rows if r["received_at"] >= end]
    if not after or min(r["received_at"] for r in after) - end >= SILENCE_S:
        raise Censor("endpoint_unbracketed")
    terminal = _asof(rows, end)
    if _order(terminal) < _order(entry):
        raise Censor("causal_order_invalid")
    if any(entry["received_at"] < g["at"] <= end for g in gaps):
        raise Censor("tape_gap")
    times = sorted({r["received_at"] for r in rows if entry["received_at"] <= r["received_at"] <= min(r["received_at"] for r in after)})
    if any(b - a >= SILENCE_S and a < end and b > start for a, b in zip(times, times[1:])):
        raise Censor("coin_silence")
    for row in rows:
        if start < row["received_at"] <= end and _order(entry) <= _order(row) <= _order(terminal):
            _state(row)
    return terminal


def _evidence(row):
    return {k: row.get(k) for k in ("slot", "tx_index", "ordinal", "signature", "received_at", "quote_mint")}


def grade_case(case, rows, gaps, fx, *, read_error=None, horizons=HORIZONS):
    start = _epoch(case["call_started_at"]) + DELAY_S
    result = {k: case[k] for k in ("call_id", "ticket_id", "mint", "sheet_sha256")}
    result.update(entry_at=start, clip_usd=25, frozen_sol_usd=str(fx) if fx is not None else None,
                  stress_haircut_pp=2.5, scope="conditional_reserve_model", outcomes={})
    error, entry_state, entry = read_error, None, None
    if not error:
        try:
            if fx is None or not fx.is_finite() or fx <= 0:
                raise Censor("fx_unproven")
            entry_state = _asof(rows, start)
            if any(entry_state["received_at"] < g["at"] <= start for g in gaps):
                raise Censor("tape_gap")
            with localcontext() as ctx:
                ctx.prec = 60
                spend = int(CLIP_USD / fx * 10**9)
            entry = buy_exact(entry_state, spend)
            result["entry"] = {**_evidence(entry_state), **{k: str(v) if type(v) is int else v for k, v in entry.items()}}
        except Censor as exc:
            error = str(exc)
    for horizon in horizons:
        out = {"endpoint_at": start + horizon, "state": "censored", "reason": error,
               "return_pct": None, "stress_return_pct": None}
        if error is None:
            try:
                terminal = _endpoint(rows, gaps, entry_state, start, out["endpoint_at"])
                sale = sell_exact(terminal, entry)
                with localcontext() as ctx:
                    ctx.prec = 60
                    unspent = CLIP_USD - Decimal(entry["spend_atomic"]) / 10**9 * fx
                    proceeds = Decimal(sale["out_atomic"]) / 10**9 * fx + unspent
                    ret = (proceeds / CLIP_USD - 1) * 100
                out.update(state="modeled", reason=None, return_pct=float(ret),
                           stress_return_pct=float(ret - STRESS_PP),
                           exit={**_evidence(terminal), **{k: str(v) if type(v) is int else v for k, v in sale.items()}})
            except Censor as exc:
                out["reason"] = str(exc)
        result["outcomes"][str(horizon)] = out
    return result


def load_window(desk_dir, mint, lo, hi, *, row_limit=20_000):
    """Bound every query by mint and receive window; absent columns remain NULL."""
    rows, gaps, error = [], [], None
    day = datetime.fromtimestamp(lo, timezone.utc).date()
    last = datetime.fromtimestamp(hi, timezone.utc).date()
    while day <= last:
        tape = Path(desk_dir) / "swaps" / f"{day}.db"
        gap_file = Path(desk_dir) / "gaps" / f"{day}.db"
        if not tape.is_file():
            return rows, gaps, "missing_tape"
        try:
            with readonly(tape) as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(swaps)")}
                if not {"mint", "received_at", "slot", "ordinal", "signature"} <= cols:
                    return rows, gaps, "missing_tape_schema"
                selection = ",".join(k if k in cols else "NULL AS " + k for k in COLUMNS)
                rows += [dict(r) for r in conn.execute(
                    "SELECT " + selection + " FROM swaps WHERE mint=? AND received_at>=? AND received_at<=? LIMIT ?",
                    (mint, lo, hi, row_limit + 1 - len(rows)))]
            if len(rows) > row_limit:
                return rows, gaps, "row_limit"
            if not gap_file.is_file():
                error = "missing_gap_evidence"
            else:
                with readonly(gap_file) as conn:
                    gaps += [dict(r) for r in conn.execute("SELECT at,cause FROM gaps WHERE at>=? AND at<=?", (lo, hi))]
        except sqlite3.Error:
            return rows, gaps, "tape_read_error"
        day += timedelta(days=1)
    return rows, gaps, error


def run(manifest_path, fx_path, desk_db, desk_dir, output):
    manifest_bytes, fx_bytes = Path(manifest_path).read_bytes(), Path(fx_path).read_bytes()
    cases = json.loads(manifest_bytes)["cases"]
    proofs = json.loads(fx_bytes)["rows"]
    if len({c["call_id"] for c in cases}) != len(cases) or len({p["call_id"] for p in proofs}) != len(proofs):
        raise ValueError("duplicate case/provenance identity")
    by_call, records = {p["call_id"]: p for p in proofs}, []
    with readonly(desk_db) as conn:
        for case in cases:
            original = conn.execute("SELECT ticket_id,call_started_at,result_json FROM desk_judge_calls WHERE call_id=?", (case["call_id"],)).fetchone()
            if original is None or original["ticket_id"] != case["ticket_id"] or original["call_started_at"] != case["call_started_at"]:
                fx, error = None, "fx_source_identity_mismatch"
            else:
                fx, error = verify_fx(case, by_call.get(case["call_id"]), original["result_json"])
            start = _epoch(case["call_started_at"]) + DELAY_S
            record = {"outcomes": {}}
            for horizon in HORIZONS:
                rows, gaps, read_error = load_window(desk_dir, case["mint"], start - SILENCE_S, start + horizon + SILENCE_S)
                graded = grade_case(case, rows, gaps, fx, read_error=error or read_error, horizons=(horizon,))
                record["outcomes"].update(graded.pop("outcomes"))
                record.update(graded)
            record.update(manifest_sha256=_hash(manifest_bytes), fx_provenance_sha256=_hash(fx_bytes),
                          result_json_sha256=(by_call.get(case["call_id"]) or {}).get("result_json_sha256"))
            records.append(record)
    # Exclusive creation freezes labels; an existing set must never be overwritten after arm results.
    with Path(output).open("x") as fh:
        for record in records:
            fh.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = ROOT / "data/research/2026-10-26-judge-speed-replay"
    parser.add_argument("--manifest", type=Path, default=base / "manifest.json")
    parser.add_argument("--fx-provenance", type=Path, default=base / "fx-provenance.json")
    parser.add_argument("--desk-db", type=Path, default=ROOT / "data/memecoin_desk.db")
    parser.add_argument("--desk-dir", type=Path, default=ROOT / "data/desk")
    parser.add_argument("--output", type=Path, default=base / "outcomes.jsonl")
    args = parser.parse_args()
    records = run(args.manifest, args.fx_provenance, args.desk_db, args.desk_dir, args.output)
    from collections import Counter
    print(json.dumps({"cases": len(records), "outcomes": dict(Counter(
        o["reason"] or o["state"] for r in records for o in r["outcomes"].values())),
        "output_sha256": _hash(args.output.read_bytes())}, sort_keys=True))


if __name__ == "__main__":
    main()
