#!/usr/bin/env python3
"""Wallet follow funnel: candidate -> roster -> paper -> trial -> armed. Read-only, 0 network calls.

J7 IS OUT OF THE WALLET PATH (operator 2026-10-02, via Alpha): same-slot/S+1 fills come only from our
own on-chain detection (the slot lane subscribes to the armed roster's owners). So a candidate's
commands are the roster row and the trial start; no J7 slot is emitted, no J7 list budget gates
them, and rotation emits no J7 command. The J7 list is still read for the "tracked" stage label and
the candidate filters below (listed for a later change, not dropped here).

Operator order 2026-10-01 night (via Alpha): "the map must tell us exactly who to follow". Paper
funded_clone is the only measure that predicts our funded return (Gamma: Spearman +0.33; a wallet's
own return -0.13; copy-replay v1 void), and paper exists only for wallets J7 relays and the roster
curates. So the job is a funnel that moves new wallets INTO paper:

1. Rotation. Paper copies what the roster curates (`memecoin_wallets` paused=0, excluded=0).
   Rotation names the J7-listed ("tracked"), unarmed wallets that are losing on paper
   (n >= ROTATE_MIN_N, pooled < 0) or silent >= SILENT_DAYS; the operator decides (no command).
2. Candidates, Solana, from the Wallet Map: addresses that bought BEFORE one of our best funded
   wallets (slice-3b early buyers, and same-slot buyers at a lower index), plus shared-funder links
   to a best wallet, plus slice-3b's upstream addresses. Ranked by distinct coins with that evidence.
   EVM chains have no trader identity in our tapes (routers), so their candidates are untracked Fomo
   users from the stored feed (buy-only; their return is unmeasured until paper runs).
3. Every candidate gets its two exact operator commands: the roster row, then the trial start
   (memecoin_trial_arm.py refuses without our own on-chain evidence of a buy in 7 days).

Filters (each only where its data exists; a candidate without the data is "unchecked", never clean):
our own and roster addresses, already J7-tracked, slice-3b copy_bot / hub_funded, same-slot copiers
of our sources (co_buy_slot / slot_follower dst), sniper (coin age <= SNIPE_S at half or more of its
early buys), dev (it created the coin, ps_tokens) and funded by a coin's creator.

    python3 scripts/research/wallet_follow_funnel.py            # text
    python3 scripts/research/wallet_follow_funnel.py --write    # also data/wallet_follow_funnel.json (the map panel)
    python3 scripts/research/wallet_follow_funnel.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import collections
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

ROTATE_MIN_N = 20
SILENT_DAYS = 7
SNIPE_S = 2
TOP = 15


def _ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- 1. the J7 stage label and rotation
def tracked(ledger):
    """{uid: (account, kind, handle)} for every J7-tracked wallet."""
    return {r["source_user_id"]: (int(r["account_index"]), r["kind"], r["handle"])
            for r in ledger.execute("SELECT account_index, kind, source_user_id, handle FROM j7_tracked_wallets")}


def rotation(rows, now_t):
    """Tracked, unarmed, not on trial, and losing on paper at n >= ROTATE_MIN_N or silent >= SILENT_DAYS."""
    out = []
    for r in rows:
        if not r["tracked"] or r["armed"] or r["trial"]:
            continue
        silent_d = (now_t - r["last_t"]) / 86400.0 if r["last_t"] else None
        why = []
        if r["paper_n"] >= ROTATE_MIN_N and (r["paper_ret"] or 0) < 0:
            why.append("paper %+.1f%% on %d" % (100 * r["paper_ret"], r["paper_n"]))
        if silent_d is None or silent_d >= SILENT_DAYS:
            why.append("silent %s" % ("ever" if silent_d is None else "%.0f d" % silent_d))
        if why:
            out.append(dict(r, why=why, silent_d=silent_d))
    return sorted(out, key=lambda r: (r["paper_ret"] if r["paper_ret"] is not None else 0.0))


# ---------------------------------------------------------------- the roster's own funnel rows
def funnel_rows(ledger, now_t):
    """One row per wallet that is tracked or curated: stage, paper and funded records."""
    from wallet_follow_candidates import paper_and_funded, gated_view
    from sniper.persistence import db_memecoin as m
    # The score is ungated paper; config-gated paper is shown beside it with censored shares, and its
    # trailing-14-day count per chain is the trial's feasibility screen (Alpha and Gamma, 2026-10-02).
    paper, funded, gated = paper_and_funded(ledger)
    j7 = tracked(ledger)
    wallets = {r["source_user_id"]: r for r in ledger.execute(
        "SELECT source_user_id, handle, paused, COALESCE(excluded,0) AS excluded FROM memecoin_wallets")}
    trials = set()
    try:
        trials = {r[0] for r in ledger.execute("SELECT source_user_id FROM memecoin_trial_arms WHERE ended_at IS NULL")}
    except sqlite3.OperationalError:
        pass
    last = {r[0]: r[1] for r in ledger.execute(
        "SELECT source_user_id, MAX(created_at) FROM memecoin_events GROUP BY source_user_id")}

    def agg(table, uid):
        n = pnl = 0
        parts = [(k, v) for k, v in table.items() if k[0] == uid and v[0]]
        n = sum(v[0] for _k, v in parts)
        if not n:
            return 0, None
        # Pooled across chains, weighted by close count (each record is already pooled per chain).
        return n, sum(v[1] * v[0] for _k, v in parts) / n
    rows = []
    for uid in sorted(set(j7) | {u for u, w in wallets.items() if not w["paused"] and not w["excluded"]}):
        w = wallets.get(uid)
        p_n, p_ret = agg(paper, uid)
        f_n, f_ret = agg(funded, uid)
        armed = sorted(m.wallet_armed_chains(ledger, uid)) if w else []
        # A trial arms the chain at the trial clip, so "trial" is checked before "armed".
        stage = ("trial" if uid in trials else "armed" if armed else "paper" if p_n else
                 "tracked" if uid in j7 else "curated")
        rows.append({"uid": uid, "handle": (w["handle"] if w else None) or (j7.get(uid) or (0, 0, None))[2],
                     "tracked": uid in j7, "j7": j7.get(uid), "in_roster": bool(w),
                     "curated": bool(w) and not w["paused"] and not w["excluded"],
                     "excluded": bool(w and w["excluded"]), "armed": armed, "trial": uid in trials,
                     "stage": stage, "paper_n": p_n, "paper_ret": p_ret, "funded_n": f_n, "funded_ret": f_ret,
                     **gated_view([g for k, g in gated.items() if k[0] == uid]),
                     "feasible_by_chain": {k[1]: g["feasible"] for k, g in gated.items() if k[0] == uid},
                     "last_t": _ts(last[uid]) if last.get(uid) else None})
    return rows


# ---------------------------------------------------------------- 2. candidates
def best_wallets(ledger, *, min_n=5):
    """Solana wallets whose funded pooled return is above 0 on >= min_n closes: who to learn from."""
    from wallet_follow_candidates import pooled
    funded = pooled(ledger, "execution_mode='real' AND network_id='solana'", ())
    return {k[0]: v for k, v in funded.items() if v[0] >= min_n and (v[1] or 0) > 0}


class _Evidence(collections.defaultdict):
    base_win = None


def solana_candidates(ledger, graph, best, known_addresses, creators, funders):
    """{address: evidence} from the Wallet Map graph, before the filters."""
    units = {r["signature"]: dict(r) for r in graph.execute(
        "SELECT signature, source_user_id, mint, age_s, win FROM memecoin_wallet_upstream_reads WHERE status='ok'")}
    ev = _Evidence(lambda: {"coins_before": set(), "won": set(), "sources": set(), "leads": [],
                                          "slot_before": set(), "ages": [], "funder_link": set(),
                                          "upstream": None, "dev_coins": set()})
    for r in graph.execute("SELECT signature, trader, lead_s FROM memecoin_wallet_early_buys"):
        u = units.get(r["signature"])
        if not u or u["source_user_id"] not in best or not r["lead_s"] or r["lead_s"] <= 0:
            continue
        e = ev[r["trader"]]
        e["coins_before"].add(u["mint"])
        e["sources"].add(u["source_user_id"])
        e["leads"].append(r["lead_s"])
        if u["win"]:
            e["won"].add(u["mint"])
        if u["age_s"] is not None:
            e["ages"].append(u["age_s"] - r["lead_s"])           # the coin's age at THEIR buy
        if creators.get(u["mint"]) == r["trader"]:
            e["dev_coins"].add(u["mint"])
    for r in graph.execute("SELECT source_user_id, mint, idx, companions FROM memecoin_wallet_slot_reads "
                           "WHERE status='ok' AND companions IS NOT NULL AND companions != '[]'"):
        if r["source_user_id"] not in best or r["idx"] is None:
            continue
        for c in json.loads(r["companions"] or "[]"):
            if c.get("index") is not None and c["index"] < r["idx"]:   # same slot, AHEAD of our source
                e = ev[c["trader"]]
                e["slot_before"].add(r["mint"])
                e["sources"].add(r["source_user_id"])
    best_addresses = {a for a, uid in known_addresses.items() if uid in best}
    best_funders = {funders.get(a) for a in best_addresses} - {None}
    for wallet, funder in funders.items():
        if wallet in best_addresses:
            continue
        if funder in best_addresses or funder in best_funders:
            ev[wallet]["funder_link"].add(funder)
    for r in graph.execute("SELECT trader, k, m, verdict, excluded FROM memecoin_wallet_upstream"):
        ev[r["trader"]]["upstream"] = dict(r)
    mine = [u for u in units.values() if u["source_user_id"] in best and u["win"] is not None]
    ev.base_win = (sum(1 for u in mine if u["win"]) / len(mine)) if mine else None   # the units' own win rate
    return ev


def screen_solana(ev, *, known_addresses, j7_addresses, copiers, creators, funders, own):
    out, dropped = [], collections.Counter()
    for addr, e in ev.items():
        why = []
        if addr in own:
            why.append("ours")
        if addr in known_addresses:
            why.append("roster")
        if addr in j7_addresses:
            why.append("j7_tracked")
        up = e["upstream"]
        if up and up.get("excluded"):
            why.append("3b:%s" % up["excluded"])
        if addr in copiers:
            why.append("same_slot_copier")
        if e["ages"] and sum(a <= SNIPE_S for a in e["ages"]) / len(e["ages"]) >= 0.5:
            why.append("sniper")
        if e["dev_coins"]:
            why.append("dev")
        if funders.get(addr) and funders[addr] in set(creators.values()):
            why.append("funded_by_dev")
        coins = e["coins_before"] | e["slot_before"]
        if not coins and not e["funder_link"] and not up:
            why.append("no_evidence")
        if why:
            dropped.update(why)
            continue
        out.append({"address": addr, "coins_before_best": len(e["coins_before"]), "slot_before_best": len(e["slot_before"]),
                    "coins": len(coins), "won": len(e["won"]), "best_sources": len(e["sources"]),
                    "lead_median_s": statistics.median(e["leads"]) if e["leads"] else None,
                    "funder_link": bool(e["funder_link"]),
                    "upstream": {"k": up["k"], "m": up["m"], "verdict": up["verdict"]} if up else None,
                    "found_by": [k for k, ok in (("early_buyer_before_best", e["coins_before"]),
                                                  ("same_slot_ahead_of_best", e["slot_before"]),
                                                  ("shared_funder_with_best", e["funder_link"]),
                                                  ("slice3b_upstream", up)) if ok],
                    "unchecked": [k for k, ok in (("sniper", e["ages"]), ("funder", addr in funders)) if not ok]})
    out.sort(key=lambda c: (-c["coins"], -c["won"], -c["best_sources"]))
    return out, dropped


def fomo_candidates(chain, j7_ids, known):
    """Untracked Fomo users with >= 10 coins whose coins ran 2x more often than the chain's base rate
    (the stored feed; buy-only, so their return is unmeasured until paper runs)."""
    import wallet_follow_candidates as W
    from sniper.util.state_paths import state_root
    ledger = W.ro(state_root() / "sniper_tracker.db")
    events, prices, handles, last = W.load(ledger)
    rows = W.score(events, prices)
    W.screen(rows, known, handles, last, {}, {})
    b = W.table_b(rows, known).get(chain, {})
    ledger.close()
    return [{"uid": r["uid"], "handle": r["handle"], "coins": r["coins"], "ran": r["ran"],
             "ran_share": r["ran_share"], "p_ran": r["p_ran"], "last_active": r["last_active"]}
            for r in b.get("wallets", []) if r["uid"] not in j7_ids], b.get("base_rate")


def commands_for(cand, *, chain):
    """The two operator steps, in order: the roster row, then the trial start on `chain` (J7 out)."""
    import shlex
    if chain == "solana":
        uid, handle = "pump:%s" % cand["address"], cand["address"][:8]
    else:
        uid, handle = cand["uid"], cand.get("handle") or cand["uid"][:8]
    return ["python3 scripts/memecoin_follow_add.py %s --handle %s --confirm" % (shlex.quote(uid), shlex.quote(handle)),
            "python3 scripts/memecoin_trial_arm.py start %s %s --confirm" % (shlex.quote(handle), shlex.quote(chain))]


def evm_commands(cand, *, chain, has_address):
    """`(commands, why_none)`: an EVM candidate gets its commands only when our on-chain detection
    has a proven address to watch on `chain` (Gamma LOW on 7f0ee065b: step 1 wrote a roster row and
    step 2, the trial start, refused it -- OtherAboveFowl on 56)."""
    if not has_address:
        return [], "no proven %s address" % chain
    return commands_for(cand, chain=chain), None


def selftest():
    now = 10 * 86400.0
    base = {"tracked": True, "armed": [], "trial": False, "paper_n": 25, "paper_ret": -0.05, "last_t": now - 3600}
    assert [r["why"] for r in rotation([base], now)] == [["paper -5.0% on 25"]]
    assert rotation([dict(base, armed=["solana"])], now) == []
    assert rotation([dict(base, paper_ret=0.02)], now) == []
    assert rotation([dict(base, paper_n=5, paper_ret=0.02, last_t=now - 8 * 86400)], now)[0]["why"] == ["silent 8 d"]
    cmds = commands_for({"address": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"}, chain="solana")
    assert cmds == ["python3 scripts/memecoin_follow_add.py pump:9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin "
                    "--handle 9xQeWvG8 --confirm", "python3 scripts/memecoin_trial_arm.py start 9xQeWvG8 solana --confirm"]
    assert not any("j7_watchlist" in c for c in cmds)
    evm = commands_for({"uid": "87f77c24-44e0-5696-baaf-dfe732083954", "handle": "o'k"}, chain="4663")
    assert evm[1] == "python3 scripts/memecoin_trial_arm.py start 'o'\"'\"'k' 4663 --confirm"
    ev = {"A": {"coins_before": {"m1", "m2"}, "won": {"m1"}, "sources": {"s"}, "leads": [5, 9], "slot_before": set(),
                "ages": [40, 50], "funder_link": set(), "upstream": None, "dev_coins": set()},
          "B": {"coins_before": {"m1"}, "won": set(), "sources": {"s"}, "leads": [1], "slot_before": set(),
                "ages": [1], "funder_link": set(), "upstream": None, "dev_coins": set()}}
    out, dropped = screen_solana(ev, known_addresses={}, j7_addresses=set(), copiers=set(), creators={}, funders={},
                                 own=set())
    assert [c["address"] for c in out] == ["A"] and dropped["sniper"] == 1
    print("selftest ok")
    return 0


def publish_universe(path, body):
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(body, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="also write data/wallet_follow_funnel.json for the map panel")
    ap.add_argument("--out", help="write the JSON here instead (a scratch file; implies --write)")
    ap.add_argument("--universe-only", action="store_true", help="materialize chain-local existing facts")
    ap.add_argument('--observation-db',help='explicit research observations, read only')
    ap.add_argument('--quote-db',help='explicit research quote measurements, read only')
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env, state_root, state_path
    state = state_root()
    load_env_files(str(state))
    configure_memecoin_state_env()
    import sniper.settings.memecoin as mc
    from sniper.services.memecoin_wallet_map import own_address_set
    if a.universe_only:
        from sniper.services.memecoin_wallet_map import build_universe,UNIVERSE_MAX_BYTES
        from sniper.services.memecoin_chains import CHAINS
        from sniper.persistence.db_core import get_readonly_connection
        ledger = get_readonly_connection(str(state / 'sniper_tracker.db'))
        graph = None
        auxiliary=[]
        try:
            captures=get_readonly_connection(str(state/'data'/'memecoin_refusal_captures.db')) if (state/'data'/'memecoin_refusal_captures.db').exists() else None
            if captures is not None: auxiliary.append(captures)
            observation=get_readonly_connection(a.observation_db) if a.observation_db else None
            if observation is not None: auxiliary.append(observation)
            quotes=get_readonly_connection(a.quote_db) if a.quote_db else None
            if quotes is not None: auxiliary.append(quotes)
            if (state / 'data' / 'memecoin_wallet_graph.db').exists():
                graph = get_readonly_connection(str(state / 'data' / 'memecoin_wallet_graph.db'))
            at = datetime.now(timezone.utc).isoformat()
            for spec in CHAINS:
                if spec['key']=='unresolved': continue
                chain = spec['network_ids'][0]
                body = build_universe(ledger, graph, chain=chain, generated_at=at,
                                      observation=observation,quotes=quotes,captures=captures)
                if len(json.dumps(body,allow_nan=False).encode('utf-8'))>UNIVERSE_MAX_BYTES:
                    raise ValueError('universe exceeds bounded API payload; previous snapshot retained')
                publish_universe(Path(a.out) / ('wallet_universe_' + chain + '.json') if a.out else state_path('data', 'wallet_universe_' + chain + '.json'), body)
            return 0
        finally:
            ledger.close()
            for handle in auxiliary: handle.close()
            if graph is not None:
                graph.close()
    ledger, graph = ro(state / "sniper_tracker.db"), ro(state / "data" / "memecoin_wallet_graph.db")
    now_t = datetime.now(timezone.utc).timestamp()
    rows = funnel_rows(ledger, now_t)
    j7 = tracked(ledger)
    known_addresses = {r["address"]: r["source_user_id"] for r in ledger.execute(
        "SELECT address, source_user_id FROM memecoin_address_registry WHERE network_id='solana'")}
    j7_addresses = {u[len("pump:"):] for u in j7 if u.startswith("pump:")}
    best = best_wallets(ledger)
    copiers = {r["dst"] for r in graph.execute(
        "SELECT dst FROM memecoin_wallet_edges WHERE edge_type IN ('co_buy_slot','slot_follower')")}
    funders = {r["wallet"]: r["funder"] for r in graph.execute(
        "SELECT wallet, funder FROM memecoin_wallet_funders WHERE funder IS NOT NULL")}
    mints = {r[0] for r in graph.execute("SELECT DISTINCT mint FROM memecoin_wallet_upstream_reads")}
    from wallet_follow_candidates import pump_creators
    creators = pump_creators(state / "data" / "pump_snipe_tape.sqlite", mints)
    ev = solana_candidates(ledger, graph, best, known_addresses, creators, funders)
    sol, dropped = screen_solana(ev, known_addresses=known_addresses, j7_addresses=j7_addresses, copiers=copiers,
                                 creators=creators, funders=funders, own=own_address_set(mc.MEMECOIN_OWN_ADDRESSES))
    for c in sol[:TOP]:
        c["commands"] = commands_for(c, chain="solana")
    known = {r["source_user_id"]: {"handle": r["handle"], "excluded": bool(r["excluded"]), "armed": set(), "trials": set()}
             for r in ledger.execute("SELECT source_user_id, handle, COALESCE(excluded,0) AS excluded FROM memecoin_wallets")}
    from sniper.services.memecoin_trial_ops import evm_addresses   # the trial gate's own address rule
    evm = {}
    for chain in ("4663", "56"):
        cands, base = fomo_candidates(chain, set(j7), known)
        for c in cands[:TOP]:
            c["commands"], c["no_commands"] = evm_commands(
                c, chain=chain, has_address=bool(evm_addresses(ledger, c["uid"], chain)))
        evm[chain] = {"base_rate": base, "candidates": cands[:TOP]}
    rot = rotation(rows, now_t)                 # no command: there is no J7 slot to free (J7 out)
    report = {"stamp": {"generated_at": datetime.now(timezone.utc).isoformat()[:19],
                        "best_wallets": len(best), "paper_since": "2026-09-01", "method": "follow-funnel-v2"},
              "funnel": rows, "rotation": rot, "solana_base_win": ev.base_win,
              "solana_candidates": sol[:TOP], "solana_dropped": dict(dropped.most_common()), "evm_candidates": evm}
    ledger.close()
    graph.close()
    if a.write or a.out:
        Path(a.out or state_path("data", "wallet_follow_funnel.json")).write_text(json.dumps(report, default=str, indent=1))
    pct = lambda x: "-" if x is None else "%+.1f%%" % (100 * x)  # noqa: E731
    stages = collections.Counter(r["stage"] for r in rows)
    print("funnel now: %s" % dict(stages))
    print("\nROTATION (tracked, unarmed, losing on paper at n>=%d or silent >= %d d): %d" % (ROTATE_MIN_N, SILENT_DAYS, len(rot)))
    for r in rot:
        print("  %-20s paper %s n=%d  %s" % ((r["handle"] or r["uid"])[:20], pct(r["paper_ret"]), r["paper_n"],
                                            "; ".join(r["why"])))
    print("\nSOLANA CANDIDATES (best funded wallets learned from: %d; their units won +15%% at 60 s on %s; dropped %s)" % (
        len(best), pct(ev.base_win), dict(dropped.most_common())))
    for i, c in enumerate(sol[:TOP], 1):
        print("  %2d %s coins=%d (before %d, same-slot ahead %d) won=%d best_sources=%d lead_med=%ss %s unchecked=%s" % (
            i, c["address"], c["coins"], c["coins_before_best"], c["slot_before_best"], c["won"], c["best_sources"],
            c["lead_median_s"], ",".join(c["found_by"]), ",".join(c["unchecked"]) or "-"))
        for cmd in c.get("commands", []):
            print("       " + cmd)
    for chain, b in evm.items():
        print("\n%s CANDIDATES (untracked Fomo users, buy-only; base rate %s)" % (chain, pct(b["base_rate"])))
        for c in b["candidates"]:
            print("  %-22s coins=%d ran=%d (%.0f%%) p=%.2g last=%s" % ((c["handle"] or c["uid"])[:22], c["coins"], c["ran"],
                                                                       100 * c["ran_share"], c["p_ran"], c["last_active"]))
            for cmd in c.get("commands", []):
                print("       " + cmd)
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('rotation', 'solana_candidates', 'main'))

if __name__ == "__main__":
    sys.exit(main())
