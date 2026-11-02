"""Batch runner — drives the thesis-extractor conveyor at volume over a NON-survivorship
feed. candidate (Becker, opened-count) → fetch → profile → admission/enroll → (optional) D4.

Guardrails (spec 2026-05-30-batch-runner-spec.md):
- no profit/rank filter (the feed floors on opened activity, outcome-neutral);
- enrollment card input is capped at `entry_ts ≤ as_of` (no forward leakage) [I7];
- dry-run is the DEFAULT (store=None → no persistence); `--persist` passes a Supabase store;
- counts-only output; checkpoint after every wallet; the batch writes ONLY
  paper_card_enrollments (via the store) + its checkpoint;
- a Null Calibration Gate (within-wallet outcome-shuffle MC) [I6] is provided separately —
  no gold-board claim until the real promotion rate clears 2× the MC null.
"""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from sniper.analysis.thesis_cards.becker_feed import Candidate
from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore, enroll_profile
from sniper.analysis.thesis_cards.forward_tracking import run_forward_tracking
from sniper.analysis.thesis_cards.forward_window_provider import (
    WalletForwardWindowProvider, _row_is_negrisk, sizing_row_core,
)
from sniper.analysis.thesis_cards.profile import assemble_profile

PositionFetcher = Callable[[str], "Sequence[Mapping[str, Any]] | None"]


@dataclass
class BatchCounts:
    wallets_scanned: int = 0
    eligible: int = 0
    profiles_built: int = 0
    cards_admitted: int = 0
    cards_rejected: int = 0
    enrollments_queued: int = 0
    failed: int = 0
    d4_advanced: dict = field(default_factory=dict)            # new_state -> count
    by_activity_tier: dict = field(default_factory=lambda: {
        "floor_2x": {"eligible": 0, "promoted": 0}, "above_2x": {"eligible": 0, "promoted": 0}})
    unique_wallets_promoted: set = field(default_factory=set)  # I4: unique, not total

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["unique_wallets_promoted"] = len(self.unique_wallets_promoted)
        return d


class InMemoryCheckpoint:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def done_wallets(self) -> set[str]:
        return {w for w, r in self.records.items() if r.get("status") == "done"}

    def record(self, wallet: str, status: str, counts: dict) -> None:
        self.records[wallet] = {"wallet": wallet, "status": status, "counts": counts}


class JsonlCheckpoint(InMemoryCheckpoint):
    """File-backed per-wallet status; rerun skips `done`, retries `failed`/missing."""
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        self.records[r["wallet"]] = r

    def record(self, wallet: str, status: str, counts: dict) -> None:
        super().record(wallet, status, counts)
        with open(self.path, "a") as f:
            f.write(json.dumps(self.records[wallet]) + "\n")


def _tier(opened: int, min_opened: int) -> str:
    return "above_2x" if opened > 2 * min_opened else "floor_2x"


def _wallet_negrisk_predicate(raw: Sequence[Mapping[str, Any]],
                              is_negrisk: Callable[[Mapping], bool],
                              negrisk_lookup: "Callable[[list[str]], set[str]] | None"
                              ) -> Callable[[Mapping], bool]:
    """Per-wallet negRisk predicate: enrich from this wallet's condition_ids (cached Gamma
    lookup) so the in-sample card is hedge-clean. Falls back to the row-key detector."""
    if negrisk_lookup is None:
        return is_negrisk
    cids = sorted({str(p.get("conditionId")) for p in raw if p.get("conditionId")})
    negset = set(negrisk_lookup(cids))
    return lambda r, _ns=negset: _row_is_negrisk(r) or (r.get("conditionId") in _ns)


def map_enrollment_positions(raw: Sequence[Mapping[str, Any]], as_of: int,
                             is_negrisk: Callable[[Mapping], bool]) -> list[dict]:
    """Sizing rows for positions entered at/before `as_of` (no forward leakage) [I7]."""
    out = []
    for p in raw:
        core = sizing_row_core(p, is_negrisk)
        if core is None:
            continue
        ts, row = core
        if ts <= as_of:
            out.append(row)
    return out


