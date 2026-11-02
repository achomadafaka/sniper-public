"""Closed, causal PumpSwap facts for a new paper cohort. Local indexed reads only.

The already-refreshed candidate owns FX/decimals/slot. Market windows end at receipt_cut;
as_of is the later claim/knowledge clock. Neither clock is re-read here. Captured history,
on-chain creation and provider-reported creation are deliberately separate facts.
"""
from __future__ import annotations

import copy
import math
import re
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_structure as S
from sniper.services import memecoin_desk_structure_tape as ST

VERSION = "pumpswap-facts-v2"
LEGACY_VERSION = "pumpswap-facts-v1"
VERSION3 = "pumpswap-facts-v3"      # cohort D: chart from observed prints, losses counted beside, never a veto
V2PLUS = (VERSION, VERSION3)
OWN_LOSS = ("own_pool_losses",)
UNATTRIBUTED_LOSS = ("unknown_loss_receipts", "global_loss_receipts")
CHART_WINDOWS = (15, 60, 300)
CHART_NULL_REASONS = ("prints_lt_2",)
CHART_DEFINITION = {"version": VERSION3, "windows": ["15s", "60s", "300s"], "null_reasons": list(CHART_NULL_REASONS),
                    "min_prints": 2, "own_pool_losses": "continuity gaps + own_pool_losses receipts in [cut-w, cut)",
                    "unattributed_loss_receipts": "DISTINCT (signature, cause) receipts of unknown_loss_receipts + "
                                              "global_loss_receipts received in [cut-w, cut): a receipt count, each "
                                              "MAY hold this pool's swaps; not an estimate of missing swaps",
                    "rule": "fields from observed prints when prints_n >= 2; losses never null; a window is null only on "
                            "prints_lt_2; a failed loss read makes the whole block unknown:loss_read_failed",
                    "field_unknown": {"trend": "same_timestamp (fewer than 2 distinct print times)",
                                      "pos_in_range": "zero_range (high == low; range 0 stays a real 0)",
                                      "breakout": "no_reference (no earlier high in the window; 15s breakout is "
                                                  "always null by definition)"},
                    "always_defined_when_prints_ge_2": ["range", "return_pct"],
                    "fields": {"trend": "slope_pct_per_min", "range": "range_pct", "pos_in_range": "pos_in_range_pct",
                               "return_pct": "return_pct", "breakout": "crossed (60s/300s; null in 15s)"}}
