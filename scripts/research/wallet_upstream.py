"""Wallet Map slice 3b: upstream discovery (pre-registration
docs/superpowers/specs/2026-09-30-memecoin-wallet-map-slice3b-upstream-design.md, Gamma CLEAN a6ccd12dd).

Which wallets, not on our roster, bought a coin shortly BEFORE one of our Solana sources did, on
coins that were higher 60 s later more often than that same source's other coins? Descriptive: a
wallet that qualifies is an "upstream candidate" on the map. Nothing here arms, sizes, admits or
exits.

Pure: transactions in, rows out. The reads (research key, flat Solana book) and the pass that calls
this live in wallet_link_graph.py; this module imports nothing from it, so it has one identity.
"""
import collections
import json
import statistics
from datetime import datetime, timezone

UPSTREAM_METHOD_A1 = ("upstream-v1:pre60:launch:signer-paid001:win60p15:fxwin:trade01:nofill:m8:"
                      "strat-shrink10-pb:bh05:k3:d2:fwd8")
UPSTREAM_METHOD = UPSTREAM_METHOD_A1 + ":fxband-a1b12h3"     # amendment A1b (Gamma bff373583)
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PRE_S, LABEL_S, LAUNCH_AGE_S = 60, 60, 60
WIN_RET = 0.15
WIN_EPS = 1e-9                             # p_end / p0 - 1 of a nominal +15.0% is 0.1499999999999999 in binary
PAY_SOL, PAY_USDC = 0.01, 1.0              # rule 4: a buyer paid at least this much
TRADE_SOL, TRADE_USDC = 0.1, 10.0          # amendment A2: a trade that prices the end of the minute
FX_BAND = 0.05                             # amendment A1 (Gamma): a window rate this far off its median is no rate
FX_NEAR_S, FX_NEAR_MIN = 12 * 3600, 3      # amendment A1b: the median of >= 3 OTHER stored windows within 12 h
LABEL_COLS = ("quote", "fx_rate", "p0", "p_end", "ret_60", "win", "our_fill", "n_end", "n_end_other",
              "label_reason", "status")
UP_PAGES = 10
UP_CALLS_MAX = 1 + UP_PAGES + 1            # the age probe, the window pages, the end-price page
UPSTREAM_BUDGET = 150
PROBE_EMPTY_MAX = 3
FAMILY_M, SHRINK, MIN_K, MIN_DAYS, FWD_N = 8, 10, 3, 2, 8
CONTROL_MIN, BH_Q, FWD_P = 0.9, 0.05, 0.05
OUR_FILL_SLACK_S = 10                      # ledger fill time against chain time
COINS_MAX = 10


# ---------------------------------------------------------------- one transaction
def view(tx):
    """The parts of a Helius/RPC `json` transaction this slice reads: chain order, signers, the fee
    payer, native SOL changes by address and token changes by (owner, mint)."""
    meta = tx.get("meta") or {}
    msg = tx["transaction"]["message"]
    loaded = meta.get("loadedAddresses") or {}
    keys = (list(msg.get("accountKeys") or []) + list(loaded.get("writable") or [])
            + list(loaded.get("readonly") or []))
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    tok = collections.defaultdict(int)
    dec = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            tok[(b.get("owner"), b.get("mint"))] += sign * int(b["uiTokenAmount"]["amount"])
            dec[b.get("mint")] = int(b["uiTokenAmount"]["decimals"])
    return {"sig": tx["transaction"]["signatures"][0], "bt": tx.get("blockTime"),
            "order": (tx.get("slot") or 0, tx.get("transactionIndex") or 0),
            "slot_known": type(tx.get('slot')) is int and tx['slot']>=0,
            "index_known": type(tx.get('transactionIndex')) is int and tx['transactionIndex']>=0,
            "signers": keys[:int((msg.get("header") or {}).get("numRequiredSignatures") or 0)],
            "payer": keys[0] if keys else None, "fee": int(meta.get("fee") or 0),
            "native": {keys[i]: post[i] - pre[i] for i in range(min(len(keys), len(pre), len(post)))},
            "tok": tok, "dec": dec}


def signer_trades(v, mint):
    """One row per SIGNER whose own balance of `mint` changed: {trader, side, tokens, quote, amount,
    paid}. The trader is proven by its own signature and its own balance deltas, never by the
    largest mover. `amount` is what that signer paid (buy) or received (sell): USDC when its own
    USDC moved by at least PAY_USDC, else SOL (native net of the fee it paid as fee payer, plus its
    own WSOL). `paid` is the spec's rule 4, for buys only."""
    out = []
    for s in v["signers"]:
        delta = v["tok"].get((s, mint), 0)
        if not delta:
            continue
        side = 1 if delta < 0 else -1                 # a buyer's quote balances fall
        native, wsol, usdc = v["native"].get(s, 0), v["tok"].get((s, WSOL), 0), v["tok"].get((s, USDC), 0)
        sol = side * (native + (v["fee"] if s == v["payer"] else 0) + wsol) / 1e9
        usd = side * usdc / 1e6
        quote, amount = ("USDC", usd) if usd >= PAY_USDC else ("SOL", sol) if sol > 0 else (None, None)
        paid = delta > 0 and (-native > v["fee"] + PAY_SOL * 1e9 or -wsol >= PAY_SOL * 1e9
                              or -usdc >= PAY_USDC * 1e6)
        out.append({"trader": s, "side": "buy" if delta > 0 else "sell",
                    "tokens": abs(delta) / 10 ** v["dec"].get(mint, 6), "quote": quote, "amount": amount,
                    "paid": bool(paid)})
    return out


