"""Chain recomputes for gate_instrument_audit.py (`chain` subcommand). Research key only.

Every call waits for a flat funded Solana book (0 open real positions, 0 reserved entries) and uses
funded_exit_truth.rpc on MEMECOIN_RESEARCH_HELIUS_RPC_URL, one job at a time. Samples are seeded.

  source floor   the source's buy rebuilt from its own transaction: GROSS = what the trader's
                 account paid (lamports net of fee and created-account rent, plus USDC/WSOL token
                 outflow), NET = what the pool received (dump_forensics.trade_of). Against the size the
                 gate read (trace substage source_buy_floor.source_size_usd).
  coin age       the mint's first transaction on chain (getTransactionsForAddress asc, limit 1)
                 against the age the gate printed. blockTime is whole seconds, fine against 120 s.
  mcap supply    getTokenSupply x the gate's own source price, against the gate's effective_mcap.
                 This checks the SUPPLY the mcap assumes, not the price.
SOL is valued at one SOL/USD for the whole sample (--sol-usd); a ratio test uses a 0.5x-2x band so
a +-15% move in SOL cannot flip a verdict.
"""
import contextlib
import json
import random
import re
import sys
from datetime import datetime

sys.path.insert(0, "/home/bigpapi/Desktop/Sniper/scripts/research")
SEED = 20260928
USDC = "EPjFWdd5AufqSSqeM2qN1xYaGNcuyVxv8m1QvbnvhbjB"
WSOL = "So11111111111111111111111111111111111111112"
RENT = 2039280


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def flat_gate():
    from slot_minout_grading import wait_flat
    from sniper.persistence.db_core import get_readonly_connection
    wait_flat(lambda: get_readonly_connection("/home/bigpapi/Desktop/Sniper/sniper_tracker.db"))


def rpc(method, params):
    import funded_exit_truth as fet
    fet.SPACING_S = 0.5
    flat_gate()
    return fet.rpc(method, params)


def source_sig(origin):
    o = str(origin or "")
    if o.startswith("swap:raybot:"):
        return o.split(":")[2]
    if o.startswith("onchain:") or o.startswith("slotlane:"):
        return o.split(":", 1)[1]
    return None


def gross_spend_usd(tx, trader, sol_usd):
    """What `trader` paid in this transaction: SOL out net of fee and new-account rent, plus USDC
    and WSOL token outflow. None when the trader is not a signer-side account in the message."""
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = list(msg.get("accountKeys") or [])
    loaded = meta.get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    if trader not in keys:
        return None
    i = keys.index(trader)
    lamports = meta["preBalances"][i] - meta["postBalances"][i]
    if i == 0:
        lamports -= meta.get("fee") or 0
    created = sum(1 for b in (meta.get("postTokenBalances") or []) if b.get("owner") == trader) - \
        sum(1 for b in (meta.get("preTokenBalances") or []) if b.get("owner") == trader)
    lamports -= max(0, created) * RENT
    usd = max(0, lamports) / 1e9 * sol_usd
    for mint, px in ((USDC, 1.0), (WSOL, sol_usd)):
        pre = sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get("preTokenBalances") or []
                  if b.get("owner") == trader and b.get("mint") == mint)
        post = sum(int(b["uiTokenAmount"]["amount"]) for b in meta.get("postTokenBalances") or []
                   if b.get("owner") == trader and b.get("mint") == mint)
        usd += max(0, pre - post) / (1e6 if mint == USDC else 1e9) * px
    return usd


def source_floor(conn, since, n, sol_usd, rng):
    import dump_forensics as dfx
    rows = conn.execute(
        "SELECT attempt_id, signal_source, mint, terminal_reason, origin_event_id, trace_json FROM "
        "memecoin_entry_attempts WHERE network_id='solana' AND decision_started_at>=? AND terminal_reason "
        "LIKE 'source buy%'", (since,)).fetchall()
    pool = []
    for r in rows:
        size = next((e.get("source_size_usd") for e in (json.loads(r["trace_json"] or "{}")
                                                         .get("substage_events") or [])
                     if e.get("name") == "source_buy_floor"), None)
        if source_sig(r["origin_event_id"]):
            pool.append((r, size))
    out = []
    for lane in ("onchain_watch", "follow_activity"):
        cand = [p for p in pool if p[0]["signal_source"] == lane]
        for r, size in rng.sample(cand, min(n, len(cand))):
            flat_gate()
            tx = dfx.get_tx(source_sig(r["origin_event_id"]))
            if not tx:
                out.append((lane, r["attempt_id"][:8], size, None, None, "tx_not_found"))
                continue
            t = dfx.trade_of(tx, r["mint"], sol_usd)
            net = (t["sol"] * sol_usd) if t and t.get("side") == "buy" else None
            gross = gross_spend_usd(tx, t["trader"], sol_usd) if t else None
            out.append((lane, r["attempt_id"][:8], size, gross, net,
                        "no_buy_decoded" if not t else t.get("side")))
    return out


