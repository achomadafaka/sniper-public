#!/usr/bin/env python3
"""Tape reader v2 confirmatory (prereg 2026-10-23-tape-reader-direction-payoff-preregistration.md + parent + Amendment 1).
  fit:     tape_reader_confirm.py fit      -> fits P / HIT / DIR / M1 on T, calibrates on V, writes artifacts + lock.json
  holdout: tape_reader_confirm.py holdout H_FEATURES.npz H_SAMPLES.npz   -> refuses outside lock; ONE look; writes holdout_read.json first
Inputs: CONF/features-DAY.npz (Lambda tapefeat-v2) joined on (mint, t) to CONF/samples-DAY.npz (labels from tape_reader_samples.py)."""
import bisect, datetime, hashlib, json, os, sqlite3, subprocess, sys, time
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from tape_reader_samples import FEATS
from tape_reader_train import fit_one, grade, slope, boot_ci
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parents[2]
CONF = ROOT / "data" / "research" / "tape-reader"
CONFS = CONF / "confirm"
OUT = ROOT / "data" / "models" / "tape_reader" / "v2"
LOCK = ROOT / "docs" / "audits" / "frozen" / "2026-10-26-tape-reader-v2-lock.json"
READ = OUT / "holdout_read.json"
TRAIN_DAYS, VAL_DAY, HOLD_DAY = ("2026-10-21", "2026-10-22"), "2026-10-23", "2026-10-24"
BANDS, AGES = (5000, 10000, 20000, 35000), (30, 120, 300)
# claims: name -> (target index in samples y, tp, sl, hold)
SCALP, M1 = (0.20, 0.10, 300.0), (0.50, 0.20, 600.0)
TOP, GAP, MAXOPEN, COOL, LAT, COST = 0.02, 120.0, 5, 900.0, 1.5, 0.025


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def load_day(day, feat_path=None, samp_path=None):
    f = np.load(feat_path or CONF / ("features-%s.npz" % day), allow_pickle=False)
    s = np.load(samp_path or CONFS / ("samples-%s.npz" % day), allow_pickle=False)
    kf = np.char.add(f["mint"].astype(str), np.round(f["t"] * 1000).astype(np.int64).astype(str))
    ks = np.char.add(s["mint"].astype(str), np.round(s["t"] * 1000).astype(np.int64).astype(str))
    idx = {k: i for i, k in enumerate(kf)}
    pairs = [(idx[k], j) for j, k in enumerate(ks) if k in idx]
    fi, sj = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
    d = {"X": f["X"][fi], "mint": s["mint"][sj], "t": s["t"][sj], "reg": s["reg"][sj], "mcap": s["mcap"][sj], "y": s["y"][sj], "y3": s["y3"][sj]}
    d["stat"] = {"feat_rows": int(len(kf)), "samp_rows": int(len(ks)), "joined": int(len(sj))}
    return d


def cat(ds):
    return {k: np.concatenate([d[k] for d in ds]) for k in ("X", "mint", "t", "reg", "mcap", "y", "y3")}


def cells(d):
    band = np.digitize(np.exp(d["X"][:, FEATS.index("log_mcap")]), BANDS)
    tier = np.digitize(d["X"][:, FEATS.index("age_s")], AGES)
    return band, tier, d["reg"]


def clim_fit(d, y, ok):
    """Amendment 1 point 5 hierarchy: cell (band,tier,reg) >=200 rows, else (band,tier), else band, else global."""
    b, a, r = cells(d)
    yy = y[ok].astype(float)
    levels = [(b[ok] * 100 + a[ok] * 10 + r[ok]), (b[ok] * 10 + a[ok]), b[ok]]
    tabs = []
    for lv in levels:
        t = {}
        for k in np.unique(lv):
            m = lv == k
            if m.sum() >= 200:
                t[int(k)] = float(yy[m].mean())
        tabs.append(t)
    return {"tabs": tabs, "glob": float(yy.mean())}


def clim_pred(c, d):
    b, a, r = cells(d)
    keys = (b * 100 + a * 10 + r, b * 10 + a, b)
    out = np.full(len(b), c["glob"])
    for lvl in (2, 1, 0):
        tab = c["tabs"][lvl]
        if tab:
            for k, v in tab.items():
                out[keys[lvl] == k] = v
    return out


def rule_fit(d, y, ok, cond):
    f = cond(d)
    return (float(y[ok & f].mean()) if (ok & f).any() else 0.0, float(y[ok & ~f].mean()))


def rule_pred(d, tab, cond):
    return np.where(cond(d), tab[0], tab[1])


def col(d, n):
    return np.nan_to_num(d["X"][:, FEATS.index(n)], nan=0.0)


