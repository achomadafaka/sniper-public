"""LESSONS DIGEST (REPORT ONLY): what the lessons table says about how the desk traded and what selection missed.

Reads data/desk/lessons.db (immutable rows, newest feature version per key). Produces: tag counts with USD attached (realized trades only),
the top recurring mistakes, and for each a PROPOSED change with the number behind it computed from the same table. A proposal is text for
humans: it never applies, tunes or switches anything (rule b9930440); an adopted change ships as a new cohort or prompt version and is later
graded by this same table (did that tag's USD fall).

Bias, stated in every digest: realized rows are the trades WE took (our selection and our exits); the counterfactual rows are hindsight on the
tape, not decisions we could have known; USD sums are realized rows only. Every comparison carries n and a coin-clustered bootstrap
half-width; fewer than MIN_N tagged trades is 'insufficient evidence, watch', never 'recurring'.
"""
from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timezone

from sniper.services import memecoin_desk_lessons as L

MIN_N = 20                    # closes a proposal needs before it is a candidate (Gamma's forward-read size, rule: no change on a handful)
RECURRING_N = 5               # tagged trades before a tag is called 'recurring'
MISTAKES = ("entered_top", "late_curve", "shaken_out", "held_through_death", "migration_step", "entered_farm")
PROPOSALS = {
    "late_curve": {"feature": "curve_fill_at_entry (fraction of the 85 SOL completion, known at the verdict)",
                   "rule": "refuse or WAIT an ENTER whose curve fill is at or above the cut",
                   "comparator": "JUDGE-rails replay R for fill >= cut versus fill < cut, same cohort, realized + refused units",
                   "forward": "a new JUDGE/trigger cohort carrying the cut; read after MIN_N usable closes"},
    "entered_top": {"feature": "entry_rank_prior_pct: price paid versus the 120 s executable window BEFORE the verdict (prior-only, known at the "
                               "verdict; the tag itself is retrospective, it uses the window around the fill)",
                    "rule": "WAIT for the first pullback instead of entering in the prior window's top decile",
                    "comparator": "JUDGE-rails replay R by entry_rank_prior_pct, top decile versus the rest, same cohort",
                    "forward": "a new watch/trigger cohort (Beta); read after MIN_N usable closes"},
    "shaken_out": {"feature": "the position's own noise-scaled drawdown at the exit (known at the exit second)",
                   "rule": "no RISK/trail exit inside k x the coin's median 5 s range unless a death indicator fired",
                   "comparator": "realized R versus hold-300 s with a catastrophe stop, paired per trade",
                   "forward": "a new RISK/rails cohort; the retrospective tag is a hypothesis, not a rule"},
    "held_through_death": {"feature": "Gamma's six death indicators at one noise multiple k (known each second)",
                           "rule": "exit when any indicator fires (k provisional until her discovery table)",
                           "comparator": "realized R versus the death-rule exit, paired per trade",
                           "forward": "a new RISK cohort; the retrospective tag is a hypothesis, not a rule"},
    "migration_step": {"feature": "curve fill at the time of the exit decision (known)",
                       "rule": "sell on the curve once fill >= 0.90 instead of holding into the pool step",
                       "comparator": "realized R versus sell_before_migration, paired per trade",
                       "forward": "a new exit-rule cohort; read after MIN_N usable closes"},
    "entered_farm": {"feature": "farm/wash features (not defined yet)", "rule": "hard-filter KILL once Gamma's definitions land",
                     "comparator": "none until a feature version carries the features", "forward": "none yet"}}
# tag -> (alternative to compare against, or None) for the paired evidence
ALT = {"shaken_out": "hold300_cat", "held_through_death": "death_any", "migration_step": "sell_before_migration"}


