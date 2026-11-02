"""Tape sources for the dump-group worker: keyset tailers over the desk's curve and PumpSwap tapes and the normalizers that turn tape rows into the
detector's row shape (memecoin_desk_dump_group). Read-only: every query is one short statement on a fresh mode=ro connection (no long read
transaction, so the tape's WAL is never pinned). Pure besides sqlite reads and the injected clock; nothing here trades or decides."""
from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

INIT_Q, INIT_B, INIT_TOL = 30e9, 1.073e15, 0.005
SOL_QUOTES = (None, "", "So11111111111111111111111111111111111111112", "1" * 32)
CURVE_COLS = ("slot, signature, ordinal, mint, curve, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, received_at, real_token, "
              "fee_bps, creator_fee_bps, tx_index")
MAX_DAY_HOPS = 40
FAILED = object()                     # a typed failed read: never confused with an empty (exhausted) page
RELEVANT_DROPS = ("no_actor", "bad_reserves")      # a dropped row of a scored coin (identity or size unknown) makes that pool's window unknown


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _next_day(day):
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


class CurveNormalizer:
    """One curve-tape row -> the detector row. A coin is scored only if its FIRST swap shows the standard SOL-quote start (as the research path does)."""

    def __init__(self):
        self.coin_ok = {}
        self.dropped = defaultdict(int)

    def normalize(self, r):
        (slot, sig, ordinal, mint, pool, buy, qa, ta, actor, vq, vb, recv, rt, fee, cfee, tx_index, qm, real_quote) = r
        if not actor:
            return self._drop("no_actor")
        if qm not in SOL_QUOTES:
            self.coin_ok[mint] = False
            return self._drop("non_sol_quote")
        if not (isinstance(vq, int) and isinstance(vb, int) and vq > 0 and vb > 0 and isinstance(qa, int) and isinstance(ta, int) and qa >= 0 and ta >= 0):
            return self._drop("bad_reserves")
        pre_q, pre_b = (vq - qa, vb + ta) if buy else (vq + qa, vb - ta)
        if mint not in self.coin_ok:
            self.coin_ok[mint] = abs(pre_q / INIT_Q - 1) <= INIT_TOL and abs(pre_b / INIT_B - 1) <= INIT_TOL
        if not self.coin_ok[mint]:
            return self._drop("nonstandard_start")
        if pre_q <= 0 or pre_b <= 0:
            return self._drop("bad_reserves")
        # The REAL quote reserve before the swap comes from the row's authoritative real_quote (post-swap): buys put the net qa in, sells take the GROSS qa out
        # (checked on 171,517 consecutive 10-23 swaps: pre = post -/+ qa equals the previous swap's post in 99.94%). Unknown stays None: that burst is not counted.
        pre_real = None
        if isinstance(real_quote, int) and real_quote >= 0:
            pre_real = (real_quote - qa if buy else real_quote + qa)
            pre_real = pre_real / 1e9 if pre_real >= 0 else None
        row = {"venue": "curve", "pool": pool, "mint": mint, "signature": sig, "ordinal": ordinal, "slot": slot, "known_at": float(recv), "is_buy": bool(buy),
               "actor": actor, "quote_sol": qa / 1e9, "price_sol": vq / vb * 1e-3, "price_before_sol": pre_q / pre_b * 1e-3, "pre_real_quote_sol": pre_real}
        if tx_index is not None:
            row["tx_index"] = tx_index
        return row, None

    def _drop(self, why):
        self.dropped[why] += 1
        return None, why


