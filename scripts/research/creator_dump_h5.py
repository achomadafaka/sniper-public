#!/usr/bin/env python3
"""H5 pre-entry blocker (creator-dump prereg, Amendments 1-3). Stages (each writes a file under data/research/h5/):
  bursts-curve DAY      -> bursts-curve-DAY.jsonl   (B_5 bursts on standard SOL-curve coins; cache entries (actor, t_known))
  bursts-ps    LO HI    -> bursts-ps.jsonl          (B_5 bursts on PumpSwap pools, SOL quote, received_at in [LO, HI))
  moments DAY           -> moments-DAY.jsonl        (curve decision moments every 30 s: known shares + crash50 + R300; cache = all bursts files)
  enters                -> enters.jsonl             (actual desk vet ENTER verdicts with the same measures)
Burst (earliest prefix): within 4 consecutive slots, >= 5 distinct sellers AND sell SOL >= 3% of the REAL quote reserve before the window AND buy SOL / total < 5%.
Cache entry time of a participant = the observable time it is known: detection time for the sellers already in the window, then each later seller of the same active burst at its own sell time."""
import bisect, glob, json, os, sqlite3, sys, time
from collections import defaultdict, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "research" / "h5"
N_SELL, WIN_SLOTS, MIN_SELL_FRAC, MAX_BUY_FRAC, MAX_PRICE_RATIO = 20, 3, 0.03, 0.05, 0.5      # Amendment 4: B_live
CACHE_S = 24 * 3600
SAMPLE_S, LAT, COST = 30.0, 1.5, 0.025
MAYHEM = {"BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"}


def detect(t, slot, buy, qa, actor, res_pre, pre_price, post_price=None, baseline="first"):
    """-> list of bursts [{'detect_t', 'detect_k', 'participants': {actor: t_known}, 'slots': (s0, s1)}] over one pool/coin stream (arrival order)."""
    out, n = [], len(t)
    win = deque()                       # indices in the 4-slot window
    sellers = defaultdict(int)
    sell_sol = buy_sol = 0.0
    active = None
    for k in range(n):
        while win and slot[win[0]] < slot[k] - WIN_SLOTS:
            j = win.popleft()
            if buy[j]:
                buy_sol -= qa[j]
            else:
                sell_sol -= qa[j]; sellers[actor[j]] -= 1
                if sellers[actor[j]] == 0: del sellers[actor[j]]
        win.append(k)
        if buy[k]:
            buy_sol += qa[k]
        else:
            sell_sol += qa[k]; sellers[actor[k]] += 1
        if active is not None and slot[k] - active["last_sell_slot"] > WIN_SLOTS:
            out.append(active); active = None
        if buy[k]:
            continue
        if actor[k] in MAYHEM:
            continue
        if active is not None:
            active["last_sell_slot"] = slot[k]
            active["participants"].setdefault(actor[k], t[k])
            continue
        tot = buy_sol + sell_sol
        r0 = res_pre[win[0]]
        if len(sellers) < N_SELL:
            continue
        if baseline == "peak":          # Amendment 6: live core = latest priced state / max priced state (pre or post) in the window
            vals = [x for j in win for x in (pre_price[j], post_price[j] if post_price else None) if x]
            p0 = max(vals) if vals else None
            p1 = (post_price[k] if post_price and post_price[k] else pre_price[k])
        else:                           # first-pre -> current-pre (sensitivity)
            p0, p1 = pre_price[win[0]], pre_price[k]
        if p0 and p1 is not None and p1 / p0 <= MAX_PRICE_RATIO:
            live5 = bool(r0 and r0 > 0 and sell_sol >= MIN_SELL_FRAC * r0 and tot > 0 and buy_sol / tot < MAX_BUY_FRAC)
            active = {"detect_t": t[k], "detect_k": k, "last_sell_slot": slot[k], "slots": (slot[win[0]], slot[k]), "live5": live5,
                      "participants": {a: t[k] for a in sellers if a not in MAYHEM}}
    if active is not None:
        out.append(active)
    return out


