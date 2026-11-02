"""One entry point for market-structure context from the local U tape: JUDGE (at its as-of claim) and the RISK watcher (each
cycle) both call structure_for(). Read-only (mode=ro), zero network, zero provider calls. Every failure is a named 'blind'
reason handed to memecoin_desk_structure.structure(), which then reports NO figures: a read problem is never a quiet market.

Cut (Gamma's pinned recipe): cut_slot = (max slot over blocks rows with received_at < as_of_read) + 1; a row is read only if
slot < cut_slot AND received_at < as_of_read. The curve tape has no committed-prefix watermark (the writer batches commits about
every second), so as_of_read = as_of - read_margin_s (default 2.0, MEMECOIN_DESK_ATTN_COMMIT_MARGIN_S); the margin is reported in
meta so the F0 can state the lag. Prices are USD per token from the virtual reserves after each swap at the heartbeat SOL/USD.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from datetime import datetime, timezone

from sniper.services import memecoin_desk_structure as S

class TapeUnreadable(Exception):
    pass


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def days_between(lo, hi):
    out, d = [], datetime.fromtimestamp(lo, timezone.utc).date()
    end = datetime.fromtimestamp(hi, timezone.utc).date()
    while d <= end:
        out.append(d.isoformat())
        d = d.fromordinal(d.toordinal() + 1)
    return out


def ro(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro&timeout=0" % p, uri=True, timeout=0)
    except sqlite3.Error as exc:
        raise TapeUnreadable("open:%s" % type(exc).__name__) from None


def gaps_between(desk_dir, lo, hi):
    """Gap times (epoch) with lo <= at <= hi from the curve gaps day files."""
    out = []
    for day in days_between(lo, hi):
        conn = ro(Path(desk_dir) / "gaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            out += [r[0] for r in conn.execute("SELECT at FROM gaps WHERE at>=? AND at<=?", (lo, hi))]
        except sqlite3.Error as exc:
            raise TapeUnreadable("gaps:%s" % type(exc).__name__) from None
        finally:
            conn.close()
    return out


HEARTBEAT_MAX_AGE_S = 5.0
READ_MARGIN_S = 2.0
BLOCK_MAX_AGE_S = 3.0               # the newest block used for cut_slot must be this fresh against the read cut
MAX_LOOKBACK_S = 3600.0


def _heartbeat(desk_dir, as_of):
    """(sol_usd, None) or (None, reason)."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts, sol = float(beat["ts"]), beat["sol_usd"]
    except Exception:
        return None, "heartbeat_unreadable"
    if not math.isfinite(ts) or ts > as_of or as_of - ts > HEARTBEAT_MAX_AGE_S or beat.get("connected") is not True:
        return None, "tape_stale"
    if type(sol) not in (int, float) or not math.isfinite(sol) or not 0 < sol < 1e9:
        return None, "sol_usd_unknown"
    return float(sol), None


