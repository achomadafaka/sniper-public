#!/usr/bin/env python3
"""Wallet follow candidates: wallets we do not follow on a chain, ranked by their own percent return.

Operator order 2026-10-01 22:1xZ via Alpha: "an insane lack of good wallets to follow; the map must
actually give us that". Read-only, 0 network calls, from data we already store:

- memecoin_events (the Fomo global feed until it died on 09-09, and J7 follow_activity since): every
  wallet's own buys and sells with their trade price;
- ps_tokens in data/pump_snipe_tape.sqlite: pump.fun coin creators, for the dev screen;
- the Wallet Map graph (data/memecoin_wallet_graph.db): coin age at each source buy (sniper screen),
  follows and dumps_on edges (copier and dumper screens), funders (funded-by-dev screen), and slice
  3b's upstream-early addresses, listed apart because no sell of theirs is stored.

Per wallet and coin (one chain): VWAP buy and VWAP sell prices from the wallet's own events, token
weights from size_usd / price. A coin is CLOSED when the estimated tokens sold reach 90% of those
bought; its return is VWAP sell / VWAP buy - 1. Open or partly sold coins have no return. A coin
RAN when any stored trade price on that coin within 24 h after the wallet's first buy is at least
2x that first buy price.

TABLE A (the ranking): our PAPER percent return on the wallet since PAPER_SINCE (the funded_clone
arm, closed positions, pooled: sum PnL / sum notional), at least PAPER_MIN_N closes; beside it the
CONFIG-GATED paper (gated_paper: the closes funded's mcap and source-buy gates admit today), whose
trailing-14-day count is the trial's feasibility screen (FEASIBLE_MIN_GATED); not retired,
armed or trialled on the chain, not screened. Gamma (2026-10-01, 61 wallet-chain pairs): the wallet's
OWN median return does not predict our funded return (Spearman -0.13) and paper does (+0.31, n=47),
so paper leads and own return is a column. Trial commands only for a paper return above 0.
TABLE B (return unmeasured): wallets we do not track whose stored trades are buys only (the global
feed relayed few sells), ranked by how often a coin ran after their buy, beside the chain's base
rate. They cannot be trialled until they are tracked (the trial needs J7 buy signals).

This is a candidate list for the funded trial (scripts/memecoin_trial_arm.py), not a claim: the
sample is small and old (see the doc), and the trial is the test. Nothing here arms anything.

    python3 scripts/research/wallet_follow_candidates.py [--json] [--top 10] [--chains solana,4663,56]
    python3 scripts/research/wallet_follow_candidates.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import bisect
import collections
import json
import math
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MIN_CLOSED = 5          # closed coins: never judged on one coin
MIN_RAN = 3             # coins that ran 2x after the wallet's first buy
RAN_X = 2.0
RAN_PRINTS = 2          # two prints at RAN_X or more: one bad print never makes a coin run (Gamma)
PAPER_SINCE = "2026-09-01T00:00:00+00:00"
PAPER_ARM = "funded_clone"
PAPER_MIN_N = 20
RAN_WINDOW_S = 86400
CLOSED_SOLD_SHARE = 0.9
DUP_S = 10.0            # one trade relayed twice (REST and websocket lanes): same side and price
SNIPE_S = 2.0           # coin age at the buy (Wallet Map units): in or next to the dev's block
SNIPER_SHARE = 0.5
SNIPER_MIN_UNITS = 3
B_MIN_COINS = 10        # table B: distinct coins bought
COPIER_MIN = 3          # a consistent follower on this many coins
BOT_HOLD_S = 30.0       # median first-buy-to-first-sell hold under this: a bot
BOT_COINS_PER_DAY = 50.0
RET_MAX = 1000.0        # a VWAP ratio over 1000x is a broken price, not a trade


def _ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def chain_of(network_id):
    n = str(network_id or "solana")
    return "solana" if n in ("solana", "") else n


def dedupe(events):
    """Drop a repeat of the same side at the same price within DUP_S (the same trade relayed twice).
    `events`: (t, side, price, usd) sorted by t."""
    out, last = [], {}
    for e in events:
        key = (e[1], e[2])
        if key in last and e[0] - last[key] <= DUP_S:
            continue
        last[key] = e[0]
        out.append(e)
    return out


def coin_result(events):
    """{first_t, first_price, buys, sells, closed, ret, hold_s} from one wallet's events on one coin.
    Tokens are size_usd / price; a missing size weighs 1 token so the VWAP is still a price."""
    buys = [e for e in events if e[1] == "buy"]
    if not buys:
        return None
    first_t, first_price = buys[0][0], buys[0][2]
    sells = [e for e in events if e[1] == "sell" and e[0] >= first_t]

    def vwap(rows):
        tokens = sum((u / p) if u and u > 0 else 1.0 for _t, _s, p, u in rows)
        usd = sum(u if u and u > 0 else p for _t, _s, p, u in rows)
        return (usd / tokens if tokens > 0 else None), tokens
    buy_px, buy_tokens = vwap(buys)
    out = {"first_t": first_t, "first_price": first_price, "buys": len(buys), "sells": len(sells),
           "closed": False, "ret": None, "hold_s": None}
    if not sells or not buy_px:
        return out
    sell_px, sell_tokens = vwap(sells)
    out["hold_s"] = sells[0][0] - first_t
    ratio = sell_px / buy_px if sell_px and buy_px else None
    if sell_tokens >= CLOSED_SOLD_SHARE * buy_tokens and ratio and 0 < ratio <= RET_MAX:
        out.update(closed=True, ret=ratio - 1.0)
    return out


def ran(prices, first_t, first_price):
    """True when at least RAN_PRINTS stored trade prices on the coin within RAN_WINDOW_S after first_t
    are >= RAN_X x first_price. `prices`: (t, price) sorted by t for the coin, every wallet."""
    i = bisect.bisect_right(prices, (first_t, math.inf))
    j = bisect.bisect_right(prices, (first_t + RAN_WINDOW_S, math.inf))
    return sum(1 for _t, p in prices[i:j] if p >= RAN_X * first_price) >= RAN_PRINTS


def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load(ledger):
    """events[(uid, chain)][mint] -> [(t, side, price, usd)], prices[(chain, mint)] -> [(t, price)],
    handles, last activity."""
    events = collections.defaultdict(lambda: collections.defaultdict(list))
    prices = collections.defaultdict(list)
    handles, last = {}, {}
    for r in ledger.execute("SELECT source_user_id, source_handle, network_id, mint, side, their_price, "
                            "their_size_usd, created_at FROM memecoin_events WHERE their_price > 0 "
                            "AND side IN ('buy', 'sell') AND mint IS NOT NULL"):
        try:
            t = _ts(r["created_at"])
            price = float(r["their_price"])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(price) and price > 0):
            continue
        chain = chain_of(r["network_id"])
        key = (r["source_user_id"], chain)
        events[key][r["mint"]].append((t, r["side"], price, float(r["their_size_usd"] or 0)))
        prices[(chain, r["mint"])].append((t, price))
        if r["source_handle"]:
            handles[r["source_user_id"]] = r["source_handle"]
        last[key] = max(last.get(key, 0.0), t)
    for v in prices.values():
        v.sort()
    return events, prices, handles, last


def roster(ledger):
    """{uid: (handle, excluded, armed chains, trial chains)} for every wallet we know."""
    from sniper.persistence import db_memecoin as m
    out = {}
    for r in ledger.execute("SELECT source_user_id, handle, COALESCE(excluded,0) AS excluded FROM memecoin_wallets"):
        out[r["source_user_id"]] = {"handle": r["handle"], "excluded": bool(r["excluded"]),
                                    "armed": set(m.wallet_armed_chains(ledger, r["source_user_id"])),
                                    "trials": set()}
    try:
        for r in ledger.execute("SELECT source_user_id, chain FROM memecoin_trial_arms WHERE ended_at IS NULL"):
            out.setdefault(r["source_user_id"], {"handle": None, "excluded": False, "armed": set(),
                                                 "trials": set()})["trials"].add(chain_of(r["chain"]))
    except sqlite3.OperationalError:
        pass                                            # no trial table yet: no open trials
    return out


def pump_creators(path, mints):
    """{mint: creator} for the pump.fun coins among `mints` (the dev screen). ps_tokens.created_ts is
    NOT a creation time: 66% of first buys read before it (measured 2026-10-01), so no sniper screen
    uses it."""
    if not Path(path).exists():
        return {}
    conn = ro(path)
    out, mints = {}, list(mints)
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        q = "SELECT mint, creator FROM ps_tokens WHERE mint IN (%s) AND creator IS NOT NULL" % ",".join("?" * len(chunk))
        out.update({r["mint"]: r["creator"] for r in conn.execute(q, chunk)})
    conn.close()
    return out


def pooled(conn, where, params):
    """{(uid, chain): (n, pooled return, mean return, censored)} over closed positions matching
    `where`. A close with no realized PnL is censored: counted apart, never as 0."""
    out = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0])
    for r in conn.execute("SELECT source_user_id, network_id, realized_pnl, entry_notional FROM memecoin_positions "
                          "WHERE is_open=0 AND entry_notional > 0 AND " + where, params):
        a = out[(r["source_user_id"], chain_of(r["network_id"]))]
        if r["realized_pnl"] is None:
            a[4] += 1
            continue
        a[0] += 1
        a[1] += float(r["realized_pnl"])
        a[2] += float(r["entry_notional"])
        a[3] += float(r["realized_pnl"]) / float(r["entry_notional"])
    return {k: (n, (pnl / notional) if n else None, (total / n) if n else None, cens)
            for k, (n, pnl, notional, total, cens) in out.items()}


# GATE-MATCHED PAPER AS A FEASIBILITY SCREEN (Alpha and Gamma, 2026-10-02). funded_clone clones
# funded's EXIT rails, never its admission gates, so paper also books signals funded refuses: on 56,
# 229 of 299 closes were coins over the $250k mcap ceiling. Gamma measured gated paper as a SCORE on 27
# wallets and it predicted funded worse than ungated (Spearman +0.27 and +0.31 against +0.44), so the
# score stays ungated paper. Gated paper answers a different question: would funded have entered
# enough of this wallet's signals for a trial to reach its 10 closes in 14 days? CONFIG-ONLY for every
# wallet (funded's own decision exists only for armed wallets, so using it would not transfer to
# candidates). APPROXIMATED: today's config for every entry (per-entry config is not stored); the
# volume and turnover floors are not applied (the paper row holds no volume reading).
#
# Gamma's N (2026-10-02): trial grades at 5, 10 and 20 closes and reads INSUFFICIENT under 10 by day
# 14; funded fills per config-gated paper close since 09-21, median: solana 0.26 (28 wallets), 4663
# 0.94 (6 wallets). 10 / 0.26 = 38 -> 40, 10 / 0.94 = 11 -> 12. Censored closes count (the conversion
# was measured with them). 56 and 8453 have no N until a trial clip exists there and a conversion is
# measured.
FEASIBLE_MIN_GATED = {"solana": 40, "4663": 12}
FEASIBLE_DAYS = 14


def funded_gate(r, prior_net_fn=None):
    """None when funded's mcap and source-buy gates admit this paper row under today's config, else the
    refusal. Calls the funded functions themselves, never a restatement of their arithmetic."""
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    chain, uid = chain_of(r["network_id"]), r["source_user_id"]
    ceiling = rr._wallet_mcap_ceiling(cfg.MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET or {}, chain, uid,
                                      cfg.MEMECOIN_REAL_MAX_MCAP_USD)
    floor = rr._wallet_mcap_floor(cfg.MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET or {}, chain, uid,
                                  cfg.MEMECOIN_REAL_MIN_MCAP_USD)
    event_mc = r["their_market_cap"] if (r["their_market_cap"] or 0) > 0 else None
    allow, eff, why = rr.mcap_entry_decision(event_mc, r["entry_mcap"], ceiling, floor)
    if not allow:
        return "mcap:%s" % why
    their = r["their_size_usd"]
    their = float(their) if their is not None and math.isfinite(float(their)) and float(their) >= 0 else None
    ratio = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO or 0.0))
    bands = cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND or {}
    source_mcap = eff or event_mc
    if source_mcap and bands:
        ratio = rr._band_source_buy_ratio(bands, chain, source_mcap, ratio)
    unknown_floor = max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD or 0.0))
    window = float(cfg.MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S or 0)
    prior = (prior_net_fn(r, window, their) if prior_net_fn and source_mcap and ratio > 0
             and their is not None and window > 0 else None)
    reason, _info = rr.source_buy_family_reason(
        their_usd=their, source_mcap=source_mcap, prior_net_usd=prior, ratio=ratio,
        unknown_mcap_floor=unknown_floor,
        abs_min=max(0.0, float(cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_USD or 0.0)),
        abs_max=max(0.0, float(cfg.MEMECOIN_REAL_MAX_SOURCE_BUY_USD or 0.0)),
        family_active=bool(ratio > 0 or unknown_floor > 0 or bands))
    return "source:%s" % reason if reason else None


def gate_config():
    """The funded gate values this run applied, printed so a run on code defaults (no .env) shows."""
    from sniper.settings import memecoin as cfg
    return {k: getattr(cfg, k) for k in (
        "MEMECOIN_REAL_MAX_MCAP_USD", "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", "MEMECOIN_REAL_MIN_MCAP_USD",
        "MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET", "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO",
        "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD",
        "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", "MEMECOIN_REAL_MAX_SOURCE_BUY_USD",
        "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT")}


def prior_net_reader(ledger):
    """funded's cumulative read: the trader's net USD into the coin in the window before the event."""
    from sniper.persistence import db_memecoin as m

    def read(r, window_s, judged):
        return m.source_prior_net_buy_usd(
            ledger, source_user_id=r["source_user_id"], network_id=r["network_id"], mint=r["mint"],
            before_iso=r["created_at"] or r["opened_at"], window_s=window_s,
            exclude_event_id=r["origin_event_id"], judged_usd=judged)
    return read


def gated_paper(conn, since, recent_since, prior_net_fn=None):
    """{(uid, chain): sums} over the funded_clone closes opened since `since`, each close judged by
    funded_gate (config only). Sums: all_notional and all_cens_notional (every close), n, pnl,
    notional (gated, priced), censored and cens_notional (gated, censored), feasible (gated closes,
    censored included, closed at or after `recent_since`), out (Counter of refusals)."""
    out = collections.defaultdict(lambda: {"all_notional": 0.0, "all_cens_notional": 0.0, "n": 0, "pnl": 0.0,
                                           "notional": 0.0, "censored": 0, "cens_notional": 0.0, "feasible": 0,
                                           "out": collections.Counter()})
    q = ("SELECT p.source_user_id, p.network_id, p.mint, p.opened_at, p.closed_at, p.realized_pnl, "
         "p.entry_notional, p.entry_mcap, p.their_size_usd, p.origin_event_id, e.created_at, e.their_market_cap "
         "FROM memecoin_positions p LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
         "WHERE p.execution_mode='paper' AND p.strategy=? AND p.opened_at >= ? AND p.is_open=0 "
         "AND p.entry_notional > 0")
    for r in conn.execute(q, (PAPER_ARM, since)):
        a = out[(r["source_user_id"], chain_of(r["network_id"]))]
        notional, censored = float(r["entry_notional"]), r["realized_pnl"] is None
        a["all_notional"] += notional
        a["all_cens_notional"] += notional if censored else 0.0
        why = funded_gate(r, prior_net_fn)
        if why:
            a["out"][why] += 1
            continue
        if (r["closed_at"] or "") >= recent_since:
            a["feasible"] += 1
        if censored:
            a["censored"] += 1
            a["cens_notional"] += notional
            continue
        a["n"] += 1
        a["pnl"] += float(r["realized_pnl"])
        a["notional"] += notional
    return dict(out)


def gated_view(parts):
    """One summary over one or more gated_paper sums (several chains for the funnel's pooled row)."""
    t = {k: sum(p[k] for p in parts) for k in ("all_notional", "all_cens_notional", "n", "pnl", "notional",
                                                 "censored", "cens_notional", "feasible")}
    gated_total = t["notional"] + t["cens_notional"]
    return {"gated_n": t["n"] + t["censored"], "gated_ret": (t["pnl"] / t["notional"]) if t["notional"] else None,
            "gated_cens_share": (t["cens_notional"] / gated_total) if gated_total else None,
            "paper_cens_share": (t["all_cens_notional"] / t["all_notional"]) if t["all_notional"] else None,
            "feasible_n": t["feasible"]}


def paper_and_funded(ledger, now=None):
    """(paper, funded, gated): ungated paper (the score), funded, and gated_paper's sums."""
    now = now or datetime.now(timezone.utc)
    recent = (now - timedelta(days=FEASIBLE_DAYS)).isoformat()
    return (pooled(ledger, "execution_mode='paper' AND strategy=? AND opened_at >= ?", (PAPER_ARM, PAPER_SINCE)),
            pooled(ledger, "execution_mode='real'", ()),
            gated_paper(ledger, PAPER_SINCE, recent, prior_net_reader(ledger)))


def score(events, prices):
    """One row per (uid, chain) with the measures and the bot flags."""
    rows = []
    for (uid, chain), coins in events.items():
        results, ran_n, days, mints = [], 0, set(), []
        for mint, evs in coins.items():
            evs = dedupe(sorted(evs))
            res = coin_result(evs)
            if res is None:
                continue
            days.add(int(res["first_t"] // 86400))
            if ran(prices[(chain, mint)], res["first_t"], res["first_price"]):
                ran_n += 1
            results.append(res)
            mints.append(mint)
        closed = [r for r in results if r["closed"]]
        holds = [r["hold_s"] for r in closed if r["hold_s"] is not None]
        rets = [r["ret"] for r in closed]
        flags = []
        if holds and statistics.median(holds) < BOT_HOLD_S:
            flags.append("bot:hold")
        if days and len(results) / len(days) > BOT_COINS_PER_DAY:
            flags.append("bot:rate")
        rows.append({"uid": uid, "chain": chain, "coins": len(results), "closed": len(closed), "ran": ran_n,
                     "closed_share": len(closed) / len(results) if results else None,
                     "sells": sum(r["sells"] for r in results), "mints": mints,
                     "median_ret": statistics.median(rets) if rets else None,
                     "p25_ret": (statistics.quantiles(rets, n=4)[0] if len(rets) >= 4 else None),
                     "win_share": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
                     "median_hold_s": statistics.median(holds) if holds else None, "flags": flags})
    return rows


def graph_flags(graph_path, ledger, rows, creators):
    """Add the Wallet Map screens to `rows` in place: sniper (coin age <= SNIPE_S at >= half of the
    wallet's units), copier (a consistent follower in `follows`, whose src is the LEADER), dumper
    (src of `dumps_on`), dev (its registered address created a coin it traded) and funded_by_dev (its
    funder created one). Each screen reads only what is stored: a wallet with no units, no edge or no
    registered address is unchecked on that screen, never cleared."""
    if not Path(graph_path).exists():
        return
    g = ro(graph_path)
    ages = collections.defaultdict(list)
    for r in g.execute("SELECT source_user_id, age_s FROM memecoin_wallet_upstream_reads WHERE age_s IS NOT NULL"):
        ages[r["source_user_id"]].append(r["age_s"])
    copiers = {r["dst_user_id"] for r in g.execute(
        "SELECT dst_user_id FROM memecoin_wallet_edges WHERE edge_type='follows' AND n_consistent >= ?", (COPIER_MIN,))}
    dumpers = {r["src_user_id"] for r in g.execute("SELECT src_user_id FROM memecoin_wallet_edges WHERE edge_type='dumps_on'")}
    funders = {r["wallet"]: r["funder"] for r in g.execute(
        "SELECT wallet, funder FROM memecoin_wallet_funders WHERE funder IS NOT NULL")}
    g.close()
    addresses = collections.defaultdict(set)
    for r in ledger.execute("SELECT source_user_id, address FROM memecoin_address_registry WHERE status='verified' "
                            "AND network_id='solana'"):
        addresses[r["source_user_id"]].add(r["address"])
    for r in rows:
        if r["chain"] != "solana":                     # units, creators and funders are Solana-only
            r["screened"] = ["bot", "copier", "dumper", "sniper/dev:unchecked"]
            if r["uid"] in copiers:
                r["flags"].append("copier")
            if r["uid"] in dumpers:
                r["flags"].append("dumper")
            continue
        a = ages.get(r["uid"])
        if a and len(a) >= SNIPER_MIN_UNITS and sum(x <= SNIPE_S for x in a) / len(a) >= SNIPER_SHARE:
            r["flags"].append("sniper")
        if r["uid"] in copiers:
            r["flags"].append("copier")
        if r["uid"] in dumpers:
            r["flags"].append("dumper")
        made = {creators.get(mint) for mint in r["mints"]} - {None}
        mine = addresses.get(r["uid"], set())
        if mine & made:
            r["flags"].append("dev")
        if {funders.get(x) for x in mine} & made:
            r["flags"].append("funded_by_dev")
        r["screened"] = ["bot", "copier", "dumper"] + [
            k if ok else k + ":unchecked" for k, ok in (("sniper", bool(a)), ("dev", bool(mine)),
                                                         ("funded_by_dev", any(x in funders for x in mine)))]


def screen(rows, known, handles, last, paper, funded):
    """(candidates, excluded) with the reason on each excluded row. Every row is annotated with its
    paper and funded record; the bar is the paper sample (own return is a column, never the bar)."""
    cands, out = [], []
    for r in rows:
        r["paper_n"], r["paper_ret"], r["paper_mean"], r["paper_censored"] = paper.get(
            (r["uid"], r["chain"]), (0, None, None, 0))
        r["funded_n"], r["funded_ret"], _, r["funded_censored"] = funded.get((r["uid"], r["chain"]), (0, None, None, 0))
        w = known.get(r["uid"]) or {}
        r["handle"] = w.get("handle") or handles.get(r["uid"])
        r["in_roster"] = r["uid"] in known
        r["last_active"] = datetime.fromtimestamp(last.get((r["uid"], r["chain"]), 0), timezone.utc).isoformat()[:16]
        why = []
        if w.get("excluded"):
            why.append("retired")
        if r["chain"] in (w.get("armed") or ()):
            why.append("armed")
        if r["chain"] in (w.get("trials") or ()):
            why.append("on trial")
        why += r["flags"]
        if r["paper_n"] < PAPER_MIN_N:
            why.append("paper<%d" % PAPER_MIN_N)
        r["why"] = why
        (out if why else cands).append(r)
    cands.sort(key=lambda r: (-(r["paper_ret"] if r["paper_ret"] is not None else -9), -r["paper_n"]))
    return cands, out


FUNDED_SAYS_N = 10      # a funded record this long already answers what a trial would ask
PAPER_MIN_RET = 0.005   # "above 0" at the precision the list prints (+0.5%): songz's +0.05% is not


def trial_worthy(r):
    """A paper return above 0, and no funded record of >= FUNDED_SAYS_N closes at or below 0: the
    operator changes the roster on funded results, so a wallet funded already disproved is not
    re-trialled on paper's word (chrollo0o0: paper +23%, funded -0.1% on 32)."""
    if not (r.get("paper_ret") or 0) >= PAPER_MIN_RET:
        return False
    return not (r.get("funded_n", 0) >= FUNDED_SAYS_N and (r.get("funded_ret") or 0) <= 0)


def feasible(r):
    """Enough config-gated paper closes in the trailing FEASIBLE_DAYS for a trial to reach 10 closes
    in 14 days (Gamma's N). A chain without an N is never feasible: no trial clip or conversion yet."""
    need = FEASIBLE_MIN_GATED.get(r.get("chain"))
    return need is not None and (r.get("feasible_n") or 0) >= need


def retired_lead(rows):
    """Retired wallets with a positive paper return on >= PAPER_MIN_N closes, beside their funded
    record: the operator changes the roster on funded results only, so this is context, not a list."""
    out = [r for r in rows if "retired" in r["why"] and r["paper_n"] >= PAPER_MIN_N and (r["paper_ret"] or 0) > 0]
    return sorted(out, key=lambda r: -r["paper_ret"])


def binom_tail(k, n, p):
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def table_b(rows, known):
    """{chain: {base_rate, wallets}}: the chain's base rate (share of every scored wallet's coins
    that ran) and the untracked, unscreened wallets whose stored trades cannot price a return (fewer
    than MIN_CLOSED closed coins) with >= B_MIN_COINS coins and >= MIN_RAN that ran, ranked by the
    binomial tail of their ran count against the base rate. Descriptive: thousands of wallets are
    tested, so a few small tails are expected by chance (shown as `expected_by_chance`)."""
    out = {}
    for chain in sorted({r["chain"] for r in rows}):
        mine = [r for r in rows if r["chain"] == chain]
        coins = sum(r["coins"] for r in mine)
        base = sum(r["ran"] for r in mine) / coins if coins else 0.0
        pool = [r for r in mine if r["uid"] not in known and not r["flags"] and r["closed"] < MIN_CLOSED
                and r["coins"] >= B_MIN_COINS and r["ran"] >= MIN_RAN]
        for r in pool:
            r["ran_share"] = r["ran"] / r["coins"]
            r["p_ran"] = binom_tail(r["ran"], r["coins"], base) if 0 < base < 1 else None
        pool.sort(key=lambda r: (r["p_ran"] if r["p_ran"] is not None else 1.0, -r["coins"]))
        tested = sum(1 for r in mine if r["uid"] not in known and r["coins"] >= B_MIN_COINS)
        out[chain] = {"base_rate": base, "tested": tested, "wallets": pool,
                      "expected_by_chance_p01": round(0.01 * tested, 1)}
    return out


def trial_precheck(ledger, handle, chain):
    """The operator script's own START checks, on a read-only connection (confirm=False never
    writes): ok, or the reason it would refuse today."""
    if not handle:
        return "no handle"
    from scripts.memecoin_trial_arm import start
    from sniper.util.clock import real_utc_now_iso
    try:
        out = start(ledger, handle, chain, confirm=False, now_iso=real_utc_now_iso())
    except Exception as exc:                          # a read failure is reported, never swallowed
        return "precheck failed: %r" % exc
    return "ok (dry run)" if out.get("ok") else out.get("reason")


def upstream_early(graph_path, known_addresses):
    """Slice 3b's upstream-early addresses not excluded by its own screen (copy_bot, hub_funded) and
    not ours: early buyers on coins that won at 60 s. No sell of theirs is stored: return unmeasured."""
    if not Path(graph_path).exists():
        return []
    g = ro(graph_path)
    rows = [dict(r) for r in g.execute(
        "SELECT trader, m, k, expected, days, lead_median_s, q_value, verdict, excluded FROM memecoin_wallet_upstream "
        "WHERE COALESCE(excluded,'')='' ORDER BY k DESC, m ASC")]
    g.close()
    return [r for r in rows if r["trader"] not in known_addresses]


def selftest():
    e = [(0.0, "buy", 1.0, 100.0), (5.0, "buy", 1.0, 100.0), (60.0, "sell", 3.0, 600.0)]
    assert len(dedupe(e)) == 2                                   # the 5 s repeat is one trade
    r = coin_result(dedupe(e))
    assert r["closed"] and abs(r["ret"] - 2.0) < 1e-9 and r["hold_s"] == 60.0
    r = coin_result([(0.0, "buy", 1.0, 100.0), (60.0, "sell", 3.0, 30.0)])   # 10% sold: open
    assert not r["closed"] and r["ret"] is None
    r = coin_result([(0.0, "buy", 1e-6, 100.0), (60.0, "sell", 2.0, 100.0)])   # a broken price
    assert not r["closed"]
    assert coin_result([(0.0, "sell", 1.0, 1.0)]) is None
    p = [(10.0, 1.0), (100.0, 2.5), (150.0, 2.2), (90000.0, 9.0)]
    assert ran(p, 0.0, 1.0)                                      # two prints at 2x within 24 h
    assert not ran(p, 0.0, 1.2)                                  # only one print at 2.4 or more
    assert not ran([(100.0, 2.5)], 0.0, 1.0)                      # one print never makes a run
    assert not ran(p, 200.0, 1.0)                                # the 9.0 print is past 24 h
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chains", default="solana,4663,56,8453")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import configure_memecoin_state_env, state_root
    state = state_root()
    load_env_files(str(state))
    configure_memecoin_state_env()
    ledger = ro(state / "sniper_tracker.db")
    events, prices, handles, last = load(ledger)
    creators = pump_creators(state / "data" / "pump_snipe_tape.sqlite",
                             {mint for (uid, ch), coins in events.items() if ch == "solana" for mint in coins})
    known = roster(ledger)
    paper, funded, gated = paper_and_funded(ledger)
    rows = score(events, prices)
    graph_flags(state / "data" / "memecoin_wallet_graph.db", ledger, rows, creators)
    cands, excluded = screen(rows, known, handles, last, paper, funded)
    for r in cands + excluded:
        g = gated.get((r["uid"], r["chain"]))
        r.update(gated_view([g]) if g else gated_view([]))
        r["gated_out"] = dict(g["out"]) if g else {}
    unmeasured = table_b(rows, known)
    retired = retired_lead(excluded)
    chains = [c.strip() for c in a.chains.split(",") if c.strip()]
    report = {"generated_at": datetime.now(timezone.utc).isoformat()[:19],
              "rules": {"MIN_CLOSED": MIN_CLOSED, "MIN_RAN": MIN_RAN, "RAN_X": RAN_X, "SNIPE_S": SNIPE_S,
                        "BOT_HOLD_S": BOT_HOLD_S, "BOT_COINS_PER_DAY": BOT_COINS_PER_DAY},
              "gates": gate_config(), "chains": {}}
    for chain in chains:
        mine = [r for r in cands if r["chain"] == chain]
        for r in mine[:a.top]:
            r["trial"] = trial_precheck(ledger, r["handle"], chain)
        ex = collections.Counter(w for r in excluded if r["chain"] == chain for w in r["why"])
        report["chains"][chain] = {
            "wallets_scored": sum(1 for r in cands + excluded if r["chain"] == chain),
            "candidates": mine, "excluded_by_reason": dict(ex.most_common()),
            "unmeasured": unmeasured.get(chain, {}),
            "retired_positive_paper": [r for r in retired if r["chain"] == chain]}
    report["upstream_early_unmeasured"] = upstream_early(
        state / "data" / "memecoin_wallet_graph.db",
        {r["address"] for r in ledger.execute("SELECT address FROM memecoin_address_registry")})
    ledger.close()
    for r in rows:
        r.pop("mints", None)                           # every coin address: too long for the report
    if a.json:
        print(json.dumps(report, indent=1, default=str))
        return 0
    pct = lambda x: "-" if x is None else "%+.0f%%" % (100 * x)  # noqa: E731
    g = report["gates"]
    print("gates applied (today's config): mcap ceiling %s (overrides %d), source buy floor $%s, ratio %s"
          % (g["MEMECOIN_REAL_MAX_MCAP_USD"], len(g["MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET"] or {}),
             g["MEMECOIN_REAL_MIN_SOURCE_BUY_USD"], g["MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO"]))
    for chain, block in report["chains"].items():
        print("\n== %s: %d wallets scored, %d pass the bar" % (chain, block["wallets_scored"], len(block["candidates"])))
        print("   excluded: %s" % block["excluded_by_reason"])
        share = lambda x: "-" if x is None else "%.0f%%" % (100 * x)  # noqa: E731
        print("   %-4s %-20s %7s %5s %7s %6s | %7s %7s %6s %5s | %9s %8s %7s %6s %4s %5s  %-16s %s" % (
            "rank", "handle", "paper_n", "cens", "paper%", "cens$", "gated_n", "gated%", "cens$", "14d",
            "funded_n", "funded%", "own_med", "closed", "ran", "win", "last_active", "trial precheck"))
        for i, r in enumerate(block["candidates"], 1):
            print("   %-4d %-20s %7d %5d %7s %6s | %7d %7s %6s %5d | %9d %8s %7s %6s %4d %4.0f%%  %-16s %s" % (
                i, (r["handle"] or r["uid"])[:20], r["paper_n"], r["paper_censored"], pct(r["paper_ret"]),
                share(r["paper_cens_share"]), r["gated_n"], pct(r["gated_ret"]), share(r["gated_cens_share"]),
                r["feasible_n"], r["funded_n"],
                pct(r["funded_ret"]), pct(r["median_ret"]), "%d/%d" % (r["closed"], r["coins"]), r["ran"],
                100 * (r["win_share"] or 0), r["last_active"], r.get("trial", "")))
        positive = [r for r in block["candidates"][:a.top] if trial_worthy(r)]
        runnable = [r for r in positive if str(r.get("trial", "")).startswith("ok") and feasible(r)]
        need = FEASIBLE_MIN_GATED.get(chain)
        print("   trial commands (top %d with paper >= %+.1f%%, no losing funded record of >= %d closes, a passing"
              " precheck, and >= %s config-gated paper closes in %d days: %d):" % (
                  a.top, 100 * PAPER_MIN_RET, FUNDED_SAYS_N, need if need is not None else "(no N on this chain)",
                  FEASIBLE_DAYS, len(runnable)))
        for r in positive:
            if r in runnable:
                print("   python3 scripts/memecoin_trial_arm.py start %s %s --confirm   # paper %s n=%d (censored %d),"
                      " gated %d in %d d" % (r["handle"], chain, pct(r["paper_ret"]), r["paper_n"],
                                             r["paper_censored"], r["feasible_n"], FEASIBLE_DAYS))
            elif not feasible(r):
                print("   %-20s would not reach 10 closes in 14 days: %d config-gated paper closes in %d d%s" % (
                    (r["handle"] or r["uid"])[:20], r["feasible_n"], FEASIBLE_DAYS,
                    "" if need is not None else " (no N on this chain)"))
            else:
                print("   %-20s refused by the precheck: %s" % ((r["handle"] or r["uid"])[:20], r.get("trial", "")))
        b = block["unmeasured"]
        if b:
            print("   TABLE B, LEADS ONLY (buys only; not tracked; p-values too small: clustered buys): base rate %.1f%% of coins ran 2x in 24 h;"
                  " %d wallets tested, ~%.1f expected under p<0.01 by chance" % (100 * b["base_rate"], b["tested"],
                                                                               b["expected_by_chance_p01"]))
            for r in b["wallets"][:a.top]:
                print("   %-24s coins=%d ran=%d (%.0f%%) p=%.2g last_active=%s" % (
                    (r["handle"] or r["uid"])[:24], r["coins"], r["ran"], 100 * r["ran_share"],
                    r["p_ran"] if r["p_ran"] is not None else float("nan"), r["last_active"]))
    print("\n== upstream-early addresses (slice 3b, Solana), return unmeasured: %d" % len(report["upstream_early_unmeasured"]))
    for r in report["upstream_early_unmeasured"]:
        print("   %s k=%d/m=%d expected=%.1f days=%d lead_med=%ss verdict=%s" % (
            r["trader"], r["k"], r["m"], r["expected"] or 0, r["days"] or 0, r["lead_median_s"], r["verdict"]))
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('screen', 'trial_worthy', 'main'))

if __name__ == "__main__":
    sys.exit(main())