def run_batch(
    candidates: Sequence[Candidate],
    *,
    fetch_positions: PositionFetcher,
    as_of: int,
    min_opened: int = 30,
    store: Any = None,                               # None = DRY RUN (no persistence)
    run_forward: bool = False,
    now_schedule: Sequence[int] | None = None,       # D4 replay points (two-pass slicing)
    is_negrisk: Callable[[Mapping], bool] = _row_is_negrisk,
    negrisk_lookup: "Callable[[list[str]], set[str]] | None" = None,
    checkpoint: InMemoryCheckpoint | None = None,
    max_wallets: int | None = None,
) -> BatchCounts:
    counts = BatchCounts()
    done = checkpoint.done_wallets() if checkpoint else set()
    tier_by_wallet: dict[str, str] = {}
    selected = candidates if max_wallets is None else list(candidates)[:max_wallets]

    for cand in selected:
        if cand.wallet in done:
            continue
        counts.wallets_scanned += 1
        counts.eligible += 1                          # feed already floored -> every candidate eligible
        tier = _tier(cand.opened, min_opened)
        tier_by_wallet[cand.wallet] = tier
        counts.by_activity_tier[tier]["eligible"] += 1
        try:
            raw = fetch_positions(cand.wallet)
            if raw is None:                           # fetch failed -> retry on rerun
                counts.failed += 1
                if checkpoint:
                    checkpoint.record(cand.wallet, "failed", {"reason": "fetch_none"})
                continue
            # hedge-clean the IN-SAMPLE card too (same cached Gamma lookup as the D4 window)
            wallet_is_negrisk = _wallet_negrisk_predicate(raw, is_negrisk, negrisk_lookup)
            positions = map_enrollment_positions(raw, as_of, wallet_is_negrisk)
            profile = assemble_profile(cand.wallet, positions)
            counts.profiles_built += 1
            res = enroll_profile(profile, store=store, enrolled_at=as_of)
            admitted = sum(1 for r in res.admission_log if r.decision == "admitted")
            counts.cards_admitted += admitted
            counts.cards_rejected += len(res.admission_log) - admitted
            counts.enrollments_queued += len(res.enrollments)
            if checkpoint:
                checkpoint.record(cand.wallet, "done",
                                  {"admitted": admitted, "enrolled": len(res.enrollments),
                                   "opened": cand.opened})
        except Exception as exc:                      # noqa: BLE001 — isolate per-wallet failures
            counts.failed += 1
            if checkpoint:
                checkpoint.record(cand.wallet, "failed", {"error": str(exc)})

    if run_forward and store is not None:
        provider = WalletForwardWindowProvider(fetch_positions, negrisk_lookup=negrisk_lookup)
        for now in (now_schedule or [as_of]):
            for key, outcome in run_forward_tracking(store, provider, now=now):
                counts.d4_advanced[outcome] = counts.d4_advanced.get(outcome, 0) + 1
                if outcome == "promoted":
                    wallet = key.split("|", 1)[0]
                    counts.unique_wallets_promoted.add(wallet)
                    t = tier_by_wallet.get(wallet)
                    if t:
                        counts.by_activity_tier[t]["promoted"] += 1
    return counts


# --------------------------------------------------------------------------------------
# Null Calibration Gate [I6] — within-wallet outcome-shuffle Monte Carlo.
# --------------------------------------------------------------------------------------

def _shuffle_fetcher(fetch_positions: PositionFetcher, as_of: int, seed: int) -> PositionFetcher:
    """Wrap a fetcher so each wallet's FORWARD (entry_ts > as_of) won labels are permuted
    deterministically (count + timing preserved), destroying any real size↔outcome link."""
    def fetch(wallet: str):
        raw = fetch_positions(wallet)
        if raw is None:
            return None
        fwd_idx = [i for i, p in enumerate(raw) if _safe_ts(p) is not None and _safe_ts(p) > as_of]
        wons = [_safe_won(raw[i]) for i in fwd_idx]
        rng = random.Random((hash((wallet, seed)) & 0x7FFFFFFF))
        rng.shuffle(wons)
        out = [dict(p) for p in raw]
        for i, w in zip(fwd_idx, wons):
            if w is not None:
                out[i]["curPrice"] = 1.0 if w == 1 else 0.0      # re-encode the shuffled label
        return out
    return fetch