def coin_age(conn, since):
    rows = conn.execute("SELECT attempt_id, network_id, mint, decision_started_at, terminal_reason FROM "
                        "memecoin_entry_attempts WHERE decision_started_at>=? AND terminal_reason LIKE "
                        "'coin_too_young%' AND network_id='solana'", (since,)).fetchall()
    out = []
    for r in rows:
        res = rpc("getTransactionsForAddress", [r["mint"], {"transactionDetails": "signatures",
                                                            "sortOrder": "asc", "limit": 1}]) or {}
        first = (res.get("data") or [None])[0]
        claimed = float(re.search(r"(\d+)s old", r["terminal_reason"]).group(1))
        if not first or first.get("blockTime") is None:
            out.append((r["attempt_id"][:8], claimed, None))
            continue
        age = (utc(r["decision_started_at"]).timestamp() - first["blockTime"])
        out.append((r["attempt_id"][:8], claimed, round(age, 1)))
    return out


def mcap_supply(conn, since, n, rng):
    rows = conn.execute("SELECT attempt_id, mint, source_price, classification_json FROM memecoin_entry_attempts "
                        "WHERE network_id='solana' AND decision_started_at>=? AND terminal_reason LIKE "
                        "'mcap $%over ceiling%' AND source_price>0", (since,)).fetchall()
    out = []
    for r in rng.sample(rows, min(n, len(rows))):
        facts = (json.loads(r["classification_json"] or "{}").get("facts") or {})
        eff, basis = facts.get("effective_mcap"), facts.get("mcap_basis")
        sup = rpc("getTokenSupply", [r["mint"]]) or {}
        amount = (sup.get("value") or {}).get("uiAmount")
        rebuilt = amount * r["source_price"] if amount else None
        out.append((r["attempt_id"][:8], basis, eff, rebuilt,
                    round(eff / rebuilt, 3) if eff and rebuilt else None))
    return out


def main(args):
    from sniper.persistence.db_core import get_readonly_connection
    import slot_lane_send_monitor as mon
    from sniper.execution.solana_rpc_pool import key_id
    rng = random.Random(SEED)
    sol_usd = float(getattr(args, "sol_usd", 0) or 118.6)
    flat_gate()
    print("research key %s; SOL/USD %.2f; since %s" % (key_id(mon.rpc_url()), sol_usd, args.since))
    with contextlib.closing(get_readonly_connection("/home/bigpapi/Desktop/Sniper/sniper_tracker.db")) as conn:
        print("\nSOURCE FLOOR: gate size vs the source tx rebuilt (gross paid / net to pool), USD")
        rows = source_floor(conn, args.since, 20, sol_usd, rng)
        for lane in ("onchain_watch", "follow_activity"):
            sub = [x for x in rows if x[0] == lane]
            bad = [x for x in sub if x[2] is not None and x[3] and not (0.5 <= (x[2] or 0) / x[3] <= 2)]
            print("  %s: n=%d; gate size outside 0.5x-2x of gross paid: %d" % (lane, len(sub), len(bad)))
            for x in sub:
                print("    %s gate %s gross %s net %s %s" % (x[1], None if x[2] is None else round(x[2], 2),
                                                           None if x[3] is None else round(x[3], 2),
                                                           None if x[4] is None else round(x[4], 2), x[5]))
        print("\nCOIN AGE: printed age vs first on-chain tx")
        for x in coin_age(conn, args.since):
            print("    %s printed %.0f s, chain %s s" % x)
        print("\nMCAP SUPPLY: effective_mcap / (getTokenSupply x gate source price)")
        for x in mcap_supply(conn, args.since, 20, rng):
            print("    %s basis %s eff %s rebuilt %s ratio %s" % x)
    return 0