def window_rate(views):
    """USDC per SOL inside the window (amendment A1): the median over non-signer accounts whose own
    WSOL and USDC moved in opposite directions in one transaction (a SOL/USDC pool leg of a routed
    swap), and the number of such legs. (None, 0) when the window holds none."""
    rates = []
    for v in views:
        for (owner, mint), w in v["tok"].items():
            u = v["tok"].get((owner, USDC), 0)
            if mint == WSOL and owner not in v["signers"] and abs(w) >= PAY_SOL * 1e9 and u and (w > 0) != (u > 0):
                rates.append((abs(u) / 1e6) / (abs(w) / 1e9))
    return (statistics.median(rates) if rates else None), len(rates)


def _price(trade, rate, quote):
    """Quote per token of one signer trade, or None. With a window rate every trade is in SOL; without
    one only a trade in `quote` has a price (the registered same-quote rule)."""
    if not trade["quote"] or not trade["tokens"]:
        return None
    if rate:
        amount = trade["amount"] if trade["quote"] == "SOL" else trade["amount"] / rate
    elif trade["quote"] == quote:
        amount = trade["amount"]
    else:
        return None
    return amount / trade["tokens"]


# ---------------------------------------------------------------- one unit
def upstream_read(txs, unit, *, truncated, end_txs, launch, excluded, source_addresses, ours,
                  our_fill_near=False, creator_fn=None):
    """(read row, early buys) for one unit from its window pages.

    Status, first that applies: truncated_before, not_found, clock_mismatch, no_label, our_fill, ok.
    `control_named` (the buyer rule names the source in its own transaction) exists whenever the
    source transaction was read. Early buyers are complete whenever the source is inside the pages."""
    sig, t0, mint = unit["signature"], int(unit["t0"]), unit["mint"]
    views = [view(t) for t in txs if t.get("blockTime") is not None]
    src = next((v for v in views if v["sig"] == sig), None)
    if src is None:
        return {"status": "truncated_before" if truncated else "not_found"}, []
    if src["bt"] != t0:
        return {"status": "clock_mismatch"}, []

    mine = [tr for tr in signer_trades(src, mint) if tr["side"] == "buy" and tr["trader"] in source_addresses]
    named = next((tr for tr in mine if tr["paid"]), None)
    row = {"control_named": int(named is not None),
           "control_fail": None if named else ("under_floor" if mine else "no_rise"
                                                if set(source_addresses) & set(src["signers"]) else "not_signer")}

    skip = set(excluded)
    if creator_fn is not None:
        creator = next((c for c in (creator_fn(t, mint) for t in txs) if c), None)
        if creator:
            skip.add(creator)
    def before_source(v):
        if not v['slot_known'] or not src['slot_known']:return False
        return v['order'][0]<src['order'][0] or (v['order'][0]==src['order'][0] and
            v['index_known'] and src['index_known'] and v['order'][1]<src['order'][1])
    before = sorted((v for v in views if before_source(v) and (launch or v["bt"] >= t0 - PRE_S)),
                    key=lambda v: v["order"])
    row['n_order_unknown']=sum(v['sig']!=sig and v['bt']<=t0 and (launch or v['bt']>=t0-PRE_S) and
        (not v['slot_known'] or not src['slot_known'] or
         (v['order'][0]==src['order'][0] and not (v['index_known'] and src['index_known']))) for v in views)
    buys, seen = [], set()
    n_unnamed = n_under = n_buy_tx = n_sell_tx = n_nomove = 0
    for v in before:
        every = signer_trades(v, mint)
        trades = [tr for tr in every if tr["side"] == "buy"]
        sold = len(trades) < len(every)
        # What the before-transactions are (POST HOC instrument counters, after the smoke read: a
        # launch showed 978 of them and 24 buyers). They feed no rule.
        n_buy_tx += bool(trades)
        n_sell_tx += sold
        n_nomove += not any(d for (_o, m), d in v["tok"].items() if m == mint)
        n_under += sum(1 for tr in trades if not tr["paid"])
        paid = [tr for tr in trades if tr["paid"]]
        if not paid:
            rose = any(d > 0 for (o, m), d in v["tok"].items() if m == mint and o not in v["signers"])
            n_unnamed += int(rose and not sold and not trades)
        for tr in paid:
            if tr["trader"] in skip or tr["trader"] in seen:
                continue
            seen.add(tr["trader"])
            buys.append({"signature": sig, "buy_signature": v["sig"], "trader": tr["trader"], "lead_s": t0 - v["bt"],
                         "slot": v["order"][0], "tx_index": v["order"][1] if v['index_known'] else None,
                         "order_proven": 1,"tokens": tr["tokens"]})
    row.update(n_before_tx=len(before), n_early=len(buys), n_unnamed=n_unnamed, n_under_floor=n_under,
               n_before_buy_tx=n_buy_tx, n_before_sell_tx=n_sell_tx, n_before_nomove_tx=n_nomove)

    # The label: the source's own fill against the last 3 priced trades of the minute after it.
    end_views = views if not truncated else None if end_txs is None else [
        view(t) for t in end_txs if t.get("blockTime") is not None]
    rate, legs = window_rate(views + (end_views or [] if truncated else []))
    label = _label(named, end_views, rate, t0, mint, ours, our_fill_near, truncated)
    # The same label under the registered same-quote rule, kept until the run's rates are known:
    # a rate outside the run's band is no rate (apply_rate_band).
    alt = _label(named, end_views, None, t0, mint, ours, our_fill_near, truncated) if rate else None
    return dict(row, fx_legs=legs, fx_rejected=None, _alt=alt, **label), buys


