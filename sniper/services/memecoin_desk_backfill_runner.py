"""Helius history backfill runner (docs/superpowers/specs/2026-10-27-helius-history-backfill-design.md sections 3, 4 and 6). LIBRARY ONLY: the transport and the swap decoder are
injected, there is no CLI and no HTTP client here, so importing or testing this module can never reach a provider.

Targets are written to `backfill_targets` BEFORE any fetch (the denominator is fixed; an unfetched target is a typed gap, never absent). Every request goes through the ledger:
reserve (on disk) -> send -> classify -> commit or mark_ambiguous. A quota answer stops the whole run and persists the stop. A pool is `complete` only if pagination finished, every
row decoded, no store claim of this pool is pending and the reserves chain; a break is marked `reserve_break`, never repaired."""
import json
import sqlite3
import time
from pathlib import Path

from sniper.services import memecoin_desk_backfill_ledger as L
from sniper.services import memecoin_desk_backfill_store as S

METHOD = "getTransactionsForAddress"
MAX_PAGES = 200
MAX_CONSECUTIVE_AMBIGUOUS = 3
GAPS = ("credits_exhausted", "max_usage", "unreadable_tx", "pagination_incomplete", "rate_limited", "decode_unsupported", "transport_error", "identity_conflict", "rpc_error", "http_error",
        "no_cycle")
DDL = """
create table if not exists backfill_targets(day text not null, kind text not null, network_id text not null, pool text not null, mint text, priority integer not null,
  state text not null check(state in ('pending','done','gap')), gap text, created_at real not null, primary key(day, network_id, pool));
create table if not exists pool_runs(day text not null, network_id text not null, pool text not null, status text not null check(status in ('complete','reserve_break','partial','gap')),
  gap text, credits_used integer, rows integer, first_slot integer, last_slot integer, finished_at real not null, primary key(day, network_id, pool));
"""
_REFUSAL_GAP = {"month_cap": "credits_exhausted", "run_cap": "credits_exhausted", "quota_stop": "max_usage", "cost_bound_exceeded": "max_usage",
                "cycle_expired": "no_cycle", "no_cycle_attestation": "no_cycle", "unknown_method_cost": "no_cycle", "credits_invalid": "no_cycle"}


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("pragma journal_mode=wal")
    con.executescript(DDL)
    return con


def write_targets(con, day, targets, *, now):
    """targets: dicts with kind, network_id, pool, mint, priority. Insert-or-ignore: re-writing the list never resets a finished target."""
    for t in targets:
        con.execute("insert or ignore into backfill_targets values(?,?,?,?,?,?, 'pending', NULL, ?)", (day, t["kind"], t["network_id"], t["pool"], t.get("mint"), int(t["priority"]), now))
    con.commit()


def _order_key(r):
    return (r["slot"], r["tx_index"], r["instruction_index"], r["inner_index"], r["event_ordinal"])


def _chain_breaks(rows):
    """True if the pool's reserves do not chain (post[i] != pre[i+1]) over rows with all four reserve fields; absent reserves cannot prove continuity and count as a break."""
    rs = sorted(rows, key=_order_key)
    for a, b in zip(rs, rs[1:]):
        if None in (a.get("post_base"), a.get("post_quote"), b.get("pre_base"), b.get("pre_quote")):
            return True
        if (str(a["post_base"]), str(a["post_quote"])) != (str(b["pre_base"]), str(b["pre_quote"])):
            return True
    return False