def load(db_path, *, since=None, until=None, version=None, cohort=None, strict=False, latest_per_key=False):
    """Rows of ONE feature version (default: the NUMERICALLY newest present, v10 > v2) as dicts {key, kind, mint, t0, facts, tags}; optionally
    one cohort. latest_per_key retains each position's newest version for display only. Immutable rowid orders arrivals;
    strict raises source/JSON/type errors instead of treating missing evidence as an empty population."""
    rows = L.q(db_path, "SELECT rowid AS sequence, key, feature_version, kind, mint, position_id, ticket_id, computed_at, t0, facts_json, tags_json "
                       "FROM lessons ORDER BY rowid", strict=strict)
    rows = [r for r in rows if not ((since is not None and (r["t0"] or 0) < since) or (until is not None and (r["t0"] or 0) >= until))]
    if not rows:
        return []
    if latest_per_key and version is None:
        newest = {}
        for r in rows:
            if r["key"] not in newest or L.version_num(r["feature_version"]) > L.version_num(newest[r["key"]]["feature_version"]):
                newest[r["key"]] = r
        rows = [r for r in rows if newest[r["key"]] is r]
    else:
        ver = version or max((r["feature_version"] for r in rows), key=L.version_num)
        rows = [r for r in rows if r["feature_version"] == ver]
    out = []
    for r in rows:
        try:
            d = {**r, "facts": json.loads(r["facts_json"]), "tags": json.loads(r["tags_json"])}
            if not isinstance(d["facts"], dict) or not isinstance(d["tags"], list) or any(not isinstance(t, str) for t in d["tags"]):
                raise TypeError("lessons facts must be an object and tags a string list")
            for k in ("identity", "exit", "alternatives", "migration", "learning_loop"):
                if d["facts"].get(k) is not None and not isinstance(d["facts"][k], dict):
                    raise TypeError("invalid lessons fact object: " + k)
            loop = d["facts"].get("learning_loop") or {}
            for k in ("stages", "watch_entry", "returns", "alternatives", "times", "costs"):
                if loop.get(k) is not None and not isinstance(loop[k], dict):
                    raise TypeError("invalid learning fact object: " + k)
            if loop.get("censored") is not None and (not isinstance(loop["censored"], list) or any(not isinstance(c, str) for c in loop["censored"])):
                raise TypeError("learning censor reasons must be a string list")
        except (ValueError, TypeError):
            if strict:
                raise
            continue
        if cohort is not None and ((d["facts"].get("identity") or {}).get("cohort_id") != cohort):
            continue
        out.append(d)
    return out


def report_t(row):
    """The row's REPORTING time (corrected UTC): facts.t0_corrected, else the raw tape t0 (rows of an older feature version)."""
    t = row["facts"].get("t0_corrected")
    return t if t is not None else row.get("t0")


def cohort_key(row):
    ident = row["facts"].get("identity") or {}
    return "cohort=%s prompt=%s" % (ident.get("cohort_id") or "unknown", ident.get("prompt_version") or "unknown")


def split_by_cohort(rows):
    """{cohort/prompt key: rows}: decision cohorts and prompt versions are never pooled in one comparison."""
    out = {}
    for r in rows:
        out.setdefault(cohort_key(r), []).append(r)
    return dict(sorted(out.items()))


def _alt(row, name):
    f = row["facts"]
    if "learning_loop" in f:
        a = ((f["learning_loop"] or {}).get("alternatives") or {}).get("live_rails" if name == "rails" else name) or {}
        return a.get("R") if not a.get("censored") and L._fin(a.get("R")) else None
    a = (f.get("alternatives") or {}).get("rails" if name == "live_rails" else name) or {}
    return a.get("R")


def _real_R(row):
    f = row["facts"]
    if "learning_loop" in f:
        return (f["learning_loop"] or {}).get("realized_R_tape_model")
    return (f.get("exit") or {}).get("realized_R")


def _boot_ci(pairs, draws=500, seed=0):
    """(mean, lo, hi) of the paired differences, coin-clustered bootstrap; None under 2 coins."""
    by = {}
    for mint, d in pairs:
        by.setdefault(mint, []).append(d)
    mints = list(by)
    if len(mints) < 2:
        return None
    rnd = random.Random(seed)
    means = sorted(statistics.mean(x for m in (rnd.choice(mints) for _ in mints) for x in by[m]) for _ in range(draws))
    return statistics.mean(d for _, d in pairs), means[int(0.025 * draws)], means[int(0.975 * draws)]


def _mean(xs):
    xs = [x for x in xs if L._fin(x)]
    return statistics.mean(xs) if xs else None


