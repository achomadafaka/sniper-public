"""Wallet Map bot trail and best herd member (pre-registration
docs/audits/2026-09-30-wallet-map-reads-preregistration.md sections C and D, Gamma 17:40Z; spec
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md sections 4 and 5).

A probe is a bot that is a regular of two or more frozen herders. Its sampled buys name the wallets
that bought the same coin just before it: LEADS, discovery only, never called a herder until section
C's confirmation from their own first buys has run (Gamma, 2026-10-01). Its own round trips over a
24 h stretch say whether it is a profitable herd member. Descriptive: nothing here arms, sizes,
admits or exits, and "profitable" describes the bot's own trades, never what copying it would earn.

Pure: stored slot reads and transactions in, rows out. The reads (research key, flat Solana book)
live in wallet_link_graph.py.
"""
import collections
import random
import statistics

from scripts.research.wallet_upstream import USDC, WSOL, signer_trades, view

SAMPLE_BUYS = 10
LEAD_S = 10                                 # a leader bought in the probe's slot before it, or in the 10 s before
CAND_PROBES, CAND_COINS = 3, 3
CLOSED_LEFT = 0.01                          # closed: the end balance is at most 1% of the most held
PROFIT_MIN_CLOSED = 10
PAY_USDC = 1.0
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
# Quote tokens: never a coin row (Gamma LOW 2 on 1e114be02); a coin paid in a stable one is unpriced.
STABLES = (USDC, USDT, PYUSD)
QUOTE_MINTS = (WSOL,) + STABLES


# ---------------------------------------------------------------- herds and probes
# ONE DEFINITION with the page and Gamma's frozen lists: the unit read, herds, regulars and probes live
# in sniper/services/memecoin_wallet_map.py (Gamma CLEAN 4fb73b112, merged 027132a10).
from sniper.services.memecoin_wallet_map import (  # noqa: E402,F401  path-ok: a library module; wallet_link_graph bootstraps sys.path
    PROBE_HERDS, REG_K, REG_N, REG_SHARE, herds, probes, regulars, unit_reads)


# ---------------------------------------------------------------- one probe's transactions
def _failed(tx):
    return (tx.get("meta") or {}).get("err") is not None


def _keys(tx):
    msg, meta = tx["transaction"]["message"], tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    return set(msg.get("accountKeys") or []) | set(loaded.get("writable") or []) | set(loaded.get("readonly") or [])


def probe_buys(txs, probe):
    """[(order, blockTime, mint, signature)] of the probe's own succeeded buys, chain order."""
    out = []
    for tx in txs:
        if _failed(tx):
            continue
        v = view(tx)
        for t in _own_trades(v, probe):
            if t["side"] == "buy" and t["paid"]:
                out.append((v["order"], v["bt"], t["mint"], v["sig"]))
    return sorted(out)


def _own_trades(v, probe):
    """The probe's own trades in one view, one per mint it moved (quote tokens excluded)."""
    out = []
    for (owner, mint) in list(v["tok"]):
        if owner != probe or mint in (WSOL, USDC) or not v["tok"][(owner, mint)]:
            continue
        for t in signer_trades(v, mint):
            if t["trader"] == probe:
                out.append(dict(t, mint=mint))
    return out


def first_buys(txs, wallet, k):
    """(buys, prior_position): the wallet's first listed buy of each coin, for the k coins whose first
    buy is newest (pre-registration F, "20 first buys of distinct Solana coins"), chain order. A coin
    the listed transactions show leaving the wallet (a sale or a transfer out) BEFORE its first listed
    buy was held before the listing: it is `prior_position`, counted, never read, and the next coin
    takes its place (Gamma's F guard 1: a later buy never stands in for the first)."""
    first, out_first = {}, {}
    for tx in txs:
        if _failed(tx):
            continue
        v = view(tx)
        for (owner, mint), d in v["tok"].items():
            if owner == wallet and d < 0 and mint not in (WSOL, USDC):
                out_first[mint] = min(v["order"], out_first.get(mint, v["order"]))
    for b in probe_buys(txs, wallet):
        first.setdefault(b[2], b)                   # probe_buys is in chain order: the first is the earliest
    prior = sorted(m for m, b in first.items() if m in out_first and out_first[m] < b[0])
    eligible = [b for m, b in first.items() if m not in prior]
    return sorted(sorted(eligible, reverse=True)[:k]), prior