class CurveTail:
    """Keyset tail of data/desk/swaps/<day>.db by (slot, received_at, ordinal, signature): a batch that ends inside a slot resumes inside it, nothing is skipped
    or repeated. `exhausted` is true only when a page came back short on the newest day (a caught-up tail); it is the completeness signal, separate from the
    highest slot seen. A missing day is a recorded hole. recheck() looks behind the cursor for rows that were written late: counted, never fed."""

    def __init__(self, desk_dir, day, *, batch=20000, recheck_slots=150, today=_today, normalizer=None):
        self.desk, self.day, self.batch, self.recheck_slots, self.today = desk_dir, day, batch, recheck_slots, today
        self.norm = normalizer or CurveNormalizer()
        self.cursor = None                    # (slot, received_at, ordinal, signature) of the last consumed row of the current day
        self.seen = {}                        # (signature, ordinal) -> slot, bounded to the recheck window
        self.holes, self.late_rows, self.db_errors, self.rows_fed = [], 0, 0, 0

    def _path(self, day):
        return "%s/swaps/%s.db" % (self.desk, day)

    def _quote_sel(self, con):
        cols = {r[1] for r in con.execute("pragma table_info(swaps)")}
        return ("quote_mint" if "quote_mint" in cols else "NULL") + ", " + ("real_quote" if "real_quote" in cols else "NULL")

    def _page(self, limit):
        """Up to `limit` raw rows after the cursor, FAILED on a db error (never an empty page that would read as exhausted), None if the day file is absent."""
        path = self._path(self.day)
        if not os.path.exists(path):
            return None
        try:
            con = _ro(path)
            try:
                qsel = self._quote_sel(con)
                out = []
                if self.cursor is not None:
                    s, t, o, sig = self.cursor
                    out += con.execute("select %s, %s from swaps where slot = ? and (received_at > ? or (received_at = ? and (ordinal > ? or (ordinal = ? and signature > ?)))) "
                                       "order by received_at, ordinal, signature limit ?" % (CURVE_COLS, qsel), (s, t, t, o, o, sig, limit)).fetchall()
                    lo = s
                else:
                    lo = -1
                if len(out) < limit:
                    out += con.execute("select %s, %s from swaps where slot > ? order by slot, received_at, ordinal, signature limit ?" % (CURVE_COLS, qsel),
                                       (lo, limit - len(out))).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            self.db_errors += 1
            return FAILED
        return out

    def _advance(self):
        self.day = _next_day(self.day)
        self.cursor = None
        self.seen = {}

    def poll(self):
        """-> {'rows': detector rows, 'items': [('row', row) | ('gap', (start, end, cause, pool))] in tape order, 'exhausted': bool, 'failed': bool, 'day': str}.
        exhausted is true only when a page came back SHORT of the limit actually requested on the newest day; a failed read is never exhausted."""
        items, consumed, hops = [], 0, 0
        while True:
            limit = self.batch - consumed
            page = self._page(limit)
            if page is FAILED:
                return self._result(items, False, True)
            if page is None:                                            # the day's file does not exist
                if self.day < self.today() and hops < MAX_DAY_HOPS:
                    self.holes.append((self.day, "day_missing"))
                    self._advance()
                    hops += 1
                    continue
                return self._result(items, not items, False, missing=True)
            for r in page:
                self.cursor = (r[0], r[11], r[2], r[1])
                self.seen[(r[1], r[2])] = r[0]
                row, why = self.norm.normalize(r)
                if row is not None:
                    items.append(("row", row))
                    self.rows_fed += 1
                elif why in RELEVANT_DROPS:                              # a buy/sell of a scored coin we cannot read: that pool's window is unknown
                    items.append(("gap", (float(r[11]), float(r[11]), "dropped_" + why, r[4])))
            consumed += len(page)
            if consumed >= self.batch:
                return self._result(items, False, False)
            if len(page) < limit:                                       # short against the limit we asked for: this day is drained
                if self.day < self.today() and hops < MAX_DAY_HOPS:
                    self._advance()
                    hops += 1
                    continue
                return self._result(items, True, False)

    def _result(self, items, exhausted, failed, missing=False):
        return {"rows": [x[1] for x in items if x[0] == "row"], "items": items, "exhausted": exhausted, "failed": failed, "day": self.day, "missing": missing}

    def recheck(self):
        """Rows behind the cursor inside the trailing slot window that we never consumed: late writes. Counted once each, never fed; returned as
        [(received_at, pool)] so the caller marks those pools' target windows incomplete. A db error here is counted and returns None (not 'nothing late')."""
        if self.cursor is None:
            return []
        s = self.cursor[0]
        path = self._path(self.day)
        if not os.path.exists(path):
            return []
        try:
            con = _ro(path)
            try:
                found = con.execute("select slot, signature, ordinal, curve, received_at from swaps where slot >= ? and slot < ?", (s - self.recheck_slots, s)).fetchall()      # strictly behind the cursor slot: rows ahead of the cursor inside it are simply not read yet
            finally:
                con.close()
        except sqlite3.Error:
            self.db_errors += 1
            return None
        late = []
        for slot, sig, o, pool, recv in found:
            if (sig, o) not in self.seen:
                self.seen[(sig, o)] = slot
                late.append((float(recv), pool))
        self.late_rows += len(late)
        self.seen = {k: v for k, v in self.seen.items() if v >= s - 2 * self.recheck_slots}
        return late


