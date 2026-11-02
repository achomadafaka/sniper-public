"""Wallet Map coverage: which buys are first buys (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md
section 0; pre-registration docs/audits/2026-09-30-wallet-map-reads-preregistration.md A).

ONE DEFINITION for the graph job (scripts/research/wallet_link_graph.py) and the page
(sniper/services/memecoin_wallet_map.py): the job reads the first buys, the page counts them. It lives
under sniper/ because nothing under sniper/ may reach the research job's imports (the brain
isolation guard test).
Read-only on the ledger.
"""
from __future__ import annotations

import collections
from datetime import datetime, timedelta


def _ts(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def first_id_since(ledger, since_iso, margin=timedelta(days=1)):
    """The lowest id whose received_at is at or after since - margin, by binary search on the rowid.
    memecoin_events is ~65M rows with no received_at index, so a received_at filter alone is a full
    scan of the funded ledger. Ids grow with insert time; the margin covers receipt-vs-insert order,
    and the received_at filter still decides membership."""
    bound = (datetime.fromisoformat(since_iso) - margin).isoformat()
    lo, hi = ledger.execute("SELECT min(id), max(id) FROM memecoin_events").fetchone()
    if lo is None:
        return 0
    while lo < hi:
        mid = (lo + hi) // 2
        row = ledger.execute("SELECT received_at FROM memecoin_events WHERE id >= ? "
                             "AND received_at IS NOT NULL ORDER BY id LIMIT 1", (mid,)).fetchone()
        if row is None or row[0] >= bound:
            hi = mid
        else:
            lo = mid + 1
    return lo


J7_SOLANA_BUYS_SQL = ("SELECT source_user_id, mint, fomo_event_id, received_at, their_size_usd FROM memecoin_events "
                      "WHERE id >= ? AND received_at >= ? AND network_id='solana' AND side='buy' "
                      "AND source_user_id IS NOT NULL AND mint IS NOT NULL")


DETECTION_BUYS_SQL = ("SELECT signature, source_user_id, mint, block_time, detected_at, source_size_usd "
                      "FROM onchain_wallet_detections WHERE network_id='solana' "
                      "AND (token_delta IS NULL OR token_delta > 0) AND detected_at >= ?")


def j7_solana_buys(ledger, since_iso):
    """Every relay-reported Solana buy in the window: its transaction when the id carries one
    ('swap:raybot:<sig>:...', 'pump:<sig>'), None for J7's 'swap:<uuid>' report. An id range, never
    a scan of the ledger."""
    from sniper.persistence.db_memecoin import source_tx_of
    out = []
    for uid, mint, fid, at, usd in ledger.execute(J7_SOLANA_BUYS_SQL, (first_id_since(ledger, since_iso), since_iso)):
        out.append({"source_user_id": str(uid), "mint": mint, "signature": source_tx_of(fid), "t": _ts(at),
                    "usd": usd, "chain": False})
    return out


def coverage_records(ledger, since_iso):
    """The three intakes of the pre-registration (A): J7 reports, watcher detections (chain time when
    the record has one) and our fills that carry their source signature."""
    recs = j7_solana_buys(ledger, since_iso)
    for sig, uid, mint, bt, det_at, usd in ledger.execute(DETECTION_BUYS_SQL, (since_iso,)):
        chain = isinstance(bt, (int, float)) and bt > 0
        recs.append({"source_user_id": str(uid), "mint": mint, "signature": sig, "usd": usd, "chain": chain,
                     "t": float(bt) if chain else _ts(det_at)})
    for f in funded_solana_fills(ledger, since_iso):
        if f.get("source_sig"):
            recs.append({"source_user_id": str(f["source_user_id"]), "mint": f["mint"], "signature": f["source_sig"],
                         "usd": None, "chain": False, "t": _ts(f["opened_at"])})
    return recs


def funded_solana_fills(ledger, since_iso):
    """Funded Solana fills, newest first, each with the exact source signature its origin_event_id
    carries (db_memecoin.source_tx_of), or None."""
    from sniper.persistence.db_memecoin import source_tx_of
    return [dict(zip(("id", "source_user_id", "mint", "opened_at"), r[:4]), source_sig=source_tx_of(r[4]))
            for r in ledger.execute(
                "SELECT id, source_user_id, mint, opened_at, origin_event_id FROM memecoin_positions "
                "WHERE execution_mode='real' AND network_id='solana' AND opened_at >= ? "
                "AND source_user_id IS NOT NULL AND mint IS NOT NULL ORDER BY opened_at DESC", (since_iso,))]


PAIR_S, PAIR_PCT = 120.0, 0.05          # a uuid report and its transaction: the source-buy prior sum's pairing


def coverage_units(records):
    """(units, no_signature) per the pre-registration (A): each (wallet, coin)'s FIRST buy across all
    intakes. One transaction is one trade whoever reported it; its time is its chain time when a
    record has one, else the earliest other stamp. A 'swap:<uuid>' report is the signed trade of the
    same wallet, coin and side within PAIR_S and PAIR_PCT (both sizes known; nearest first, each trade
    once); otherwise it is a trade with no transaction. When the first buy has none, the unit is
    'no_signature' and nothing is read in its place: a later buy has different followers."""
    by = collections.defaultdict(lambda: ({}, []))
    for rec in records:
        trades, uuids = by[(rec["source_user_id"], rec["mint"])]
        if rec["signature"] is None:
            uuids.append(rec)
            continue
        tr = trades.setdefault(rec["signature"], {"chain": None, "other": None, "usd": None})
        slot = "chain" if rec["chain"] else "other"
        tr[slot] = rec["t"] if tr[slot] is None else min(tr[slot], rec["t"])
        if tr["usd"] is None:
            tr["usd"] = rec["usd"]
    units, no_signature = [], []
    for (uid, mint), (trades, uuids) in by.items():
        timed = [(tr["chain"] if tr["chain"] is not None else tr["other"], sig, tr["usd"]) for sig, tr in trades.items()]
        used, lone = set(), []
        for u in sorted(uuids, key=lambda r: r["t"]):
            near = [(abs(t - u["t"]), sig) for t, sig, usd in timed
                    if sig not in used and usd and u["usd"] and abs(t - u["t"]) <= PAIR_S
                    and abs(u["usd"] / usd - 1.0) <= PAIR_PCT + 1e-9]
            if near:
                used.add(min(near)[1])
            else:
                lone.append((u["t"], None, u["usd"]))
        t, sig, _usd = min(timed + lone, key=lambda x: (x[0], x[1] is None))
        (units if sig else no_signature).append({"source_user_id": uid, "mint": mint, "signature": sig, "t": t})
    return units, no_signature
