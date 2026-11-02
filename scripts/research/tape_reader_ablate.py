#!/usr/bin/env python3
"""Leakage / dependence check: refit one target with feature groups removed, report validation BSS vs climatology and within-stratum AUC.
usage: tape_reader_ablate.py T1.npz T2.npz -- V.npz [target_index]"""
import sys
from pathlib import Path
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
import tape_reader_train as TR
from tape_reader_samples import FEATS

GROUPS = {"full": [], "no_level_age_fill": ["log_mcap", "age_s", "curve_fill", "impact"],
          "no_cumulative": ["top_buyer", "creator_sold", "holders_pos", "age_s"],
          "no_flow_crowd": ["wal_10", "wal_30", "wal_60", "wal_180", "buy_10", "buy_30", "buy_60", "buy_180", "w60max300", "growth", "swaps_10", "swaps_60"],
          "price_only": [f for f in FEATS if not f.startswith(("ret_", "noise5", "drawdown", "since_", "log_mcap"))]}


def main():
    a = sys.argv[1:]
    k = a.index("--")
    T, V = TR.load(a[:k]), TR.load([a[k + 1]])
    ti = int(a[k + 2]) if len(a) > k + 2 else 1
    okT, okV = T["y"][:, ti] >= 0, V["y"][:, ti] >= 0
    tab, glob = TR.clim_table(T, ti)
    base = TR.clim_pred(tab, glob, V)[okV]
    yv = V["y"][okV, ti].astype(float)
    st = TR.strata(V)[okV]
    for name, drop in GROUPS.items():
        keep = [f for f in FEATS if f not in drop]
        C = TR.cols(keep)
        m = HistGradientBoostingClassifier(max_depth=5, max_leaf_nodes=15, learning_rate=0.05, max_iter=300, min_samples_leaf=500,
                                           l2_regularization=1.0, random_state=0).fit(T["X"][okT][:, C], T["y"][okT, ti])
        p = m.predict_proba(V["X"][okV][:, C])[:, 1]
        bss = 1 - np.mean((p - yv) ** 2) / np.mean((base - yv) ** 2)
        aucs = [roc_auc_score(yv[st == s], p[st == s]) for s in np.unique(st) if (st == s).sum() >= 200 and 0 < yv[st == s].sum() < (st == s).sum()]
        print("%-20s feats %2d  BSS %+.3f  AUC %.3f  within-stratum AUC %.3f" % (name, len(keep), bss, roc_auc_score(yv, p), np.mean(aucs)))


main()