def sample_buys(txs, probe, *, seed, k=SAMPLE_BUYS):
    """k of the probe's succeeded buys among `txs` (the newest 200 of its stretch), uniformly, with a
    recorded seed. Never the largest, never the most recent."""
    buys = probe_buys(txs, probe)
    return sorted(random.Random(seed).sample(buys, min(k, len(buys))))


# ---------------------------------------------------------------- leaders of one sampled buy
def launch_excluded(first_tx, window_txs, mint):
    """The coin's creator (the signers of its first transaction) and every wallet that bought in the
    creation slot (a launch bundle)."""
    if first_tx is None:
        return set()
    fv = view(first_tx)
    out = set(fv["signers"])
    for tx in [first_tx] + list(window_txs):
        v = view(tx)
        if v["order"][0] == fv["order"][0]:
            out |= {t["trader"] for t in signer_trades(v, mint) if t["side"] == "buy"}
    return out


def leaders(window_txs, buy, *, excluded):
    """Wallets that bought `buy`'s coin before the probe: in its slot with a lower transaction index,
    or in the LEAD_S seconds before it. Buyers are proven by their own signature and balance deltas
    (slice 3b's rule), and `excluded` (probes, our addresses, creator and launch bundle) never lead."""
    (slot, idx), bt, mint, _sig = buy
    out = set()
    for tx in window_txs:
        if _failed(tx):
            continue
        v = view(tx)
        if v["order"] >= (slot, idx):
            continue
        if v["order"][0] != slot and (v["bt"] is None or bt is None or v["bt"] < bt - LEAD_S):
            continue
        out |= {t["trader"] for t in signer_trades(v, mint)
                if t["side"] == "buy" and t["paid"] and t["trader"] not in excluded}
    return out


def candidates(leads):
    """{wallet: {"probes": n, "coins": n}} for wallets that lead at least CAND_PROBES distinct probes on
    at least CAND_COINS distinct coins. `leads` = [(probe, mint, leader)]. Discovery only."""
    probes_of, coins_of = collections.defaultdict(set), collections.defaultdict(set)
    for probe, mint, leader in leads:
        probes_of[leader].add(probe)
        coins_of[leader].add(mint)
    return {w: {"probes": len(probes_of[w]), "coins": len(coins_of[w])} for w in probes_of
            if len(probes_of[w]) >= CAND_PROBES and len(coins_of[w]) >= CAND_COINS}


