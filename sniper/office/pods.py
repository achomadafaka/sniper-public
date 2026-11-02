"""Office pods: the frozen, crew-owned reallocation rule (config/office/pods.json; spec §2.6, prereg Am3;
docs/superpowers/specs/2026-10-29-office-pods-reallocation-rule.md). Pure: no clock, no DB. The caller books the plan
with ledger.transfer. Unknown input always plans nothing, never a guess."""
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path


def _num(v):
    return type(v) in (int, float) and math.isfinite(v)


def _cents(v):
    return math.floor(v * 100 + 1e-9) / 100


def book_id(epoch_id: str, pod: str) -> str:
    return "%s:%s" % (epoch_id, pod)


def load_rule(path) -> dict:
    return validate_rule(json.loads(Path(path).read_text()))


def validate_rule(rule) -> dict:
    s = rule.get("score") if isinstance(rule, dict) else None
    ok = (isinstance(rule, dict) and isinstance(rule.get("version"), str) and rule["version"]
          and isinstance(s, dict) and s.get("metric") == "realized_net_usd"
          and type(s.get("window_periods")) is int and s["window_periods"] >= 1
          and type(s.get("min_closed")) is int and s["min_closed"] >= 1
          and all(_num(rule.get(k)) for k in ("step_pct", "floor_pct", "min_transfer_usd", "daily_loss_usd_total"))
          and 0 < rule["step_pct"] <= 50 and 0 <= rule["floor_pct"] < 50 and rule["min_transfer_usd"] > 0
          and rule["daily_loss_usd_total"] > 0
          and type(rule.get("max_concurrent_total")) is int and rule["max_concurrent_total"] >= 1)
    if not ok:
        raise ValueError("pods_rule_invalid")
    return rule


def initial_capital(rule, pods, total_usd) -> dict:
    pods = sorted(pods)
    if not pods or len(set(pods)) != len(pods) or not _num(total_usd) or total_usd <= 0:
        raise ValueError("pods_initial_invalid")
    each = _cents(total_usd / len(pods))
    out = {p: each for p in pods}
    out[pods[0]] = round(total_usd - each * (len(pods) - 1), 2)
    return out


def _score(rule, v):
    if not isinstance(v, tuple) or len(v) != 2:
        return None
    usd, n = v
    return usd if _num(usd) and type(n) is int and n >= rule["score"]["min_closed"] else None


def plan_reallocation(rule, *, epoch_id: str, period_key: str, scores: dict, capital: dict, free_cash: dict) -> list:
    """At most one transfer per period_key, worst pod to best pod. scores {pod: (realized_net_usd, closed_n)}."""
    if not (isinstance(epoch_id, str) and epoch_id and isinstance(period_key, str) and period_key) or len(scores) < 2:
        return []
    s = {p: _score(rule, v) for p, v in scores.items()}
    if (set(s) != set(capital) or set(s) != set(free_cash) or any(v is None for v in s.values())
            or any(not _num(capital[p]) or not _num(free_cash[p]) for p in s)):
        return []
    order = sorted(s, key=lambda p: (-s[p], p))
    best = order[0]
    worst = min(s, key=lambda p: (s[p], p))
    if s[best] == s[worst]:
        return []
    total = sum(capital[p] for p in s)
    usd = _cents(min(rule["step_pct"] * total / 100, capital[worst] - rule["floor_pct"] * total / 100, free_cash[worst]))
    if usd < rule["min_transfer_usd"]:
        return []
    key = "|".join((rule["version"], epoch_id, period_key)).encode()
    return [dict(transfer_id=hashlib.sha256(key).hexdigest()[:32], from_book=book_id(epoch_id, worst),
                 to_book=book_id(epoch_id, best), usd=usd, epoch_id=epoch_id, rule_version=rule["version"])]


def _apportion(total, weights, rank, quantum, one_each):
    """Largest remainder: exactly `total` (in units of `quantum`) split by weight, one unit first to each pod by rank
    while units last, remainders ties by rank. sum(out) == total always (Gamma ruling 2026-10-29)."""
    units = round(total / quantum)
    out = {p: 0 for p in rank}
    for p in rank[:units] if one_each else ():
        out[p] = 1
    rest = units - sum(out.values())
    w = sum(Fraction(weights[p]) for p in rank)       # exact rationals: no float overshoot, left is never negative
    exact = {p: rest * Fraction(weights[p]) / w for p in rank}
    for p in rank:
        out[p] += math.floor(exact[p])
    left = units - sum(out.values())
    for p in sorted(rank, key=lambda p: (-(exact[p] - math.floor(exact[p])), rank.index(p)))[:left]:
        out[p] += 1
    return {p: out[p] * quantum for p in rank}


def pod_caps(rule, capital: dict) -> dict:
    """Capacity by capital share, never the clip. The rule's totals are hard ceilings: per-pod max_concurrent and
    daily_loss_usd each sum exactly to max_concurrent_total and daily_loss_usd_total. A pod with 0 slots cannot
    enter (only when there are more pods than slots)."""
    if not capital or any(not _num(v) or v < 0 for v in capital.values()) or sum(capital.values()) <= 0:
        raise ValueError("pods_capital_invalid")
    total = sum(capital.values())
    rank = sorted(capital, key=lambda p: (-capital[p], p))
    slots = _apportion(rule["max_concurrent_total"], capital, rank, 1, True)
    cents = _apportion(rule["daily_loss_usd_total"], capital, rank, 0.01, False)
    return {p: dict(max_concurrent=int(slots[p]), daily_loss_usd=round(cents[p], 2),
                    model_share=round(capital[p] / total, 6), pick_rank=rank.index(p) + 1) for p in capital}
