"""Meteora DBC signals lost on the route: would policy have taken them, and what did the coin do?

Operator decision input (Alpha 13:22Z, 2026-09-27): is a DBC builder worth it? Jupiter answers
TOKEN_NOT_TRADABLE for DBC Token-2022 coins and Raydium has no route, so the signal dies at the
quote. dbc_venue_gap.py finds the lost origins (route failure, no retry filled, pool owned by the
DBC program); this file adds, per lost origin:

  policy    the attempt reached the QUOTE stage, so every pre-quote gate (source-buy floor, mcap
            ceiling, age, volume, safety) had already passed. The chase gate needs a quote and was
            never evaluated; `premium_at_decision` is a PROXY for it: the last pool trade price at
            or before our decision start against the source's price.
  move      the COIN's move, not our PnL: the last pool trade price at or before source time + 5,
            15 and 60 min against the source's own trade price, from the DBC pool's own
            transactions (Helius getTransactionsForAddress on the research key). A trade older than
            the horizon by more than 5 min is flagged stale, which also catches a pool that
            graduated (DBC migrates to another pool; its later trades are not on this address).
            Every price here is pool quote per token WITH fees, the same measure on both sides.

Source time is the source transaction's block time when the origin carries a signature, else the
J7 event's created_at (not chain time; see finding-events-created-at-is-not-source-chain-time).

Research key only; refuses while a real Solana position is open.

    python3 scripts/research/dbc_lost_outcomes.py --since 2026-09-13
    python3 scripts/research/dbc_lost_outcomes.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
HORIZONS = (300, 900, 3600)
STALE_S = 300


def epoch(stamp):
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()


def move(base, later):
    return None if not base or later is None else round((later / base - 1) * 100, 1)


def lost_dbc_origins(attempts, venue):
    """Origins with a route failure, no fill, on a coin whose venue names the DBC program."""
    from dbc_venue_gap import DBC, is_route_gap
    by = collections.defaultdict(list)
    for a in attempts:
        by[a["origin"] or a["attempt_id"]].append(a)
    out = []
    for origin, rows in by.items():
        mint = rows[0]["mint"]
        if DBC not in str(venue.get(mint, "")) or any(r["status"] == "filled" for r in rows):
            continue
        gaps = [r for r in rows if is_route_gap(r["reason"])]
        if gaps:
            out.append({"origin": origin, "mint": mint, "handle": rows[0]["handle"],
                        "attempt_id": gaps[0]["attempt_id"], "reason": gaps[-1]["reason"],
                        "attempts": len(gaps)})
    return out


def price_at(pool, mint, t):
    """(price, block_time) of the last trade on `pool` at or before epoch `t`, or (None, None)."""
    import dump_forensics as dfx
    import wallet_crowd_dump_scoreboard as sb
    txs, _complete = sb.gtfa(pool, {"blockTime": {"lte": int(t)}}, order="desc", limit=10)
    for tx in txs:
        tr = dfx.trade_of(tx, mint, 1.0)
        if tr:
            return tr["price"], tx.get("blockTime")
    return None, None


def describe(c, o, pair_of_mint):
    import dump_forensics as dfx
    from sniper.persistence.db_memecoin import event_transaction_signature
    a = c.execute("SELECT decision_started_at, source_market_cap, trace_json FROM memecoin_entry_attempts "
                  "WHERE attempt_id=?", (o["attempt_id"],)).fetchone()
    stages = (json.loads(a["trace_json"] or "{}").get("stages") or {})
    ev = c.execute("SELECT their_size_usd, created_at FROM memecoin_events WHERE fomo_event_id=?",
                   (o["origin"],)).fetchone()
    sig = event_transaction_signature(o["origin"])
    det = c.execute("SELECT source_size_usd FROM onchain_wallet_detections WHERE signature=?",
                    (sig,)).fetchone() if sig else None
    row = dict(o, decided=a["decision_started_at"], mcap=a["source_market_cap"],
               reached_quote="quote" in stages, pool=pair_of_mint.get(o["mint"]),
               source_usd=(ev["their_size_usd"] if ev and ev["their_size_usd"] else
                           det["source_size_usd"] if det else None))
    src_tx = dfx.get_tx(sig) if sig else None
    if src_tx and src_tx.get("blockTime"):
        row["t0"], row["t0_basis"] = src_tx["blockTime"], "source_tx_block_time"
        tr = dfx.trade_of(src_tx, o["mint"], 1.0)
        row["source_price"] = tr["price"] if tr else None
    elif ev and ev["created_at"]:
        row["t0"], row["t0_basis"], row["source_price"] = epoch(ev["created_at"]), "j7_created_at", None
    else:
        return dict(row, note="no source time")
    if not row["pool"]:
        return dict(row, note="no pool address")
    if row["source_price"] is None:
        row["source_price"], _ = price_at(row["pool"], o["mint"], row["t0"])
    p_dec, t_dec = price_at(row["pool"], o["mint"], epoch(row["decided"]))
    row["premium_at_decision_pct"] = move(row["source_price"], p_dec)
    for h in HORIZONS:
        if row["t0"] + h > datetime.now(timezone.utc).timestamp():
            row[f"move_{h // 60}m_pct"], row[f"stale_{h // 60}m"] = None, "pending"   # not yet reached
            continue
        p, t = price_at(row["pool"], o["mint"], row["t0"] + h)
        row[f"move_{h // 60}m_pct"] = move(row["source_price"], p)
        row[f"stale_{h // 60}m"] = t is None or (row["t0"] + h - t) > STALE_S
    return row


def cashback(c, since):
    rows = c.execute("SELECT handle, mint, signature FROM onchain_wallet_detections WHERE network_id='solana' "
                     "AND detected_at >= ? AND json_extract(source_valuation_json,'$.unknown_reason')"
                     "='unsupported_cashback_semantics'", (since,)).fetchall()
    filled = 0
    for r in rows:
        filled += c.execute("SELECT count(*) FROM memecoin_positions WHERE execution_mode='real' AND "
                            "network_id='solana' AND mint=? AND opened_recorded_at >= ?",
                            (r["mint"], since)).fetchone()[0] > 0
    return {"detections": len(rows), "wallets": len({r["handle"] for r in rows}),
            "mints": len({r["mint"] for r in rows}), "mints_filled_by_any_lane": filled}


def main(since):
    import dbc_venue_gap as g
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    attempts, pair_of_mint = g.load(since)          # refuses while a real Solana position is open
    fet._URL = mon.rpc_url()
    venue, _ = g.venues(attempts, pair_of_mint)
    lost = lost_dbc_origins(attempts, venue)
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = [describe(c, o, pair_of_mint) for o in lost]
        cb = cashback(c, since)
    for r in sorted(rows, key=lambda r: r.get("decided") or ""):
        print(json.dumps({k: r.get(k) for k in (
            "decided", "handle", "mint", "source_usd", "mcap", "reached_quote", "attempts", "reason",
            "t0_basis", "premium_at_decision_pct", "move_5m_pct", "stale_5m", "move_15m_pct",
            "stale_15m", "move_60m_pct", "stale_60m", "note")}))
    print(json.dumps({"lost_dbc_origins": len(rows), "cashback_unknown_since": since, **cb,
                      "rpc_429": fet.RATE_429[0]}))


def selftest():
    assert move(1.0, 1.25) == 25.0 and move(2.0, 1.0) == -50.0
    assert move(None, 1.0) is None and move(1.0, None) is None
    from dbc_venue_gap import DBC
    att = [{"attempt_id": "a1", "origin": "o1", "mint": "M", "handle": "w", "status": "aborted",
            "reason": "no quote (no_route: jupiter 400 TOKEN_NOT_TRADABLE)"},
           {"attempt_id": "a2", "origin": "o2", "mint": "M", "handle": "w", "status": "aborted",
            "reason": "no quote"},
           {"attempt_id": "a3", "origin": "o2", "mint": "M", "handle": "w", "status": "filled", "reason": "filled"},
           {"attempt_id": "a4", "origin": "o3", "mint": "N", "handle": "w", "status": "aborted", "reason": "no quote"},
           {"attempt_id": "a5", "origin": "o4", "mint": "M", "handle": "w", "status": "refused",
            "reason": "chase cap"}]
    lost = lost_dbc_origins(att, {"M": DBC, "N": "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"})
    assert [o["origin"] for o in lost] == ["o1"], lost      # o2 filled on retry, o3 not DBC, o4 policy
    assert lost_dbc_origins(att, {"M": "tx:" + DBC})[0]["origin"] == "o1"   # the tx-fallback venue form
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-13")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else main(a.since)