def _known(xs):
    return [x for x in xs if L._fin(x)]


def digest(rows):
    real = [r for r in rows if r["kind"] == "realized"]
    cf = [r for r in rows if r["kind"] == "counterfactual"]
    skip = [r for r in rows if r["kind"] == "skip_counterfactual"]
    versions = sorted({r["feature_version"] for r in rows if r.get("feature_version")}, key=L.version_num)
    out = {"counts": {"realized": len(real), "counterfactual": len(cf), "skip_counterfactual": len(skip)}, "feature_versions": versions,
           "censored_rows": {k: sum(1 for r in rs if r["facts"].get("censored")) for k, rs in
                             (("realized", real), ("counterfactual", cf), ("skip_counterfactual", skip))},
           "distinct_mints": {k: len({r["mint"] for r in rs}) for k, rs in (("realized", real), ("counterfactual", cf), ("skip_counterfactual", skip))},
           "tags": {}, "mistakes": [], "selection": {}}
    band = [r["facts"].get("good_exit_band") for r in real if r["facts"].get("good_exit_band")]
    out["good_exit_band"] = band[-1] if band else None
    usd = lambda r: (r["facts"].get("exit") or {}).get("realized_pnl_usd") or 0.0
    notional = lambda r: (r["facts"].get("exit") or {}).get("entry_notional_usd") or 0.0
    for tag in L.TAGS:
        tr = [r for r in real if tag in r["tags"]]
        if not tr:
            continue
        out["tags"][tag] = {"n": len(tr), "distinct_mints": len({r["mint"] for r in tr}), "pnl_usd": round(sum(usd(r) for r in tr), 2),
                            "mean_R": _r(_mean(_real_R(r) for r in tr)),
                            # what WAITING would have saved: paid versus the cheapest exact buy in the 2 minutes AFTER the fill
                            "entry_regret_usd_hindsight": round(sum(max(0.0, r["facts"]["entry_vs_best_after_pct"]) / 100.0 * notional(r)
                                                                    for r in tr if L._fin(r["facts"].get("entry_vs_best_after_pct"))), 2),
                            "entry_regret_known_n": sum(1 for r in tr if L._fin(r["facts"].get("entry_vs_best_after_pct"))),
                            "exit_regret_usd_hindsight": round(sum(r["facts"]["exit_regret_R"] * notional(r)
                                                                   for r in tr if L._fin(r["facts"].get("exit_regret_R"))), 2),
                            "exit_regret_known_n": sum(1 for r in tr if L._fin(r["facts"].get("exit_regret_R")))}
    tagged_any = [r for r in real if any(t in r["tags"] for t in L.TAGS)]
    # a trade carries several tags: per-tag dollars overlap and are NOT additive; this is the one distinct-position total
    out["tag_usd_note"] = "tags overlap, per-tag USD is not additive; distinct tagged positions total %.2f USD over n=%d" % (
        sum(usd(r) for r in tagged_any), len(tagged_any))
    ranked = sorted((t for t in MISTAKES if t in out["tags"]), key=lambda t: (out["tags"][t]["pnl_usd"], -out["tags"][t]["n"]))
    for tag in ranked[:3]:
        tagged = [r for r in real if tag in r["tags"]]
        ev = _evidence(tag, tagged, real + cf)
        usable = ev.get("n", 0) if tag in ALT else ev.get("n_usable", 0)
        coins = ev.get("distinct_mints", 0)
        if usable >= MIN_N and coins >= MIN_N // 2 and ev.get("comparator_ok", True):
            status = "candidate"
        elif len(tagged) >= MIN_N:
            status = "insufficient evaluable pairs, watch"
        elif len(tagged) >= RECURRING_N:
            status = "recurring, insufficient evidence, watch"
        else:
            status = "seen, insufficient evidence, watch"
        out["mistakes"].append({"tag": tag, "n": len(tagged), "status": status, "label": "exploratory: retrospective tag, a hypothesis",
                                "proposal": PROPOSALS[tag], "evidence": ev})

    # selection: what the sampled SKIPs and the refused/never-attempted ENTERs would have done (hindsight, alternatives as named).
    # Shares and means run over rows with a KNOWN value only; the censored count is shown beside them, never counted as a miss.
    def alt_counts(rs):
        out_ = {}
        for name in ("rails", "fixed_30s", "death_any"):
            known = sum(1 for r in rs if L._fin(_alt(r, name)))
            out_[name] = {"known": known, "censored_or_absent": len(rs) - known}
        return out_

    def block(rs):
        best = _known(r["facts"].get("best_R_hindsight") for r in rs)
        lb = _known(r["facts"].get("best_R_observed_lb") for r in rs)
        return {"n": len(rs), "known": len(best), "censored": len(rs) - len(best), "distinct_mints": len({r["mint"] for r in rs}),
                "proven_ge_20pct_lower_bound": sum(1 for x in lb if x >= 0.20), "alternatives_denominators": alt_counts(rs),
                "mean_best_R_hindsight": _r(statistics.mean(best)) if best else None,
                "share_best_ge_20pct": _r(sum(1 for x in best if x >= 0.20) / len(best)) if best else None,
                "mean_R_fixed_30s": _r(_mean(_alt(r, "fixed_30s") for r in rs)), "mean_R_death_any": _r(_mean(_alt(r, "death_any") for r in rs))}
    out["selection"] = {"entered": block(real), "refused_or_never_attempted": block(cf), "sampled_skips": block(skip),
                        "skip_reasons": _count(r["facts"].get("why") for r in skip)}
    return out


