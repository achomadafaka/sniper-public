#!/usr/bin/env python3
"""Source-entry setups, Arm B grading (Amendment 7 + 5 + 1 of docs/audits/2026-10-26-source-entry-setups-preregistration.md).
Builds the 32-feature rows and labels for the locked manifest, scores them with the UNCHANGED armB32 model, grades S1/S2/S3/S4 once.
    python3 scripts/research/source_entry_armb_grade.py check    # features + censor counts only, no score, no outcome sign, no marker
    python3 scripts/research/source_entry_armb_grade.py grade    # ONE pass: writes armB-graded.marker first, refuses a second run
Features use swaps with block time < floor(tc) (t_feat = floor(tc)-1). Label reference and entry: tc+5 s (t_lab = floor(tc)+5), executable fill +1.5 s, scalp TP +20% / SL -10% / 300 s, 2.5 pp cost."""
import bisect, hashlib, json, math, sys
from pathlib import Path

import joblib
import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import desk_tape_events as E
import desk_regime_play as RG
import tape_reader_train as TRN
from tape_reader_samples import FEATS, features, labels, REGS

ROOT = HERE.parents[1]
SE = ROOT / "data" / "research" / "source-entry"
LOCK = json.load(open(SE / "lock.json"))
MARKER = SE / "armB-graded.marker"
MARGIN, NBOOT = 5.0, 5000
TP, SL, HOLD, LAT, COST = 0.20, 0.10, 300.0, 1.5, 0.025      # copied from tape_reader_decide.py (that module runs main() on import)


def trade(c, t, adverse):
    """scalp on the exact sell value; identical to tape_reader_decide.trade"""
    i = bisect.bisect_right(c.t, t) - 1
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
    reason, trig = "time", tf + HOLD
    for k in range(j + 1, bisect.bisect_right(c.t, tf + HOLD)):
        if c.fee[k] is None or (c.rt[k] is not None and c.rt[k] <= 0):
            return None
        v = E.value(c, k, tokens, qn)
        if v >= 1 + TP:
            reason, trig = "tp", c.t[k]; break
        if v <= 1 - SL:
            reason, trig = "sl", c.t[k]; break
    e = bisect.bisect_right(c.t, trig + LAT) - 1
    if e < j or c.fee[e] is None or (c.rt[e] is not None and c.rt[e] <= 0):
        return None
    return E.value(c, e, tokens, qn) - 1.0 - COST, reason, trig + LAT


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def coin_of(r):
    c = E.Coin(r["mint"])
    seen = set()
    sw = sorted(r["swaps"] + r["swaps_fwd"], key=lambda e: (e["bt"], e["slot"], e["idx"]))
    for e in sw:
        k = (e["sig"], e["idx"])
        if k in seen:
            continue
        seen.add(k)
        c.add((None, e["bt"], e["slot"], e["idx"], bool(e["buy"]), e["qa"], e["ta"], e["actor"], e["vq"], e["vb"], e["rt"], e["fee_bps"], e["cfee_bps"], None))
    return c


def build(check):
    assert sha(SE / "tierw-fwd-2026-10-26.jsonl") == LOCK["manifest"]["inputs"]["tierw_fwd_sha256"], "input changed since lock"
    man = {m[0]: m for m in json.load(open(SE / "manifest-armB.json"))}
    X, Y, YE, T, REG, MINT, SEL, MC, R0, R1, YM0 = [], [], [], [], [], [], [], [], [], [], []
    skipped = {}
    for line in open(SE / "tierw-fwd-2026-10-26.jsonl"):
        r = json.loads(line)
        if r["event_id"] not in man:
            continue
        c = coin_of(r)
        tc_i = int(r["tc"])
        tf, tl = float(tc_i - 1), float(tc_i) + MARGIN
        i = bisect.bisect_right(c.t, tf) - 1
        il = bisect.bisect_right(c.t, tl) - 1
        if i < 7 or il < 0:
            skipped["lt8_swaps_before_tc"] = skipped.get("lt8_swaps_before_tc", 0) + 1
            continue
        x = features(c, i, tf)
        y, ye, _ = labels(c, il, tl, [], (0.0, 1e18))
        reg = "YOUNG" if tf - c.t[0] < 300 else RG.regime(c, tf)[0]
        X.append(x); Y.append(y); YE.append(ye); T.append(tf); REG.append(REGS.get(reg, 2)); MINT.append(r["mint"])
        SEL.append(bool(man[r["event_id"]][2])); MC.append(E.mcap_usd(c, i))
        if not check:
            R0.append(trade(c, tl, False)); R1.append(trade(c, tl, True))
    d = dict(X=np.array(X, np.float64), y=np.array(Y, np.int8), ye=np.array(YE, np.int8), t=np.array(T), reg=np.array(REG, np.int8),
             mcap=np.array(MC), mint=np.array(MINT), sel=np.array(SEL))
    return d, R0, R1, skipped


def boot(fn, mints, nb=NBOOT):
    u, inv = np.unique(mints, return_inverse=True)
    rng = np.random.default_rng(0)
    vals = []
    for _ in range(nb):
        pick = rng.integers(0, len(u), len(u))
        w = np.bincount(pick, minlength=len(u))[inv]
        v = fn(w)
        if v == v:
            vals.append(v)
    return float(np.percentile(vals, 5)) if vals else None


def wmean(v, w):
    return float((v * w).sum() / w.sum()) if w.sum() > 0 else float("nan")