# ---------------------------------------------------------------- one probe's round trips
def round_trips(txs, probe):
    """Per coin of the probe's stretch (`txs`: every transaction of the stretch, failed ones too):
    SOL out on buys and in on sells from its own balance changes, network fees included, and a status:

    - `unpriced`: the coin moved against another quote token (USDC) or together with another coin;
    - `partial`: the probe held it before the stretch;
    - `closed`: bought inside the stretch and its end balance is at most 1% of the most held;
    - `open`: anything else. Counted, never a win or a loss.

    KNOWN BIAS (Gamma LOW 3): a first buy's native SOL includes the rent of the probe's new token
    account (about 0.002 SOL), refunded only when the probe closes that account. `closes_ata` says
    whether it did inside the stretch; where it did not, `net_sol` is low by that rent.

    A failed transaction's fee is charged to the one stretch coin in its account keys, else it is an
    unattributed fee. Returns ({mint: row}, unattributed_fee_sol, failed_n)."""
    rows = {}
    views = sorted(((view(tx), tx) for tx in txs), key=lambda p: p[0]["order"])
    failed = []
    for v, tx in views:
        if _failed(tx):
            failed.append((v, tx))
            continue
        moved = [m for (o, m), d in v["tok"].items() if o == probe and d and m not in QUOTE_MINTS]
        for mint in moved:
            r = rows.setdefault(mint, {"net_sol": 0.0, "first_buy": None, "last_sell": None, "held": 0, "max_held": 0,
                                       "status": None, "buys": 0, "sells": 0, "fees_failed_sol": 0.0,
                                       "closes_ata": False})
            delta = v["tok"][(probe, mint)]
            if r["first_buy"] is None and r["buys"] == 0 and r["sells"] == 0:
                start = _pre_balance(tx, probe, mint)
                r["held"] = start
                if start > 0:                              # held before the stretch (a sale needs a holding)
                    r["status"] = "partial"
            stable = max(abs(v["tok"].get((probe, q), 0)) for q in STABLES)
            if len(moved) > 1 or stable >= PAY_USDC * 1e6:
                r["status"] = "unpriced"
            r["net_sol"] += (v["native"].get(probe, 0) + v["tok"].get((probe, WSOL), 0)) / 1e9
            r["held"] += delta
            r["closes_ata"] = r["closes_ata"] or _closes_account(tx, probe, mint)
            r["max_held"] = max(r["max_held"], r["held"])
            if delta > 0:
                r["buys"] += 1
                r["first_buy"] = v["bt"] if r["first_buy"] is None else r["first_buy"]
            else:
                r["sells"] += 1
                r["last_sell"] = v["bt"]
    unattributed = 0.0
    for v, tx in failed:
        if v["payer"] != probe:
            continue
        fee = v["fee"] / 1e9
        hit = [m for m in _keys(tx) if m in rows]
        if len(hit) == 1:
            rows[hit[0]]["net_sol"] -= fee
            rows[hit[0]]["fees_failed_sol"] += fee
        else:
            unattributed += fee
    for r in rows.values():
        if r["status"] is None:
            closed = r["first_buy"] is not None and r["held"] <= CLOSED_LEFT * r["max_held"]
            r["status"] = "closed" if closed else "open"
    return rows, unattributed, len(failed)


def sells_into(txs, probe, herder_buys, *, within_s=60):
    """The probe's succeeded sells of a coin inside the `within_s` after a frozen herder's buy of that
    coin (`herder_buys` = {mint: [buy times]}). Descriptive (pre-registration D)."""
    n = 0
    for tx in txs:                                   # a failed transaction moves no balance: no trade
        v = view(tx)
        for t in _own_trades(v, probe):
            if t["side"] == "sell" and v["bt"] is not None and any(
                    0 <= v["bt"] - hb <= within_s for hb in herder_buys.get(t["mint"], ())):
                n += 1
    return n


def _closes_account(tx, owner, mint):
    """The owner's token account for `mint` is in the pre balances and gone from the post ones."""
    meta = tx.get("meta") or {}
    has = lambda side: any(b.get("owner") == owner and b.get("mint") == mint  # noqa: E731
                           for b in meta.get(side) or [])
    return has("preTokenBalances") and not has("postTokenBalances")


def _pre_balance(tx, owner, mint):
    return sum(int(b["uiTokenAmount"]["amount"]) for b in (tx.get("meta") or {}).get("preTokenBalances") or []
               if b.get("owner") == owner and b.get("mint") == mint)


def profitable(rows, unattributed_fee_sol=0.0):
    """Section D's verdict for one probe: at least PROFIT_MIN_CLOSED closed coins, the sum of net SOL
    over closed coins (minus unattributed fees) above 0, and the median per-coin net above 0."""
    closed = [r for r in rows.values() if r["status"] == "closed"]
    nets = [r["net_sol"] for r in closed]
    total = sum(nets) - unattributed_fee_sol
    holds = [r["last_sell"] - r["first_buy"] for r in closed if r["last_sell"] is not None and r["first_buy"] is not None]
    counts = collections.Counter(r["status"] for r in rows.values())
    return {"closed": len(closed), "open": counts["open"], "partial": counts["partial"], "unpriced": counts["unpriced"],
            "net_sol": total, "median_net_sol": statistics.median(nets) if nets else None,
            "gain_share": (sum(1 for x in nets if x > 0) / len(nets)) if nets else None,
            "median_hold_s": statistics.median(holds) if holds else None,
            "profitable": len(closed) >= PROFIT_MIN_CLOSED and total > 0 and statistics.median(nets) > 0}
