"""Canonical copy signal + chain confirmation semantics. A `processed` (Solana) or `reorged`
(EVM) observation can un-happen, so nothing downstream may ACT on a non-actionable state."""
from __future__ import annotations
from dataclasses import dataclass

# higher rank = stronger finality. 'actionable' = at least the chain's minimum trustable state.
_SOLANA = {"processed": 0, "confirmed": 1, "finalized": 2}
_EVM = {"observed": 0, "included": 1, "safe": 2, "confirmed": 2, "finalized": 3, "reorged": -1}
CONFIRMATION_RANK = {"solana": _SOLANA, "evm": _EVM}
_MIN_ACTIONABLE = {"solana": 1, "evm": 1}   # Solana>=confirmed, EVM>=included


def _chain_family(chain: str) -> str:
    return "solana" if str(chain).lower() == "solana" else "evm"


def is_actionable(state: str | None, chain: str) -> bool:
    fam = _chain_family(chain)
    rank = CONFIRMATION_RANK[fam].get((state or "").lower())
    return rank is not None and rank >= _MIN_ACTIONABLE[fam]


@dataclass(frozen=True)
class CopySignal:
    chain_id: str
    token_address: str
    source_wallet: str
    source_tx: str
    source_event_ts: str | None
    received_ts: str | None
    confirmation_state: str | None
    token_amount: float | None
    notional_estimate: float | None
    source_entry_price: float | None


def normalize(ev) -> CopySignal:
    token_address = getattr(ev, "mint", None)
    network_id = getattr(ev, "network_id", None)
    if not network_id:
        network_id = "evm-unknown" if str(token_address or "").startswith("0x") else "solana"
    return CopySignal(
        chain_id=network_id,
        token_address=token_address,
        source_wallet=getattr(ev, "source_user_id", None),
        source_tx=getattr(ev, "fomo_event_id", None),
        source_event_ts=getattr(ev, "source_ts", None),
        received_ts=getattr(ev, "received_at_normalized", None) or getattr(ev, "received_at", None),
        confirmation_state=getattr(ev, "confirmation_state", None),
        token_amount=getattr(ev, "token_amount", None),
        notional_estimate=getattr(ev, "their_size_usd", None),
        source_entry_price=getattr(ev, "their_price", None),
    )