def check_mode():
    d, _, _, skipped = build(True)
    nanr = np.isnan(d["X"]).mean(axis=0)
    print(json.dumps({"rows": len(d["y"]), "coins": len(np.unique(d["mint"])), "selected": int(d["sel"].sum()), "skipped": skipped,
                      "censored_primary": int((d["y"][:, 1] == -2).sum()), "ambiguous_primary": int((d["y"][:, 1] == -1).sum()),
                      "feature_nan_rate_max": float(np.nanmax(nanr)), "nan_cols": [FEATS[k] for k in range(len(FEATS)) if nanr[k] > 0.2]}))


def grade_mode():
    if MARKER.exists():
        raise SystemExit("armB already graded (marker exists): one pass only")
    MARKER.write_text("started\n")
    art = joblib.load(ROOT / LOCK["model"]["file"])
    assert sha(ROOT / LOCK["model"]["file"]) == LOCK["model"]["sha256"], "model changed since lock"
    d, R0, R1, skipped = build(False)
    C = [FEATS.index(f) for f in art["feats"]]
    p = art["iso"].predict(art["model"].predict_proba(d["X"][:, C])[:, 1])
    k = 1
    ok = d["y"][:, k] >= 0
    base_p = TRN.clim_pred(art["clim"], art["clim_global"], d)
    rules = {rn: TRN.apply_rule(d, rn, art["rules"]) for rn in ("crowd", "growth")}
    rep = {"lock_model_sha256": LOCK["model"]["sha256"], "skipped": skipped, "margin_s": MARGIN}
    rep["S1_all"] = TRN.grade("armB all", p, d["y"][:, k], d, ok, k, base_p, rules)
    sm = d["sel"]
    rep["S1_selected_stratum"] = TRN.grade("armB selected", p[sm], d["y"][sm, k], {kk: (v[sm] if hasattr(v, "__len__") and len(v) == len(sm) else v) for kk, v in d.items()},
                                           ok[sm], k, base_p[sm], {rn: v[sm] for rn, v in rules.items()}) if (ok & sm).sum() > 20 else None
    # S2: scalp rule on selected-by-score rows, p* = 0.5
    PSTAR = LOCK["thresholds"]["p_star"]
    for name, RR in (("optimistic", R0), ("adverse", R1)):
        rv = np.array([np.nan if x is None else x[0] for x in RR])
        okr = ~np.isnan(rv)
        enter = (p >= PSTAR) & okr
        res = {"rows_with_R": int(okr.sum()), "entered": int(enter.sum()), "coins": int(len(np.unique(d["mint"][enter]))) if enter.any() else 0}
        if enter.any():
            res["mean_R"] = float(rv[enter].mean())
            res["lo95_R"] = boot(lambda w, e=enter, r=rv: wmean(np.where(e, r, 0), w * e) if (w * e).sum() > 0 else float("nan"), d["mint"])
            un = okr & ~enter
            res["mean_R_unselected_signal"] = float(rv[un].mean()) if un.any() else None
            res["lo95_diff_selected_minus_unselected"] = boot(lambda w, e=enter, u=un, r=rv: (wmean(np.where(e, r, 0), w * e) - wmean(np.where(u, r, 0), w * u))
                                                              if (w * e).sum() > 0 and (w * u).sum() > 0 else float("nan"), d["mint"]) if un.any() else None
        rep["S2_" + name] = res
    # S3: share of primary winners with score >= p*, and control-free false-positive proxy = share of non-winners above p*
    w = ok & (d["y"][:, k] == 1)
    rep["S3"] = {"winners": int(w.sum()), "share_above_pstar": float((p[w] >= PSTAR).mean()) if w.any() else None,
                 "nonwinners_above_pstar": float((p[ok & ~w] >= PSTAR).mean()) if (ok & ~w).any() else None}
    # S4: selected wallets vs rest, primary label and exec label, matched by band x age tier strata (stratum weights = selected counts)
    st = TRN.strata(d)
    y1 = (d["y"][:, k] == 1).astype(float)
    def s4(w_, sel, yv, okm):
        tot, acc = 0.0, 0.0
        for s in np.unique(st[sel & okm]):
            a, b = sel & okm & (st == s), ~sel & okm & (st == s)
            if (w_ * b).sum() == 0 or (w_ * a).sum() == 0:
                continue
            wa = (w_ * a).sum()
            acc += wa * (wmean(yv, w_ * a) - wmean(yv, w_ * b)); tot += wa
        return acc / tot if tot > 0 else float("nan")
    rep["S4_primary_label"] = {"point": s4(np.ones(len(y1)), sm, y1, ok), "lo95": boot(lambda w_: s4(w_, sm, y1, ok), d["mint"]),
                               "n_sel": int((sm & ok).sum()), "n_rest": int((~sm & ok).sum())}
    rv0 = np.array([np.nan if x is None else x[0] for x in R0]); okr0 = ~np.isnan(rv0)
    rep["S4_scalp_R_optimistic"] = {"point": s4(np.ones(len(y1)), sm, np.nan_to_num(rv0), okr0), "lo95": boot(lambda w_: s4(w_, sm, np.nan_to_num(rv0), okr0), d["mint"]),
                                    "n_sel": int((sm & okr0).sum()), "n_rest": int((~sm & okr0).sum())}
    ye1 = (d["ye"][:, k] == 1).astype(float); oke = d["ye"][:, k] >= 0
    rep["exec_label"] = {"win_rate": float(ye1[oke].mean()), "n": int(oke.sum()), "price_label_win_rate": float(y1[ok].mean()), "n_price": int(ok.sum())}
    out = SE / "armB-graded.json"
    json.dump(rep, open(out, "w"), indent=1, default=float)
    print("written", out)


if __name__ == "__main__":
    {"check": check_mode, "grade": grade_mode}[sys.argv[1]]()
