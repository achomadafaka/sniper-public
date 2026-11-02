"""Market-Efficiency Gate — baseline metrics (Phase 1C #3).

Measures how good the market midpoint baseline already is. Midpoint = the displayed implied probability
((bid+ask)/2); executable entry is the ask (handled by the hurdle report, #4) — kept SEPARATE here.
Primary baseline = raw Up midpoint; normalized = diagnostic.

Pure forecast metrics over (predicted Up prob, up_won) pairs: Brier, log loss, reliability bins, ECE, and
a logistic calibration intercept/slope. The DB reader emits per-(window, checkpoint) observations for
RESOLVED windows only; the per-checkpoint report groups them. The unit of independence is the WINDOW —
clustered uncertainty is added in #5; this module returns point estimates + the raw observations so #5 can
resample by window.
"""
from __future__ import annotations

import json
import math

_EPS = 1e-15
_LOGIT_EPS = 1e-6


def midpoint(book) -> float | None:
    b = (book or {}).get("best_bid")
    a = (book or {}).get("best_ask")
    if b is None or a is None:
        return None
    return (float(b) + float(a)) / 2.0


def normalized_up_midpoint(up_mid, down_mid) -> float | None:
    if up_mid is None or down_mid is None:
        return None
    s = float(up_mid) + float(down_mid)
    return (float(up_mid) / s) if s > 0 else None


def _clip(p, eps=_EPS):
    return min(max(float(p), eps), 1.0 - eps)


def brier_score(preds, outcomes) -> float | None:
    if not preds:
        return None
    return sum((float(p) - float(y)) ** 2 for p, y in zip(preds, outcomes)) / len(preds)


def log_loss(preds, outcomes) -> float | None:
    if not preds:
        return None
    tot = 0.0
    for p, y in zip(preds, outcomes):
        pc = _clip(p)
        tot += -(float(y) * math.log(pc) + (1 - float(y)) * math.log(1 - pc))
    return tot / len(preds)


