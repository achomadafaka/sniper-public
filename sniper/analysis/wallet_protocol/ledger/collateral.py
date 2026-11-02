from __future__ import annotations

from typing import Any


CLOB_V2_COLLATERAL_CUTOFF_TS = 1777334400
CLOB_V2_COLLATERAL_CUTOFF_ISO = "2026-04-28T00:00:00Z"
USDCE_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

_ADDRESS_REGISTRY = {
    USDCE_ADDRESS.lower(): "USDC.e",
    PUSD_ADDRESS.lower(): "pUSD",
}
_TOKEN_REGISTRY = {
    "usdc.e": ("USDC.e", USDCE_ADDRESS),
    "usdce": ("USDC.e", USDCE_ADDRESS),
    "pusd": ("pUSD", PUSD_ADDRESS),
}


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _token_from_address(address: str | None) -> str | None:
    if not address:
        return None
    return _ADDRESS_REGISTRY.get(address.lower())


def _token_registry_entry(token: str | None) -> tuple[str, str | None] | None:
    if not token:
        return None
    return _TOKEN_REGISTRY.get(token.lower())


def resolve_collateral(row: dict[str, Any], *, capture_ts: int | None) -> tuple[str | None, str | None, dict[str, Any]]:
    token = _clean_str(row.get("collateralToken") or row.get("collateral_token"))
    address = _clean_str(row.get("collateralTokenAddress") or row.get("collateral_token_address"))

    token_entry = _token_registry_entry(token)
    if token_entry is not None:
        canonical_token, registry_address = token_entry
        return (
            canonical_token,
            address or registry_address,
            {"collateral_source": "row_explicit"},
        )
    if token:
        return token, address, {"collateral_source": "row_explicit"}

    address_token = _token_from_address(address)
    if address_token is not None:
        return (
            address_token,
            address,
            {
                "collateral_source": "address_registry",
                "collateral_address": address,
            },
        )

    if capture_ts is not None:
        if capture_ts < CLOB_V2_COLLATERAL_CUTOFF_TS:
            return (
                "USDC.e",
                USDCE_ADDRESS,
                {
                    "collateral_source": "cutoff_inferred",
                    "cutoff_ts": CLOB_V2_COLLATERAL_CUTOFF_TS,
                    "cutoff_iso": CLOB_V2_COLLATERAL_CUTOFF_ISO,
                },
            )
        return (
            "pUSD",
            PUSD_ADDRESS,
            {
                "collateral_source": "cutoff_inferred",
                "cutoff_ts": CLOB_V2_COLLATERAL_CUTOFF_TS,
                "cutoff_iso": CLOB_V2_COLLATERAL_CUTOFF_ISO,
            },
        )

    return None, None, {"collateral_source": "unresolved"}