# ----------------------------------------------------------------------------------------------------------- group state + coverage
import json
import math
import time
from collections import deque

SILENCE_S = 10.0              # a gap this long inside the curve tape is an outage, not a quiet market (~50 swaps/s)
TARGET_WINDOW_S = 60.0
MAX_HOLES_PUBLISHED = 50


class Exclusions:
    """Our own trades must never read as a dump group. Primary: our wallet addresses (every own trade is signed by them), loaded once from the funded
    wallet config (MEMECOIN_OWN_ADDRESSES), static. Secondary: our fill signatures from the ledger, refreshed on a timer; a failed refresh keeps the last
    set and is reported, it never invalidates the address match. `valid` is false (no kill authority) when no own address is configured."""

    def __init__(self, own_addresses, *, ledger=None, refresh_s=60.0, clock=time.time):
        self.addresses = frozenset(a for a in own_addresses if a)
        self.signatures = frozenset()
        self.ledger, self.refresh_s, self.clock = ledger, refresh_s, clock
        self.loaded_at, self.sig_refresh_at, self.sig_refresh_ok, self.next_refresh = clock(), None, None, 0.0
        self.version = 0

    def maybe_refresh(self):
        now = self.clock()
        if self.ledger is None or now < self.next_refresh:
            return
        self.next_refresh = now + self.refresh_s
        try:
            sigs = frozenset(self.ledger())
        except Exception:
            self.sig_refresh_ok = False
            return
        if sigs != self.signatures:
            self.version += 1
        self.signatures, self.sig_refresh_at, self.sig_refresh_ok = sigs, now, True

    def excluded(self):
        return self.addresses | self.signatures

    @property
    def valid(self):
        return bool(self.addresses)

    def state(self):
        return {"valid": self.valid, "own_addresses": len(self.addresses), "loaded_at": self.loaded_at, "signatures": len(self.signatures),
                "signature_refresh_at": self.sig_refresh_at, "signature_refresh_ok": self.sig_refresh_ok}