def _count(xs):
    out = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _evidence(tag, tagged, pool):
    if tag == "entered_farm":
        return {"note": "no feature version carries the farm features yet", "n_usable": 0}
    if tag in ALT:
        pairs = [(r["mint"], (_real_R(r) - _alt(r, ALT[tag]))) for r in tagged
                 if L._fin(_alt(r, ALT[tag])) and L._fin(_real_R(r))]
        ci = _boot_ci(pairs)
        ev = {"compare": "realized_R minus %s alternative R, paired on the tagged trades" % ALT[tag], "n": len(pairs),
              "tagged": len(tagged), "unevaluable": len(tagged) - len(pairs), "distinct_mints": len({m for m, _ in pairs})}
        if ci:
            ev.update({"mean_diff_R": _r(ci[0]), "ci95": [_r(ci[1]), _r(ci[2])]})
        if tag == "migration_step":
            ev["mean_migration_step_R"] = _r(_mean((r["facts"].get("migration") or {}).get("step_R") for r in tagged))
        return ev
    key, thr = ("curve_fill_at_entry", 0.9) if tag == "late_curve" else ("entry_rank_prior_pct", 90)
    ok = lambda r: L._fin(_alt(r, "rails"))
    a = [r for r in pool if (r["facts"].get(key) if r["facts"].get(key) is not None else -1) >= thr and ok(r)]
    b = [r for r in pool if r["facts"].get(key) is not None and r["facts"][key] < thr and ok(r)]
    usable_tagged = [r for r in tagged if ok(r)]
    return {"compare": "JUDGE-rails replay R, %s >= %s versus below, over realized + counterfactual units" % (key, thr),
            "n_at_or_above": len(a), "n_below": len(b), "n_usable": len(usable_tagged),
            "comparator_ok": len(a) >= RECURRING_N and len(b) >= RECURRING_N,
            "distinct_mints": len({r["mint"] for r in usable_tagged}),
            "mean_R_at_or_above": _r(_mean(_alt(r, "rails") for r in a)), "mean_R_below": _r(_mean(_alt(r, "rails") for r in b))}


