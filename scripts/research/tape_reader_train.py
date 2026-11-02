#!/usr/bin/env python3
"""Train + grade the tape reader (pre-reg docs/audits/2026-10-23-tape-reader-preregistration.md).
  train:  tape_reader_train.py train  T1.npz T2.npz -- V.npz   -> data/models/tape_reader/v1/ + validation report
  holdout: tape_reader_train.py holdout H.npz                   -> ONE look, fixed models/calibration/baselines
Features subset: env TR_FEATURES=comma list (the live tracker's list); default all."""
import json, os, subprocess, sys, time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from tape_reader_samples import TARGETS, FEATS

OUT = Path(os.environ["TR_OUT"]) if os.environ.get("TR_OUT") else Path(__file__).resolve().parents[2] / "data" / "models" / "tape_reader" / "v1"      # TR_OUT: write elsewhere (Arm B refit), never over v1
GRID = [(3, 15), (3, 31), (5, 15), (5, 31)]
BANDS = (5000, 10000, 20000, 35000)


def load(paths):
    parts = [np.load(p, allow_pickle=False) for p in paths]
    return {k: np.concatenate([p[k] for p in parts]) for k in ("X", "y", "ye", "t", "reg", "mcap", "mint")}


def cols(feats):
    return [FEATS.index(f) for f in feats]


def strata(d):
    band = np.digitize(np.exp(d["X"][:, FEATS.index("log_mcap")]), BANDS)
    age = d["X"][:, FEATS.index("age_s")]
    tier = np.digitize(age, (30, 120, 300))
    return band * 100 + tier * 10 + d["reg"]


def clim_table(d, k):
    ok = d["y"][:, k] >= 0
    s = strata(d)[ok]; y = d["y"][ok, k]
    tab = {}
    for key in np.unique(s):
        m = s == key
        if m.sum() >= 200:
            tab[int(key)] = float(y[m].mean())
    return tab, float(y.mean())


def clim_pred(tab, glob, d):
    return np.array([tab.get(int(s), glob) for s in strata(d)])


def brier(p, y):
    return float(np.mean((p - y) ** 2))


def boot_ci(vals, mints, q, draws=500):
    rng = np.random.default_rng(0)
    u, inv = np.unique(mints, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    res = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(u), len(u)) for _ in range(draws))]
    return float(np.percentile(res, q))


def slope(p, y):
    z = np.log(np.clip(p, 1e-4, 1 - 1e-4) / (1 - np.clip(p, 1e-4, 1 - 1e-4))).reshape(-1, 1)
    m = LogisticRegression(C=1e6, max_iter=500).fit(z, y)
    return float(m.coef_[0][0]), float(m.intercept_[0])


def grade(name, p, y, d, ok, k, base_p, rules):
    pk, yk, mk = p[ok], y[ok].astype(float), d["mint"][ok]
    bs, bb = (pk - yk) ** 2, (base_p[ok] - yk) ** 2
    out = {"target": TARGETS[k], "n": int(ok.sum()), "coins": int(len(np.unique(mk))), "pos": float(yk.mean()),
           "BS": float(bs.mean()), "BS_clim": float(bb.mean()), "BSS": float(1 - bs.mean() / bb.mean()),
           "dBS_lo95": boot_ci(bb - bs, mk, 5)}
    out["slope"], out["intercept"] = slope(pk, yk)
    st = strata(d)[ok]; aucs = []
    for key in np.unique(st):
        m = st == key
        if m.sum() >= 200 and 0 < yk[m].sum() < m.sum():
            aucs.append(roc_auc_score(yk[m], pk[m]))
    out["within_stratum_auc"] = float(np.mean(aucs)) if aucs else None
    out["AUC"] = float(roc_auc_score(yk, pk)) if 0 < yk.sum() < len(yk) else None
    for rn, rp in rules.items():
        out["dBS_vs_" + rn + "_lo95"] = boot_ci(((rp[ok] - yk) ** 2) - bs, mk, 5)
    bins = np.digitize(pk, np.linspace(0, 1, 11)[1:-1])
    out["reliability"] = [(int(b), int((bins == b).sum()), float(pk[bins == b].mean()), float(yk[bins == b].mean())) for b in range(10) if (bins == b).sum() >= 20]
    print(json.dumps(out, default=float))
    return out


def rule_tables(d, k):
    ok = d["y"][:, k] >= 0
    rules = {}
    for rn, col, cond in (("crowd", "w60max300", lambda v: v >= 100), ("growth", "growth", lambda v: v >= 1.5)):
        v = d["X"][:, FEATS.index(col)]
        f = np.where(np.isnan(v), False, cond(np.nan_to_num(v)))
        rules[rn] = (float(d["y"][ok & f, k].mean()) if (ok & f).any() else 0.0, float(d["y"][ok & ~f, k].mean()))
    return rules


def apply_rule(d, rn, tab):
    col, cond = {"crowd": ("w60max300", lambda v: v >= 100), "growth": ("growth", lambda v: v >= 1.5)}[rn]
    v = d["X"][:, FEATS.index(col)]
    f = np.where(np.isnan(v), False, cond(np.nan_to_num(v)))
    return np.where(f, tab[rn][0], tab[rn][1])