class GroupState:
    """Feeds ONE venue's rows (global known_at order) through the pure detector, keeps the episodes of the last 24 h, the last 60 s of buy rows per pool and the
    coverage gaps, and produces per-pool score dicts. No I/O. Kill authority needs target_complete AND exclusions_valid AND the buyer predicate proven (Codex).
    A gap (global, or for one pool) RETRACTS every episode whose window overlaps it and resets the detector's state for the affected pools: a burst whose
    predicates may depend on a missing row is not counted, now or when the gap ages out of the 60 s target window."""
    WINDOW_S = 3.0                                   # 4 consecutive slots plus margin

    def __init__(self, detector, exclusions, *, lookback_s=86400.0):
        self.det, self.excl, self.lookback_s = detector, exclusions, lookback_s
        self._base_excluded, self._excl_version = frozenset(detector.excluded), None
        self._sync_exclusions()
        self.episodes, self.retracted = {}, {}
        self.buys = {}                        # (venue, pool) -> deque of buy rows (last 120 s)
        self.mints = {}
        self.active = {}                      # (venue, pool) -> last known_at fed (for the detector reset)
        self.gaps = []                        # (start, end, cause, pool|None, venue|None) in host time
        self.pending = []                     # gaps ahead of the fed cursor, applied when the feed reaches them
        self.rows_fed, self.last_known_at, self.errors, self.out_of_order, self.rejected_episodes = 0, None, 0, 0, 0
        self.last_by_venue = {}

    def _sync_exclusions(self):
        """The detector's exclusion set follows ours (addresses AND signatures) whenever the signature set changes."""
        if self._excl_version != self.excl.version:
            self.det.excluded = self._base_excluded | self.excl.excluded()
            self._excl_version = self.excl.version

    def add_gap(self, start, end, cause, pool=None, venue=None):
        """Schedule a gap: applied immediately when the fed cursor is already past it (retraction), else when the feed reaches it."""
        g = (float(start), float(end), cause, pool, venue)
        if self.last_known_at is not None and g[0] <= self.last_known_at:
            self.note_gap(*g)
        else:
            self.pending.append(g)
            self.pending.sort()

    def note_gap(self, start, end, cause, pool=None, venue=None):
        self.gaps.append((float(start), float(end), cause, pool, venue))
        lo, hi = start - self.WINDOW_S, end + self.WINDOW_S
        for eid in [k for k, e in self.episodes.items()
                    if (pool is None or e["pool"] == pool) and (venue is None or e["venue"] == venue)
                    and e.get("first_sell_known_at", e["detected_at"]) - self.WINDOW_S <= hi and e["known_at"] + self.WINDOW_S >= lo]:
            self.retracted[eid] = self.episodes.pop(eid)["detected_at"]
        for key in [k for k, at in self.active.items() if (pool is None or k[1] == pool) and (venue is None or k[0] == venue) and at >= lo]:
            self.det.close(*key)                         # the open window may hold rows that depend on the missing one: start clean after the gap
            self.active.pop(key, None)

    def feed(self, row):
        at = row["known_at"]
        if self.last_known_at is not None and at < self.last_known_at:
            self.out_of_order += 1                                       # the pure feed needs nondecreasing known_at: a row behind the cursor is a gap, never fed
            self.add_gap(at, self.last_known_at, "out_of_order", row["pool"], row["venue"])
            return None
        lv = self.last_by_venue.get(row["venue"])
        if lv is not None and at - lv > SILENCE_S:                       # per venue: one venue's outage is not hidden by the other's traffic
            self.note_gap(lv, at, "tape_silence", None, row["venue"])
        self.last_by_venue[row["venue"]] = at
        self.last_known_at = at if self.last_known_at is None else max(self.last_known_at, at)
        while self.pending and self.pending[0][0] <= at:
            self.note_gap(*self.pending.pop(0))
        self.rows_fed += 1
        key = (row["venue"], row["pool"])
        self.mints[key] = row["mint"]
        self.active[key] = at
        if row["is_buy"] and not row.get("price_only"):
            dq = self.buys.setdefault(key, deque())
            dq.append(row)
            while dq and dq[0]["known_at"] < at - 120.0:
                dq.popleft()
        self._sync_exclusions()
        ev = self.det.feed(row)
        if ev is not None and ev["id"] not in self.retracted:
            if self._overlaps_gap(ev):
                self.retracted[ev["id"]] = ev["detected_at"]                 # prospective invalidation: a burst supported by rows around a KNOWN gap is never stored
                self.episodes.pop(ev["id"], None)
                self.rejected_episodes += 1
            else:
                self.episodes[ev["id"]] = ev
        return ev

    def _overlaps_gap(self, ev):
        lo, hi = ev.get("first_sell_known_at", ev["detected_at"]) - self.WINDOW_S, ev["known_at"] + self.WINDOW_S
        return any(g[0] - self.WINDOW_S <= hi and g[1] + self.WINDOW_S >= lo and (g[3] is None or g[3] == ev["pool"]) and (g[4] is None or g[4] == ev["venue"])
                   for g in self.gaps)

    def _apply_pending(self, asof):
        """A gap scheduled ahead of the fed cursor still counts for any decision cutoff at or past its start (a quiet tape feeds nothing to pull it in)."""
        while self.pending and self.pending[0][0] <= asof:
            self.note_gap(*self.pending.pop(0))

    def target_complete(self, asof, pool=None, venue=None):
        """No known gap (global, this venue's, or this pool's) overlaps the trailing 60 s buyer window of the decision cutoff."""
        self._apply_pending(asof)
        lo = asof - TARGET_WINDOW_S
        return not any(g[1] > lo and g[0] < asof and (pool is None or g[3] is None or g[3] == pool) and (venue is None or g[4] is None or g[4] == venue)
                       for g in self.gaps)

    def prune(self, asof):
        floor = asof - self.lookback_s
        self.episodes = {k: v for k, v in self.episodes.items() if v["detected_at"] > floor}
        self.retracted = {k: v for k, v in self.retracted.items() if v > floor}
        self.gaps = [g for g in self.gaps if g[1] > floor]
        self.active = {k: v for k, v in self.active.items() if v >= asof - 600.0}
        for key in [k for k, dq in self.buys.items() if not dq or dq[-1]["known_at"] < asof - 120.0]:
            self.buys.pop(key, None)
            self.mints.pop(key, None)

    def scores(self, asof):
        from sniper.services.memecoin_desk_dump_group import actor_flags, buyer_shares
        self._apply_pending(asof)
        excluded = self.excl.excluded()
        flags = actor_flags(list(self.episodes.values()), asof=asof, excluded=excluded)
        excl_ok, out = self.excl.valid, {}
        for (venue, pool), dq in self.buys.items():
            rows = [r for r in dq if asof - TARGET_WINDOW_S < r["known_at"] <= asof]
            if not rows:
                continue
            target_ok = self.target_complete(asof, pool, venue)
            s = buyer_shares(rows, flags, asof=asof, excluded=excluded)
            s.update(mint=self.mints[(venue, pool)], pool=pool, venue=venue, target_complete=target_ok, exclusions_valid=excl_ok)
            proven = s["status"] == "kill" and target_ok and excl_ok
            s["positive_proven"] = proven
            if s["status"] == "kill" and not proven:
                s.update(status="unknown", reason="kill_not_proven", would_block=True)         # diagnostic only: never a kill without proof of the target window
            out[pool] = s
        return out