def learning_loop(rows, batch_n=20):
    """Stored-evidence cadence, report only; arrival sequence freezes completed cohort/prompt batches."""
    if not isinstance(batch_n, int) or isinstance(batch_n, bool) or batch_n < 1:
        raise ValueError("batch_n must be a positive integer")
    real = []
    for row in rows:
        if row["kind"] != "realized":
            continue
        loop = row["facts"].get("learning_loop") or {}
        censored = list(loop.get("censored") or [])
        if not loop:
            censored.append("learning_loop_unavailable")
        real.append({**row, "learning": loop, "learning_censored": censored})
    # Direct callers without a stored sequence retain their input order; the SQLite loader supplies immutable rowids.
    real = [r for _, r in sorted(enumerate(real), key=lambda ir: (0, ir[1]["sequence"]) if L._fin(ir[1].get("sequence")) else (1, ir[0]))]
    versions = {r["feature_version"] for r in real if r["learning"] and r.get("feature_version")}
    batch_version = max(versions, key=L.version_num) if versions else None
    population = [r for r in real if r["learning"] and r.get("feature_version") == batch_version] if batch_version else []
    out = {"rows": real, "batches": [], "pending": {}, "comparison_count": 0, "emitted_comparison_count": 0, "stage_shifts": {},
           "batch_feature_version": batch_version, "batch_population_n": len(population)}
    for ck, group in split_by_cohort(real).items():
        out["stage_shifts"][ck] = _count("%s -> %s" % ((r["learning"].get("stages") or {}).get("judge", "unknown"),
                                                       (r["learning"].get("stages") or {}).get("fill", "unknown")) for r in group)
    for ck, group in split_by_cohort(population).items():
        ident = group[0]["facts"].get("identity") or {}
        known_identity = all(ident.get(k) not in (None, "", "unknown") for k in ("cohort_id", "prompt_version"))
        out["pending"][ck] = len(group) % batch_n if known_identity else len(group)
        if not known_identity:
            continue
        out["comparison_count"] += len(L.LEARNING_TRAILS) * len(L.LEARNING_STAGE_NAMES)
        for start in range(0, len(group) - batch_n + 1, batch_n):
            block = group[start:start + batch_n]
            batch = {"cohort": ck, "batch": start // batch_n + 1, "n": len(block), "coin_n": len({r["mint"] for r in block}),
                     "keys": [r["key"] for r in block], "feature_version": batch_version,
                     "live_rails_known": sum(L._fin(_alt(r, "live_rails")) for r in block),
                     "known": sum(L._fin(r["learning"].get("realized_R_tape_model")) for r in block),
                     "censored": sum(bool(r["learning_censored"]) for r in block), "comparisons": [], "digest": digest(block)}
            for stage in L.LEARNING_STAGE_NAMES:
                band = [r for r in block if (r["learning"].get("stages") or {}).get("fill") == stage]
                for trail in L.LEARNING_TRAILS:
                    name = "trail_%dpct" % trail
                    pairs = [(r["mint"], _alt(r, name) - _alt(r, "live_rails")) for r in band
                             if L._fin(_alt(r, name)) and L._fin(_alt(r, "live_rails"))]
                    ci = _boot_ci(pairs)
                    batch["comparisons"].append({"stage": stage, "alternative": name, "n": len(band), "known": len(pairs),
                                                 "censored": len(band) - len(pairs), "coin_n": len({m for m, _ in pairs}),
                                                 "mean_diff_R": _r(ci[0]) if ci else _r(_mean(d for _, d in pairs)),
                                                 "ci95": [_r(ci[1]), _r(ci[2])] if ci else None})
            out["emitted_comparison_count"] += len(batch["comparisons"])
            out["batches"].append(batch)
    return out