def fit_one(Xt, yt, mint, k):
    u = np.unique(mint)
    rng = np.random.default_rng(0)
    val = set(rng.choice(u, size=max(1, len(u) // 5), replace=False))
    vm = np.array([m in val for m in mint])
    best = None
    for depth, leaves in GRID:
        m = HistGradientBoostingClassifier(max_depth=depth, max_leaf_nodes=leaves, learning_rate=0.05, max_iter=400, min_samples_leaf=500,
                                           l2_regularization=1.0, early_stopping=False, random_state=0).fit(Xt[~vm], yt[~vm])
        losses = [log_loss(yt[vm], p[:, 1], labels=[0, 1]) for p in m.staged_predict_proba(Xt[vm])]
        it = int(np.argmin(losses)) + 1
        if best is None or losses[it - 1] < best[0]:
            best = (losses[it - 1], depth, leaves, it)
    _, depth, leaves, it = best
    final = HistGradientBoostingClassifier(max_depth=depth, max_leaf_nodes=leaves, learning_rate=0.05, max_iter=it, min_samples_leaf=500,
                                           l2_regularization=1.0, early_stopping=False, random_state=0).fit(Xt, yt)
    return final, {"depth": depth, "leaves": leaves, "iters": it, "grouped_val_logloss": best[0]}


def train(argv):
    k = argv.index("--")
    T, V = load(argv[:k]), load(argv[k + 1:])
    feats = os.environ.get("TR_FEATURES", ",".join(FEATS)).split(",")
    C = cols(feats)
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"git": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(), "features": feats,
            "train_rows": int(len(T["y"])), "valid_rows": int(len(V["y"])), "targets": TARGETS, "models": {}}
    for ti in ([int(x) for x in os.environ["TR_TARGETS"].split(",")] if os.environ.get("TR_TARGETS") else range(len(TARGETS))):
        okT, okV = T["y"][:, ti] >= 0, V["y"][:, ti] >= 0
        Xt, yt, mt = T["X"][okT][:, C], T["y"][okT, ti], T["mint"][okT]
        t0 = time.time()
        model, info = fit_one(Xt, yt, mt, ti)
        info["fit_s"] = round(time.time() - t0, 1)
        pv = model.predict_proba(V["X"][:, C])[:, 1]
        # isotonic fitted on half of V coins, evaluated on the other half (and the reverse), then fitted on all V for the artifact
        uv = np.unique(V["mint"]); rng = np.random.default_rng(1); half = set(rng.choice(uv, len(uv) // 2, replace=False))
        hm = np.array([m in half for m in V["mint"]])
        pc = np.zeros_like(pv)
        for tr_m in (hm, ~hm):
            ok = okV & tr_m
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999).fit(pv[ok], V["y"][ok, ti])
            pc[~tr_m] = iso.predict(pv[~tr_m])
        iso_all = IsotonicRegression(out_of_bounds="clip", y_min=0.001, y_max=0.999).fit(pv[okV], V["y"][okV, ti])
        tab, glob = clim_table(T, ti)
        rtab = rule_tables(T, ti)
        rules = {rn: apply_rule(V, rn, rtab) for rn in ("crowd", "growth")}
        print("== target %s fit %s" % (TARGETS[ti], info))
        raw = grade("raw", pv, V["y"][:, ti], V, okV, ti, clim_pred(tab, glob, V), rules)
        cal = grade("cal", pc, V["y"][:, ti], V, okV, ti, clim_pred(tab, glob, V), rules)
        t1 = time.time(); model.predict_proba(V["X"][:500][:, C]); b500 = time.time() - t1
        t1 = time.time(); model.predict_proba(V["X"][:1][:, C]); b1 = time.time() - t1
        joblib.dump({"model": model, "iso": iso_all, "clim": tab, "clim_global": glob, "rules": rtab, "feats": feats}, OUT / ("target_%d.joblib" % ti))
        meta["models"][str(ti)] = {"fit": info, "valid_raw": raw, "valid_cal_crossfit": cal, "infer_ms_batch500": b500 * 1000, "infer_ms_single": b1 * 1000}
    json.dump(meta, open(OUT / "meta.json", "w"), default=float, indent=1)
    json.dump(feats, open(OUT / "features.json", "w"))


def holdout(argv):
    H = load(argv)
    meta = json.load(open(OUT / "meta.json"))
    for ti in range(len(TARGETS)):
        art = joblib.load(OUT / ("target_%d.joblib" % ti))
        C = cols(art["feats"])
        p = art["iso"].predict(art["model"].predict_proba(H["X"][:, C])[:, 1])
        ok = H["y"][:, ti] >= 0
        rules = {rn: apply_rule(H, rn, art["rules"]) for rn in ("crowd", "growth")}
        print("== HOLDOUT target", TARGETS[ti])
        grade("holdout", p, H["y"][:, ti], H, ok, ti, clim_pred(art["clim"], art["clim_global"], H), rules)


if __name__ == "__main__":
    {"train": train, "holdout": holdout}[sys.argv[1]](sys.argv[2:])