def cut_slot_at(desk_dir, as_of_read):
    """(cut_slot, newest_block_received_at): cut_slot = (max slot with received_at < as_of_read) + 1 over the two newest UTC day
    files, and the receipt time of that newest-slot block; None when there is no block."""
    best, best_at = None, None
    for day in sorted({utc_day(as_of_read), utc_day(as_of_read - 86400)}):
        conn = ro(Path(desk_dir) / "blocks" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT slot, received_at FROM blocks WHERE received_at < ? ORDER BY slot DESC LIMIT 1",
                               (as_of_read,)).fetchone()
        except sqlite3.Error as exc:
            raise TapeUnreadable("blocks:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row and row[0] is not None and (best is None or row[0] > best):
            best, best_at = row[0], row[1]
    return None if best is None else (best + 1, best_at)


def launch_received_at(desk_dir, mint, now, back_days=3):
    """The launch row's tape receipt time, or None when the launch is not in the retained tape."""
    for i in range(back_days + 1):
        conn = ro(Path(desk_dir) / "launches" / ("%s.db" % utc_day(now - 86400 * i)))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT received_at FROM launches WHERE mint=? AND received_at<?", (mint, now)).fetchone()
        except sqlite3.Error as exc:
            raise TapeUnreadable("launches:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row is not None and row[0] is not None:
            return float(row[0])
    return None


def launch_creator(desk_dir, mint, now, back_days=3):
    """The launch row's creator, or None when the launch is not in the retained tape (unknown, never a guess)."""
    for i in range(back_days + 1):
        conn = ro(Path(desk_dir) / "launches" / ("%s.db" % utc_day(now - 86400 * i)))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT creator FROM launches WHERE mint=? AND received_at<?", (mint, now)).fetchone()
        except sqlite3.Error as exc:
            raise TapeUnreadable("launches:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row is not None:
            return row[0] if isinstance(row[0], str) and row[0] else None
    return None


def load_priced_swaps(desk_dir, mint, lo, hi, sol_usd):
    """Curve swaps with lo <= received_at < hi as structure() rows (price_usd from the virtual reserves after the swap)."""
    out = []
    for day in days_between(lo, hi):
        conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            rows = conn.execute("SELECT received_at, slot, signature, ordinal, is_buy, quote_atomic, actor, virtual_quote, "
                                "virtual_token, token_atomic FROM swaps WHERE mint=? AND received_at>=? AND received_at<? "
                                "ORDER BY received_at",
                                (mint, lo, hi)).fetchall()
        except sqlite3.Error as exc:
            raise TapeUnreadable("swaps:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        for at, slot, sig, ordinal, is_buy, quote, actor, vq, vt, tok in rows:
            if None in (at, slot, sig, ordinal, is_buy, quote, actor):
                raise TapeUnreadable("swaps:null_field")
            price = ((vq / 1e9) / (vt / 1e6) * sol_usd) if vq and vt and vq > 0 and vt > 0 else None
            out.append({"received_at": at, "slot": slot, "signature": sig, "ordinal": ordinal, "is_buy": int(is_buy),
                        "usd": quote / 1e9 * sol_usd, "price_usd": price, "actor": actor,
                        "tokens": tok / 1e6 if type(tok) is int and tok >= 0 else None})     # v23 creator_sold_share
    return out


def curve_graduated(desk_dir, mint, lo, hi):
    """True when the mint's last curve swap received in [lo, hi) left real SOL at or above the graduation level."""
    from sniper.services.memecoin_desk_ps_tape import GRADUATION_REAL_SOL
    for day in reversed(days_between(lo, hi)):
        conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT real_quote FROM swaps WHERE mint=? AND received_at>=? AND received_at<? "
                               "ORDER BY received_at DESC LIMIT 1", (mint, lo, hi)).fetchone()
        except sqlite3.Error as exc:
            if "no such column" in str(exc):
                return False                         # a day file from before real_quote: no proof either way
            raise TapeUnreadable("graduated:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row is not None:
            return type(row[0]) is int and row[0] >= GRADUATION_REAL_SOL
    return False


def actors_of_signatures(desk_dir, sigs, lo, hi):
    """Actors of the curve swaps whose transaction signature is in `sigs` (our fill transactions): our wallet, learned from data."""
    sigs = [x for x in sigs if isinstance(x, str) and x]
    out = set()
    for day in days_between(lo, hi) if sigs else ():
        conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            for i in range(0, len(sigs), 200):
                chunk = sigs[i:i + 200]
                out |= {r[0] for r in conn.execute("SELECT actor FROM swaps WHERE signature IN (%s)" % ",".join("?" * len(chunk)),
                                                   chunk) if r[0]}
        except sqlite3.Error as exc:
            raise TapeUnreadable("ours:%s" % type(exc).__name__) from None
        finally:
            conn.close()
    return out


V23_FACTS_VERSION = "v23-facts-v1"     # = memecoin_desk_v23_facts.VERSION (pinned by a test)


def judge_structure_fn(desk_dir, cohort_of, fill_sigs_of=None, close_of=None):
    """structure_fn for memecoin_desk_judge.run_once: (ticket, candidate, now_epoch) -> the structure dict, or None when the
    cohort's frozen policy has no 'structure' (an older cohort gets no block). A PumpSwap candidate is read from the PumpSwap tape
    (memecoin_desk_ps_tape; 2026-10-23, was 'not_applicable').
    cohort_of(ticket) -> the stored cohort dict. fill_sigs_of(mint) -> OUR funded fill signatures on that mint (ledger, read-only);
    it is REQUIRED for a block to be produced: if it is missing or raises, we cannot name our own trades, so the block is
    blind ('ours_unknown'), never computed with our swaps counted. The absolute clocks in 'meta' stay off the JUDGE sheet."""
    def fn(ticket, candidate, ts):
        cohort = cohort_of(ticket)
        if not isinstance((cohort.get("policy") or {}).get("structure"), dict):
            return None
        bounds = {k: tuple(v) for k, v in (cohort["policy"].get("rail_bounds") or {}).items()
                  if isinstance(v, (list, tuple)) and len(v) == 2}
        margin = float(cohort["policy"]["structure"].get("read_margin_s", READ_MARGIN_S))
        sheet = cohort["policy"]["structure"].get("sheet")            # absent = the v1 sheet; "facts-v2" = structure-v2; anything else is refused, never downgraded
        if sheet not in (None, "facts-v2"):
            return {"version": S.VERSION, "tape": "unknown", "reason": "structure_invalid"}
        params = S.Params(sheet="v2") if sheet == "facts-v2" else S.Params()
        try:
            sigs = tuple(fill_sigs_of(candidate["mint"])) if fill_sigs_of is not None else None
        except Exception:
            sigs = None
        if sigs is None:
            return {"version": S.VERSION, "tape": "unknown", "reason": "ours_unknown"}
        v23 = None
        if cohort["policy"].get("facts") == V23_FACTS_VERSION:   # v23: qualify facts; a re-entry ticket also gets reentry
            prev = (candidate.get("reentry") or {}).get("prev_position_id")
            close = None
            if prev is not None:
                try:
                    close = close_of(prev) if close_of is not None else None
                except Exception:
                    close = None
                close = close if isinstance(close, dict) else {}     # unknown close: every reentry figure None
            v23 = {"close": close}
        out = structure_for(candidate["mint"], desk_dir=desk_dir, as_of=ts, bounds=bounds, read_margin_s=margin, fill_sigs=sigs,
                            v23=v23, params=params)
        out.pop("meta", None)
        so = cohort["policy"].get("standing_orders")
        if not (isinstance(so, dict) and (so.get("noise_basis") == "true_range" or so.get("drift_k"))) and isinstance(out.get("volatility"), dict):
            for key in ("median_5s_true_range_pct", "true_range_bars"):      # a cohort that does not read the new noise sees the sheet it always saw
                out["volatility"].pop(key, None)
        return out
    return fn


def _v23_facts(out, v23, swaps, *, desk_dir, mint, as_read, cut, ours, ours_sigs, gaps, lo, whole_life, as_of,
               whole_life_venue=True):
    """Adds out['qualify'] and, with v23['close'], out['reentry'] (memecoin_desk_v23_facts). Ours are removed from both; the
    Mayhem agent from the qualify counts only (it still moves the price path)."""
    from sniper.services import memecoin_desk_v23_facts as F
    rows = S._usable(swaps, as_read, cut, ours, ours_sigs)
    flow = [r for r in rows if S._flow(r, S.AGENTS)]
    creator = launch_creator(desk_dir, mint, as_of) if whole_life else None
    out["qualify"] = F.qualify(flow, as_of=as_read, gaps=gaps, covered_from=lo, creator=creator,
                               creator_whole_life=whole_life)
    out["entry_state"] = F.entry_state(rows, as_of=as_read, gaps=gaps, covered_from=lo)
    close = v23.get("close")
    if close is not None:
        vol = out.get("volatility") or {}
        r5 = vol.get("median_5s_swing_pct") if (vol.get("bars") or 0) >= 12 else None
        # the post-exit path needs ONE price identity over the whole (close, as_of): a coin read from its PumpSwap pool
        # (a venue switch, maybe after the close) or a close older than the loaded window has an unknown path
        out["reentry"] = F.reentry(rows, close, as_of=as_read, gaps=gaps, range5_pct=r5,
                                   covered_from=None if not whole_life_venue else lo)


def structure_for(mint, *, desk_dir, as_of, ours=frozenset(), ours_sigs=frozenset(), bounds=None, params=S.Params(),
                  read_margin_s=READ_MARGIN_S, check_heartbeat=True, sol_usd=None, max_lookback_s=MAX_LOOKBACK_S, fill_sigs=(),
                  v23=None):
    """The structure dict for `mint` as of `as_of` (epoch), with a 'meta' block. Never raises on a tape problem: it returns
    {'tape': 'unknown', 'reason': ...} (plus meta). `sol_usd` overrides the heartbeat value (offline replays only)."""
    as_read = as_of - read_margin_s
    meta = {"as_of": as_of, "tape_cut": as_read, "read_margin_s": read_margin_s}
    blind = lambda reason: {**S.structure([], as_of=as_read, cut_slot=0, blind=reason), "meta": meta}
    try:
        if check_heartbeat:
            sol, why = _heartbeat(desk_dir, as_of)
            if why:
                return blind(why)
        else:
            sol = sol_usd
        if sol is None:
            return blind("sol_usd_unknown")
        meta["sol_usd"] = sol
        cut_info = cut_slot_at(desk_dir, as_read)
        if cut_info is None:
            return blind("no_block_before_as_of")
        cut, block_at = cut_info
        if as_read - block_at > BLOCK_MAX_AGE_S:           # the block stream stalled: a stalled commit must not read as a quiet market
            return blind("tape_stale")
        launched = launch_received_at(desk_dir, mint, as_of)
        meta["launch_known"] = launched is not None
        truncated = launched is None or launched < as_read - max_lookback_s       # the since-launch window cannot be built whole
        meta["launch_truncated"] = truncated
        lo = max(launched if launched is not None else as_read - max_lookback_s, as_read - max_lookback_s)
        from sniper.services import memecoin_desk_ps_tape as PT
        if fill_sigs:                                        # our wallet, learned from our own fill transactions
            ours = (frozenset(ours) | actors_of_signatures(desk_dir, fill_sigs, max(lo, as_read - 7 * 86400), as_of)
                    | PT.ps_actors_of_signatures(desk_dir, fill_sigs))
            ours_sigs = frozenset(ours_sigs) | frozenset(fill_sigs)
        swaps = load_priced_swaps(desk_dir, mint, lo, as_read, sol)
        gaps = gaps_between(desk_dir, lo, as_of)
        # AFTER GRADUATION the coin trades on its canonical PumpSwap pool (2026-10-23): one chart across both venues, and
        # the PumpSwap capture's own gaps count only when this coin has pool rows in the window.
        ps, pool_gaps = PT.load_ps_swaps(desk_dir, mint, lo, as_read, sol, with_gaps=True)
        lost = ()
        meta["ps_swaps_read"] = len(ps)
        if ps:
            # ONE VENUE PER CHART (Gamma: the pool opens ~20% under the last curve print, a step every structure rule
            # would read as a crash). Once the coin trades on its pool, the windows are the pool's swaps only.
            swaps = [{**{k: r[k] for k in ("received_at", "slot", "signature", "ordinal", "is_buy", "usd", "price_usd",
                                           "actor")}, **({"price_only": True} if r.get("price_only") else {})} for r in ps]
            gaps = PT.ps_point_gaps(desk_dir, lo, as_of) + PT.ps_gap_intervals(desk_dir, lo, as_of)   # capture discontinuities and stop SPANS (overlap) blind; this pool's lost swaps are counted
            lost = pool_gaps                        # (Gamma (b)): a window blinds only at LOST_BLIND of them
            meta["venue"] = "pumpswap"
        elif curve_graduated(desk_dir, mint, lo, as_read):
            return blind("pool_unread")             # the curve ended and its pool reads nothing: blind, never a calm ok
        out = S.structure(swaps, as_of=as_read, cut_slot=cut, ours=ours, ours_sigs=ours_sigs, lost=lost,
                          launched_at=None if truncated else launched, launch_unknown="beyond_lookback" if truncated else None,
                          gaps=gaps, bounds=bounds, params=params)
        out["meta"] = {**meta, "swaps_read": len(swaps), "gaps_in_range": len(gaps)}
        if v23 is not None:              # v23 cohort: qualify (+ reentry on a re-entry ticket) from the SAME cut rows
            _v23_facts(out, v23, swaps, desk_dir=desk_dir, mint=mint, as_read=as_read, cut=cut, ours=ours,
                       ours_sigs=ours_sigs, gaps=gaps, lo=lo, whole_life=not truncated and not ps, as_of=as_of,
                       whole_life_venue=not ps)
        return out
    except TapeUnreadable as exc:
        return blind("tape_unreadable:%s" % str(exc).split(":")[0])
    except Exception as exc:                                 # memecoin_desk_ps_tape.TapeUnreadable: blind, never calm
        if type(exc).__name__ != "TapeUnreadable":
            raise
        return blind("tape_unreadable:%s" % str(exc).split(":")[0])
    except (sqlite3.Error, OSError, OverflowError, ValueError):
        return blind("tape_unreadable")