RULES = {"crowd": lambda d: col(d, "w60max300") >= 100, "growth": lambda d: col(d, "growth") >= 1.5}
DRULES = {"ret60": lambda d: col(d, "ret_60") > 0, "bshare60": lambda d: col(d, "bshare_60") >= 0.5}


def calib(pv, yv, okv, mint):
    """cross-fit isotonic on halves of V coins (for grading); full fit for the artifact."""
    u = np.unique(mint); rng = np.random.default_rng(1)
    half = set(rng.choice(u, len(u) // 2, replace=False))
    hm = np.array([m in half for m in mint])
    pc = np.zeros_like(pv)
    for tr in (hm, ~hm):
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999).fit(pv[okv & tr], yv[okv & tr])
        pc[~tr] = iso.predict(pv[~tr])
    full = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999).fit(pv[okv], yv[okv])
    return pc, full


# ---- decision test (price/executable first touch on exact sell value) ----
def load_coin(db, mint):
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    c = E.Coin(mint)
    for r in db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, "
                        "fee_bps, creator_fee_bps, %s FROM swaps WHERE mint=? ORDER BY slot, %s, received_at, ordinal" % (qm, txi), (mint,)):
        c.add(r)
    return c


def trade(c, t, adverse, tp, sl, hold):
    tf = t + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    if adverse:
        best, end = j, bisect.bisect_right(c.slot, c.slot[j] + 2)
        for k in range(j + 1, end):
            if E.price(c, k) > E.price(c, best):
                best = k
        j = best
    if c.fee[j] is None:
        return None
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    trig = tf + hold
    for k in range(j + 1, bisect.bisect_right(c.t, tf + hold)):
        if c.fee[k] is None or (c.rt[k] is not None and c.rt[k] <= 0):
            return None
        v = E.value(c, k, tokens, qn)
        if v >= 1 + tp or v <= 1 - sl:
            trig = c.t[k]; break
    e = bisect.bisect_right(c.t, trig + LAT) - 1
    if e < j or c.fee[e] is None or (c.rt[e] is not None and c.rt[e] <= 0):
        return None
    return E.value(c, e, tokens, qn) - 1.0 - COST, trig + LAT


def ci(vals, coins):
    rng = np.random.default_rng(0)
    u, inv = np.unique(coins, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    r = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(u), len(u)) for _ in range(1000))]
    return float(np.percentile(r, 5)), float(np.percentile(r, 2.5))


def decision(day, d, p, thr, tp, sl, hold):
    elig = np.where(p >= thr)[0]
    by = {}
    for r in elig:
        by.setdefault(str(d["mint"][r]), []).append(r)
    entries = []
    for m, rows in by.items():
        rows.sort(key=lambda r: d["t"][r]); last = -1e18
        for r in rows:
            if d["t"][r] - last >= GAP:
                entries.append((float(d["t"][r]), m))
            last = d["t"][r]
    entries.sort()
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (ROOT / "data" / "desk" / "swaps" / ("%s.db" % day), imm), uri=True, isolation_level=None)
    cache, res = {}, {"threshold": thr, "eligible_rows": int(len(elig)), "episodes": len(entries)}
    for adverse in (False, True):
        openu, cool, rec, skipped = [], {}, [], 0
        for t, m in entries:
            openu = [x for x in openu if x > t]
            if len(openu) >= MAXOPEN or cool.get(m, -1) > t:
                skipped += 1; continue
            if m not in cache:
                cache[m] = load_coin(db, m)
            out = trade(cache[m], t, adverse, tp, sl, hold)
            if out is None:
                skipped += 1; continue
            rec.append((out[0], m)); openu.append(out[1]); cool[m] = out[1] + COOL
        R = np.array([r[0] for r in rec]); coins = np.array([r[1] for r in rec])
        lo95, lo975 = ci(R, coins) if len(R) else (float("nan"),) * 2
        res["adverse" if adverse else "optimistic"] = {"trades": int(len(R)), "coins": int(len(set(coins))), "skipped": skipped,
                                                       "mean_R": float(R.mean()) if len(R) else None, "lo95": lo95, "lo975": lo975,
                                                       "win": float((R > 0).mean()) if len(R) else None}
    return res


