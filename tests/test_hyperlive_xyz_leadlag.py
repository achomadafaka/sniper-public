from sniper.hyperlive import xyz_leadlag as XL


def test_strip_dex_and_ref_symbol():
    assert XL.strip_dex("xyz:NVDA") == "NVDA"
    assert XL.strip_dex("CL") == "CL"
    assert XL.ref_symbol("NVDA") == "NVDA"
    assert XL.ref_symbol("CL") == "CL=F"
    assert XL.ref_symbol("SPCX") is None          # private, no public reference


def test_select_referenceable_filters_by_volume_and_mapping():
    ctxs = [
        {"name": "xyz:CL", "ctx": {"dayNtlVlm": "60000000", "midPx": "78.6"}},     # liquid + mapped
        {"name": "xyz:SPCX", "ctx": {"dayNtlVlm": "23000000"}},                    # liquid but unmapped
        {"name": "xyz:NVDA", "ctx": {"dayNtlVlm": "100000"}},                      # mapped but illiquid
        {"name": "xyz:GOLD", "ctx": {"dayNtlVlm": "10000000"}},                    # liquid + mapped
    ]
    out = XL.select_referenceable(ctxs, min_volume=5_000_000)
    assert sorted(r["asset"] for r in out) == ["CL", "GOLD"]
    cl = next(r for r in out if r["asset"] == "CL")
    assert cl["ref_symbol"] == "CL=F"


def test_basis_bps_sign_and_sanity_guard():
    assert XL.basis_bps(101.0, 100.0) == 100.0        # reference 100bps above perp
    assert XL.basis_bps(99.0, 100.0) == -100.0
    assert XL.basis_bps(None, 100.0) is None
    assert XL.basis_bps(100.0, 0.0) is None
    assert XL.basis_bps(200.0, 100.0) is None         # 100% gap -> stale/mismatch -> NULL


def test_is_live_requires_advancing_reference_timestamp():
    assert XL.is_live(1000, 1150) is True             # ref clock advanced -> price discovery
    assert XL.is_live(1000, 1000) is False            # frozen -> market closed
    assert XL.is_live(None, 1150) is False            # first cycle, unknown
    assert XL.is_live(1000, None) is False