def reliability_bins(preds, outcomes, *, n_bins=10) -> list:
    bins = [{"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "sum_pred": 0.0, "sum_y": 0.0}
            for i in range(n_bins)]
    for p, y in zip(preds, outcomes):
        idx = min(int(float(p) * n_bins), n_bins - 1)
        b = bins[idx]
        b["n"] += 1
        b["sum_pred"] += float(p)
        b["sum_y"] += float(y)
    out = []
    for b in bins:
        if b["n"]:
            out.append({"lo": b["lo"], "hi": b["hi"], "n": b["n"],
                        "mean_pred": b["sum_pred"] / b["n"], "observed_freq": b["sum_y"] / b["n"]})
    return out


def expected_calibration_error(preds, outcomes, *, n_bins=10) -> float | None:
    if not preds:
        return None
    n = len(preds)
    return sum((b["n"] / n) * abs(b["mean_pred"] - b["observed_freq"])
               for b in reliability_bins(preds, outcomes, n_bins=n_bins))


def calibration_fit(preds, outcomes, *, max_iter=50):
    """Logistic calibration fit: y ~ sigmoid(intercept + slope·logit(p)). slope=1, intercept=0 is perfect.
    None on degenerate data (<5 obs, single outcome class, or no variance in logit(p))."""
    ys = [float(y) for y in outcomes]
    if len(ys) < 5 or len(set(ys)) < 2:
        return None
    xs = [math.log(_clip(p, _LOGIT_EPS) / (1 - _clip(p, _LOGIT_EPS))) for p in preds]
    if max(xs) - min(xs) < 1e-9:
        return None
    a, b = 0.0, 1.0
    for _ in range(max_iter):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            p = 1.0 / (1.0 + math.exp(-(a + b * x)))
            w = p * (1 - p)
            r = p - y
            g0 += r; g1 += r * x
            h00 += w; h01 += w * x; h11 += w * x * x
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-12:
            break
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a -= da; b -= db
        if abs(da) + abs(db) < 1e-9:
            break
    return {"intercept": a, "slope": b}


def forecast_metrics(preds, outcomes, *, n_bins=10) -> dict:
    n = len(preds)
    fit = calibration_fit(preds, outcomes)
    return {
        "n": n,
        "brier": brier_score(preds, outcomes),
        "log_loss": log_loss(preds, outcomes),
        "mean_pred": (sum(float(p) for p in preds) / n) if n else None,
        "actual_freq": (sum(float(y) for y in outcomes) / n) if n else None,
        "ece": expected_calibration_error(preds, outcomes, n_bins=n_bins),
        "calibration_intercept": (fit["intercept"] if fit else None),
        "calibration_slope": (fit["slope"] if fit else None),
        "reliability_bins": reliability_bins(preds, outcomes, n_bins=n_bins),
    }


def baseline_observations(conn, *, feature_version=None) -> list:
    """One observation per (resolved window × checkpoint): the Up-midpoint prediction + the up_won label.
    Only CLEAN, decision-grade snapshots: settlement winner known and not conflict; feature_status=='ok';
    book_integrity_status=='usable'; not receive_time_missing; the window's book interval not quarantined;
    and a valid up midpoint. A quarantined/stale/leaky book yields an unreliable midpoint — excluded."""
    from . import features as FE
    feature_version = feature_version or FE.FEATURE_VERSION
    settle = {r["condition_id"]: r for r in conn.execute(
        "SELECT condition_id, winning_outcome, reconciliation_status FROM crypto5m_settlements")}
    out = []
    for cid, cp, ws, feats in _clean_snapshots(conn, feature_version):
        st = settle.get(cid)
        if not st or st["winning_outcome"] not in ("Up", "Down") or st["reconciliation_status"] == "conflict":
            continue
        up_mid = feats.get("up_midpoint_probability")
        if up_mid is None:
            continue
        out.append({
            "condition_id": cid, "checkpoint": cp, "window_start_ts": ws, "up_midpoint": float(up_mid),
            "normalized_up_midpoint": normalized_up_midpoint(up_mid, feats.get("down_midpoint_probability")),
            "up_won": 1 if st["winning_outcome"] == "Up" else 0,
        })
    return out


STAKES = (5, 10, 20)
SIDES = ("up", "down")


def _percentile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    f = idx - lo
    return s[lo] * (1 - f) + s[hi] * f


def _clean_snapshots(conn, feature_version):
    """Yield (condition_id, checkpoint, features) for decision-grade snapshots only: feature_status=='ok',
    per-snapshot book_integrity_status=='usable', not receive_time_missing. Resolution NOT required
    (execution cost is outcome-independent); callers that need a label add the settlement filter.
    NOTE: the condition-level crypto5m_book_reconciliation quarantine is a DIAGNOSTIC, not a gate — it
    fires on benign WS-vs-REST-refetch book movement; the per-snapshot integrity is the real guard."""
    if not feature_snapshots_available(conn):
        return
    for s in conn.execute(
            "SELECT condition_id, checkpoint, window_start_ts, feature_status, book_integrity_status, "
            "receive_time_missing, features_json FROM crypto5m_feature_snapshots "
            "WHERE feature_version = ? ORDER BY window_start_ts, condition_id, checkpoint",
            (feature_version,)):
        if (s["feature_status"] != "ok" or s["book_integrity_status"] != "usable"
                or s["receive_time_missing"]):
            continue
        yield s["condition_id"], s["checkpoint"], s["window_start_ts"], json.loads(s["features_json"] or "{}")


def feature_snapshots_available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='crypto5m_feature_snapshots'").fetchone() is not None


def executable_hurdle_for_features(feats, *, side, stake) -> dict:
    """Hurdle = the forecast improvement a model needs just to beat execution. breakeven_probability for a
    $1-payout token = cost per share; here the per-share executable price (fee-exclusive) and fee-inclusive
    cost ARE the breakeven probabilities, so hurdle = (cost per share) - midpoint. Fee-inclusive hurdle is
    None when the fee is unresolved."""
    mid = feats.get(f"{side}_midpoint_probability")
    exec_price = feats.get(f"{side}_executable_price_{stake}_usdc")
    fee_inc = feats.get(f"{side}_fee_inclusive_cost_{stake}_usdc")
    fillable = bool(feats.get(f"{side}_fillability_{stake}_usdc"))
    return {
        "side": side, "stake": stake, "midpoint": mid, "executable_price": exec_price,
        "fee_inclusive_cost": fee_inc, "fillable": fillable,
        "executable_hurdle": (exec_price - mid) if (exec_price is not None and mid is not None) else None,
        "fee_inclusive_hurdle": (fee_inc - mid) if (fee_inc is not None and mid is not None) else None,
    }


def executable_hurdle_observations(conn, *, feature_version=None) -> list:
    from . import features as FE
    fv = feature_version or FE.FEATURE_VERSION
    out = []
    for cid, cp, ws, feats in _clean_snapshots(conn, fv):
        for side in SIDES:
            for stake in STAKES:
                o = executable_hurdle_for_features(feats, side=side, stake=stake)
                o["condition_id"] = cid
                o["checkpoint"] = cp
                o["window_start_ts"] = ws
                out.append(o)
    return out


def executable_hurdle_report(conn, *, feature_version=None) -> list:
    """Per (checkpoint, side, stake): n, fillable_share, median/p75/p90 executable hurdle, fee-inclusive
    availability + median fee-inclusive hurdle. Hurdle percentiles over FILLABLE observations only."""
    groups = {}
    for o in executable_hurdle_observations(conn, feature_version=feature_version):
        groups.setdefault((o["checkpoint"], o["side"], o["stake"]), []).append(o)
    rep = []
    for (cp, side, stake), obs in sorted(groups.items()):
        n = len(obs)
        fillable = [o for o in obs if o["fillable"]]
        eh = [o["executable_hurdle"] for o in fillable if o["executable_hurdle"] is not None]
        fih = [o["fee_inclusive_hurdle"] for o in fillable if o["fee_inclusive_hurdle"] is not None]
        rep.append({
            "checkpoint": cp, "side": side, "stake": stake, "n": n,
            "fillable_share": (len(fillable) / n) if n else 0.0,
            "median_executable_hurdle": _percentile(eh, 0.5),
            "p75_executable_hurdle": _percentile(eh, 0.75),
            "p90_executable_hurdle": _percentile(eh, 0.90),
            "fee_inclusive_available_share": (len(fih) / len(fillable)) if fillable else 0.0,
            "median_fee_inclusive_hurdle": _percentile(fih, 0.5),
        })
    return rep


def cluster_bootstrap(rows, *, statistic, cluster_key="condition_id", n_boot=1000, seed=0, ci=0.95):
    """Window-clustered bootstrap: resample WINDOWS (clusters) with replacement — one window contributing
    12 checkpoint rows is ONE correlated unit, never 12 independent rows. ``statistic(rows)->float``.
    Returns the point estimate + a percentile CI. Deterministic given ``seed``."""
    import random
    clusters = {}
    for r in rows:
        clusters.setdefault(r[cluster_key], []).append(r)
    keys = list(clusters)
    if not keys:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        sample = []
        for _ in range(len(keys)):
            sample.extend(clusters[keys[rng.randrange(len(keys))]])
        v = statistic(sample)
        if v is not None:
            vals.append(v)
    return {
        "point": statistic(rows),
        "ci_low": _percentile(vals, (1 - ci) / 2),
        "ci_high": _percentile(vals, 1 - (1 - ci) / 2),
        "n_clusters": len(keys), "n_boot": len(vals),
    }


def temporal_blocks(rows, *, time_key="window_start_ts", n_blocks=2, cluster_key="condition_id") -> list:
    """Partition rows into ``n_blocks`` contiguous OUT-OF-TIME blocks by window cluster.

    One market window can contribute multiple checkpoint rows. Those correlated rows must stay together;
    otherwise the same ``condition_id`` can leak into both train and test. Rows without ``cluster_key`` fall
    back to one-row clusters for generic metric tests.
    """
    if n_blocks < 1:
        return []
    if not rows:
        return []
    clusters = {}
    first_time = {}
    for idx, r in enumerate(rows):
        cid = r.get(cluster_key)
        key = ("cluster", cid) if cid is not None else ("row", idx)
        clusters.setdefault(key, []).append(r)
        t = r[time_key]
        first_time[key] = min(first_time.get(key, t), t)
    ordered = sorted(clusters.items(), key=lambda kv: (first_time[kv[0]], str(kv[0])))
    size = len(ordered) / n_blocks
    blocks = []
    for i in range(n_blocks):
        block_clusters = ordered[int(i * size):int((i + 1) * size)]
        block = [r for _, rs in block_clusters for r in rs]
        blocks.append(sorted(block, key=lambda r: (r[time_key], str(r.get(cluster_key, "")),
                                                   r.get("checkpoint", 0))))
    return blocks


# Predeclared diagnostic model ladder (Gate E2). Each value = features ADDED on top of logit(midpoint).
# M0 = market midpoint only (no fit). Temporal-holdout only; NOT a strategy, NOT broad feature search.
MODEL_LADDER = {
    "M0": [],
    "M1": ["normalized_distance_to_beat", "seconds_remaining", "vol_per_s"],
    "M2": ["normalized_distance_to_beat", "seconds_remaining", "vol_per_s",
           "chainlink_return_30s", "binance_chainlink_basis_bps"],
    "M3": ["normalized_distance_to_beat", "seconds_remaining", "vol_per_s",
           "chainlink_return_30s", "binance_chainlink_basis_bps", "up_spread", "down_spread"],
}
MIN_TRAIN = 20


def _logit_safe(p):
    pc = _clip(p, _LOGIT_EPS)
    return math.log(pc / (1 - pc))


def _solve(A, b):
    """Solve A x = b by Gaussian elimination with partial pivoting. None if singular."""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def _fit_logistic(X, y, *, ridge=1.0, max_iter=50):
    """Ridge-regularized logistic regression by IRLS. X rows include the intercept (col 0); ridge is not
    applied to the intercept."""
    k = len(X[0])
    w = [0.0] * k
    for _ in range(max_iter):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = sum(w[j] * xi[j] for j in range(k))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            s = p * (1 - p)
            for a in range(k):
                g[a] += (p - yi) * xi[a]
                for b2 in range(k):
                    H[a][b2] += s * xi[a] * xi[b2]
        for a in range(1, k):
            g[a] += ridge * w[a]
            H[a][a] += ridge
        dw = _solve(H, g)
        if dw is None:
            break
        w = [w[j] - dw[j] for j in range(k)]
        if max(abs(d) for d in dw) < 1e-8:
            break
    return w


def _predict(w, xrow):
    z = sum(w[j] * xrow[j] for j in range(len(w)))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def _col_stats(feature_rows):
    if not feature_rows:
        return []
    ncol = len(feature_rows[0])
    stats = []
    for j in range(ncol):
        col = [row[j] for row in feature_rows]
        m = sum(col) / len(col)
        sd = math.sqrt(sum((x - m) ** 2 for x in col) / len(col))
        stats.append((m, sd if sd > 1e-9 else None))
    return stats


def _design(feature_rows, stats):
    """Prepend an intercept and z-standardize each feature (constant features -> 0, absorbed by intercept)."""
    out = []
    for fr in feature_rows:
        row = [1.0]
        for x, (m, sd) in zip(fr, stats):
            row.append((x - m) / sd if sd else 0.0)
        out.append(row)
    return out


def _usable_for(r, feat_keys):
    if r.get("midpoint") is None:
        return False
    return all(r["features"].get(k) is not None for k in feat_keys)


def model_observations(conn, *, feature_version=None) -> list:
    """Resolved, clean snapshots with the Up-midpoint, up_won label, and the ladder's feature values."""
    from . import features as FE
    fv = feature_version or FE.FEATURE_VERSION
    needed = sorted({k for ks in MODEL_LADDER.values() for k in ks})
    settle = {r["condition_id"]: r for r in conn.execute(
        "SELECT condition_id, winning_outcome, reconciliation_status FROM crypto5m_settlements")}
    out = []
    for cid, cp, ws, feats in _clean_snapshots(conn, fv):
        st = settle.get(cid)
        if not st or st["winning_outcome"] not in ("Up", "Down") or st["reconciliation_status"] == "conflict":
            continue
        mid = feats.get("up_midpoint_probability")
        if mid is None:
            continue
        out.append({"condition_id": cid, "checkpoint": cp, "window_start_ts": ws,
                    "midpoint": float(mid), "up_won": 1 if st["winning_outcome"] == "Up" else 0,
                    "features": {k: feats.get(k) for k in needed}})
    return out


def walk_forward_comparison(obs, *, n_blocks=3, models=None) -> dict:
    """Walk-forward OOS comparison of the diagnostic model ladder. For each model and each test block i>=1,
    fit on blocks 0..i-1 and predict block i; aggregate OOS Brier/log-loss. M0 predicts the midpoint
    directly; M1+ fit logistic(up_won ~ logit(midpoint) + features). Reports ΔBrier/Δlog-loss vs M0.
    Diagnostic only — no promotion, no feature search."""
    models = models or MODEL_LADDER
    blocks = temporal_blocks(obs, n_blocks=n_blocks)
    result = {}
    # M0 global reference: the midpoint over ALL OOS test rows.
    g_preds = [_clip(r["midpoint"]) for i in range(1, len(blocks)) for r in blocks[i]
               if r.get("midpoint") is not None]
    g_ys = [r["up_won"] for i in range(1, len(blocks)) for r in blocks[i] if r.get("midpoint") is not None]
    result["M0"] = {"n": len(g_ys), "brier": brier_score(g_preds, g_ys), "log_loss": log_loss(g_preds, g_ys)}

    for name, feat_keys in models.items():
        if name == "M0":
            continue
        m_preds, mid_preds, ys = [], [], []
        for i in range(1, len(blocks)):
            train = [r for b in blocks[:i] for r in b if _usable_for(r, feat_keys)]
            test = [r for r in blocks[i] if _usable_for(r, feat_keys)]
            if not test or len(train) < MIN_TRAIN:
                continue
            tr_feat = [[_logit_safe(r["midpoint"])] + [float(r["features"][k]) for k in feat_keys]
                       for r in train]
            stats = _col_stats(tr_feat)
            w = _fit_logistic(_design(tr_feat, stats), [r["up_won"] for r in train])
            te_feat = [[_logit_safe(r["midpoint"])] + [float(r["features"][k]) for k in feat_keys]
                       for r in test]
            for r, xrow in zip(test, _design(te_feat, stats)):
                m_preds.append(_predict(w, xrow))
                mid_preds.append(_clip(r["midpoint"]))   # midpoint over the SAME OOS rows -> apples-to-apples
                ys.append(r["up_won"])
        mb, mlog = brier_score(m_preds, ys), log_loss(m_preds, ys)
        m0b, m0log = brier_score(mid_preds, ys), log_loss(mid_preds, ys)
        result[name] = {
            "n": len(ys), "brier": mb, "log_loss": mlog, "m0_brier_aligned": m0b,
            "delta_brier_vs_m0": (mb - m0b) if (mb is not None and m0b is not None) else None,
            "delta_log_loss_vs_m0": (mlog - m0log) if (mlog is not None and m0log is not None) else None,
        }
    return result


def shuffle_labels_within_blocks(obs, *, time_key="window_start_ts", n_blocks=4, seed=0) -> list:
    """Permute up_won WITHIN each temporal block — preserves the block marginal rate, destroys any per-row
    signal. A model that still 'improves' on shuffled labels is finding phantom structure."""
    import random
    rng = random.Random(seed)
    out = []
    for b in temporal_blocks(obs, time_key=time_key, n_blocks=n_blocks):
        labels = [r["up_won"] for r in b]
        rng.shuffle(labels)
        out.extend({**r, "up_won": y} for r, y in zip(b, labels))
    return out


def with_noise_feature(obs, *, key="noise_feature", seed=0) -> list:
    """Add a lagged-irrelevant (random) feature — a model using it should not beat the midpoint."""
    import random
    rng = random.Random(seed)
    return [{**r, "features": {**r["features"], key: rng.gauss(0.0, 1.0)}} for r in obs]


def negative_controls(obs, *, n_blocks=4, seed=0, feature_model=("M1", None), materiality=0.02) -> dict:
    """Run the diagnostic comparison on negative controls; ``passed`` is True only if NO control shows a
    materially-better-than-midpoint OOS Brier (i.e. no spurious edge). Controls: (1) labels shuffled within
    temporal blocks, (2) a random noise feature. [The 'future Chainlink feature' control is enforced by the
    leakage audit; 'random checkpoint policy' by pre-registration — both out of this function's scope.]"""
    name, keys = feature_model
    keys = keys if keys is not None else MODEL_LADDER.get(name, [])

    def _spurious(res, mname):
        d = res.get(mname, {}).get("delta_brier_vs_m0")
        return d is not None and d < -materiality

    shuffled = shuffle_labels_within_blocks(obs, n_blocks=n_blocks, seed=seed)
    r_shuf = walk_forward_comparison(shuffled, n_blocks=n_blocks, models={"M0": [], name: keys})
    noised = with_noise_feature(obs, seed=seed)
    r_noise = walk_forward_comparison(noised, n_blocks=n_blocks,
                                      models={"M0": [], "Mnoise": ["noise_feature"]})
    passed = not (_spurious(r_shuf, name) or _spurious(r_noise, "Mnoise"))
    return {"passed": passed,
            "controls": {"shuffled_labels": r_shuf, "noise_feature": r_noise}}


MILESTONES = [200, 500, 1000, 2000, 5000]
PRIMARY_CHECKPOINTS = (60, 180, 240, 270)


def _go_no_go(n_resolved, e2, controls_passed, *, materiality=0.005) -> dict:
    """Predeclared go/no-go (after >=1000 windows; not a permanent verdict). proceed_candidate ONLY if a
    diagnostic model improves OOS Brier vs the midpoint AND negative controls pass — still pending the E3
    executable-hurdle check (the model must beat fee/spread/slippage, evaluated separately)."""
    if n_resolved < 1000:
        return {"verdict": "insufficient_data",
                "reasons": [f"{n_resolved} resolved windows (<1000 preliminary floor)"]}
    if not controls_passed:
        return {"verdict": "pause", "reasons": ["negative controls show spurious edge — pipeline invalid"]}
    improvers = [name for name, r in e2.items()
                 if name != "M0" and r.get("delta_brier_vs_m0") is not None
                 and r["delta_brier_vs_m0"] < -materiality]
    if not improvers:
        return {"verdict": "pause",
                "reasons": ["no diagnostic model improves OOS Brier vs the midpoint baseline"]}
    return {"verdict": "proceed_candidate",
            "reasons": [f"models improving OOS vs midpoint: {improvers}",
                        "NEXT: confirm the improvement exceeds the E3 executable hurdle and survives >=2 blocks"]}


def efficiency_gate_report(conn, *, feature_version=None, primary_checkpoints=PRIMARY_CHECKPOINTS) -> dict:
    """Assemble the three-level gate over the captured DB. E1 marginal calibration (primary checkpoints),
    E2 diagnostic model ladder (walk-forward), E3 executable hurdle, negative controls, and the go/no-go
    verdict. Reporting milestones at 200/500/1k/2k/5k usable resolved windows."""
    base = baseline_observations(conn, feature_version=feature_version)
    n_resolved = len({o["condition_id"] for o in base})
    e1_all = checkpoint_efficiency_report(conn, feature_version=feature_version)
    e1 = {cp: m for cp, m in e1_all.items() if cp in primary_checkpoints}
    obs_model = model_observations(conn, feature_version=feature_version)
    e2 = walk_forward_comparison(obs_model, n_blocks=4) if obs_model else {}
    e3 = [r for r in executable_hurdle_report(conn, feature_version=feature_version)
          if r["checkpoint"] in primary_checkpoints]
    controls = (negative_controls(obs_model, feature_model=("M1", MODEL_LADDER["M1"]))
                if obs_model else {"passed": True})
    return {
        "n_resolved_windows": n_resolved,
        "milestones": {m: (n_resolved >= m) for m in MILESTONES},
        "primary_checkpoints": list(primary_checkpoints),
        "E1_calibration": e1,
        "E2_model_ladder": e2,
        "E3_executable_hurdle": e3,
        "negative_controls_passed": controls.get("passed", True),
        "go_no_go": _go_no_go(n_resolved, e2, controls.get("passed", True)),
    }


def checkpoint_efficiency_report(conn, *, baseline="raw", feature_version=None) -> dict:
    """Per-checkpoint forecast metrics for the chosen midpoint baseline ('raw' = up_midpoint primary,
    'normalized' = diagnostic). Skips an observation whose chosen baseline value is None."""
    key = "up_midpoint" if baseline == "raw" else "normalized_up_midpoint"
    by_cp = {}
    for o in baseline_observations(conn, feature_version=feature_version):
        if o[key] is None:
            continue
        by_cp.setdefault(o["checkpoint"], {"preds": [], "outs": []})
        by_cp[o["checkpoint"]]["preds"].append(o[key])
        by_cp[o["checkpoint"]]["outs"].append(o["up_won"])
    return {cp: forecast_metrics(d["preds"], d["outs"]) for cp, d in sorted(by_cp.items())}