class Runner:
    def __init__(self, con, ledger, store, transport, decoder, *, clock=time.time):
        self.con, self.ledger, self.store, self.transport, self.decoder, self.clock = con, ledger, store, transport, decoder, clock
        self.stopped = None

    def _gap_target(self, day, net, pool, gap, kind_status="gap"):
        self.con.execute("update backfill_targets set state='gap', gap=? where day=? and network_id=? and pool=?", (gap, day, net, pool))
        self.con.execute("insert or replace into pool_runs values(?,?,?,?,?,?,?,?,?,?)", (day, net, pool, kind_status, gap, 0, 0, None, None, self.clock()))
        self.con.commit()

    def run(self, day):
        """Process this day's pending targets by priority. -> {pool: status|gap}. Stops the whole run on a quota answer, a cap, or repeated ambiguous failures."""
        out, ambiguous_streak = {}, 0
        pending = self.con.execute("select network_id, pool, mint, priority from backfill_targets where day=? and state='pending' order by priority, pool", (day,)).fetchall()
        for i, (net, pool, mint, _prio) in enumerate(pending):
            if self.stopped:
                self._gap_target(day, net, pool, self.stopped)
                out[pool] = self.stopped
                continue
            res, ambiguous_streak = self._fetch_pool(day, net, pool, ambiguous_streak)
            out[pool] = res
            if ambiguous_streak >= MAX_CONSECUTIVE_AMBIGUOUS and not self.stopped:
                self.stopped = "transport_error"
        return out

    def _fetch_pool(self, day, net, pool, streak):
        rows, sigs, unreadable, token, pages, credits = [], set(), 0, None, 0, 0
        while True:
            if pages >= MAX_PAGES:
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="pagination_incomplete"), streak
            try:
                attempt = self.ledger.reserve(METHOD, page=pool)
            except L.Refusal as exc:
                gap = _REFUSAL_GAP.get(exc.code, "credits_exhausted")
                if gap in ("credits_exhausted", "max_usage", "no_cycle"):
                    self.stopped = gap
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap=gap), streak
            credits += self.ledger.costs.get(METHOD, 0)
            try:
                status, body = self.transport(METHOD, {"address": pool, "paginationToken": token, "network_id": net})
            except Exception:                                    # timeout, reset, anything: the send may have been processed -> charged in full
                self.ledger.mark_ambiguous(attempt)
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="transport_error"), streak + 1
            kind = L.classify(status, body)
            if kind == "quota":
                self.ledger.commit(attempt)
                self.ledger.record_stop("max_usage")
                self.stopped = "max_usage"
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="max_usage"), streak
            if kind == "rate":
                self.ledger.commit(attempt)
                if self.ledger.rate_retry_allowed(pool):
                    continue
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="rate_limited"), streak
            if kind in ("http_error", "malformed"):
                self.ledger.mark_ambiguous(attempt)
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="http_error" if kind == "http_error" else "unreadable_tx"), streak + 1
            self.ledger.commit(attempt)
            if kind == "rpc_error":
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="rpc_error"), 0
            streak = 0
            pages += 1
            try:
                page_rows, token, bad = self.decoder(json.loads(body)["result"])         # classify() == ok guarantees the key
            except Exception:
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="decode_unsupported"), streak
            unreadable += bad
            for r in page_rows:
                try:
                    stored = self.store.insert_swap(r)
                except S.StoreConflict:
                    return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap="identity_conflict"), streak
                except ValueError:
                    unreadable += 1
                    continue
                rows.append(r)
                sigs.add(r["signature"])
                del stored
            if not token:
                return self._finish(day, net, pool, rows, sigs, unreadable, credits, gap=None), streak

    def _finish(self, day, net, pool, rows, sigs, unreadable, credits, *, gap):
        pending_here = [p for p in self.store.pending() if p[0] in sigs]
        if gap is None and unreadable:
            gap = "unreadable_tx"
        if gap is None and pending_here:
            gap = "pagination_incomplete"                        # a claim of this pool is not finalized: not complete
        if gap is not None:
            status = "partial" if rows else "gap"
        else:
            status = "reserve_break" if _chain_breaks(rows) else "complete"
        slots = [r["slot"] for r in rows]
        self.con.execute("update backfill_targets set state=?, gap=? where day=? and network_id=? and pool=?", ("gap" if status in ("gap", "partial") else "done", gap, day, net, pool))
        self.con.execute("insert or replace into pool_runs values(?,?,?,?,?,?,?,?,?,?)", (day, net, pool, status, gap, credits, len(rows), min(slots) if slots else None, max(slots) if slots else None, self.clock()))
        self.con.commit()
        return status if gap is None else (gap if status == "gap" else status)