def _label(named, end_views, rate, t0, mint, ours, our_fill_near, truncated):
    """The label columns and the status (no_label, our_fill or ok) under one window rate (or none)."""
    quote = "SOL" if rate else (named or {}).get("quote")
    p0 = _price(named, rate, quote) if named else None
    row = {"quote": quote, "fx_rate": rate, "p0": p0, "p_end": None, "ret_60": None, "win": None,
           "our_fill": int(bool(our_fill_near)), "n_end": 0, "n_end_other": 0, "label_reason": None}
    if p0 is None or end_views is None:
        return dict(row, status="no_label", label_reason="source_price" if p0 is None else "end_unread")
    prices, other = [], 0
    for v in sorted(end_views, key=lambda v: v["order"]):
        if not t0 < v["bt"] <= t0 + LABEL_S:        # a later second is a later slot: after the source
            continue
        if v["sig"] in ours:
            row["our_fill"] = 1
            continue
        for tr in signer_trades(v, mint):
            if not tr["quote"] or tr["amount"] < (TRADE_USDC if tr["quote"] == "USDC" else TRADE_SOL):
                continue
            p = _price(tr, rate, quote)
            if p is None:
                other += 1
            else:
                prices.append(p)
    row.update(n_end=len(prices), n_end_other=other)
    if len(prices) >= 3:
        row["p_end"] = statistics.median(prices[-3:])
        row["ret_60"] = row["p_end"] / p0 - 1
        row["win"] = int(row["ret_60"] >= WIN_RET - WIN_EPS)
    elif truncated or other:
        # The minute was not fully read, or it traded in a quote p0 cannot be compared with.
        return dict(row, status="no_label", label_reason="other_quote" if other else "end_thin")
    else:
        row["win"] = 0                              # a fully read minute with under 3 priced trades
    return dict(row, status="our_fill" if row["our_fill"] else "ok")


def a1b_rejects(sig, t0, rate, windows):
    """Amendment A1b: `rate` is more than FX_BAND off the median window rate of the OTHER units in
    `windows` ({signature: (t0, rate)}, every stored rate, kept or rejected, from any run) whose t0
    lies within FX_NEAR_S of `t0`. Fewer than FX_NEAR_MIN such windows: the rate is kept."""
    near = [r for s, (t, r) in windows.items() if s != sig and abs(int(t) - int(t0)) <= FX_NEAR_S]
    return len(near) >= FX_NEAR_MIN and abs(rate / statistics.median(near) - 1) > FX_BAND


def apply_rate_band(reads, stored_windows=None):
    """Amendment A1b (primary): a unit whose window rate a1b_rejects takes the label it has without a
    rate (the registered same-quote rule) and keeps the rate in fx_rejected. The label NOT used is
    stored in fx_alt_label, so a band change never needs a re-read. A1 (the run's median, Gamma's
    first addition) is kept for its sensitivity line in fx_a1_rejected, never applied; a re-read
    unit carries the A1 decision of its first read (`_a1_prior`). Returns how many units A1b changed."""
    windows = dict(stored_windows or {})
    windows.update({r["signature"]: (int(r["t0"]), r["fx_rate"]) for r in reads if r.get("_alt") and r.get("fx_rate")})
    run = [r["fx_rate"] for r in reads if r.get("_alt") and r.get("fx_rate")]
    run_mid, changed = (statistics.median(run) if run else None), 0
    for r in reads:
        alt, a1_prior = r.pop("_alt", None), r.pop("_a1_prior", None)
        if not alt:
            continue
        rate = r["fx_rate"]
        a1 = a1_prior if a1_prior is not None else abs(rate / run_mid - 1) > FX_BAND
        r["fx_a1_rejected"] = rate if a1 else None
        if a1b_rejects(r["signature"], r["t0"], rate, windows):
            r["fx_alt_label"] = json.dumps({c: r.get(c) for c in LABEL_COLS})
            r.update(alt, fx_rejected=rate)
            changed += 1
        else:
            r["fx_alt_label"] = json.dumps(alt)
    return changed