def bursts_curve(day):
    OUT.mkdir(parents=True, exist_ok=True)
    base = ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, fee_bps, creator_fee_bps, %s "
                     "FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
    mint, coin, nb, ncoins = None, None, 0, 0
    with open(OUT / ("bursts-live3-curve-%s.jsonl" % day), "w") as fh:
        def flush(c):
            nonlocal nb, ncoins
            if c is None or len(c.t) < 8 or not (c.first_ok and c.quote_ok):
                return
            ncoins += 1
            res = [0.0] + [max(q - 30e9, 0.0) for q in c.q[:-1]]          # real quote reserve BEFORE each swap (post of the previous one)
            pp = [30e9 / 1.073e15] + [c.q[k] / c.b[k] for k in range(len(c.q) - 1)]      # reserve-ratio price BEFORE each swap
            po = [c.q[k] / c.b[k] for k in range(len(c.q))]                               # ... and AFTER each swap
            for b in detect(c.t, c.slot, c.buy, c.qa, c.actor, res, pp, po):
                fh.write(json.dumps({"venue": "curve", "day": day, "mint": c.mint, "detect_t": b["detect_t"], "slots": b["slots"], "live5": b["live5"], "participants": b["participants"]}) + "\n"); nb += 1
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
            coin.add(r)
        flush(coin)
    print(json.dumps({"stage": "bursts-curve", "day": day, "coins": ncoins, "bursts": nb}))


def bursts_ps(lo, hi):
    OUT.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "desk" / "pumpswap" / "tape.db"), uri=True, isolation_level=None, timeout=60)
    pools = [r[0] for r in db.execute("select distinct pool from ps_pool_hours where last_received >= ? and first_received < ?", (float(lo), float(hi)))]
    nb = ns = 0
    with open(OUT / "bursts-live3-ps.jsonl", "w") as fh:
        for p in pools:
            from sniper.services import memecoin_desk_ps_tape as PT
            rows = PT.window_rows(db, "slot,tx_index,ordinal,is_buy,actor,quote_atomic,pre_quote_reserve_atomic,"
                                  "received_at,quote_mint,pre_base_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic",
                                  p, float(lo), float(hi), order="slot,tx_index,ordinal")
            if len(rows) < 8:
                continue
            if any(r[8] != "So11111111111111111111111111111111111111112" for r in rows[:3]):
                continue
            t, slot, buy, qa, actor, res, pp, po = [], [], [], [], [], [], [], []
            for sl, tx, o, b, a, q, pre, rc, qmint, preb, postq, postb in rows:
                try:
                    qv = float(q); rv = float(pre) if pre is not None else 0.0; pb = float(preb) if preb else 0.0
                except (TypeError, ValueError):
                    continue
                t.append(rc); slot.append(sl); buy.append(bool(b)); qa.append(qv); actor.append(a); res.append(rv); pp.append(rv / pb if pb > 0 and rv > 0 else None)
                try:
                    po.append(float(postq) / float(postb) if postq and postb and float(postb) > 0 else None)
                except (TypeError, ValueError):
                    po.append(None)
            ns += len(t)
            for b in detect(t, slot, buy, qa, actor, res, pp, po):
                fh.write(json.dumps({"venue": "ps", "pool": p, "detect_t": b["detect_t"], "slots": b["slots"], "live5": b["live5"], "participants": b["participants"]}) + "\n"); nb += 1
    print(json.dumps({"stage": "bursts-ps", "pools": len(pools), "swaps": ns, "bursts": nb}))


def _live_fold(rows, excluded=()):
    """rows of ONE pool/coin (normalized, any order) -> final episodes via ONE live BurstDetector fed in known_at order (the shipped module, not a re-implementation)."""
    from sniper.services.memecoin_desk_dump_group import BurstDetector
    det, eps = BurstDetector(excluded=excluded), {}
    for r in sorted(rows, key=lambda x: (x["known_at"], x["slot"], x.get("tx_index", -1) or -1, x["ordinal"], x["signature"])):
        ev = det.feed(r)
        if ev is not None:
            eps[ev["id"]] = ev
    return eps.values()


