"""Funded market-cap band admission and its configuration rollback."""
import subprocess
import sys
from pathlib import Path

import pytest

import sniper.settings.memecoin as cfg
from sniper.services import memecoin_funded_policy as fp


@pytest.fixture
def bands(monkeypatch):
    def _set(value):
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_RESERVABLE_BANDS", tuple(value))
    return _set


@pytest.mark.parametrize("band", ["nano", "assumed_nano", "micro"])
def test_floor_refuses_nano_and_micro(band, bands):
    bands(("small", "mid"))
    assert fp.band_is_reservable(band) is False


@pytest.mark.parametrize("band", ["small", "mid"])
def test_floor_still_admits_small_and_mid(band, bands):
    bands(("small", "mid"))
    assert fp.band_is_reservable(band) is True
    pol = fp.resolve_funded_policy(coin_band=band, network_id="solana")
    assert pol.coin_band == band and pol.stop_pct > 0     # rails intact


@pytest.mark.parametrize("band", ["nano", "assumed_nano", "micro"])
def test_exits_still_resolve_for_an_already_open_refused_band(band, bands):
    # CRITICAL: the floor is an ADMISSION rule. resolve_funded_policy also builds the policy shape
    # for positions already open, so gating it there would strand an open nano position with no
    # exit rails. It must stay a pure resolver.
    bands(("small", "mid"))
    pol = fp.resolve_funded_policy(coin_band=band, network_id="solana")
    assert pol.stop_pct > 0


def test_manual_operator_buy_bypasses_the_floor(bands):
    # Manual /real/buy bypasses the mcap ceiling by design; the floor must not become the one
    # automated gate that overrides an explicit human decision on a watch-only signal.
    bands(("small", "mid"))
    assert fp.band_is_reservable("nano", signal_source="manual") is True
    assert fp.band_is_reservable("micro", signal_source="manual") is True
    assert fp.band_is_reservable("nano", signal_source="global_feed") is False


def test_assumed_nano_follows_nano_not_a_separate_allow(bands):
    # assumed_nano is an UNRESOLVABLE mcap, i.e. the nano class by construction. If it did not
    # follow nano the floor would leak exactly the coins it exists to stop (-$146.84 on n=7).
    bands(("nano", "small", "mid"))
    assert fp.band_is_reservable("assumed_nano") is True
    bands(("small", "mid"))
    assert fp.band_is_reservable("assumed_nano") is False


def test_ceiling_still_refuses_out_of_scope(bands):
    bands(("small", "mid"))
    with pytest.raises(ValueError, match="not reservable"):
        fp.resolve_funded_policy(coin_band="out_of_scope", network_id="solana")
    assert fp.band_is_reservable("out_of_scope") is False
    # the ceiling is NOT bypassable by a manual buy at the resolver — it has no policy shape
    with pytest.raises(ValueError, match="not reservable"):
        fp.resolve_funded_policy(coin_band="out_of_scope", network_id="solana")


def test_floor_is_reversible_without_a_redeploy(bands):
    # The knob must restore the OLD behavior from env alone — this session shipped a setting on a
    # small cohort and had to revert it, so a config-only rollback is required, not a code change.
    bands(("nano", "micro", "small", "mid"))
    for band in ("nano", "assumed_nano", "micro", "small", "mid"):
        assert fp.band_is_reservable(band) is True


def test_default_shipped_setting_admits_nano_through_mid():
    # Read the default in a CLEAN interpreter. Reloading the settings module in-process resets
    # every value a sibling test monkeypatched, which cross-contaminates under xdist.
    out = subprocess.run(
        [sys.executable, "-c",
         "import os; os.environ.pop('MEMECOIN_REAL_RESERVABLE_BANDS', None);"
         "import sniper.settings.memecoin as c; print(','.join(c.MEMECOIN_REAL_RESERVABLE_BANDS))"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]), check=True)
    assert out.stdout.strip() == "nano,micro,small,mid"


@pytest.mark.parametrize("band", ["nano", "assumed_nano", "micro", "small", "mid"])
def test_default_policy_admits_nano_through_mid(band):
    assert fp.band_is_reservable(band) is True


def test_automated_nano_entry_is_rejected_at_the_reservation_seam(bands):
    """END TO END, through prepare_funded_contract — not the helper. This is the assertion that
    matters: the floor must fire on the real admission path, and it must produce a clean reject
    rather than a contract-less reservation (which the startup reconcile gate would refuse)."""
    from sniper.services import memecoin_market_state as ms
    from sniper.services.memecoin_risk import prepare_funded_contract
    bands(("small", "mid"))
    cls = ms.EntryClassification(
        classification_version=1, network_id="solana", mint="Mint1",
        signal_source="global_feed", decision_at="2026-07-27T00:00:00+00:00",
        coin_band="nano", lifecycle="fresh", liquidity_class="thin",
        momentum_phase="unknown", market_regime="normal", data_quality="complete",
        risk_scale=1.0, reason_codes=("coin_band:nano",), facts={"effective_mcap": 30_000.0})
    with pytest.raises(ValueError, match="not reservable"):
        prepare_funded_contract(
            None, classification=cls, network_id="solana", mint="Mint1",
            attempt_id="a1", origin_event_id="e1", base_clip_usd=50.0, approved_clip_usd=50.0)


def test_automated_small_entry_still_reserves(bands):
    from sniper.services import memecoin_market_state as ms
    from sniper.services.memecoin_funded_policy import resolve_funded_policy
    bands(("small", "mid"))
    cls = ms.EntryClassification(
        classification_version=1, network_id="solana", mint="Mint2",
        signal_source="global_feed", decision_at="2026-07-27T00:00:00+00:00",
        coin_band="small", lifecycle="fresh", liquidity_class="ok",
        momentum_phase="unknown", market_regime="normal", data_quality="complete",
        risk_scale=1.0, reason_codes=("coin_band:small",), facts={"effective_mcap": 600_000.0})
    from sniper.services.memecoin_funded_policy import band_is_reservable
    assert band_is_reservable(cls.coin_band, signal_source=cls.signal_source) is True
    assert resolve_funded_policy(coin_band="small", network_id="solana").coin_band == "small"