def fit_models(T, V):
    """returns artifacts dict + V report."""
    art, rep = {}, {}
    # P and M1: price label on resolved rows
    for name, k in (("P", 1), ("M1", 4)):
        okT, okV = T["y"][:, k] >= 0, V["y"][:, k] >= 0
        model, info = fit_one(T["X"][okT], T["y"][okT, k], T["mint"][okT], k)
        pv = model.predict_proba(V["X"])[:, 1]
        pc, iso = calib(pv, V["y"][:, k], okV, V["mint"])
        c = clim_fit(T, T["y"][:, k], okT)
        rt = {rn: rule_fit(T, T["y"][:, k], okT, cond) for rn, cond in RULES.items()}
        rules = {rn: rule_pred(V, rt[rn], RULES[rn]) for rn in RULES}
        rep[name] = {"fit": info, "V": grade("V:" + name, pc, V["y"][:, k], V, okV, k, clim_pred(c, V), rules)}
        art[name] = {"model": model, "iso": iso, "clim": c, "rules": rt, "k": k}
    # HIT: resolved rows (y3 in 0,1,2); hit = y3 in (0,1)
    okT, okV = np.isin(T["y3"], (0, 1, 2)), np.isin(V["y3"], (0, 1, 2))
    hT, hV = (T["y3"] != 2).astype(int), (V["y3"] != 2).astype(int)
    model, info = fit_one(T["X"][okT], hT[okT], T["mint"][okT], 1)
    pv = model.predict_proba(V["X"])[:, 1]
    pc, iso = calib(pv, hV, okV, V["mint"])
    c = clim_fit(T, hT, okT)
    rep["HIT"] = {"fit": info, "V": grade("V:HIT", pc, hV, V, okV, 1, clim_pred(c, V), {})}
    art["HIT"] = {"model": model, "iso": iso, "clim": c}
    # DIR: P(UP | hit) on hit rows only
    okT, okV = np.isin(T["y3"], (0, 1)), np.isin(V["y3"], (0, 1))
    uT, uV = (T["y3"] == 1).astype(int), (V["y3"] == 1).astype(int)
    model, info = fit_one(T["X"][okT], uT[okT], T["mint"][okT], 1)
    pv = model.predict_proba(V["X"])[:, 1]
    pc, iso = calib(pv, uV, okV, V["mint"])
    c = clim_fit(T, uT, okT)
    rt = {rn: rule_fit(T, uT, okT, cond) for rn, cond in DRULES.items()}
    rules = {rn: rule_pred(V, rt[rn], DRULES[rn]) for rn in DRULES}
    rep["DIR"] = {"fit": info, "V": grade("V:DIR", pc, uV, V, okV, 1, clim_pred(c, V), rules)}
    art["DIR"] = {"model": model, "iso": iso, "clim": c, "rules": rt}
    return art, rep


def score(a, d):
    return a["iso"].predict(a["model"].predict_proba(d["X"])[:, 1])


def grade_h(art, H, hday):
    out = {}
    for name, k in (("P", 1), ("M1", 4)):
        a = art[name]; ok = H["y"][:, k] >= 0; p = score(a, H)
        rules = {rn: rule_pred(H, a["rules"][rn], RULES[rn]) for rn in RULES}
        out[name] = {"grade": grade("H:" + name, p, H["y"][:, k], H, ok, k, clim_pred(a["clim"], H), rules)}
        out[name]["decision"] = decision(hday, H, p, a["thr"], *(SCALP if name == "P" else M1))
    a = art["HIT"]; ok = np.isin(H["y3"], (0, 1, 2)); h = (H["y3"] != 2).astype(int)
    out["HIT"] = {"grade": grade("H:HIT", score(a, H), h, H, ok, 1, clim_pred(a["clim"], H), {})}
    a = art["DIR"]; ok = np.isin(H["y3"], (0, 1)); u = (H["y3"] == 1).astype(int)
    rules = {rn: rule_pred(H, a["rules"][rn], DRULES[rn]) for rn in DRULES}
    out["DIR"] = {"grade": grade("H:DIR", score(a, H), u, H, ok, 1, clim_pred(a["clim"], H), rules), "hit_rows": int(ok.sum()), "coins": int(len(set(H["mint"][ok])))}
    return out


