"""Typed, non-executing venue quote used to validate a price-triggered real exit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sniper.analysis.memecoin_quote_failures import QuoteFailure


def quote_evidence(payload, *, quote_asset, quote_asset_decimals, quote_asset_usd,
                   input_atomic=None, slippage_bps=None, venue=None):
    """Copy quote amounts and route identifiers without transaction bytes."""
    try:
        route = payload.get("route") or {}
        route_keys = ("protocol", "pool", "pool_id", "router", "curve", "path", "fee", "request_id", "pool_ids")
        plan = payload.get("routePlan") or (payload.get("_raydium_response") or {}).get("data", {}).get("routePlan") or []
        plan_keys = ("ammKey", "poolId", "label", "inputMint", "outputMint", "inAmount", "outAmount", "feeAmount", "feeMint")
        def atomic(value):
            return str(int(value)) if value is not None else None
        curve = payload.get("_curve_leg") or {}
        return {
            "version": 1, "venue": payload.get("_venue") or route.get("protocol") or venue,
            "input_atomic": atomic(input_atomic if input_atomic is not None else payload.get("sellAmount", payload.get("inAmount"))),
            "expected_output_atomic": atomic(payload.get("buyAmount", payload.get("outAmount"))),
            "minimum_output_atomic": atomic(payload.get("minBuyAmount", payload.get("otherAmountThreshold"))),
            "quote_asset": quote_asset, "quote_asset_decimals": quote_asset_decimals,
            "quote_asset_usd": quote_asset_usd,
            "slippage_bps": slippage_bps if slippage_bps is not None else payload.get("_requested_slippage_bps", payload.get("slippageBps")),
            # A V2 exit's expected output is the whole bag less a tax allowance, or the 15% cut when
            # that read failed (evm_real_seams._with_exit_value). Absent for every other venue.
            **({"value_basis": payload["_value_basis"]} if payload.get("_value_basis") else {}),
            "context_slot": payload.get("contextSlot"),
            "route_target": (payload.get("transaction") or {}).get("to"),
            "route": {k: route[k] for k in route_keys if k in route},
            "route_plan": [{k: (atomic((leg.get("swapInfo") or leg)[k])
                                if k in ("inAmount", "outAmount", "feeAmount")
                                else (leg.get("swapInfo") or leg)[k]) for k in plan_keys
                            if k in (leg.get("swapInfo") or leg)} for leg in plan],
            "curve_leg": ({"quote_mint": curve.get("quote_mint"), "curve": curve.get("curve"),
                           "expected_out": atomic(curve.get("expected_out")),
                           "min_out": atomic(curve.get("min_out"))} if curve else None),
        }
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


@dataclass(frozen=True)
class ExitPreview:
    expected_usdc: float
    amount_atomic: int
    route_id: str | None
    quoted_at: str
    quote: dict | None = None
    quote_evidence: dict | None = None
    input_quantity: float | None = None


@dataclass(frozen=True)
class PreviewResult:
    """Typed exact-quote outcome. Exactly one of preview/failure is populated."""
    preview: Any = None
    failure: QuoteFailure | None = None
    provider_id: str | None = None
    provider_group: str | None = None

    def __post_init__(self):
        if (self.preview is None) == (self.failure is None):
            raise ValueError("PreviewResult requires exactly one of preview or failure")

    @classmethod
    def succeeded(cls, preview, *, provider_id=None, provider_group=None):
        return cls(preview=preview, provider_id=provider_id, provider_group=provider_group)

    @classmethod
    def failed(cls, failure, *, provider_id=None, provider_group=None):
        return cls(failure=failure, provider_id=provider_id, provider_group=provider_group)

    def _value(self, name):
        if isinstance(self.preview, dict):
            return self.preview.get(name)
        return getattr(self.preview, name, None)

    @property
    def expected_usdc(self):
        return self._value("expected_usdc")

    @property
    def amount_atomic(self):
        return self._value("amount_atomic")

    @property
    def input_quantity(self):
        return self._value("input_quantity")

    @property
    def route_id(self):
        return self._value("route_id")

    @property
    def quoted_at(self):
        return self._value("quoted_at")

    @property
    def quote_evidence(self):
        return self._value("quote_evidence")
