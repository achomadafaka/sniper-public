"""ps_asof prices a 'mapped_curve_derived' pool only when the caller opts in (the Office sheet) and the Am19/Am23 binding
checks hold. Live 2026-10-29: 32 of 50 near-pass tickets were unmapped_pool for exactly this tape status."""
import pytest

from sniper.services import memecoin_desk_ps_asof as A
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_ps_evidence import curve_derived, NOW


def go(c, desk, **kw):
    return A.refresh(c, desk_dir=desk, now=NOW - .5, **kw)


def test_default_still_refuses_curve_derived(tmp_path):
    desk, c = curve_derived(tmp_path)
    with pytest.raises(StaleSnapshot, match="unmapped_pool"):
        go(c, desk)


def test_opt_in_prices_a_checked_curve_derived_pool(tmp_path):
    desk, c = curve_derived(tmp_path)
    out, _ = go(c, desk, accept_curve_derived=True)
    assert out["snapshot"]["fields"]["mcap_usd"]["value"] > 0


def test_opt_in_still_refuses_a_wrong_graduation_mint(tmp_path):
    desk, c = curve_derived(tmp_path, grad_mint="OtherMint")
    with pytest.raises(StaleSnapshot, match="pool_binding_unknown"):
        go(c, desk, accept_curve_derived=True)


def test_opt_in_still_stops_on_the_tripwire(tmp_path):
    desk, c = curve_derived(tmp_path, tripwire=True)
    with pytest.raises(StaleSnapshot, match="curve_binding_tripwire"):
        go(c, desk, accept_curve_derived=True)