def render_learning_loop(rows, batch_n=20):
    d = learning_loop(rows, batch_n=batch_n)
    lines = ["## Desk learning loop", "", "_descriptive; no rule change from a digest alone_", "",
             "Model contract when proof exists: basis=tape_model_net_after_known_venue_costs; all_in_net=unknown. Native venue fees and clip impact, "
             "plus residual_model_cost_pp=2.5; entry_lag_s=1.5; return_horizon_s=300; existing exit lag=1s. "
             "R values are fractions, not percentage points. These are the desk's selected realized positions; hindsight bounds are descriptive.",
             "Trail alternatives are rails + trail 10/15/20%; booked SL/TP retained.",
             "Batches follow immutable stored arrival sequence within cohort/prompt; they set cadence only. "
             "Unknown cohort/prompt rows never produce a proposal batch. Missing learning facts are unavailable.",
             "Batch feature version: %s; population n=%d realized positions with learning facts from that version only; "
             "display n=%d latest-per-key positions, including older unavailable learning evidence." % (
                 d["batch_feature_version"] or "unavailable", d["batch_population_n"], len(d["rows"])), "",
             "| position | coin | cohort / prompt | close (corrected UTC) | sheet -> fill stage (recorded USD cap) | "
             "watch entry upper bound (hindsight: SOL/token @ corrected UTC; regret %) | R_fill / R_response / R_touch | "
             "realized_R_tape_model | exit alternatives R | censored / unavailable |",
             "|---|---|---|---|---|---|---|---|---|---|"]

    def number(x):
        return str(x) if L._fin(x) else "unavailable"

    def utc(x):
        return datetime.fromtimestamp(x, timezone.utc).isoformat() if L._fin(x) else "unavailable"

    alternatives = ("live_rails",) + tuple("trail_%dpct" % t for t in L.LEARNING_TRAILS) + tuple("fixed_%ds" % t for t in L.FIXED_EXITS) + (
        "hold300_cat", "hold_plan", "no_exit_mark900", "sell_before_migration")
    trail_labels = {"trail_%dpct" % t: "rails + trail %d%%" % t for t in L.LEARNING_TRAILS}
    for row in d["rows"]:
        loop = row["learning"]
        stages, returns = loop.get("stages") or {}, loop.get("returns") or {}
        bound = (loop.get("watch_entry") or {}).get("upper_bound_hindsight")
        watch = "%s @ %s; %s%%" % (number(bound.get("price_sol_per_token")), utc(bound.get("entry_at_utc")), number(bound.get("regret_pct"))) if bound else "unavailable"
        stage = "%s (%s) -> %s (%s)" % (stages.get("judge", "unknown"), number(stages.get("judge_mcap_usd")),
                                            stages.get("fill", "unknown"), number(stages.get("fill_mcap_usd")))
        alt_text = []
        for name in alternatives:
            a = (loop.get("alternatives") or {}).get(name) or {}
            value = _alt(row, name) if loop else None
            suffix = " [mark only]" if a.get("mark_only") else ""
            if not L._fin(value):
                suffix += " [%s]" % (a.get("reason") or "censored_or_absent")
            alt_text.append("%s=%s%s" % (trail_labels.get(name, name), number(_r(value)), suffix))
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            row["key"], row["mint"], cohort_key(row), utc(loop.get("closed_at_corrected")), stage, watch,
            " / ".join(number(_r(returns.get(k))) for k in ("R_fill", "R_response", "R_touch")),
            number(_r(loop.get("realized_R_tape_model"))), "; ".join(alt_text), ", ".join(row["learning_censored"]) or "none"))
    if not d["rows"]:
        lines.append("_No stored realized positions._")
    lines += ["", "### Sheet-to-fill stage shifts (all stored realized positions, per cohort/prompt)"]
    for ck, shifts in d["stage_shifts"].items():
        lines.append("- %s: %s" % (ck, json.dumps(shifts, sort_keys=True)))
    lines += ["", "Trail-grid comparison scope: %d known cohort/prompt groups x %d trails x %d fill bands = %d configured comparisons; "
              "%d batch-cell comparisons emitted across completed batches. "
              "Coin-clustered paired 95%% intervals are nominal, descriptive, and unadjusted; missing live rails censor the pair. "
              "Other timing/exit views and repeated batch looks are outside this grid count." % (
                  d["comparison_count"] // (len(L.LEARNING_TRAILS) * len(L.LEARNING_STAGE_NAMES)), len(L.LEARNING_TRAILS),
                  len(L.LEARNING_STAGE_NAMES), d["comparison_count"], d["emitted_comparison_count"]),
              "", "### Stored-close batches: hypothesis, needs prereg + holdout"]
    for batch in d["batches"]:
        lines += ["", "#### %s | batch %d" % (batch["cohort"], batch["batch"]),
                  "n=%d; usable booked-rails baseline=%d/%d; known realized tape R=%d; censored rows=%d; distinct coins=%d." % (
                      batch["n"], batch["live_rails_known"], batch["n"], batch["known"], batch["censored"], batch["coin_n"]),
                  "| fill band | trail vs live rails | n | known pairs | censored / absent | coins in pairs | mean delta R | coin-clustered 95% interval |",
                  "|---|---|---|---|---|---|---|---|"]
        for c in batch["comparisons"]:
            lines.append("| %s | %s | %d | %d | %d | %d | %s | %s |" % (
                c["stage"], trail_labels[c["alternative"]], c["n"], c["known"], c["censored"], c["coin_n"], number(c["mean_diff_R"]),
                json.dumps(c["ci95"]) if c["ci95"] else "unavailable (fewer than 2 coins)"))
        for m in batch["digest"]["mistakes"]:
            lines.append("- %s: hypothesis, needs prereg + holdout. %s; evidence: %s" % (
                m["tag"], m["proposal"]["rule"], json.dumps(m["evidence"], sort_keys=True)))
    if not d["batches"]:
        lines.append("_No complete known-identity batch._")
    lines += ["", "Pending stored closes (not a proposal batch): %s" % json.dumps(d["pending"], sort_keys=True), ""]
    return lines