def a1b_label(row):
    """The label under A1b, the primary: the stored label columns."""
    return {c: row.get(c) for c in LABEL_COLS}


def a1_label(row):
    """The label under A1, the sensitivity line: the stored label when A1 and A1b decided alike (or
    the row is still stored under A1), else the stored other label; None when unknown."""
    if (row.get("method_version") == UPSTREAM_METHOD_A1
            or (row.get("fx_a1_rejected") is None) == (row.get("fx_rejected") is None)):
        return a1b_label(row)
    return json.loads(row["fx_alt_label"]) if row.get("fx_alt_label") else None


def read_upstream(units, *, stored_windows=None, **kw):
    """`_read_units`, then the rate band over what it read and every stored window (apply_rate_band)."""
    reads, buys, calls, why = _read_units(units, **kw)
    apply_rate_band(reads, stored_windows)
    return reads, buys, calls, why


def _read_units(units, *, fetch_age, fetch_window, fetch_end, budget, flat_fn, excluded, addresses_for,
                ours, our_fill_near, stop, creator_fn=None):
    """(reads, early buys, calls, why stopped). Per unit: the age (slice 3's stored age, else the
    probe), the window (from t0 - 60 s, or from the first transaction on a launch), and one
    descending end-price page when the window was cut after the source. A read starts only when its
    worst case (UP_CALLS_MAX) fits the budget and the Solana book is flat; `stop` (the caller's
    ReadStop) records the unit unread and ends the pass."""
    reads, buys, calls = [], [], 0
    for u in units:
        if calls + UP_CALLS_MAX > budget:
            return reads, buys, calls, "budget"
        if not flat_fn():
            return reads, buys, calls, "solana book open"
        base = {k: u[k] for k in ("signature", "source_user_id", "mint", "origin", "t0")}
        base["empty_probes"] = int(u.get("empty_probes") or 0)
        for carried in ("_a1_prior", "first_read_at"):         # an A1b re-read (relabel_a1b)
            if carried in u:
                base[carried] = u[carried]
        used, t0 = 0, int(u["t0"])
        try:
            age = u.get("age_s")
            if age is None:
                first, n = fetch_age(u)
                used += n
                age = None if first is None else t0 - int(first)
                if age is None or age < 0:            # an index gap, never a launch (slice 3's rule)
                    empties = base["empty_probes"] + 1
                    reads.append(dict(base, status="probe_empty" if empties >= PROBE_EMPTY_MAX else "unread",
                                      empty_probes=empties, calls=used))
                    calls += used
                    continue
            launch = age <= LAUNCH_AGE_S
            txs, n, truncated = fetch_window(u, t0 - age if launch else t0 - PRE_S)
            used += n
            end = None
            if truncated and any(t["transaction"]["signatures"][0] == u["signature"] for t in txs):
                end, n = fetch_end(u)
                used += n
        except stop as exc:
            reads.append(dict(base, status="unread", calls=used))
            return reads, buys, calls + used, "stopped: %s" % exc
        calls += used
        row, early = upstream_read(txs, u, truncated=truncated, end_txs=end, launch=launch, excluded=excluded,
                                   source_addresses=addresses_for(u["source_user_id"]), ours=ours,
                                   our_fill_near=our_fill_near(u), creator_fn=creator_fn)
        reads.append(dict(base, age_s=age, launch=int(launch), calls=used, **row))
        buys.extend(early)
    return reads, buys, calls, "done"


