"""Checkpointed, idempotent batch over candidate winners. Dry-run by default; no
production DB mutation, no live trading. Each profile is stamped with its intake provenance
(source + survivorship risk)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet


@dataclass
class InMemoryProfileStore:
    profiles: dict[str, dict] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)

    def done_wallets(self) -> set[str]:
        return {wallet for wallet, status in self.statuses.items() if status == "done"}

    def record_done(self, wallet: str, profile: dict) -> None:
        self.profiles[wallet] = profile
        self.statuses[wallet] = "done"

    def record_failed(self, wallet: str, error: str) -> None:
        self.statuses[wallet] = f"failed:{error}"


def run_wallet_batch(
    candidates: list[CandidateWallet],
    *,
    run_profile: Callable[[str], dict[str, Any]],
    store: InMemoryProfileStore,
    max_wallets: int | None = None,
) -> dict[str, int]:
    counts = {"seen": 0, "ran": 0, "done": 0, "failed": 0, "skipped": 0}
    done = store.done_wallets()
    selected = candidates if max_wallets is None else candidates[:max_wallets]
    for candidate in selected:
        counts["seen"] += 1
        if candidate.wallet in done:
            counts["skipped"] += 1
            continue
        try:
            counts["ran"] += 1
            profile = run_profile(candidate.wallet)
            profile["intake"] = {
                "source": candidate.source,
                "selection_note": candidate.selection_note,
                "survivorship_risk": candidate.survivorship_risk,
            }
            store.record_done(candidate.wallet, profile)
            counts["done"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad wallet must not kill the batch
            store.record_failed(candidate.wallet, str(exc))
            counts["failed"] += 1
    return counts