def _safe_ts(p: Mapping[str, Any]) -> int | None:
    try:
        return int(p["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None


def _safe_won(p: Mapping[str, Any]) -> int | None:
    try:
        return 1 if float(p["curPrice"]) >= 0.97 else 0
    except (KeyError, TypeError, ValueError):
        return None


def _population_promoted(
    candidates: Sequence[Candidate], *, enroll_fetch: PositionFetcher,
    forward_fetch: PositionFetcher, as_of: int, now_schedule: Sequence[int],
    is_negrisk: Callable[[Mapping], bool] = _row_is_negrisk,
    negrisk_lookup: "Callable[[list[str]], set[str]] | None" = None,
) -> tuple[int, int]:
    """(promoted, eligible) over a wallet group: build the real (pre-as_of) card from
    `enroll_fetch`, advance D4 across `now_schedule` with `forward_fetch`. The two fetchers
    differ only for the null (forward labels shuffled). Enrollment + forward are hedge-clean
    via `negrisk_lookup` so real and null share the same cleaning."""
    promoted = eligible = 0
    for cand in candidates:
        raw = enroll_fetch(cand.wallet)
        if raw is None:
            continue
        eligible += 1
        store = InMemoryPaperEnrollmentStore()
        wallet_is_negrisk = _wallet_negrisk_predicate(raw, is_negrisk, negrisk_lookup)
        positions = map_enrollment_positions(raw, as_of, wallet_is_negrisk)
        enroll_profile(assemble_profile(cand.wallet, positions), store=store, enrolled_at=as_of)
        provider = WalletForwardWindowProvider(forward_fetch, negrisk_lookup=negrisk_lookup)
        for now in now_schedule:
            run_forward_tracking(store, provider, now=now)
        if any(e.state == "promoted" for e in store.records.values()):
            promoted += 1
    return promoted, eligible


def null_calibration_rate(
    candidates: Sequence[Candidate],
    *,
    fetch_positions: PositionFetcher,
    as_of: int,
    now_schedule: Sequence[int],
    seed: int = 0,
    min_opened: int = 30,
    is_negrisk: Callable[[Mapping], bool] = _row_is_negrisk,
    negrisk_lookup: "Callable[[list[str]], set[str]] | None" = None,
    max_wallets: int | None = None,
) -> dict:
    """Run the FULL D4 path on outcome-shuffled forward labels; return the null promotion rate.

    For each wallet: build the real (pre-`as_of`) card, then advance D4 with the wallet's
    forward win/loss labels permuted. A real-data promotion rate must exceed 2× this [I6]."""
    selected = candidates if max_wallets is None else list(candidates)[:max_wallets]
    shuffled = _shuffle_fetcher(fetch_positions, as_of, seed)
    promoted, eligible = _population_promoted(
        selected, enroll_fetch=fetch_positions, forward_fetch=shuffled,
        as_of=as_of, now_schedule=now_schedule, is_negrisk=is_negrisk,
        negrisk_lookup=negrisk_lookup)
    rate = promoted / eligible if eligible else 0.0
    return {"null_promotion_rate": rate, "promoted": promoted, "eligible": eligible}


def replication_check(
    candidates: Sequence[Candidate],
    *,
    fetch_positions: PositionFetcher,
    as_of: int,
    now_schedule: Sequence[int],
    seed: int = 0,
    is_negrisk: Callable[[Mapping], bool] = _row_is_negrisk,
    negrisk_lookup: "Callable[[list[str]], set[str]] | None" = None,
    max_wallets: int | None = None,
) -> dict:
    """Cross-wallet replication (the second-stage test): does the forward-edge PHENOMENON
    hold in two DISJOINT wallet halves, each beating its own null?

    A single D4 promotion is within-wallet persistence (not a generalizable edge). This
    splits the population deterministically (hash-ordered) into halves A/B and, for each,
    measures the real vs outcome-shuffle-null promotion rate. `replicates` is True only if
    BOTH halves clear 2× their own null — i.e. the edge reproduces across independent
    wallets, not just one. (This is the population-level harness; characterizing WHICH
    behavioral features transfer is a further step that needs a real promoted cohort.)"""
    selected = candidates if max_wallets is None else list(candidates)[:max_wallets]
    ordered = sorted(selected, key=lambda c: _h_wallet(c.wallet))
    mid = len(ordered) // 2
    halves = {"a": ordered[:mid], "b": ordered[mid:]}
    out: dict = {}
    for name, grp in halves.items():
        shuffled = _shuffle_fetcher(fetch_positions, as_of, seed)
        real_p, n = _population_promoted(grp, enroll_fetch=fetch_positions,
                                         forward_fetch=fetch_positions, as_of=as_of,
                                         now_schedule=now_schedule, is_negrisk=is_negrisk,
                                         negrisk_lookup=negrisk_lookup)
        null_p, _ = _population_promoted(grp, enroll_fetch=fetch_positions,
                                         forward_fetch=shuffled, as_of=as_of,
                                         now_schedule=now_schedule, is_negrisk=is_negrisk,
                                         negrisk_lookup=negrisk_lookup)
        real_rate = real_p / n if n else 0.0
        null_rate = null_p / n if n else 0.0
        out[name] = {"n": n, "real_rate": real_rate, "null_rate": null_rate,
                     "beats_null": real_rate > 2 * null_rate}
    out["replicates"] = bool(out["a"]["beats_null"] and out["b"]["beats_null"])
    return out


def _h_wallet(s: str) -> int:
    import hashlib
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)