def render_markdown(d, day=None):
    c = d["counts"]
    L_ = ["## Lessons digest" + (" (%s)" % day if day else ""), "",
          "_Report only. Proposals are for the operator; nothing here applies or tunes anything. Realized rows are the trades WE took; "
          "counterfactual rows are tape hindsight. n = realized %d, refused/never-attempted %d, sampled skips %d; feature version(s) %s; "
          "censored rows: %s; distinct coins: %s._" % (c["realized"], c["counterfactual"], c["skip_counterfactual"],
                                                       ", ".join(d.get("feature_versions") or []) or "none", json.dumps(d.get("censored_rows")),
                                                       json.dumps(d.get("distinct_mints"))), ""]
    band = d.get("good_exit_band")
    if band and band.get("median_regret_R") is not None:
        L_.append("good-exit band (running median exit regret, derived): %.3f R over n=%d earlier trades" % (band["median_regret_R"], band["n"]))
    if d["tags"]:
        L_ += ["", "| tag | n | coins | realized USD | mean R | waiting would have saved USD (hindsight, known n) | exit regret USD (hindsight, known n) |",
               "|---|---|---|---|---|---|---|"]
        known = lambda usd, k, n: "unavailable (known 0/%d)" % n if not k else "%s (known %d/%d)" % (usd, k, n)
        for tag, v in d["tags"].items():
            L_.append("| %s | %d | %d | %s | %s | %s | %s |" % (tag, v["n"], v["distinct_mints"], v["pnl_usd"], v["mean_R"],
                                                              known(v["entry_regret_usd_hindsight"], v["entry_regret_known_n"], v["n"]),
                                                              known(v["exit_regret_usd_hindsight"], v["exit_regret_known_n"], v["n"])))
        L_.append("")
        L_.append("_%s_" % d["tag_usd_note"])
    else:
        L_.append("_No tagged realized trade yet._")
    L_ += ["", "### Top recurring mistakes (exploratory: retrospective tags are hypotheses, each proposal states what would be tested forward)"]
    if not d["mistakes"]:
        L_.append("_none yet_")
    for m in d["mistakes"]:
        p = m["proposal"]
        L_ += ["- **%s** (n=%d, %s)" % (m["tag"], m["n"], m["status"]),
               "  - decision-time feature: %s" % p["feature"], "  - frozen rule: %s" % p["rule"], "  - comparator: %s" % p["comparator"],
               "  - forward cohort: %s" % p["forward"], "  - evidence: %s" % json.dumps(m["evidence"], sort_keys=True)]
    s = d["selection"]
    L_ += ["", "### What selection and admission missed (hindsight on the tape; shares over rows with a known value, censored shown)",
           "- entered (realized): %s" % json.dumps(s["entered"], sort_keys=True),
           "- refused / never attempted ENTERs: %s" % json.dumps(s["refused_or_never_attempted"], sort_keys=True),
           "- sampled JUDGE SKIPs / never-vetted PASSes: %s" % json.dumps(s["sampled_skips"], sort_keys=True),
           "- skip reasons (sampled): %s" % json.dumps(s["skip_reasons"], sort_keys=True), ""]
    return L_