def fit():
    Ts = [load_day(d) for d in TRAIN_DAYS]
    V = load_day(VAL_DAY)
    T = cat(Ts)
    # a coin belongs to the day of its first swap: drop V/T rows whose coin appeared on an earlier day
    seen = set()
    for d in (Ts[0], Ts[1], V):
        m = np.array([x not in seen for x in d["mint"]])
        seen |= set(d["mint"])
        for k in ("X", "mint", "t", "reg", "mcap", "y", "y3"):
            d[k] = d[k][m]
    T, V = cat(Ts), V
    print({"T_rows": len(T["mint"]), "V_rows": len(V["mint"]), "join": [d["stat"] for d in Ts] + [V["stat"]]})
    art, rep = fit_models(T, V)
    # thresholds = top 2% of calibrated V scores (scores only)
    for name in ("P", "M1"):
        art[name]["thr"] = float(np.quantile(score(art[name], V), 1 - TOP))
    # V decision tests (M1 enters H only if point mean R > 0; P and the scalp are reported)
    rep["decision_V"] = {"P": decision(VAL_DAY, V, score(art["P"], V), art["P"]["thr"], *SCALP),
                         "M1": decision(VAL_DAY, V, score(art["M1"], V), art["M1"]["thr"], *M1)}
    OUT.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for n, a in art.items():
        p = OUT / ("%s.joblib" % n); joblib.dump(a, p); hashes[n] = sha(p)
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    meta = {"git": git, "feats": FEATS, "V_report": rep, "T_rows": int(len(T["mint"])), "V_rows": int(len(V["mint"]))}
    json.dump(meta, open(OUT / "meta.json", "w"), default=float, indent=1)
    dir_dec = rep["DIR"]["V"]["dBS_lo95"]
    entry = {"D1": bool(dir_dec > 0), "M1": bool((rep["decision_V"]["M1"]["optimistic"]["mean_R"] or -1) > 0), "P": True}
    lock = {"artifacts_sha256": hashes, "meta_sha256": sha(OUT / "meta.json"),
            "features_json_sha256": sha(ROOT / "docs/audits/frozen/2026-10-26-tape-reader-v2-features.json"),
            "samples_sha256": {d: sha(CONFS / ("samples-%s.npz" % d)) for d in TRAIN_DAYS + (VAL_DAY,)},
            "features_sha256": {d: sha(CONF / ("features-%s.npz" % d)) for d in TRAIN_DAYS + (VAL_DAY,)},
            "thresholds": {n: art[n]["thr"] for n in ("P", "M1")}, "entry_to_H": entry, "holdout_day": HOLD_DAY, "script_git": git}
    json.dump(lock, open(LOCK, "w"), indent=1)
    print(json.dumps({"entry_to_H": entry, "V_decision": rep["decision_V"], "V_DIR_dBS_lo95": dir_dec}, default=float))


def holdout(fpath, spath):
    global READ, HOLD_DAY
    lock = json.load(open(LOCK))
    now = datetime.datetime.now(datetime.timezone.utc)
    if os.environ.get("TR_DRY"):          # dry run of the code path on a day that is already in the fit; never a look
        READ, HOLD_DAY = OUT / "dry_read.json", os.environ["TR_DRY"]
    else:
        assert now >= datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc), "before 2026-10-25 00:30Z"
        assert not READ.exists(), "holdout_read.json exists: a second look is a new holdout day"
    for n, h in lock["artifacts_sha256"].items():
        assert sha(OUT / ("%s.joblib" % n)) == h, "artifact hash mismatch " + n
    assert sha(OUT / "meta.json") == lock["meta_sha256"] and sha(ROOT / "docs/audits/frozen/2026-10-26-tape-reader-v2-features.json") == lock["features_json_sha256"]
    for grp in ("samples_sha256", "features_sha256"):
        base = CONFS if grp == "samples_sha256" else CONF
        for d, h in lock[grp].items():
            assert sha(base / ("%s-%s.npz" % (grp.split("_")[0], d))) == h, "input hash mismatch " + d
    H = load_day(HOLD_DAY, fpath, spath)
    prior = set()                          # a coin belongs to the day of its first swap: drop midnight spanners already seen
    for d in lock["samples_sha256"]:
        if d != HOLD_DAY:
            prior |= set(np.load(CONFS / ("samples-%s.npz" % d), allow_pickle=False)["mint"])
    keep = np.array([m not in prior for m in H["mint"]])
    H = {k: (v[keep] if k != "stat" else v) for k, v in H.items()}
    H["stat"]["dropped_prior_day_coins_rows"] = int((~keep).sum())
    art = {n: joblib.load(OUT / ("%s.joblib" % n)) for n in lock["artifacts_sha256"]}
    for n, v in lock["thresholds"].items():
        art[n]["thr"] = v
    manifest = {"time": now.isoformat(), "features_sha256": sha(fpath), "samples_sha256": sha(spath), "join": H["stat"], "entry_to_H": lock["entry_to_H"]}
    READ.parent.mkdir(parents=True, exist_ok=True)
    json.dump(manifest, open(READ, "w"), indent=1)
    res = grade_h(art, H, HOLD_DAY)
    manifest["results"] = res
    json.dump(manifest, open(READ, "w"), default=float, indent=1)
    print(json.dumps(res, default=float))


if __name__ == "__main__":
    {"fit": lambda: fit(), "holdout": lambda: holdout(*sys.argv[2:4])}[sys.argv[1]]()
