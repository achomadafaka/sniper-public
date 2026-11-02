"""As-of-decision refresh of a U candidate's price and flow facts, from the LOCAL U tape (zero calls).

The U emission snapshot is frozen at emission; a JUDGE call lands 30-120 s later and band coins move fast
(Gamma 2026-10-22: 37% sit more than 4.75% above the emission price after 30 s). The JUDGE must see, and the
entry cap must use, ONE snapshot as of the decision. refresh() returns that snapshot as a new, valid
`desk-candidate/v1` envelope (same ticket identity, same fields, `emitted_at` / `snapshot.emission_at` = the
as-of time, hash recomputed) plus a meta dict for the audit. The original candidate is never edited.

Never a silent fallback to emission facts: a stale or gapped tape, a graduated curve, a missing emission state
or an unreadable quote raises StaleSnapshot(reason) and the caller records a no-call refusal.

What is recomputed (all from swaps rows with received_at <= the as-of time; the trigger path, rank_inputs=True, STRICTLY <):
  mcap_usd, liq_usd   original value x (reserve ratio now / at the emission trigger swap): the SOL/USD rule and the
                      token supply are those of the emission build by construction. liq is guarded by a unit
                      cross-check against the live SOL/USD and goes missing (never wrong) when it disagrees.
  curve_pct           from the last swap's real token reserves, as the emission build does.
  flow_60s, price_vs_60s_low/high_pct   over swaps received in (as_of - 60 s, as_of], USD at the live SOL/USD.
  age_s               the original age plus the elapsed time (exact), when the original was complete.
Everything else (creator priors, safety, crawler fields) is carried unchanged with its own stamp.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sniper.services import memecoin_desk_contract as contract

INITIAL_REAL_TOKEN = 793_100_000 * 10**6          # Pump.fun curve real token reserve at launch (6 dp)
INITIAL_VIRTUAL_QUOTE = 30_000_000_000            # the 30 SOL virtual floor: real quote = virtual - this
FLOW_S = 60.0
HEARTBEAT_MAX_AGE_S = 5.0
SOL_MAX_AGE_S = 120.0
LIQ_CHECK_TOL = 0.10                              # |liq recomputed at the emission state / original - 1|
SRC = "pumpfun_grpc"
_COLS = "slot, signature, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, received_at, real_token"   # index 11 = real_quote, appended by _rows


class StaleSnapshot(Exception):
    """The as-of snapshot cannot be built honestly. `reason` is a short machine slug."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _days(ts, back=1):
    day = datetime.fromtimestamp(ts, timezone.utc).date()
    return [(day.fromordinal(day.toordinal() - i)).isoformat() for i in range(back + 1)]


def _ro(path):
    p = Path(path)
    if not p.exists():
        return None
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=2)


