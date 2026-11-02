"""Two-stage boundary-rule calibration: select on a calibration set, evaluate ONCE on an untouched
holdout. Prevents in-sample selection of the boundary rule.

Input rows are per-window comparisons (``price_to_beat_resolver.compare_to_oracle`` outputs):
``{rule: {"abs_error": Decimal|None, "missing": bool}}``. Selection uses raw ``abs_error`` so the
tolerance is chosen on the calibration set, not after seeing the holdout.

The module never mutates ``price_to_beat_resolver.LOCKED_BOUNDARY_RULE`` — a ``"locked"`` status is a
recommendation for an operator to apply.
"""
from __future__ import annotations

from decimal import Decimal

from .price_to_beat_resolver import CANDIDATE_RULES

# match rates reported at these precisions; selection prefers the strictest that clears the gate
PRECISIONS = (Decimal("0.0001"), Decimal("0.001"), Decimal("0.01"))


def _rule_stats(rows: list, *, precision: Decimal):
    """Per-rule ``(n_usable, n_matched)`` at a precision."""
    precision = Decimal(precision)
    stats = {}
    for rule in CANDIDATE_RULES:
        usable = [r[rule] for r in rows if r.get(rule) and not r[rule].get("missing")
                  and r[rule].get("abs_error") is not None]
        matched = sum(1 for c in usable if Decimal(c["abs_error"]) <= precision)
        stats[rule] = (len(usable), matched)
    return stats


def match_rates(rows: list, *, precision: Decimal) -> dict:
    """Per-rule fraction of USABLE windows whose abs_error <= precision (missing windows excluded)."""
    return {rule: (m / n if n else None) for rule, (n, m) in _rule_stats(rows, precision=precision).items()}


def _has_any_candidate(row: dict) -> bool:
    """A window is usable for calibration only if it has at least one reconstructed candidate (a
    fully-missing window is not a usable resolved window and must not consume a cal/holdout slot)."""
    return any(row.get(rule) and not row[rule].get("missing") for rule in CANDIDATE_RULES)


def multi_precision_report(rows: list) -> dict:
    """``{precision: {rule: match_rate}}`` across the reported precisions."""
    return {p: match_rates(rows, precision=p) for p in PRECISIONS}


def select_rule(rows: list, *, gate: float = 0.99, min_usable: int = 1):
    """Choose ``(rule, precision)`` on the calibration set: the strictest precision at which some rule
    clears ``gate`` with at least ``min_usable`` usable windows (no n=1 locks), and among those the
    highest match-rate (ties broken by candidate order). ``(None, None)`` if none qualifies."""
    for precision in PRECISIONS:          # strictest first
        stats = _rule_stats(rows, precision=precision)
        clearing = [(rule, m / n) for rule in CANDIDATE_RULES
                    for (n, m) in [stats[rule]]
                    if n >= min_usable and (m / n) >= gate]
        if clearing:
            best = max(clearing, key=lambda rc: rc[1])   # ties -> candidate order (stable)
            return best[0], precision
    return None, None


def evaluate_holdout(rows: list, *, rule: str, precision: Decimal) -> dict:
    """Single evaluation of a frozen ``(rule, precision)`` on the holdout set."""
    precision = Decimal(precision)
    usable = [r[rule] for r in rows if r.get(rule) and not r[rule].get("missing")
              and r[rule].get("abs_error") is not None]
    matched = sum(1 for c in usable if Decimal(c["abs_error"]) <= precision)
    return {"n_usable": len(usable), "n_missing": len(rows) - len(usable),
            "match_rate": (matched / len(usable)) if usable else None}


def two_stage(rows: list, *, cal_size: int = 100, gate: float = 0.99,
              min_usable: int = None) -> dict:
    """Two-stage boundary-rule lock. Uses the first ``cal_size`` USABLE resolved windows as the
    calibration set and the next ``cal_size`` as an untouched holdout (fully-missing windows are
    dropped first so they can't consume a slot). Rows are sorted by ``window_start_ts`` when present
    so "first/next" is a true out-of-time split; otherwise the caller MUST pass time-ordered windows.
    A rule must clear ``gate`` with >= ``min_usable`` usable windows on BOTH cal and holdout to lock —
    preventing a degenerate (e.g. n=1) lock. Status: ``insufficient_windows`` / ``no_rule`` /
    ``insufficient_usable`` / ``locked`` / ``rejected``."""
    if min_usable is None:
        min_usable = max(1, int(cal_size * 0.8))
    usable = [r for r in rows if _has_any_candidate(r)]
    if usable and all("window_start_ts" in r for r in usable):
        usable = sorted(usable, key=lambda r: r["window_start_ts"])

    cal, holdout = usable[:cal_size], usable[cal_size:cal_size * 2]
    if len(cal) < cal_size or len(holdout) < cal_size:
        return {"status": "insufficient_windows", "selected_rule": None, "selected_precision": None,
                "have_cal": len(cal), "have_holdout": len(holdout), "need_each": cal_size}

    rule, precision = select_rule(cal, gate=gate, min_usable=min_usable)
    cal_report = multi_precision_report(cal)
    if rule is None:
        return {"status": "no_rule", "selected_rule": None, "selected_precision": None,
                "calibration_match_rates": cal_report, "min_usable": min_usable}

    holdout_result = evaluate_holdout(holdout, rule=rule, precision=precision)
    enough = holdout_result["n_usable"] >= min_usable
    passed = enough and holdout_result["match_rate"] is not None and holdout_result["match_rate"] >= gate
    status = "locked" if passed else ("insufficient_usable" if not enough else "rejected")
    return {
        "status": status,
        "selected_rule": rule,
        "selected_precision": precision,
        "gate": gate,
        "min_usable": min_usable,
        "calibration_match_rates": cal_report,
        "holdout": holdout_result,
        "cal_size": cal_size,
    }