def venue_coverage(*, warm, cutoff, now, state, tail, holes, exclusions, capture_alive, history_start, lookback_s=86400.0, venue=None):
    """The coverage block the cache publishes for one venue. complete = warm AND no hole/gap inside the 24 h lookback AND exclusions valid AND capture alive
    AND the cutoff is current. Partial coverage still permits POSITIVE blocks (the cache requires the per-score proofs); it never certifies a coin clean."""
    floor = cutoff - lookback_s
    inside = [h for h in holes if h[1] > floor and (venue is None or len(h) < 5 or h[4] in (None, venue))]
    memory = None if history_start is None else cutoff - history_start
    sound = bool(warm and exclusions.valid and capture_alive and (now - cutoff) <= 5.0 and memory is not None and memory >= lookback_s - 60.0)       # everything complete needs EXCEPT "no hole anywhere"
    complete = bool(sound and not inside)
    return {"complete": complete, "sound": sound, "warm": bool(warm), "history_start": history_start, "memory_s": None if memory is None else round(memory, 1), "cutoff": cutoff, "lag_s": round(now - cutoff, 3), "n_holes": len(inside),
            "holes": [list(h[:3]) for h in inside[-MAX_HOLES_PUBLISHED:]], "late_rows": tail.late_rows, "rows_fed": tail.rows_fed, "db_errors": tail.db_errors,
            "dropped": dict(tail.norm.dropped), "capture_alive": bool(capture_alive), "exclusions": exclusions.state(), "detector_errors": state.errors}