def _live_write(fh, venue, key, eps, day=None):
    n = 0
    for e in eps:
        fh.write(json.dumps({"venue": venue, "day": day, "pool": e["pool"], "mint": e["mint"], "detect_t": e["detected_at"], "slots": (e["first_slot"], e["last_slot"]),
                             "live5": True, "same_slot": e["same_slot_dump"], "drop_pct": e["drop_pct"],
                             "participants": {a: p["known_at"] for a, p in e["participants"].items()}}) + "\n"); n += 1
    return n


def _live_meta(stage, extra):
    import hashlib
    from sniper.services import memecoin_desk_dump_group as G, memecoin_desk_dump_group_source as S
    src = hashlib.sha256(b"".join(open(m.__file__, "rb").read() for m in (G, S))).hexdigest()
    meta = dict(stage=stage, version=G.VERSION, policy_hash=G.POLICY_HASH, source_sha256=src, **extra)
    print(json.dumps(meta))
    with open(OUT / ("bursts-live-%s.meta.json" % stage), "w") as f:
        json.dump(meta, f)


def bursts_live_curve(day):
    sys.path.insert(0, str(ROOT))
    from sniper.services import memecoin_desk_dump_group_source as S
    OUT.mkdir(parents=True, exist_ok=True)
    base = ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    rq = "real_quote" if "real_quote" in cols else "NULL"
    cc = S.CURVE_COLS if "tx_index" in cols else S.CURVE_COLS.replace("tx_index", "NULL")
    cur = db.execute("SELECT %s, %s, %s FROM swaps ORDER BY curve, slot, received_at, ordinal, signature" % (cc, qm, rq))
    norm, pool, rows, nb, npool = S.CurveNormalizer(), None, [], 0, 0
    with open(OUT / ("bursts-live-curve-%s.jsonl" % day), "w") as fh:
        def flush():
            nonlocal nb, npool
            if rows:
                npool += 1; nb += _live_write(fh, "curve", pool, _live_fold(rows), day)
        for r in cur:
            if r[4] != pool:
                flush(); pool, rows = r[4], []
            row, _ = norm.normalize(r)
            if row is not None:
                rows.append(row)
        flush()
    _live_meta("curve-" + day, dict(day=day, pools=npool, bursts=nb, dropped=dict(norm.dropped)))


def bursts_live_ps(lo, hi):
    sys.path.insert(0, str(ROOT))
    from sniper.services import memecoin_desk_dump_group_source as S
    OUT.mkdir(parents=True, exist_ok=True)
    desk = str(ROOT / "data" / "desk")
    db = sqlite3.connect("file:%s/pumpswap/tape.db?mode=ro" % desk, uri=True, isolation_level=None, timeout=60)
    sel = "select %s, CASE WHEN is_buy=1 AND payload_json LIKE ? THEN payload_json END from swaps_ps where received_at >= ? and received_at < ? order by pool, received_at" % S.PS_COLS
    norm, pool, rows, nb, npool = S.PSNormalizer(S.load_pool_mints(desk)), None, [], 0, 0
    with open(OUT / "bursts-live-ps.jsonl", "w") as fh:
        def flush():
            nonlocal nb, npool
            if rows:
                npool += 1; nb += _live_write(fh, "ps", pool, _live_fold(rows))
        for r in db.execute(sel, (S.PS_PROOF_LIKE, float(lo), float(hi))):
            if r[0] != pool:
                flush(); pool, rows = r[0], []
            row, _, _ = norm.normalize(r)       # per-pool continuity state is keyed by pool, so pool-major order is safe
            if row is not None:
                rows.append(row)
        flush()
    _live_meta("ps", dict(lo=lo, hi=hi, pools=npool, bursts=nb, dropped=dict(norm.dropped)))


def load_cache():
    ent = defaultdict(list)            # actor -> sorted [t_known]
    nb = 0
    for f in glob.glob(str(OUT / "bursts-live-*.jsonl")):
        for line in open(f):
            b = json.loads(line); nb += 1
            for a, tk in b["participants"].items():
                ent[a].append((tk, b["detect_t"]))
    for a in ent:
        ent[a].sort()
    return ent, nb


