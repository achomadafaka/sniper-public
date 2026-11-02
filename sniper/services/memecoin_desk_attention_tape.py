"""Read-only U-tape readers for the attention worker. Every read is mode=ro and typed: a read error raises TapeUnreadable,
it never returns an empty list that could be read as 'calm'."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LAMPORTS = 1e9


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


def _ps(fn, *a):
    """memecoin_desk_ps_tape call with its read errors as this module's TapeUnreadable (blind, never calm)."""
    from sniper.services import memecoin_desk_ps_tape as PT
    try:
        return fn(PT)(*a)
    except PT.TapeUnreadable as exc:
        raise TapeUnreadable(str(exc)) from None


def load_swaps(desk_dir, mint, lo, hi, sol_usd):
    """The mint's curve swaps with lo <= received_at < hi as dicts (usd at `sol_usd`), followed by its canonical PumpSwap
    pool's swaps once it has graduated (memecoin_desk_ps_tape, 2026-10-23), in receipt order."""
    out = []
    for day in days_between(lo, hi):
        conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            rows = conn.execute("SELECT received_at, slot, signature, ordinal, is_buy, quote_atomic, token_atomic, actor "
                                "FROM swaps WHERE mint=? AND received_at>=? AND received_at<? ORDER BY received_at",
                                (mint, lo, hi)).fetchall()
        except sqlite3.Error as exc:
            raise TapeUnreadable("swaps:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        for r, slot, sig, ordinal, is_buy, quote, token, actor in rows:
            if None in (r, slot, sig, ordinal, is_buy, quote, token, actor):
                raise TapeUnreadable("swaps:null_field")
            out.append({"received_at": r, "slot": slot, "signature": sig, "ordinal": ordinal, "is_buy": int(is_buy),
                        "usd": quote / LAMPORTS * sol_usd, "actor": actor, "token_atomic": token})
    ps = _ps(lambda PT: PT.load_ps_swaps, desk_dir, mint, lo, hi, sol_usd)
    if ps:
        keys = ("received_at", "slot", "signature", "ordinal", "is_buy", "usd", "actor", "token_atomic")
        out = sorted(out + [{k: r[k] for k in keys} for r in ps], key=lambda r: (r["received_at"], r["slot"], r["ordinal"]))
    return out


def latest_curve_price(desk_dir, mint, cut, sol_usd, now):
    """(price_sol, price_usd, slot, signature, received_at) from the newest curve swap received before `cut`
    (virtual reserves after that swap), or None when there is none / the reserves are unusable. SOL per whole token (6 dp)."""
    for day in (utc_day(now), utc_day(now - 86400)):
        conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT virtual_quote, virtual_token, slot, signature, received_at FROM swaps WHERE mint=? "
                               "AND received_at<? ORDER BY received_at DESC, slot DESC, ordinal DESC LIMIT 1",
                               (mint, cut)).fetchone()
        except sqlite3.Error as exc:
            raise TapeUnreadable("price:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row is not None:
            vq, vt, slot, sig, at = row
            if not vq or not vt or vq <= 0 or vt <= 0 or None in (slot, sig, at):
                return None
            price = (vq / LAMPORTS) / (vt / 1e6)
            return price, price * sol_usd, slot, sig, at
    return None


def actors_of_signatures(desk_dir, sigs, lo, hi):
    """{actor} of the curve swaps whose transaction signature is in `sigs` (our fill transactions), searched over the UTC
    days between lo and hi by primary key. Empty when none is on the tape yet (our wallet is then learned later)."""
    sigs = [x for x in sigs if isinstance(x, str) and x]
    out = set()
    if not sigs:
        return out
    for day in days_between(lo, hi):
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
    return out | _ps(lambda PT: PT.ps_actors_of_signatures, desk_dir, sigs)


def creator_of(desk_dir, mint, now, back_days=3):
    """The launch creator wallet, or None when the launch is not in the retained tape (unknown, not 'no creator').
    A graduated coin launched before the retained curve tape falls back to its canonical pool's coin_creator."""
    for i in range(back_days + 1):
        conn = ro(Path(desk_dir) / "launches" / ("%s.db" % utc_day(now - 86400 * i)))
        if conn is None:
            continue
        try:
            row = conn.execute("SELECT creator FROM launches WHERE mint=?", (mint,)).fetchone()
        except sqlite3.Error as exc:
            raise TapeUnreadable("launches:%s" % type(exc).__name__) from None
        finally:
            conn.close()
        if row is not None:
            return row[0]
    rows = _ps(lambda PT: PT.load_ps_swaps, desk_dir, mint, now - 86400 * back_days, now, None)
    creators = {r["coin_creator"] for r in rows if r["coin_creator"]}
    return creators.pop() if len(creators) == 1 else None


def gaps_between(desk_dir, lo, hi, mint=None):
    """Gap rows (at, cause) with lo <= at <= hi from the curve gaps day files; with `mint`, plus the PumpSwap capture's gaps
    when that coin has canonical-pool rows in the window (a curve-only coin is never blinded by the pool tape)."""
    out = []
    if mint and _ps(lambda PT: PT.has_pool_rows, desk_dir, mint, lo, hi):
        out += [(at, "pumpswap") for at in _ps(lambda PT: PT.ps_gaps, desk_dir, lo, hi)]

    for day in days_between(lo, hi):
        conn = ro(Path(desk_dir) / "gaps" / ("%s.db" % day))
        if conn is None:
            continue
        try:
            out += conn.execute("SELECT at, cause FROM gaps WHERE at>=? AND at<=?", (lo, hi)).fetchall()
        except sqlite3.Error as exc:
            raise TapeUnreadable("gaps:%s" % type(exc).__name__) from None
        finally:
            conn.close()
    return out


def holders_before(cluster_db, mint, opened_at, top=10):
    """{owner: balance_atomic} of the top holders from the newest cluster snapshot taken at or before `opened_at`
    (the entry snapshot), or None when there is none: holder_dump is then UNKNOWN for that position."""
    conn = ro(cluster_db)
    if conn is None:
        return None
    try:
        snap = None
        for sid, at in conn.execute("SELECT snapshot_id, snapshot_at FROM cluster_snapshots WHERE mint=? ORDER BY snapshot_at DESC",
                                    (mint,)):
            try:
                ts = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if ts <= opened_at:
                snap = sid
                break
        if snap is None:
            return None
        rows = conn.execute("SELECT owner, balance_atomic FROM cluster_snapshot_holders WHERE snapshot_id=? AND owner IS NOT NULL "
                            "AND (exclusion IS NULL OR exclusion='')", (snap,)).fetchall()
    except sqlite3.Error as exc:
        raise TapeUnreadable("cluster:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    held = []
    for owner, balance in rows:
        try:
            held.append((owner, int(balance)))
        except (TypeError, ValueError):
            continue
    held.sort(key=lambda r: -r[1])
    return dict(held[:top]) or None