def ps_window_holes(gaps, pool, created_at, asof):
    """Amendment 31: the holes that overlap the coin's OWN pool window [created - 60 s, asof], from the venue's gap tuples (start, end, cause, pool, venue).
    holes_any counts every PumpSwap-venue hole overlapping the window (proven-scope and unknown); holes_blocking counts those of UNKNOWN scope (pool None) or proven for THIS pool.
    An unknown creation time makes the window start unknown (created_known False): the cache then keeps the pass unknown. Pure."""
    if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (created_at, asof))
            or not 0 <= created_at <= asof):
        return {"created_known": False, "window_from": None, "holes_any": 0, "holes_blocking": 0}
    lo = created_at - 60.0
    inside = [g for g in gaps if (len(g) < 5 or g[4] in (None, "ps")) and g[1] >= lo and g[0] <= asof]
    return {"created_known": True, "window_from": lo, "holes_any": len(inside), "holes_blocking": sum(1 for g in inside if g[3] is None or g[3] == pool)}


def ps_loss_fraction(desk_dir, now, *, hours=24, min_hours=22, tail_bytes=262144):
    """Amendment 31: the venue loss fraction over the trailing complete 24 UTC hours = loss receipts / (swaps + loss receipts), from the hourly census (census_hourly.jsonl: `gaps` receipts, `venue_swaps`).
    None (unknown, never 0) when the file is missing or unreadable, fewer than min_hours hours are present, or the denominator is not positive. Reads only the file's tail."""
    out = {"fraction": None, "hours": 0}
    path = Path(desk_dir) / "pumpswap" / "census_hourly.jsonl"
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return out
    top = int(now // 3600) * 3600
    seen, swaps, losses = set(), 0, 0
    for line in raw.splitlines():
        try:
            r = json.loads(line)
            h, s, g = int(r["hour"]), int(r["venue_swaps"]), int(r["gaps"])
        except (ValueError, KeyError, TypeError):
            continue                                                  # a torn first line of the tail or a bad row is skipped, never read as zero
        if top - hours * 3600 <= h < top and h not in seen and s >= 0 and g >= 0:
            seen.add(h)
            swaps += s
            losses += g
    out["hours"] = len(seen)
    if len(seen) >= min_hours and swaps + losses > 0:
        out["fraction"] = losses / (swaps + losses)
    return out


# -------------------------------------------------------------------------------------------------------------- PumpSwap (canonical wSOL pools)
WSOL = "So11111111111111111111111111111111111111112"
PS_COLS = ("pool, mint, quote_mint, slot, signature, ordinal, tx_index, is_buy, actor, base_atomic, quote_atomic, pre_base_reserve_atomic, "
           "pre_quote_reserve_atomic, post_base_reserve_atomic, post_quote_reserve_atomic, received_at, processed_at")
PS_PROOF_LIKE = "%pumpswap_grpc:buy_event_virtual_quote%"
PS_LOSS_CAUSES = ("decode_loss", "virtual_quote_unproven")      # per-event losses with no pool: pool continuity is the attribution, not these


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class PSNormalizer:
    """One PumpSwap tape row -> the detector row, under Codex's proof contract: the pre-current price uses the most recent ALREADY-known proven virtual quote of
    the pool (memecoin_desk_ps_tape._proof on a legacy buy's payload), never a later one; known_at = max(received_at, processed_at); an unproven price is None, never
    zero. A lost swap is detected per pool by reserve continuity (previous post state != this pre state) and returned as a pool gap."""

    def __init__(self, pool_mints=None):
        from sniper.services import memecoin_desk_ps_tape as PT
        self._proof = PT._proof
        self.pool_mints = pool_mints or {}
        self.vq = {}                                  # pool -> (virtual_quote_atomic, known_at of its proof event)
        self.prev = {}                                # pool -> (post_base, post_quote, known_at) of the last swap with a proven post state
        self.dropped = defaultdict(int)

    def _drop(self, why, gaps=()):
        self.dropped[why] += 1
        return None, why, list(gaps)

    def normalize(self, r):
        from sniper.services.memecoin_desk_dump_group import pumpswap_pre_price
        (pool, mint, qmint, slot, sig, ordinal, tx_index, buy, actor, base, quote, pre_b, pre_q, post_b, post_q, recv, proc, payload) = r
        mint = mint or self.pool_mints.get(pool)
        if mint is None or (qmint not in (None, WSOL)):
            return self._drop("unmapped_pool" if mint is None else "non_sol_quote")
        base_i, quote_i, recv_f = _int(base), _int(quote), recv
        if not actor or buy not in (0, 1) or base_i is None or quote_i is None or quote_i < 0 or not isinstance(recv_f, (int, float)) or slot is None or not sig or ordinal is None:
            return self._drop("bad_row", [(float(recv or 0), float(recv or 0), "dropped_bad_row", pool, "ps")])
        known_at = max(float(recv_f), float(proc)) if isinstance(proc, (int, float)) else float(recv_f)
        gaps = []
        prev = self.prev.get(pool)
        pre_b_i, pre_q_i0 = _int(pre_b), _int(pre_q)
        if prev is not None and pre_b_i is not None and ((prev[0] is not None and prev[0] != pre_b_i) or (prev[1] is not None and pre_q_i0 is not None and prev[1] != pre_q_i0)):
            gaps.append((prev[2], known_at, "ps_lost_swap", pool, "ps"))             # the pool state moved without a swap we hold: a lost row
        carried = self.vq.get(pool)
        price = None
        if carried is not None:
            price = pumpswap_pre_price(pre_b, pre_q, virtual_quote_atomic=carried[0], state_known_at=known_at, virtual_quote_known_at=carried[1])
        proof = self._proof(payload) if payload else None
        if proof is not None:
            self.vq[pool] = (proof, known_at)
        # What the next row's pre-state must equal: a buy states its post reserves; a sell states none, but adds exactly base_atomic to the base reserve
        # (10,363 of 10,413 consecutive pairs after sells match on the live tape; the quote side is checked only where a post state exists).
        exp_base = _int(post_b) if post_b is not None else (pre_b_i + base_i if (not buy and pre_b_i is not None) else None)
        exp_quote = _int(post_q) if buy else None     # a sell's decoded quote post is an observation, never a gap witness
        self.prev[pool] = (exp_base, exp_quote, known_at) if (exp_base is not None or exp_quote is not None) else None
        pre_q_i = _int(pre_q)
        row = {"venue": "ps", "pool": pool, "mint": mint, "signature": sig, "ordinal": ordinal, "slot": slot, "known_at": known_at, "is_buy": bool(buy), "actor": actor,
               "quote_sol": quote_i / 1e9, "price_sol": price, "price_before_sol": price, "price_known_at": known_at, "price_before_known_at": known_at,
               "pre_real_quote_sol": None if pre_q_i is None else pre_q_i / 1e9}
        if tx_index is not None:
            row["tx_index"] = tx_index
        return row, None, gaps


def load_pool_mints(desk_dir):
    """{pool: token mint} from the PumpSwap capture's side table (status 'read', wSOL quote only)."""
    p = "%s/pumpswap/pool_mints.db" % desk_dir
    if not os.path.exists(p):
        return {}
    try:
        con = _ro(p)
        try:
            return {pl: m for pl, m, q in con.execute("select pool, base_mint, quote_mint from pool_mints where status='read'") if q == WSOL}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


class PSTail:
    """Tail of data/desk/pumpswap/tape.db by received_at (index swaps_ps_received). The cursor is the last FULLY consumed received_at: a page that ends inside a tie
    group drops that group and re-reads it next time (no skipped or repeated row, and no multi-column ORDER BY that would sort the whole slice). Same exhausted /
    failed / recheck contract as CurveTail."""

    def __init__(self, desk_dir, start_at, *, batch=20000, recheck_s=30.0, normalizer=None):
        self.desk, self.batch, self.recheck_s = desk_dir, batch, recheck_s
        self.cursor = float(start_at)
        self.norm = normalizer or PSNormalizer(load_pool_mints(desk_dir))
        self.seen = {}                                # (signature, ordinal, pool) -> received_at, bounded to the recheck window
        self.holes, self.late_rows, self.db_errors, self.rows_fed = [], 0, 0, 0
        self.day = "ps"

    def _path(self):
        return "%s/pumpswap/tape.db" % self.desk

    def _fetch(self, con, sql, args):
        return con.execute(sql, args).fetchall()

    def poll(self):
        path = self._path()
        if not os.path.exists(path):
            return {"rows": [], "items": [], "exhausted": True, "failed": False, "day": self.day, "missing": True}
        sel = ("select %s, CASE WHEN is_buy=1 AND payload_json LIKE ? THEN payload_json END from swaps_ps where received_at > ? order by received_at limit ?" % PS_COLS)
        try:
            con = _ro(path)
            try:
                page = self._fetch(con, sel, (PS_PROOF_LIKE, self.cursor, self.batch))
                if len(page) >= self.batch:                              # full page: drop the trailing tie group (it may continue past the limit)
                    last = page[-1][15]
                    kept = [r for r in page if r[15] != last]
                    if kept:
                        page = kept
                    else:                                                # the whole page is one received_at: read that group completely
                        page = self._fetch(con, sel.replace("received_at > ?", "received_at = ?").replace(" limit ?", ""), (PS_PROOF_LIKE, last))
                    short = False
                else:
                    short = True
            finally:
                con.close()
        except sqlite3.Error:
            self.db_errors += 1
            return {"rows": [], "items": [], "exhausted": False, "failed": True, "day": self.day, "missing": False}
        page.sort(key=lambda r: (r[15], r[3], -1 if r[6] is None else r[6], r[5], r[4]))
        items = []
        for r in page:
            self.seen[(r[4], r[5], r[0])] = r[15]
            row, why, gaps = self.norm.normalize(r)
            items += [("gap", g) for g in gaps]
            if row is not None:
                items.append(("row", row))
                self.rows_fed += 1
        if page:
            self.cursor = page[-1][15]
        return {"rows": [x[1] for x in items if x[0] == "row"], "items": items, "exhausted": short, "failed": False, "day": self.day, "missing": False}

    def recheck(self):
        """Rows inside the trailing received_at window that we never consumed (written late): counted once each, returned as [(received_at, pool)]; None on a db error."""
        path = self._path()
        if not os.path.exists(path):
            return []
        try:
            con = _ro(path)
            try:
                found = con.execute("select signature, ordinal, pool, received_at from swaps_ps where received_at > ? and received_at <= ?",
                                    (self.cursor - self.recheck_s, self.cursor)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            self.db_errors += 1
            return None
        late = []
        for sig, o, pool, recv in found:
            if (sig, o, pool) not in self.seen:
                self.seen[(sig, o, pool)] = recv
                late.append((float(recv), pool))
        self.late_rows += len(late)
        self.seen = {k: v for k, v in self.seen.items() if v > self.cursor - 2 * self.recheck_s}
        return late