def known(ent, a, t, win=CACHE_S):
    """True if actor a had a cache entry known in [t - win, t) (strictly before t)."""
    lst = ent.get(a)
    if not lst:
        return False
    i = bisect.bisect_left(lst, (t, -1)) - 1
    return i >= 0 and lst[i][0] >= t - win


def frequent(ent, a, t, k=5, win=CACHE_S):
    lst = ent.get(a, [])
    return len({d for tk, d in lst if t - win <= tk < t}) >= k


def measures(c, i, t, ent, t_first_cache):
    lo = bisect.bisect_right(c.t, t - 60)
    hi = bisect.bisect_right(c.t, t)
    buyers = {c.actor[k] for k in range(lo, hi) if c.buy[k]}
    active = {c.actor[k] for k in range(lo, hi)}
    bs = sum(c.qa[k] for k in range(lo, hi) if c.buy[k]); ks = sum(c.qa[k] for k in range(lo, hi) if c.buy[k] and known(ent, c.actor[k], t))
    kb = [a for a in buyers if known(ent, a, t)]
    return {"buyers": len(buyers), "known_buyers": len(kb), "known_buyer_share": len(kb) / len(buyers) if buyers else None,
            "known_buyers_nonfreq": sum(1 for a in kb if not frequent(ent, a, t)),
            "active": len(active), "known_active_share": sum(1 for a in active if known(ent, a, t)) / len(active) if active else None,
            "known_buy_sol_share": ks / bs if bs else None}


def outcomes(c, i, t):
    p0 = E.price(c, i)
    j = bisect.bisect_right(c.t, t + LAT) - 1
    k5 = bisect.bisect_right(c.t, t + 300)
    mn = min([E.price(c, k) / p0 for k in range(i + 1, k5)] or [1.0]) - 1
    comp = any(c.rt[k] is not None and c.rt[k] <= 0 for k in range(i + 1, k5))
    R = None
    if c.fee[j] is not None and not comp:
        qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
        e = bisect.bisect_right(c.t, t + LAT + 300) - 1
        if e >= j and c.fee[e] is not None:
            R = E.value(c, e, tok, qn) - 1 - COST
    return {"min_ret_300": mn, "crash50": mn <= -0.5, "completed": comp, "R300": R}


def moments(day):
    ent, nb = load_cache()
    allt = [tk for lst in ent.values() for tk, _ in lst]
    t_first = min(allt) if allt else None
    base = ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    silent, span = E.silence_intervals(db)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, fee_bps, creator_fee_bps, %s "
                     "FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
    mint, coin, nrow = None, None, 0
    with open(OUT / ("moments-%s.jsonl" % day), "w") as fh:
        def flush(c):
            nonlocal nrow
            if c is None or len(c.t) < 8 or not (c.first_ok and c.quote_ok):
                return
            t = c.t[0] + 10.0
            while t <= c.t[-1] and t - c.t[0] <= 1800:
                i = bisect.bisect_right(c.t, t) - 1
                m = E.mcap_usd(c, i)
                if m > 42800 or (c.rt[i] is not None and c.rt[i] <= 0):
                    break
                if m >= 10000 and i + 1 >= 8 and t + 301.5 + 2 <= span[1] and not E.overlaps(silent, t, t + 303.5):
                    row = {"day": day, "mint": c.mint, "t": t, "mcap": m, "age": t - c.t[0], "cache_hist_s": (t - t_first) if t_first else None}
                    row.update(measures(c, i, t, ent, t_first)); row.update(outcomes(c, i, t))
                    fh.write(json.dumps(row) + "\n"); nrow += 1
                t += SAMPLE_S
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
            coin.add(r)
        flush(coin)
    print(json.dumps({"stage": "moments", "day": day, "rows": nrow, "cache_bursts": nb}))


if __name__ == "__main__":
    st = sys.argv[1]
    {"bursts-curve": lambda: bursts_curve(sys.argv[2]), "bursts-ps": lambda: bursts_ps(sys.argv[2], sys.argv[3]), "bursts-live-curve": lambda: bursts_live_curve(sys.argv[2]), "bursts-live-ps": lambda: bursts_live_ps(sys.argv[2], sys.argv[3]), "moments": lambda: moments(sys.argv[2])}[st]()