# ---------------------------------------------------------------- storage (the graph DB)
UPSTREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS memecoin_wallet_upstream_reads (
    signature TEXT PRIMARY KEY,
    mint TEXT,
    source_user_id TEXT,
    origin TEXT NOT NULL,
    t0 INTEGER,
    age_s INTEGER,
    launch INTEGER,
    status TEXT NOT NULL,
    label_reason TEXT,
    n_before_tx INTEGER,
    n_early INTEGER,
    n_unnamed INTEGER,
    n_under_floor INTEGER,
    n_before_buy_tx INTEGER,
    n_before_sell_tx INTEGER,
    n_before_nomove_tx INTEGER,
    n_order_unknown INTEGER,
    control_named INTEGER,
    control_fail TEXT,
    p0 REAL,
    p_end REAL,
    quote TEXT,
    fx_rate REAL,
    fx_legs INTEGER,
    fx_rejected REAL,
    fx_a1_rejected REAL,
    fx_alt_label TEXT,
    ret_60 REAL,
    win INTEGER,
    n_end INTEGER,
    n_end_other INTEGER,
    our_fill INTEGER,
    p_null REAL,
    empty_probes INTEGER NOT NULL DEFAULT 0,
    calls INTEGER,
    method_version TEXT NOT NULL,
    read_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memecoin_wallet_early_buys (
    signature TEXT NOT NULL,
    trader TEXT NOT NULL,
    lead_s INTEGER,
    slot INTEGER,
    tx_index INTEGER,
    tokens REAL,
    buy_signature TEXT,
    order_proven INTEGER,
    PRIMARY KEY (signature, trader)
);
CREATE INDEX IF NOT EXISTS idx_wallet_early_buys_trader ON memecoin_wallet_early_buys (trader);
CREATE TABLE IF NOT EXISTS memecoin_wallet_upstream (
    trader TEXT NOT NULL,
    method_version TEXT NOT NULL,
    m INTEGER NOT NULL,
    m_small INTEGER,
    k INTEGER,
    expected REAL,
    days INTEGER,
    lead_median_s REAL,
    p_value REAL,
    q_value REAL,
    verdict TEXT NOT NULL,
    verdict_reason TEXT,
    coins TEXT,
    first_candidate_at TEXT,
    fwd_m INTEGER,
    fwd_k INTEGER,
    fwd_expected REAL,
    fwd_p REAL,
    fwd_verdict TEXT,
    fwd_read_at TEXT,
    excluded TEXT,
    window_from TEXT,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (trader, method_version)
);
"""
ADDED_COLS = {"n_before_buy_tx": "INTEGER", "n_before_sell_tx": "INTEGER",     # after 2ae02483d
              "n_before_nomove_tx": "INTEGER", "fx_a1_rejected": "REAL", "fx_alt_label": "TEXT",
              "n_order_unknown": "INTEGER"}   # A1b + identity order coverage


def ensure_schema(conn):
    """Create the tables, and add the columns a table made by an earlier release lacks (the three
    POST HOC counters came one commit after the tables)."""
    conn.executescript(UPSTREAM_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_wallet_upstream_reads)")}
    for col, kind in ADDED_COLS.items():
        if col not in have:
            conn.execute("ALTER TABLE memecoin_wallet_upstream_reads ADD COLUMN %s %s" % (col, kind))
    have={r[1] for r in conn.execute('PRAGMA table_info(memecoin_wallet_early_buys)')}
    for col,kind in (('buy_signature','TEXT'),('order_proven','INTEGER')):
        if col not in have:conn.execute('ALTER TABLE memecoin_wallet_early_buys ADD COLUMN '+col+' '+kind)


READ_COLS = ("signature", "mint", "source_user_id", "origin", "t0", "age_s", "launch", "status", "label_reason",
             "n_before_tx", "n_early", "n_unnamed", "n_under_floor", "n_before_buy_tx", "n_before_sell_tx",
             "n_before_nomove_tx", "n_order_unknown", "control_named", "control_fail", "p0",
             "p_end", "quote", "fx_rate", "fx_legs", "fx_rejected", "fx_a1_rejected", "fx_alt_label", "ret_60", "win", "n_end", "n_end_other", "our_fill", "p_null",
             "empty_probes", "calls")
ROW_COLS = ("trader", "m", "m_small", "k", "expected", "days", "lead_median_s", "p_value", "q_value", "verdict",
            "verdict_reason", "coins", "first_candidate_at", "fwd_m", "fwd_k", "fwd_expected", "fwd_p",
            "fwd_verdict", "fwd_read_at", "excluded", "window_from", "computed_at")
CARRIED = ("first_candidate_at", "fwd_m", "fwd_k", "fwd_expected", "fwd_p", "fwd_verdict", "fwd_read_at")


def save_reads(conn, reads, buys, *, read_at):
    """Store reads and their early buys in one transaction. An `unread` unit is read again (it has no
    buys), and so, once, is a unit A1b relabels (relabel_a1b): its old early buys are dropped and its
    first read time is kept (`first_read_at`)."""
    ensure_schema(conn)
    with conn:
        again = [r["signature"] for r in reads if r.get("first_read_at")]
        for i in range(0, len(again), 500):
            chunk = again[i:i + 500]
            conn.execute("DELETE FROM memecoin_wallet_early_buys WHERE signature IN (%s)" % ",".join("?" * len(chunk)),
                         chunk)
        conn.executemany(
            "INSERT OR REPLACE INTO memecoin_wallet_upstream_reads (%s, method_version, read_at) VALUES (%s)"
            % (", ".join(READ_COLS), ",".join("?" * (len(READ_COLS) + 2))),
            [tuple(int(r.get(c) or 0) if c == "empty_probes" else r.get(c) for c in READ_COLS)
             + (UPSTREAM_METHOD, r.get("first_read_at") or read_at) for r in reads])
        conn.executemany(
            "INSERT OR REPLACE INTO memecoin_wallet_early_buys (signature, trader, lead_s, slot, tx_index, tokens, buy_signature,order_proven) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(b["signature"], b["trader"], b["lead_s"], b["slot"], b["tx_index"], b["tokens"], b.get('buy_signature'),b.get('order_proven')) for b in buys])


def load_reads(conn):
    ensure_schema(conn)
    rows = conn.execute("SELECT %s FROM memecoin_wallet_upstream_reads WHERE method_version=?"
                        % ", ".join(READ_COLS), (UPSTREAM_METHOD,)).fetchall()
    return {r[0]: dict(zip(READ_COLS, r)) for r in rows}


def load_buys(conn, signatures):
    """{signature: [early-buy rows]} for the given units, 500 per query."""
    out, sigs = collections.defaultdict(list), list(signatures)
    for i in range(0, len(sigs), 500):
        chunk = sigs[i:i + 500]
        for s, trader, lead in conn.execute(
                "SELECT signature, trader, lead_s FROM memecoin_wallet_early_buys WHERE signature IN (%s)"
                % ",".join("?" * len(chunk)), chunk):
            out[s].append({"trader": trader, "lead_s": lead})
    return out


def prune(conn, keep_signatures):
    """Drop reads and early buys of units that left the window; returns the reads dropped."""
    keep = set(keep_signatures)
    gone = [r[0] for r in conn.execute("SELECT signature FROM memecoin_wallet_upstream_reads") if r[0] not in keep]
    with conn:
        for i in range(0, len(gone), 500):
            chunk = gone[i:i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute("DELETE FROM memecoin_wallet_upstream_reads WHERE signature IN (%s)" % marks, chunk)
            conn.execute("DELETE FROM memecoin_wallet_early_buys WHERE signature IN (%s)" % marks, chunk)
    return len(gone)


# ---------------------------------------------------------------- units
def upstream_units(impact_units):
    """One unit per COIN: the earliest slice-3 unit (first ok slot read per source and mint) by chain
    time across all sources."""
    best = {}
    for u in impact_units:
        key = (u["t0"], u.get("slot") or 0, u.get("idx") or 0)
        if u["mint"] not in best or key < best[u["mint"]][0]:
            best[u["mint"]] = (key, u)
    return [u for _k, u in best.values()]


def stored_windows(conn):
    """{signature: (t0, rate)} of every stored read with a window rate, kept or rejected, any run."""
    ensure_schema(conn)
    return {s: (t, r) for s, t, r in conn.execute(
        "SELECT signature, t0, COALESCE(fx_rate, fx_rejected) FROM memecoin_wallet_upstream_reads "
        "WHERE COALESCE(fx_rate, fx_rejected) IS NOT NULL AND t0 IS NOT NULL")}


def relabel_a1b(conn, dry_run=False):
    """Amendment A1b, once over the rows stored under A1 (UPSTREAM_METHOD_A1). A row A1b decides like
    A1 moves to UPSTREAM_METHOD in place (fx_a1_rejected = A1's fx_rejected), and so do the A1
    per-wallet rows, whose carried forward columns must survive. A row A1b decides otherwise needs the
    label A1 never stored: it stays under A1, so `pending` reads it again (`reread_carry`). Counts."""
    ensure_schema(conn)
    rows = conn.execute("SELECT signature, t0, fx_rate, fx_rejected FROM memecoin_wallet_upstream_reads "
                        "WHERE method_version=?", (UPSTREAM_METHOD_A1,)).fetchall()
    windows = stored_windows(conn)
    out = {"rated": 0, "a1_rejected": 0, "a1b_rejected": 0, "kept_sparse": 0, "reread": []}
    for sig, t0, rate, rejected in rows:
        own = rate if rate is not None else rejected
        if own is None or t0 is None:
            continue
        out["rated"] += 1
        out["a1_rejected"] += rejected is not None
        near = sum(1 for s, (t, _r) in windows.items() if s != sig and abs(int(t) - int(t0)) <= FX_NEAR_S)
        out["kept_sparse"] += near < FX_NEAR_MIN
        a1b = a1b_rejects(sig, t0, own, windows)
        out["a1b_rejected"] += a1b
        if a1b != (rejected is not None):
            out["reread"].append(sig)
    if not dry_run and rows:
        keep = set(out["reread"])
        with conn:
            conn.executemany(
                "UPDATE memecoin_wallet_upstream_reads SET method_version=?, fx_a1_rejected=fx_rejected "
                "WHERE signature=? AND method_version=?",
                [(UPSTREAM_METHOD, r[0], UPSTREAM_METHOD_A1) for r in rows if r[0] not in keep])
            conn.execute("UPDATE memecoin_wallet_upstream SET method_version=? WHERE method_version=?",
                         (UPSTREAM_METHOD, UPSTREAM_METHOD_A1))
    return out


def reread_carry(conn):
    """{signature: carried fields} for the rows still under A1 that A1b reads again: the A1 decision
    of the first read and the first read time."""
    ensure_schema(conn)
    return {s: {"_a1_prior": rejected is not None, "first_read_at": read_at} for s, rejected, read_at in conn.execute(
        "SELECT signature, fx_rejected, read_at FROM memecoin_wallet_upstream_reads WHERE method_version=?",
        (UPSTREAM_METHOD_A1,))}


def pending(units, done, ages, carry=None):
    """Units with no final read (an unread one keeps its empty-probe count), detection-fed first, then
    newest t0 first. `ages` is slice 3's stored age per signature: a known age skips the probe."""
    out = []
    for u in units:
        prior = done.get(u["signature"])
        if prior is not None and prior["status"] != "unread":
            continue
        out.append(dict(u, empty_probes=(prior or {}).get("empty_probes") or 0, age_s=ages.get(u["signature"]),
                        **(carry or {}).get(u["signature"], {})))
    return sorted(out, key=lambda u: (u["origin"] != "detection", -u["t0"]))


# ---------------------------------------------------------------- the null, the test, the forward grade
def unit_nulls(reads):
    """{signature: (p_i, n)} for every detection-fed ok unit that has a null probability.

    p_i = (w + SHRINK x r) / (n + SHRINK): n and w are the count and the winners of the OTHER ok units
    of the same source and launch flag; r is the win rate of all other ok units with that launch flag.
    No p_i when that launch-flag set holds fewer than SHRINK other units."""
    ok = [r for r in reads if r["status"] == "ok" and r["origin"] == "detection" and r["win"] is not None]
    by_flag, by_src = collections.Counter(), collections.Counter()
    win_flag, win_src = collections.Counter(), collections.Counter()
    for r in ok:
        by_flag[r["launch"]] += 1
        win_flag[r["launch"]] += r["win"]
        by_src[(r["source_user_id"], r["launch"])] += 1
        win_src[(r["source_user_id"], r["launch"])] += r["win"]
    out = {}
    for r in ok:
        flag, key = r["launch"], (r["source_user_id"], r["launch"])
        others = by_flag[flag] - 1
        if others < SHRINK:
            continue
        rate = (win_flag[flag] - r["win"]) / others
        n, w = by_src[key] - 1, win_src[key] - r["win"]
        out[r["signature"]] = ((w + SHRINK * rate) / (n + SHRINK), n)
    return out


def pb_tail(k, probs):
    """P(X >= k) for X a sum of independent Bernoulli(p_i): the exact Poisson-binomial upper tail."""
    dist = [1.0]
    for p in probs:
        nxt = [0.0] * (len(dist) + 1)
        for j, mass in enumerate(dist):
            nxt[j] += mass * (1 - p)
            nxt[j + 1] += mass * p
        dist = nxt
    return min(1.0, sum(dist[k:])) if k > 0 else 1.0


def _bh(pvalues):
    """Benjamini-Hochberg adjusted q per position (step-up), capped at 1."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [None] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def _day(t0):
    return datetime.fromtimestamp(int(t0), tz=timezone.utc).strftime("%Y-%m-%d")


def control(reads):
    """(named, total, {reason: n}) of the buyer rule on the source's own transaction over ok units."""
    ok = [r for r in reads if r["status"] == "ok" and r["control_named"] is not None]
    fails = collections.Counter(r["control_fail"] for r in ok if not r["control_named"])
    return sum(r["control_named"] for r in ok), len(ok), dict(fails)


def wallet_rows(reads, buys, *, copy_bots, hub_funded, funder_read, prior, window_from, computed_at):
    """The per-wallet rows of this run (spec 'Per wallet', 'The test', 'Forward grade').

    `buys` is {signature: [early-buy rows]}. `copy_bots` and `hub_funded` are excluded from the test
    and stored; a wallet that would be in the family with an unread funder is `unknown`. `prior` is
    {trader: last run's row}: first_candidate_at and the forward columns are carried, never moved.
    Rows exist for wallets with m >= FAMILY_M and for every wallet that was ever a candidate."""
    nulls = unit_nulls(reads)
    named, total, _fails = control(reads)
    control_ok = total > 0 and named / total >= CONTROL_MIN
    by_sig = {r["signature"]: r for r in reads}
    mine = collections.defaultdict(list)
    for sig in nulls:
        for b in buys.get(sig, ()):
            mine[b["trader"]].append((by_sig[sig], b["lead_s"]))
    rows = {}
    for trader in set(mine) | {t for t, p in prior.items() if p.get("first_candidate_at")}:
        units = sorted(mine.get(trader, ()), key=lambda x: x[0]["t0"])
        if len(units) < FAMILY_M and not (prior.get(trader) or {}).get("first_candidate_at"):
            continue
        wins = [r for r, _l in units if r["win"]]
        row = dict.fromkeys(ROW_COLS)
        row.update(trader=trader, m=len(units), k=len(wins),
                   m_small=sum(1 for r, _l in units if nulls[r["signature"]][1] < SHRINK),
                   expected=round(sum(nulls[r["signature"]][0] for r, _l in units), 6),
                   days=len({_day(r["t0"]) for r in wins}),
                   lead_median_s=statistics.median(l for _r, l in units) if units else None,
                   coins=json.dumps([{"mint": r["mint"], "t0": r["t0"], "lead_s": lead, "ret_60": r["ret_60"],
                                      "win": r["win"], "p_null": round(nulls[r["signature"]][0], 6)}
                                     for r, lead in units[-COINS_MAX:]]),
                   excluded="copy_bot" if trader in copy_bots else "hub_funded" if trader in hub_funded else None,
                   verdict="untested", window_from=window_from, computed_at=computed_at)
        row.update({c: (prior.get(trader) or {}).get(c) for c in CARRIED})
        if row["excluded"]:
            row["verdict_reason"] = row["excluded"]
        elif row["m"] < FAMILY_M:
            row["verdict_reason"] = "m"
        elif not control_ok:
            row.update(verdict="unknown", verdict_reason="control")
        else:
            # THE FAMILY IS EVERY NON-EXCLUDED WALLET WITH m >= FAMILY_M (spec slice 3b "The test";
            # Gamma on run 32). An unread funder makes the VERDICT unknown, not the wallet's absence
            # from BH: dropping it would let the read order of funders move every other wallet's q.
            row["p_value"] = pb_tail(row["k"], [nulls[r["signature"]][0] for r, _l in units])
            if trader not in funder_read:
                row.update(verdict="unknown", verdict_reason="funder_unread")
        rows[trader] = (row, units)
    family = [row for row, _u in rows.values() if row["p_value"] is not None]
    for row, q in zip(family, _bh([r["p_value"] for r in family])):
        row["q_value"] = round(q, 6)
        if row["verdict_reason"] == "funder_unread":
            continue
        row["verdict"] = ("candidate" if q <= BH_Q and row["k"] >= MIN_K and row["days"] >= MIN_DAYS
                          else "not_distinguished")
    for row, units in rows.values():
        if row["verdict"] == "candidate" and not row["first_candidate_at"]:
            row["first_candidate_at"] = computed_at
        if not row["first_candidate_at"] or row["fwd_verdict"] in ("confirmed", "not_confirmed"):
            continue
        since = datetime.fromisoformat(row["first_candidate_at"]).timestamp()
        forward = [r for r, _l in units if r["t0"] > since]
        row.update(fwd_m=len(forward), fwd_verdict="pending")
        if len(forward) >= FWD_N:                     # ONE read, at exactly FWD_N units
            first = forward[:FWD_N]
            probs = [nulls[r["signature"]][0] for r in first]
            k = sum(r["win"] for r in first)
            p = pb_tail(k, probs)
            row.update(fwd_m=FWD_N, fwd_k=k, fwd_expected=round(sum(probs), 6), fwd_p=p,
                       fwd_verdict="confirmed" if p <= FWD_P else "not_confirmed", fwd_read_at=computed_at)
    return [row for row, _u in rows.values()], nulls


def load_rows(conn):
    ensure_schema(conn)
    rows = conn.execute("SELECT %s FROM memecoin_wallet_upstream WHERE method_version=?" % ", ".join(ROW_COLS),
                        (UPSTREAM_METHOD,)).fetchall()
    return {r[0]: dict(zip(ROW_COLS, r)) for r in rows}


def write_rows(conn, rows, nulls):
    """Replace this method's per-wallet rows and stamp each unit's p_i, in one transaction."""
    ensure_schema(conn)
    with conn:
        conn.execute("DELETE FROM memecoin_wallet_upstream WHERE method_version=?", (UPSTREAM_METHOD,))
        conn.executemany(
            "INSERT INTO memecoin_wallet_upstream (%s, method_version) VALUES (%s)"
            % (", ".join(ROW_COLS), ",".join("?" * (len(ROW_COLS) + 1))),
            [tuple(r[c] for c in ROW_COLS) + (UPSTREAM_METHOD,) for r in rows])
        conn.execute("UPDATE memecoin_wallet_upstream_reads SET p_null=NULL WHERE method_version=?",
                     (UPSTREAM_METHOD,))
        conn.executemany("UPDATE memecoin_wallet_upstream_reads SET p_null=? WHERE signature=?",
                         [(round(p, 6), sig) for sig, (p, _n) in nulls.items()])
    return len(rows)