# the one D hashing rule (Alpha 2026-10-28): canonical compact JSON, UTF-8
CHART_DEFINITION_SHA256 = __import__("hashlib").sha256(__import__("json").dumps(
    CHART_DEFINITION, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
_CHART_WINDOW_KEYS = {"trend", "range", "pos_in_range", "return_pct", "breakout", "prints_n", "unattributed_loss_receipts",
                      "own_pool_losses", "partial", "lower_bound", "null_reason", "field_unknown"}
CHART_FIELD_REASONS = {"trend": "same_timestamp", "pos_in_range": "zero_range", "breakout": "no_reference"}
MAX_ROWS = 20000
LOOKBACK_S = 3600
WINDOWS = {"15s": 15, "60s": 60, "300s": 300, "1h": 3600}
_HEADER = {"version", "mint", "pool", "snapshot_hash", "as_of", "receipt_cut", "cut_slot", "flow_60s",
           "price_vs_60s_low_pct", "price_vs_60s_high_pct", "price_vs_1h_high_pct", "age_s", "pool_age_s",
           "ages", "coverage", "creator_prior_launches", "creator_history", "structure"}
_HEADER3 = _HEADER | {"chart"}
_AGE_KEYS = {"state", "reason", "basis", "age_s", "occurred_at", "known_at"}
_AGE_BASES = {"coin": "onchain_creation_block_time", "pool": "onchain_pool_creation_block_time",
              "observed": "captured_receipt_span", "provider_reported": "pumpfun_created_timestamp_ms"}
_AGE_REASONS = {"creation_not_retained", "pool_creation_not_retained", "creation_unreadable", "creation_unusable",
                "clock_unknown", "clock_malformed", "creation_unknown", "creation_future", "metadata_future",
                "metadata_unknown", "source_unavailable", "history_not_captured", "no_swaps"}
_COVERAGE_REASONS = {"inclusion_unknown", "inclusion_partial", "coverage_unreadable", "tape_unreadable", "tape_loss"}


def _num(value, low=None, high=1e15):
    return K._number(value, minimum=low, maximum=high)


def _unknown(kind, reason):
    return dict(state="unknown", reason=reason, basis=_AGE_BASES[kind], age_s=None, occurred_at=None, known_at=None)


def _age(kind, occurred, known, now, reason="creation_unusable"):
    if not _num(occurred, 0) or occurred <= 0 or not _num(known, 0):
        return _unknown(kind, reason)
    if occurred > now:
        return _unknown(kind, "creation_future")
    return dict(state="known", reason=None, basis=_AGE_BASES[kind], age_s=now - occurred,
                occurred_at=occurred, known_at=known)


def _clock(source_metadata, as_of, decision_as_of=None):
    """(utc_now at the cut, reason, age_clock). Cohort D Amendment 22 (Gamma eb237c128): with decision_as_of, metadata
    observed after the cut but by the decision time is accepted for IMMUTABLE creation fields only (the callers use
    nothing else from it); the age itself is still taken at the cut. Anything known after the decision is refused."""
    if (not isinstance(source_metadata, dict) or not isinstance(source_metadata.get("source"), str)
            or source_metadata["source"] not in K.INTAKE_FEEDS):
        return None, "clock_unknown", None
    if decision_as_of is not None and (type(decision_as_of) not in (int, float) or not _num(decision_as_of, as_of)):
        return None, "clock_malformed", None      # Delta HOLD 7e04cc470: a non-finite or pre-cut decision is refused
    bound = as_of if decision_as_of is None else decision_as_of
    known = source_metadata.get("known_at")
    if _num(known, 0) and known > bound:
        return None, "metadata_future", None
    offset = source_metadata.get("clock_offset_s")
    if offset is None:
        return None, "clock_unknown", None
    observed, received = source_metadata.get("observed_at"), source_metadata.get("host_received_at")
    if (not _num(offset, -86400, 86400) or not _num(known, 0) or not _num(observed, 0)
            or not _num(received, 0) or received > known or received > bound
            or abs((received - offset) - observed) > 1e-3):
        return None, "clock_malformed", None
    return as_of - offset, None, ("cut" if known <= as_of else "decision")


def _birth_witness(desk_dir, *, slot, inline, received, known, receipt_cut, cut_slot):
    """An inline timestamp may have been backfilled. Its block receipt proves when it was known.

    Read only the exact slot PK in the creation's day and adjacent days. Older missing block
    retention remains unknown; neither an event timestamp nor a host receipt substitutes for birth.
    """
    if (type(slot) is not int or not 0 < slot < cut_slot or not _num(received, 0, receipt_cut)
            or not _num(known, received, receipt_cut) or max(received, known) >= receipt_cut):
        raise ST.TapeUnreadable("birth_identity")
    if inline not in (None, 0) and (not _num(inline, 0) or inline <= 0):
        raise ST.TapeUnreadable("birth_inline")
    witnesses = []
    for day in sorted({ST.utc_day(received + offset) for offset in (-86400, 0, 86400)}):
        if day > ST.utc_day(receipt_cut):
            continue
        conn = ST.ro(Path(desk_dir) / "blocks" / (day + ".db"))
        if conn is None:
            continue
        try:
            schema = conn.execute("PRAGMA table_info(blocks)").fetchall()
            columns = {r[1] for r in schema}
            primary = tuple(r[1] for r in sorted(schema, key=lambda r: r[5]) if r[5])
            if (not {"slot", "block_time", "received_at"} <= columns or primary != ("slot",)
                    or next(r[2] for r in schema if r[1] == "slot").upper() != "INTEGER"):
                raise ST.TapeUnreadable("birth_block_schema")
            row = conn.execute("SELECT block_time,received_at FROM blocks WHERE slot=? LIMIT 1", (slot,)).fetchone()
        except sqlite3.Error:
            raise ST.TapeUnreadable("birth_block_read") from None
        finally:
            conn.close()
        if row is None:
            continue
        at, learned = row
        if not _num(learned, 0):
            raise ST.TapeUnreadable("birth_block_clock")
        if learned >= receipt_cut:
            continue
        if not _num(at, 0) or at <= 0:
            raise ST.TapeUnreadable("birth_block_value")
        witnesses.append((at, learned))
    if not witnesses:
        return None, known
    occurred = witnesses[0][0]
    if any(at != occurred for at, _ in witnesses) or inline not in (None, 0, occurred):
        raise ST.TapeUnreadable("birth_block_conflict")
    return occurred, max(known, min(at for _, at in witnesses))


def _creation(desk_dir, mint, receipt_cut, cut_slot):
    """Pump create rows are keyed by mint; receipt is knowledge, block_time is birth."""
    for back in range(4):
        conn = ST.ro(Path(desk_dir) / "launches" / (ST.utc_day(receipt_cut - back * 86400) + ".db"))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT block_time,received_at,slot FROM launches WHERE mint=? AND received_at<? "
                               "AND slot<?", (mint, receipt_cut, cut_slot)).fetchone()
        except sqlite3.Error:
            raise ST.TapeUnreadable("creation") from None
        finally:
            conn.close()
        if row is not None:
            return _birth_witness(desk_dir, slot=row[2], inline=row[0], received=row[1], known=row[1],
                                  receipt_cut=receipt_cut, cut_slot=cut_slot)
    return None


def _pool_creation(desk_dir, candidate, receipt_cut, cut_slot):
    conn = ST.ro(PT.tape_path(desk_dir))
    if conn is None:
        return None
    try:
        # A mapping receipt or pool creator is never the creation occurrence. Only the retained
        # canonical create event with a UTC block time can establish this separate age.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pools'").fetchone():
            return None
        columns = tuple(r[2] for r in conn.execute("PRAGMA index_info(pools_pool)"))
        if columns[:1] != ("pool",):
            raise ST.TapeUnreadable("pool_index")
        rows = conn.execute("SELECT block_time,received_at,known_at,slot FROM pools INDEXED BY pools_pool WHERE pool=? "
                            "AND mint=? AND quote_mint=? AND canonical=1 AND network_id='solana' AND venue='pumpswap' "
                            "AND received_at<? AND known_at<? AND slot<? ORDER BY received_at LIMIT 2",
                            (candidate["pool"], candidate["mint"], PT.WSOL_MINT, receipt_cut, receipt_cut, cut_slot)).fetchall()
        if len(rows) > 1:
            raise ST.TapeUnreadable("pool_creation_ambiguous")
        return (_birth_witness(desk_dir, slot=rows[0][3], inline=rows[0][0], received=rows[0][1],
                               known=rows[0][2], receipt_cut=receipt_cut, cut_slot=cut_slot) if rows else None)
    except sqlite3.Error:
        raise ST.TapeUnreadable("pool_creation") from None
    finally:
        conn.close()


def _ages(candidate, desk_dir, as_of, receipt_cut, cut_slot, metadata, rows, decision_as_of=None):
    out = {kind: _unknown(kind, "pool_creation_not_retained" if kind == "pool" else
                         "creation_not_retained" if kind == "coin" else "source_unavailable" if kind == "provider_reported"
                         else "no_swaps") for kind in _AGE_BASES}
    observed = [r["received_at"] for r in rows if not r.get("price_only")]
    if observed:
        first = min(observed)
        out["observed"] = _age("observed", first, first, as_of)
    utc_now, clock_reason, age_clock = _clock(metadata, as_of, decision_as_of)
    for kind, read in (("coin", lambda: _creation(desk_dir, candidate["mint"], receipt_cut, cut_slot)),
                       ("pool", lambda: _pool_creation(desk_dir, candidate, receipt_cut, cut_slot))):
        try:
            creation = read()
        except (ST.TapeUnreadable, OSError, ValueError):
            out[kind] = _unknown(kind, "creation_unreadable")
            continue
        if creation:
            out[kind] = (_unknown(kind, clock_reason) if utc_now is None else
                         _age(kind, creation[0], creation[1], utc_now))
    pumpfun = isinstance(metadata, dict) and metadata.get("source") == "pumpfun"
    if pumpfun:
        if utc_now is None:
            out["provider_reported"] = _unknown("provider_reported", clock_reason)
        else:
            created = metadata.get("created_timestamp")
            out["provider_reported"] = (_unknown("provider_reported", "creation_unknown") if not _num(created, 0)
                or created <= 0 else _age("provider_reported", created / 1000, metadata["known_at"], utc_now))
            if _num(created, 0) and created / 1000 > metadata["observed_at"]:
                out["provider_reported"] = _unknown("provider_reported", "creation_future")
    provider = out["provider_reported"]
    provider.update(source="pumpfun" if pumpfun else None,
                    observed_at=metadata["observed_at"] if pumpfun and utc_now is not None else None,
                    clock_offset_s=metadata["clock_offset_s"] if pumpfun and utc_now is not None else None,
                    age_clock=age_clock if pumpfun and provider["state"] == "known" else None,
                    decision_as_of=decision_as_of if pumpfun and provider["state"] == "known" and age_clock == "decision"
                    else None)
    return out


def _covered(intervals, lo, hi, lost=()):
    # Coverage is one proven-complete suffix ending at the cut, never a sum across holes.
    suffix = 0.0
    for a,b in intervals:
        if a <= hi <= b:
            start = max(a,lo)
            inside = [at for at in lost if start <= at < hi]
            if inside:
                start = math.nextafter(max(inside),math.inf)
            suffix = max(suffix,max(0.0,hi-start))
    return suffix


def _flow(rows):
    ordinary = [r for r in rows if S._flow(r, S.AGENTS)]
    buys = [r for r in ordinary if r["is_buy"]]
    sells = [r for r in ordinary if not r["is_buy"]]
    return {"buy_n": len(buys), "sell_n": len(sells), "buy_usd": sum(r["usd"] for r in buys),
            "sell_usd": sum(r["usd"] for r in sells), "makers": len({r["actor"] for r in ordinary}),
            "largest_buy": max((r["usd"] for r in buys), default=0),
            "buyers":len({r["actor"] for r in buys}), "sellers":len({r["actor"] for r in sells}), "n_swaps":len(ordinary)}


def _counts(lo, hi, own, unattributed):
    return {"own_pool_losses": sum(lo <= at < hi for at in own),
            "unattributed_loss_receipts": sum(lo <= at < hi for at in unattributed)}


def _structure(rows, receipt_cut, cut_slot, coverage, intervals, lost, bounds, params, blind, version, own=(), unattributed=()):
    out = S.structure(rows, as_of=receipt_cut, cut_slot=cut_slot, bounds=bounds, params=params,
                      lost=() if version == VERSION3 else lost, blind=blind, launch_unknown="insufficient_history")
    if out["tape"] != "ok":
        return out
    if version == VERSION3:          # what we did not see is evidence, not a veto: counts beside the observed figures
        for key, b in out["breakout"].items():
            if b.get("state") == "ok":
                b.update(_counts(receipt_cut - int(key[:-1]), receipt_cut, own, unattributed))
    for key in ("15s", "60s", "300s"):
        if version == VERSION and out["windows"][key].get("reason") == "swaps_lost":
            observed = S._window(rows,receipt_cut,int(key[:-1]),(),params,agents=S.AGENTS,lost=())
            # Only descriptive flow is recoverable; missing path-dependent price evidence stays unknown.
            for field in ("high","low","range_pct","pos_in_range_pct","return_pct","trend","delta_trend"):
                observed[field] = None
            observed.update(swaps_lost=sum(receipt_cut-int(key[:-1]) <= at < receipt_cut for at in lost),
                            flow_is_lower_bound=True)
            out["windows"][key] = observed
        out["windows"][key].update(covered_s=coverage[key]["covered_s"], partial=coverage[key]["state"] != "complete")
    if coverage["60s"]["state"] != "complete":
        out["recent_volume_concentration_15_of_60"] = None
        out["recent_volume_concentration_reason"] = "insufficient_history"
    if version == LEGACY_VERSION:
        if out.get("chart"):
            for bar in out["chart"]:
                lo = receipt_cut+bar[0]
                covered = sum(max(0.0,min(b,lo+params.bar_s)-max(a,lo)) for a,b in intervals)
                if covered < params.bar_s:
                    bar[5:] = [None,None,None]
        return out
    # Observed bar demand is a lower bound; no seen swap never becomes synthetic zero.
    if out.get("chart"):
        out["chart_coverage"] = []
        for bar in out["chart"]:
            lo = receipt_cut + bar[0]
            covered = _covered(intervals,lo,lo+params.bar_s,lost)
            partial = covered < params.bar_s
            out["chart_coverage"].append(dict(covered_s=covered,partial=partial))
            seen = any(lo <= r["received_at"] < lo+params.bar_s and not r.get("price_only") for r in rows)
            if not seen:
                bar[5:] = [None,None,None]
    return out


def _chart(structure, rows, receipt_cut, coverage, own, unattributed, blind):
    """The cohort D chart block (pumpswap-facts-v3): per window, figures from OBSERVED prints when prints_n >= 2, the
    pool's proven own losses and the unattributed loss receipts counted beside them. Null only when prints_n < 2 or
    the loss read failed (tape_unreadable), never 0."""
    failed = blind is not None or structure.get("tape") != "ok"
    if failed:                       # D Amendment 2 dc5fcf0dd: an unknown block carries no fields
        return {"state": "unknown", "reason": "loss_read_failed", "definition_sha256": CHART_DEFINITION_SHA256,
                "as_of": receipt_cut}
    windows = {}
    for w in CHART_WINDOWS:
        key = "%ds" % w
        lo = receipt_cut - w
        prints = sum(lo <= r["received_at"] < receipt_cut and _num(r.get("price_usd"), 0) and r["price_usd"] > 0
                     for r in rows)
        win = structure["windows"].get(key) or {}
        bk = {} if w == 15 else structure.get("breakout", {}).get(key) or {}
        null_reason = "prints_lt_2" if prints < 2 else None
        counts = _counts(lo, receipt_cut, own, unattributed)
        partial = coverage[key]["state"] != "complete"
        ok = null_reason is None
        item = {"trend": (win.get("trend") or {}).get("slope_pct_per_min") if ok else None,
                "range": win.get("range_pct") if ok else None,
                "pos_in_range": win.get("pos_in_range_pct") if ok else None,
                "return_pct": win.get("return_pct") if ok else None,
                "breakout": bk.get("crossed") if ok and bk.get("state") == "ok" else None,
                "prints_n": prints, **counts, "partial": partial,
                "lower_bound": bool(partial or counts["own_pool_losses"] or counts["unattributed_loss_receipts"]),
                "null_reason": null_reason, "field_unknown": {}}
        if ok:                       # D Amendment 12: an undefined value is null WITH its closed reason, never invented
            if item["trend"] is None:
                item["field_unknown"]["trend"] = "same_timestamp"
            if item["pos_in_range"] is None:
                item["field_unknown"]["pos_in_range"] = "zero_range"
            if w != 15 and item["breakout"] is None:
                if bk.get("reason") != "no_reference":
                    raise ValueError("ps_facts_chart_breakout")
                item["field_unknown"]["breakout"] = "no_reference"
        windows[key] = item
    return {"state": "known", "reason": None,
            "definition_sha256": CHART_DEFINITION_SHA256, "as_of": receipt_cut, "windows": windows}


def _validate_chart(chart, receipt_cut):
    if chart.get("state") == "unknown":
        if (not K._shape(chart, {"state", "reason", "definition_sha256", "as_of"}) or chart["reason"] != "loss_read_failed"
                or chart["definition_sha256"] != CHART_DEFINITION_SHA256 or chart["as_of"] != receipt_cut):
            raise ValueError
        return
    if (not K._shape(chart, {"state", "reason", "definition_sha256", "as_of", "windows"})
            or chart["definition_sha256"] != CHART_DEFINITION_SHA256 or chart["as_of"] != receipt_cut
            or chart["state"] not in ("known", "unknown")
            or (chart["state"] == "known") != (chart["reason"] is None)
            or chart["reason"] not in (None, "loss_read_failed")
            or not K._shape(chart["windows"], {"%ds" % w for w in CHART_WINDOWS})):
        raise ValueError
    for key, win in chart["windows"].items():
        # Gamma's invariants (D 1.6, Am2-4, Am12) for a KNOWN window
        if not K._shape(win, _CHART_WINDOW_KEYS) or not isinstance(win["field_unknown"], dict):
            raise ValueError
        if any(type(win[k]) is not int or not 0 <= win[k] <= MAX_ROWS
               for k in ("prints_n", "own_pool_losses", "unattributed_loss_receipts")):
            raise ValueError
        if type(win["partial"]) is not bool or type(win["lower_bound"]) is not bool:
            raise ValueError
        if win["lower_bound"] is not True and (win["partial"] or win["own_pool_losses"] or win["unattributed_loss_receipts"]):
            raise ValueError
        if win["null_reason"] != ("prints_lt_2" if win["prints_n"] < 2 else None):
            raise ValueError
        values = ("trend", "range", "pos_in_range", "return_pct", "breakout")
        for k in ("trend", "range", "pos_in_range", "return_pct"):
            if win[k] is not None and (type(win[k]) not in (int, float) or not math.isfinite(win[k])):
                raise ValueError
        if win["breakout"] is not None and type(win["breakout"]) is not bool:
            raise ValueError
        unknown = win["field_unknown"]
        if win["null_reason"] is not None:
            if any(win[k] is not None for k in values) or unknown:
                raise ValueError
            continue
        if win["range"] is None or win["return_pct"] is None:
            raise ValueError                                # always defined with 2 or more prints (Am12)
        if any(k not in CHART_FIELD_REASONS or CHART_FIELD_REASONS[k] != v for k, v in unknown.items()):
            raise ValueError
        for k in CHART_FIELD_REASONS:
            structural = key == "15s" and k == "breakout"   # 15s breakout is null by definition, no reason entry
            if structural:
                if win[k] is not None or k in unknown:
                    raise ValueError
            elif (win[k] is None) != (k in unknown):
                raise ValueError


def facts_for(candidate, *, desk_dir, as_of, receipt_cut, cut_slot, source_metadata=None,
              ours=frozenset(), ours_sigs=frozenset(), bounds=None, params=None, version=LEGACY_VERSION,
              decision_as_of=None):
    """A validated independent sheet; never modifies the immutable claim candidate."""
    try:
        c = K.validate_candidate(candidate)
        state = c["snapshot"]["fields"]["pool_state"]["value"]
        if (version not in (LEGACY_VERSION,VERSION,VERSION3) or c.get("venue") != "pumpswap" or not _num(as_of, 0) or not _num(receipt_cut, 0)
                or c["snapshot"]["emission_at"] != as_of or receipt_cut > as_of
                or type(cut_slot) is not int or cut_slot <= 0
                or state.get("cut_slot") != cut_slot or not _num(state.get("sol_usd"), 0)
                or state["sol_usd"] <= 0
                or decision_as_of is not None and (type(decision_as_of) not in (int, float) or not _num(decision_as_of, as_of))):
            raise ValueError
        decimals = state.get("base_decimals")
        if decimals is not None and (type(decimals) is not int or not 0 <= decimals <= 18):
            raise ValueError
        params = S.Params(sheet="v2") if params is None else params
        if params.sheet != "v2" or params.chart_s != 120 or params.bar_s != 5:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("ps_facts_candidate") from None
    lo = receipt_cut - LOOKBACK_S
    rows, intervals, lost, blind = [], [], [], None
    loss_reads, receipt_details = {}, []
    try:
        rows, continuity = PT.load_ps_swaps(desk_dir, c["mint"], lo, receipt_cut, state["sol_usd"],
            with_gaps=True, base_decimals=decimals, cut_slot=cut_slot, max_rows=MAX_ROWS, integrity=True, loss_reads=loss_reads)
        lost = sorted(set(continuity + L.losses(desk_dir, c["pool"], lo, receipt_cut,
                                               cut_slot=cut_slot, max_rows=MAX_ROWS, loss_reads=loss_reads, receipt_details=receipt_details)))
        own = sorted(continuity + [at for _, at, cat in receipt_details if cat in OWN_LOSS])
        # one receipt counts once: distinct (signature, cause), its earliest receipt time (D Amendment 4)
        first = {}
        for sig, at, cat in receipt_details:
            if cat in UNATTRIBUTED_LOSS and ((sig, cat) not in first or at < first[(sig, cat)]):
                first[(sig, cat)] = at
        unattributed = sorted(first.values())
    except (PT.TapeUnreadable, L.LossUnreadable, OSError, sqlite3.Error):
        rows, blind, own, unattributed = [], "tape_unreadable", [], []
    coverage_reason = None
    if blind is None:
        try:
            intervals = L.pool_coverage(desk_dir, c["pool"], lo, receipt_cut, max_rows=MAX_ROWS, cut_slot=cut_slot, loss_reads=loss_reads)
        except (L.LossUnreadable, OSError, sqlite3.Error):
            coverage_reason = "coverage_unreadable"
    rows = S._usable(rows, receipt_cut, cut_slot, ours, ours_sigs)
    coverage = {}
    for key, seconds in WINDOWS.items():
        start = receipt_cut - seconds
        covered = (_covered(intervals,start,receipt_cut,lost) if version in V2PLUS else
                   sum(max(0.0,min(b,receipt_cut)-max(a,start)) for a,b in intervals))
        loss = any(start <= at < receipt_cut for at in lost)
        complete = covered >= seconds and not loss and blind is None and coverage_reason is None
        reason = None if complete else (blind or coverage_reason or ("tape_loss" if loss else
                                                       "inclusion_partial" if covered else "inclusion_unknown"))
        coverage[key] = {"state": "complete" if complete else "partial" if covered else "unknown", "reason": reason,
                         "covered_s": covered, "window_s": seconds,
                         "observed_swaps": sum(start <= r["received_at"] and not r.get("price_only") for r in rows)}
    ages = _ages(c, desk_dir, as_of, receipt_cut, cut_slot, source_metadata, rows, decision_as_of)
    structure = _structure(rows, receipt_cut, cut_slot, coverage, intervals, lost, bounds, params, blind, version,
                           own, unattributed)
    out = dict(version=version, mint=c["mint"], pool=c["pool"], snapshot_hash=c["snapshot"]["hash"], as_of=as_of,
               receipt_cut=receipt_cut, cut_slot=cut_slot, flow_60s=None, price_vs_60s_low_pct=None,
               price_vs_60s_high_pct=None, price_vs_1h_high_pct=None, age_s=ages["coin"]["age_s"],
               pool_age_s=ages["pool"]["age_s"], ages=ages, coverage=coverage,
               creator_prior_launches=None, creator_history={"state": "unknown", "reason": "history_not_captured"},
               structure=structure)
    if version == VERSION3:
        out["chart"] = _chart(structure, rows, receipt_cut, coverage, own, unattributed, blind)
    seen_rows = [r for r in rows if r["received_at"] >= receipt_cut-60]
    if version == LEGACY_VERSION and coverage["60s"]["state"] == "complete":
        values = _flow(seen_rows)
        out["flow_60s"] = {key:values[key] for key in ("buy_n","sell_n","buy_usd","sell_usd","makers","largest_buy")}
    elif version in V2PLUS and any(not r.get("price_only") for r in seen_rows) and blind is None:
        flow = _flow(seen_rows)
        flow.update(covered_s=coverage["60s"]["covered_s"],partial=coverage["60s"]["state"] != "complete",
                    lower_bound=True,loss_classes_exclusive=True)
        for kind in ("unknown_loss_receipts","global_loss_receipts","own_pool_losses"):
            flow[kind] = sum(receipt_cut-60 <= at < receipt_cut and category==kind for _,at,category in receipt_details)
        out["flow_60s"] = flow
    price = structure.get("price_usd")
    for key, seconds, edge in (("price_vs_60s_low_pct", 60, min), ("price_vs_60s_high_pct", 60, max),
                               ("price_vs_1h_high_pct", 3600, max)):
        window_rows = [r for r in rows if r["received_at"] >= receipt_cut - seconds]
        values = [r["price_usd"] for r in window_rows if _num(r.get("price_usd"), 0) and r["price_usd"] > 0]
        window = "60s" if seconds == 60 else "1h"
        if coverage[window]["state"] == "complete" and values and len(values) == len(window_rows) and price is not None:
            out[key] = (price / edge(values) - 1) * 100
        elif coverage[window]["state"] == "complete" and not window_rows and price is not None:
            out[key] = 0.0          # a proven quiet window at a proven price: zero, never absent (order B)
    return validate(out)


def validate(facts):
    """Strict closed shape, finite numbers, no issuer text; return an independent copy."""
    try:
        v3 = facts.get("version") == VERSION3
        if not K._shape(facts, _HEADER3 if v3 else _HEADER) or facts["version"] not in (LEGACY_VERSION,VERSION,VERSION3):
            raise ValueError
        if v3:
            _validate_chart(facts["chart"], facts["receipt_cut"])
        if (not K._key(facts["mint"], 32) or not K._key(facts["pool"], 32)
                or not isinstance(facts["snapshot_hash"], str) or not re.fullmatch("[0-9a-f]{64}", facts["snapshot_hash"])
                or not _num(facts["as_of"], 0) or not _num(facts["receipt_cut"], 0, facts["as_of"])
                or type(facts["cut_slot"]) is not int or not 0 < facts["cut_slot"] <= 2**53):
            raise ValueError
        for key in ("price_vs_60s_low_pct", "price_vs_60s_high_pct", "price_vs_1h_high_pct", "age_s", "pool_age_s"):
            if facts[key] is not None and not _num(facts[key], 0 if key.endswith("age_s") else -100):
                raise ValueError
        if not K._shape(facts["coverage"], WINDOWS) or not K._shape(facts["ages"], _AGE_BASES):
            raise ValueError
        for key, seconds in WINDOWS.items():
            item = facts["coverage"][key]
            if (not K._shape(item, {"state", "reason", "covered_s", "window_s", "observed_swaps"})
                    or item["state"] not in ("complete", "partial", "unknown")
                    or item["window_s"] != seconds or not _num(item["covered_s"], 0, seconds)
                    or type(item["observed_swaps"]) is not int or not 0 <= item["observed_swaps"] <= MAX_ROWS
                    or (item["reason"] is not None and item["reason"] not in _COVERAGE_REASONS)
                    or (item["state"] == "complete") != (item["reason"] is None)
                    or item["state"] == "unknown" and item["covered_s"] != 0
                    or item["state"] == "partial" and item["covered_s"] <= 0
                    or item["state"] == "complete" and item["covered_s"] != seconds):
                raise ValueError
        for key, basis in _AGE_BASES.items():
            item = facts["ages"][key]
            expected = _AGE_KEYS | ({"source", "observed_at", "clock_offset_s", "age_clock", "decision_as_of"} if key == "provider_reported" else set())
            if not K._shape(item, expected) or item["basis"] != basis:
                raise ValueError
            if key == "provider_reported":
                if (item["source"] not in (None, "pumpfun")
                        or item["observed_at"] is not None and not _num(item["observed_at"], 0)
                        or item["clock_offset_s"] is not None and not _num(item["clock_offset_s"], -86400, 86400)
                        or (item["clock_offset_s"] is None) != (item["observed_at"] is None)
                        or item["state"] == "known" and (item["source"] != "pumpfun" or item["clock_offset_s"] is None)
                        or item["age_clock"] not in ((None,) if item["state"] != "known" else ("cut", "decision"))
                        or (item["decision_as_of"] is not None) != (item["age_clock"] == "decision")):
                    raise ValueError
            if item["state"] == "unknown":
                if item["reason"] not in _AGE_REASONS or any(item[k] is not None for k in ("age_s", "occurred_at", "known_at")):
                    raise ValueError
            elif item["state"] == "known":
                decision = item["decision_as_of"] if key == "provider_reported" and item.get("age_clock") == "decision" else None
                if (item["reason"] is not None or not _num(item["age_s"], 0) or not _num(item["occurred_at"], 0)
                        or not _num(item["known_at"], 0, facts["as_of"] if decision is None else decision)
                        or decision is not None and not (_num(decision, facts["as_of"]) and item["known_at"] > facts["as_of"])):
                    raise ValueError    # Am22/Am25: after the cut, never after the PERSISTED decision bound
            else:
                raise ValueError
        if facts["age_s"] != facts["ages"]["coin"]["age_s"] or facts["pool_age_s"] != facts["ages"]["pool"]["age_s"]:
            raise ValueError
        flow = facts["flow_60s"]
        v2 = facts["version"] in V2PLUS
        if v2 and (flow is None) != (facts["coverage"]["60s"]["observed_swaps"] == 0
                                    or facts["structure"].get("tape") != "ok"):
            raise ValueError
        if flow is not None:
            fields = {"buy_n","sell_n","buy_usd","sell_usd","makers","largest_buy"}
            if v2:
                fields |= {"buyers","sellers","n_swaps","covered_s","partial","lower_bound","loss_classes_exclusive",
                           "unknown_loss_receipts","global_loss_receipts","own_pool_losses"}
            if (not K._shape(flow,fields)
                    or any(type(flow[k]) is not int or not 0 <= flow[k] <= MAX_ROWS for k in ("buy_n","sell_n","makers"))
                    or any(not _num(flow[k],0) for k in ("buy_usd","sell_usd","largest_buy"))
                    or flow["makers"] > flow["buy_n"]+flow["sell_n"] or flow["largest_buy"] > flow["buy_usd"]):
                raise ValueError
            if v2:
                if (any(type(flow[k]) is not int or not 0 <= flow[k] <= MAX_ROWS for k in
                        ("buyers","sellers","n_swaps","unknown_loss_receipts","global_loss_receipts","own_pool_losses"))
                        or flow["n_swaps"] != flow["buy_n"]+flow["sell_n"]
                        or flow["buyers"] > flow["buy_n"] or flow["sellers"] > flow["sell_n"]
                        or not _num(flow["covered_s"],0,60)
                        or flow["covered_s"] != facts["coverage"]["60s"]["covered_s"]
                        or flow["partial"] is not (facts["coverage"]["60s"]["state"] != "complete")
                        or flow["lower_bound"] is not True or flow["loss_classes_exclusive"] is not True):
                    raise ValueError
            elif facts["coverage"]["60s"]["state"] != "complete":
                raise ValueError
        for key, window in (("price_vs_60s_low_pct", "60s"), ("price_vs_60s_high_pct", "60s"),
                            ("price_vs_1h_high_pct", "1h")):
            if facts[key] is not None and facts["coverage"][window]["state"] != "complete":
                raise ValueError
        if facts["creator_prior_launches"] is not None or facts["creator_history"] != {"state": "unknown", "reason": "history_not_captured"}:
            raise ValueError
        structure = K.validate_structure(facts["structure"])
        if structure.get("tape") == "ok":
            if (structure.get("version") != S.VERSION_V2 or structure.get("as_of") != facts["receipt_cut"]
                    or structure.get("cut_slot") != facts["cut_slot"]):
                raise ValueError
            bars, bar_coverage = structure.get("chart"), structure.get("chart_coverage")
            if v2 and bars is not None:
                if not isinstance(bar_coverage,list) or len(bars) != 24 or len(bar_coverage) != len(bars):
                    raise ValueError
                for item in bar_coverage:
                    if (not K._shape(item,{"covered_s","partial"}) or not _num(item["covered_s"],0,5)
                            or item["partial"] is not (item["covered_s"] < 5)):
                        raise ValueError
            for key in ("15s", "60s", "300s"):
                window = structure["windows"][key]
                if (window.get("covered_s") != facts["coverage"][key]["covered_s"]
                        or window.get("partial") is not (facts["coverage"][key]["state"] != "complete")):
                    raise ValueError
        elif any(item["state"] != "unknown" for item in facts["coverage"].values()):
            raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError("ps_facts") from None
    return copy.deepcopy(facts)
