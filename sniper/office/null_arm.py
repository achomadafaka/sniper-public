"""Office Task 8: the primary null arm (Gamma prereg section 6), frozen and non-LLM. On the SAME opportunities in order of arrival it enters every eligible coin at the fixed clip at
first_eligible_at + l_null_s, under the SAME capacity limits (max concurrent, daily loss), with the injected fill model (same model, same cost bound B, exit rails pinned at the epoch's version).
No decision, verdict, memo or agent input exists in its signature. Pure: no clock, no store, no tape.
Per opportunity -> {"candidate_seq", "state", "entry_t", "exit_t", "n"} (+ "reason" when censored). states: entered | capacity | daily_loss | censored.
A skipped coin (capacity, daily_loss) has n = 0.0 (a real outcome of the policy). A CENSORED fill has n = None, never 0, and keeps its slot occupied for CENSORED_SLOT_RELEASE_S after entry (contract section 6).
`fill(path, entry_t, clip_usd)` -> {"pnl_usd": float, "exit_t": float} | {"censored": reason}."""
from dataclasses import dataclass
import math
from collections.abc import Mapping
from types import MappingProxyType
from sniper.office.ledger import ENTRY_DELAY_S

DAY_S = 86400.0
CENSORED_SLOT_RELEASE_S = 1200.0   # Alpha ruling 2026-10-31, measurement contract section 6; holding forever locked the arm


@dataclass(frozen=True)
class Policy:
    clip_usd: float
    max_concurrent: int
    daily_loss_usd: float
    l_null_s: float | Mapping

    def __post_init__(self):
        if type(self.max_concurrent) is not int or self.max_concurrent < 1 or any(
                type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in (self.clip_usd, self.daily_loss_usd)):
            raise ValueError("invalid_null_capacity_policy")
        if isinstance(self.l_null_s, Mapping):
            object.__setattr__(self, "l_null_s", MappingProxyType(dict(self.l_null_s)))


def submission_time(first_eligible_at, latency_s):
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (first_eligible_at, latency_s)) or latency_s < ENTRY_DELAY_S:
        raise ValueError("infeasible_null_latency")
    target = first_eligible_at + latency_s - ENTRY_DELAY_S
    if not math.isfinite(target) or target < first_eligible_at:
        raise ValueError("infeasible_null_latency")
    return target


def run(opps, policy, fill):
    out = []
    open_exits = []                       # exit times of positions still open (entry + release bound for a censored one)
    realized = []                         # (exit_t, pnl) of closed positions
    scheduled = []
    for o in opps:
        delay = policy.l_null_s.get(o.get("shift_id")) if isinstance(policy.l_null_s, Mapping) else policy.l_null_s
        try:
            target = submission_time(o["first_eligible_at"], delay) + ENTRY_DELAY_S
        except ValueError:
            out.append({"candidate_seq": o["candidate_seq"], "entry_t": None, "exit_t": None,
                        "state": "infeasible", "n": None, "reason": "infeasible_null_latency"})
            continue
        scheduled.append((target, o))
    for t, o in sorted(scheduled, key=lambda pair: (pair[0], pair[1]["first_eligible_at"], pair[1]["candidate_seq"])):
        day = t // DAY_S
        open_exits = [e for e in open_exits if e > t]
        base = {"candidate_seq": o["candidate_seq"], "entry_t": t, "exit_t": None}
        day_pnl = sum(p for e, p in realized if e <= t and e // DAY_S == day)
        if day_pnl <= -policy.daily_loss_usd:
            out.append({**base, "state": "daily_loss", "n": 0.0})
            continue
        if len(open_exits) >= policy.max_concurrent:
            out.append({**base, "state": "capacity", "n": 0.0})
            continue
        r = fill(o["path"], t, policy.clip_usd)
        if "censored" in r:
            open_exits.append(t + CENSORED_SLOT_RELEASE_S)
            out.append({**base, "state": "censored", "n": None, "reason": r["censored"]})
            continue
        open_exits.append(r["exit_t"])
        realized.append((r["exit_t"], r["pnl_usd"]))
        out.append({**base, "state": "entered", "exit_t": r["exit_t"], "n": r["pnl_usd"]})
    return sorted(out, key=lambda row: row["candidate_seq"])