def _rows(desk_dir, now, sql, args):
    """Query today's and yesterday's swaps day files, newest day first. Rows are plain tuples. _COLS in the SQL is expanded
    with real_quote when the day file has that column (the tape writer adds it in place) and with NULL when it does not, so a
    day file from before the column existed reads as 'real quote unknown', never as an error."""
    out = []
    for d in _days(now):
        conn = _ro(Path(desk_dir) / "swaps" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            has = any(r[1] == "real_quote" for r in conn.execute("PRAGMA table_info(swaps)"))
            out += conn.execute(sql.replace(_COLS, _COLS + (", real_quote" if has else ", NULL")), args).fetchall()
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return out


def _AGENTS():
    from sniper.services.memecoin_desk_safety import MAYHEM_AGENT_IDS
    return MAYHEM_AGENT_IDS


def run_lift(rows_desc, agents):
    """(lift, run_rows): the reserve price of the newest row over the reserve price of the last REGULAR row before the current
    agent run, for `rows_desc` = the mint's rows newest first (_COLS tuples). The Mayhem agent's trades raise the curve's
    virtual SOL far beyond their reported quote (pre-registration amendment 12): this is how much one run of them has lifted
    the price. No current run (the newest row is regular) or no regular row before it in the sample = (None, run_rows): unknown
    or absent, never 1.0. Reserves are used as read, agent rows included."""
    run = 0
    for r in rows_desc:
        if r[6] in agents:
            run += 1
            continue
        if run == 0:
            return None, 0
        newest, base = _price(rows_desc[0][7], rows_desc[0][8]), _price(r[7], r[8])
        return (None if not newest or not base else newest / base), run
    return None, run


def _price(vq, vt):
    return (vq / 1e9) / (vt / 1e6) if vq and vt else None


def _heartbeat(desk_dir, now):
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        age = now - float(beat["ts"])
    except Exception:
        raise StaleSnapshot("tape_heartbeat_unreadable") from None
    if age > HEARTBEAT_MAX_AGE_S or beat.get("connected") is not True:
        raise StaleSnapshot("tape_stale")
    sol, sol_age = beat.get("sol_usd"), beat.get("sol_age_s")
    if not isinstance(sol, (int, float)) or sol <= 0 or not isinstance(sol_age, (int, float)) or sol_age > SOL_MAX_AGE_S:
        raise StaleSnapshot("sol_usd_stale")
    return beat, age, float(sol), float(sol_age)


def _gaps_since(desk_dir, since, now, strict=False):
    """Gap rows (at, cause) in [since, now]: the worker restarted or the stream dropped."""
    out = []
    for d in _days(now):
        conn = _ro(Path(desk_dir) / "gaps" / ("%s.db" % d))
        if conn is None:
            continue
        try:
            out += conn.execute("SELECT at, cause FROM gaps WHERE at >= ? AND at " + ("<" if strict else "<=") + " ?",
                                (since, now)).fetchall()
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return out


def _field(value, source, known_at, slot, completeness):
    return {"value": value, "source": source, "known_at": known_at, "slot": slot, "completeness": completeness}


def refresh(candidate, *, desk_dir, now, rank_inputs=False, intake_mint=None, integrity=False, mint_max_age_s=None,
            accept_curve_derived=False, rpc_state=None, resolve_zero_fees=False):
    """(refreshed_candidate, meta). `now` is the as-of epoch seconds (the claim time). Raises StaleSnapshot.
    rank_inputs=True (cohort U trigger path) adds meta["rank"]: the u-runners-v1 features of the same 60 s window and the
    price now, from the rows already read here plus ONE local read for the window-start price."""
    cand = contract.validate_candidate(candidate)
    if cand["producer"] != "U":
        raise StaleSnapshot("not_a_u_candidate")
    if cand.get("venue") == "pumpswap":
        from sniper.services import memecoin_desk_ps_asof
        from sniper.settings import memecoin as cfg
        # intake tickets (Delta's ps_asof intake mode) carry the assembler's mint read; every other ticket: unchanged
        return memecoin_desk_ps_asof.refresh(cand, desk_dir=desk_dir, now=now,
            reserve_max_age_s=cfg.MEMECOIN_DESK_PS_RESERVE_MAX_AGE_S,
            sol_max_age_s=cfg.MEMECOIN_DESK_PS_SOL_MAX_AGE_S,
            **({"integrity": True} if integrity else {}),
            **({"intake_mint": intake_mint} if intake_mint is not None else {}),
            **({"mint_max_age_s": mint_max_age_s} if mint_max_age_s is not None else {}),
            **({"accept_curve_derived": True} if accept_curve_derived else {}),
            **({"rpc_state": rpc_state} if rpc_state is not None else {}), resolve_zero_fees=resolve_zero_fees)
    if cand.get("venue", "pumpfun") != "pumpfun" or cand["schema"] != "desk-candidate/v1":
        raise StaleSnapshot("unsupported_venue")
    fields0 = cand["snapshot"]["fields"]
    emitted_at = cand["snapshot"]["emission_at"]
    if now < emitted_at:
        raise StaleSnapshot("clock_before_emission")
    beat, hb_age, sol_usd, sol_age = _heartbeat(desk_dir, now)
    # the emission trigger swap: the exact reserve state the emission's mcap and liq came from
    trig = _rows(desk_dir, emitted_at, "SELECT %s FROM swaps WHERE signature=? AND ordinal=?" % _COLS,
                 (cand["trigger_signature"], cand["trigger_ordinal"]))
    if not trig:
        raise StaleSnapshot("no_emission_state")
    vq_e, vt_e = trig[0][7], trig[0][8]
    price_e = _price(vq_e, vt_e)
    if price_e is None:
        raise StaleSnapshot("no_emission_state")
    mint = cand["mint"]
    # The upper cut of every read: legacy <= as-of; the trigger path (rank_inputs) STRICTLY < as-of, the pre-registered rule
    # (amendment 11 item 2) already used by the context, attention and structure readers (Codex/Gamma). The window-start base row
    # keeps its <= (now - 60 s): it is the complement of the window's exclusive lower edge, not a cut at as-of.
    cut = "<" if rank_inputs else "<="
    last = sorted(_rows(desk_dir, now, ("SELECT %s FROM swaps WHERE mint=? AND received_at" + cut + "? ORDER BY slot DESC, received_at DESC LIMIT 8")
                        % _COLS, (mint, now)), key=lambda r: (r[0], r[9], r[2]))
    if not last:
        raise StaleSnapshot("no_swaps_since_emission")
    tip = last[-1]
    vq_n, vt_n, rt_n = tip[7], tip[8], tip[10]
    price_n = _price(vq_n, vt_n)
    if price_n is None:
        raise StaleSnapshot("unreadable_quote")
    if rt_n is not None and rt_n <= 0 and not rank_inputs:
        raise StaleSnapshot("curve_complete")          # the trigger path reads the completed curve as its own band outcome
    # a gap after the last swap we hold may hide newer swaps: never claim the tip is current
    gaps_after = [g for g in _gaps_since(desk_dir, tip[9], now, strict=rank_inputs)]
    if gaps_after:
        raise StaleSnapshot("tape_gap_since_last_swap")
    window = _rows(desk_dir, now, ("SELECT %s FROM swaps WHERE mint=? AND received_at>? AND received_at" + cut + "?") % _COLS,
                   (mint, now - FLOW_S, now))
    window.sort(key=lambda r: (r[0], r[9], r[2]))
    flow_complete = not _gaps_since(desk_dir, now - FLOW_S, now, strict=rank_inputs)
    ratio = price_n / price_e

    mcap_row = fields0.get("mcap_usd")
    mcap = (round(mcap_row["value"] * ratio, 2) if mcap_row and mcap_row["completeness"] == "complete"
            and mcap_row["value"] else None)
    liq_row, liq, liq_check, liq_basis = fields0.get("liq_usd"), None, None, None
    rq_now, rq_trig = tip[11], trig[0][11]
    if rq_now is not None:
        # DIRECT: the latest swap's own real quote reserves (the tape writer stores the event's real_sol_reserves) at the live
        # SOL/USD. No assumption about the curve's initial virtual quote (it is 30 SOL on classic curves and ~180-340 SOL on others).
        liq_basis = "real_quote"
        init_now = vq_n - rq_now
        if rq_trig is not None:                       # Mayhem virtual reserves move independently; the offset is diagnostic only.
            init_e = vq_e - rq_trig
            liq_check = (init_now - init_e) / init_e if init_e else None
        if type(rq_now) is int and 0 <= rq_now <= vq_n:
            liq = round(rq_now / 1e9 * sol_usd, 2)
    else:
        # LEGACY (a tape row from before real_quote was stored): ratio from the emission, valid only on classic 30 SOL curves
        # and guarded by the unit cross-check; anything else is MISSING, never fabricated.
        liq_basis = "ratio_legacy"
        rq_e, rq_n = vq_e - INITIAL_VIRTUAL_QUOTE, vq_n - INITIAL_VIRTUAL_QUOTE
        if liq_row and liq_row["completeness"] == "complete" and liq_row["value"] and rq_e > 0 and rq_n >= 0:
            liq_check = (rq_e / 1e9 * sol_usd) / liq_row["value"] - 1          # unit guard at the emission state
            if abs(liq_check) <= LIQ_CHECK_TOL:
                liq = round(liq_row["value"] * rq_n / rq_e, 2)
    progress = None if rt_n is None else max(0.0, min(100.0, (1 - rt_n / INITIAL_REAL_TOKEN) * 100))
    buys = [r for r in window if r[3]]
    sells = [r for r in window if not r[3]]
    usd = lambda r: r[4] / 1e9 * sol_usd
    flow = {"buy_n": len(buys), "sell_n": len(sells), "buy_usd": sum(usd(r) for r in buys),
            "sell_usd": sum(usd(r) for r in sells), "makers": len({r[6] for r in window}),
            "largest_buy": max((usd(r) for r in buys), default=0)}
    prices = [p for p in (_price(r[7], r[8]) for r in window) if p is not None]
    cur = prices[-1] if prices else None
    age_row = fields0.get("age_s")
    age = (round(age_row["value"] + (now - emitted_at), 1) if age_row and age_row["completeness"] == "complete"
           and age_row["value"] is not None else None)

    slot = tip[0]
    fields = copy.deepcopy(fields0)
    fields["mcap_usd"] = _field(mcap, SRC + ":curve_reserves", now, slot, "complete" if mcap is not None else "missing")
    fields["liq_usd"] = _field(liq, SRC + ":real_quote_reserves", now, slot, "complete" if liq is not None else "missing")
    fields["curve_pct"] = _field(None if progress is None else round(progress, 2), SRC + ":real_token_reserves", now, slot,
                                 "complete" if progress is not None else "missing")
    fields["flow_60s"] = _field(flow, SRC + ":swaps", now, slot, "complete" if flow_complete else "partial")
    fields["price_vs_60s_low_pct"] = _field(round((cur / min(prices) - 1) * 100, 2) if prices else None, SRC + ":swaps",
                                            now, slot, "complete" if flow_complete and prices else "partial")
    fields["price_vs_60s_high_pct"] = _field(round((cur / max(prices) - 1) * 100, 2) if prices else None, SRC + ":swaps",
                                             now, slot, "complete" if flow_complete and prices else "partial")
    if age is not None:
        fields["age_s"] = _field(age, age_row["source"], now, age_row["slot"], "complete")
    as_of_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    as_of = contract._utc(as_of_iso)
    snap = {"emission_at": as_of, "fields": fields,
            "missing": sorted(n for n, r in fields.items() if r["value"] is None or r["completeness"] != "complete")}
    for row in fields.values():                                           # no stamp may sit after the as-of time
        if row["known_at"] is not None and row["known_at"] > as_of:
            row["known_at"] = as_of
    snap["hash"] = hashlib.sha256(json.dumps({k: v for k, v in snap.items()}, sort_keys=True,
                                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    out = {k: v for k, v in cand.items() if k not in ("snapshot", "emitted_at")}
    out["emitted_at"], out["snapshot"] = as_of_iso, snap
    try:
        contract.validate_candidate(out)
    except ValueError as exc:                                            # a bug in this module, never a quiet pass
        raise StaleSnapshot("refresh_invalid:%s" % exc) from None
    rank = None
    if rank_inputs:
        from sniper.services import memecoin_desk_ranker as R
        base_row = _rows(desk_dir, now, "SELECT %s FROM swaps WHERE mint=? AND received_at<=? ORDER BY slot DESC, received_at DESC LIMIT 1"
                         % _COLS, (mint, now - FLOW_S))
        base_price = _price(base_row[0][7], base_row[0][8]) if base_row else price_e      # a coin born inside the window: its emission price
        swaps = [(r[9], bool(r[3]), int(r[4] or 0), r[6], _price(r[7], r[8])) for r in window]
        # the last regular row before the current agent run: a short read first (most runs are short), the full bound only when the
        # first one is all agent rows; a run longer than R.RUN_LIFT_ROWS reads None (the bound is a u-inline-v1 constant, recorded)
        def newest(n):
            rows = _rows(desk_dir, now, ("SELECT %s FROM swaps WHERE mint=? AND received_at" + cut + "? ORDER BY slot DESC, received_at DESC, ordinal DESC LIMIT %d")
                         % (_COLS, n), (mint, now))
            rows.sort(key=lambda r: (r[0], r[9], r[2]), reverse=True)
            return rows[:n]
        deep = newest(40)
        if len(deep) == 40 and all(r[6] in _AGENTS() for r in deep):
            deep = newest(R.RUN_LIFT_ROWS)
        lift, run_rows = run_lift(deep, _AGENTS())
        rank = {"features": R.features(swaps, rank_at=now, base_price=base_price), "price_now": price_n, "base_price": base_price,
                "real_token": rt_n, "trigger_actor": trig[0][6], "trigger_is_agent": trig[0][6] in _AGENTS(),
                "agent_run_lift": lift, "agent_run_rows": run_rows, "agent_run_rows_max": R.RUN_LIFT_ROWS,
                "agent_rows_60s": sum(1 for r in window if r[6] in _AGENTS())}
    meta = {"as_of": as_of, "emission_at": emitted_at, "lag_s": round(as_of - emitted_at, 3),
            "emission_snapshot_hash": cand["snapshot"]["hash"], "snapshot_hash": snap["hash"],
            "last_swap": {"signature": tip[1], "slot": slot, "received_at": tip[9]},
            "tape_heartbeat_age_s": round(hb_age, 3), "sol_usd": sol_usd, "sol_age_s": round(sol_age, 1),
            "price_ratio": round(ratio, 6), "flow_complete": flow_complete, "liq_check": liq_check, "liq_basis": liq_basis,
            "window_swaps": len(window), "version": "asof-v1"}
    if rank is not None:
        meta["rank"] = rank
    return out, meta
