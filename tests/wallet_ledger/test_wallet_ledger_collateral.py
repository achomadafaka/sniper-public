from __future__ import annotations

from sniper.analysis.wallet_protocol.ledger.collateral import PUSD_ADDRESS, USDCE_ADDRESS, resolve_collateral


def test_resolve_collateral_uses_explicit_token_row() -> None:
    token, address, note = resolve_collateral(
        {"collateralToken": "pUSD", "collateralTokenAddress": PUSD_ADDRESS},
        capture_ts=1777334399,
    )

    assert token == "pUSD"
    assert address == PUSD_ADDRESS
    assert note["collateral_source"] == "row_explicit"


def test_resolve_collateral_uses_address_registry_for_usdce_address() -> None:
    token, address, note = resolve_collateral({"collateralTokenAddress": USDCE_ADDRESS.lower()}, capture_ts=1)

    assert token == "USDC.e"
    assert address == USDCE_ADDRESS.lower()
    assert note["collateral_source"] == "address_registry"


def test_resolve_collateral_uses_address_registry_for_pusd_address() -> None:
    token, address, note = resolve_collateral({"collateral_token_address": PUSD_ADDRESS.lower()}, capture_ts=1)

    assert token == "pUSD"
    assert address == PUSD_ADDRESS.lower()
    assert note["collateral_source"] == "address_registry"


def test_resolve_collateral_uses_cutoff_when_source_has_no_collateral_fields() -> None:
    before_token, before_address, before_note = resolve_collateral({}, capture_ts=1777334399)
    after_token, after_address, after_note = resolve_collateral({}, capture_ts=1777334400)

    assert before_token == "USDC.e"
    assert before_address == USDCE_ADDRESS
    assert before_note["collateral_source"] == "cutoff_inferred"
    assert after_token == "pUSD"
    assert after_address == PUSD_ADDRESS
    assert after_note["collateral_source"] == "cutoff_inferred"


def test_resolve_collateral_unresolved_without_row_fields_or_capture_ts() -> None:
    token, address, note = resolve_collateral({}, capture_ts=None)

    assert token is None
    assert address is None
    assert note == {"collateral_source": "unresolved"}
