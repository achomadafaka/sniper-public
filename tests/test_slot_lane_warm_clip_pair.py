"""Keep both funded regime clips warm without quoting on the entry thread."""
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as curve, pumpswap_buy as amm


@pytest.mark.parametrize("module,cls,unwrap", [
    (curve, curve.SlotLaneCurveBuilder, True),
    (amm, amm.SlotLanePumpSwapBuilder, False),
])
def test_refresh_keeps_both_clip_sizes_and_the_correct_wrap_shape(monkeypatch, module, cls, unwrap):
    now = [100.0]
    calls = []
    builder = cls(user="user", clock=lambda: now[0], refresh_after_s=1.5)
    builder._buyback = "known"
    if hasattr(builder, "pools"):
        monkeypatch.setattr(builder.pools, "learn_pending", lambda: None)

    def fetch(atoms, *args, **kwargs):
        calls.append((atoms, kwargs.get("unwrap", True)))
        return dict(usdc_atomic=atoms, fetched_at=now[0], output_mint=curve.WSOL_MINT,
                    min_out_lamports=atoms, out_lamports=atoms, ixs=[], tables=[])

    monkeypatch.setattr(module, "prefetch_usdc_sol_leg", fetch)
    assert builder.refresh(106.12, alternate_clip_usd=141.49)
    assert calls == [(106_120_000, unwrap), (141_490_000, unwrap)]
    assert builder.matching_leg(106.12)["usdc_atomic"] == 106_120_000
    assert builder.matching_leg(141.49)["usdc_atomic"] == 141_490_000
    # Wrong sizes cannot reuse or resize signed swap instructions.
    assert builder.matching_leg(130.0) is None
    assert builder.matching_leg(100.0) is None
    builder.refresh(106.12, alternate_clip_usd=141.49)
    assert len(calls) == 2
    now[0] += 3.1
    assert builder.matching_leg(141.49) is None


def test_failed_secondary_refresh_retains_only_fresh_exact_clip(monkeypatch):
    now = [100.0]
    builder = curve.SlotLaneCurveBuilder(user="user", clock=lambda: now[0])
    builder._buyback = "known"
    fail = [False]

    def fetch(atoms, *args, **kwargs):
        if fail[0] and atoms == 100_000_000:
            return None
        return dict(usdc_atomic=atoms, fetched_at=now[0], output_mint=curve.WSOL_MINT)

    monkeypatch.setattr(curve, "prefetch_usdc_sol_leg", fetch)
    builder.refresh(75.0, alternate_clip_usd=100.0)
    fail[0] = True
    now[0] += 2.0
    builder.refresh(75.0, alternate_clip_usd=100.0)
    assert builder.matching_leg(100.0) is not None
    now[0] += 1.1
    assert builder.matching_leg(100.0) is None
    assert builder.matching_leg(75.0) is not None
    # A new sizing plan discards the old plan; memory and possible spends stay bounded.
    builder.refresh(60.0, alternate_clip_usd=80.0)
    assert builder.matching_leg(100.0) is None


@pytest.mark.parametrize("clip,scale,alternate", [(106.12, .75, 141.49), (141.49, 1., 106.11)])
def test_prefetch_only_warms_regime_and_full_clip(monkeypatch, clip, scale, alternate):
    from sniper.services import memecoin_slot_lane as lane
    calls = []
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", .75)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_SOL", True)
    builders = {v: SimpleNamespace(refresh=lambda clip, snap, **kw:
                                  calls.append((clip, kw)))
                for v in ("pump_curve", "pump_amm", "raydium_cpmm")}
    lane.refresh_builder_legs(builders, clip, scale, object())
    assert calls == [(clip, {"alternate_clip_usd": alternate}),
                     (clip, {"alternate_clip_usd": alternate}), (clip, {})]


def test_disabled_scaled_path_keeps_the_original_quote_load(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_SOL", False)
    calls = []
    builder = SimpleNamespace(refresh=lambda *args, **kw: calls.append(kw))
    lane.refresh_builder_legs({"pump_amm": builder}, 75., .75, object())
    assert calls == [{}]


def test_actual_pumpswap_build_uses_the_matching_secondary_leg():
    from test_pumpswap_buy import _builder, _Clock, _buy, SNAP
    builder = _builder(_Clock(), clip_usd=3.75)
    builder._cash_legs = (builder._leg, {**builder._leg, "usdc_atomic": 5_000_000})
    message, validity = builder.build(_buy(), 5.0, SNAP)
    assert message is not None, validity
    assert validity["pump_price_limit"]["cost_usdc_atomic"] == 5_000_000


def test_a_warm_scaled_leg_skips_the_ondemand_quote(conn, tmp_path, monkeypatch):
    # Use the real reservation/classification path; no network/provider mock can hide an extra leg.
    from test_slot_lane_scaled_sol import _lane, _buy
    from sniper.services import memecoin_slot_lane as lane
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=.75, coin_scale=1.0,
                    sol=.05, shadow=75.0)
    ln.cash_leg_fn = lambda buy, clip: {"usdc_atomic": 100_000_000}
    facts = dict(mint="MintPump1", price_usd=3e-6, mcap_usd=3000.0, their_usd=200.0)
    monkeypatch.setattr(lane, "admission_refusal", lambda *a, **k: (None, facts))
    monkeypatch.setattr(lane, "shadow_only", lambda buy: False)
    ln.exotic_offer = lambda *a, **kw: pytest.fail("warm leg must not quote")
    assert ln.handle(_buy())["status"] == "filled"
    assert rec["reserve"] == ["usdc"]
    assert rec["built"] == [(100.0, None)]


from test_slot_lane_scaled_sol import conn  # the isolated ledger fixture


def test_exotic_preparation_inventory_uses_exact_wallet_caps_and_trial(monkeypatch):
    from sniper.services import memecoin_wallet_caps as caps
    from sniper.services import memecoin_slot_lane as L
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_SCALED_SOL', True)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_FUNDED_REGIME_RISK_SCALE', .5)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_MIN_TRADE_FLOOR_USD', 1)
    monkeypatch.setattr(caps, 'entry_cap_usd', lambda uid, chain, **kw: {'a': 7.125001, 'b': 0}.get(uid))
    assert L.exotic_preparation_clips(10., 1., ('a', 'b', 'c'), trials={'c': 2.125001}) == (2_125_001, 5_000_000, 7_125_001)
    assert L.exotic_preparation_clips(10., 1., (), trials={}) == ()
    for clip in (None, 0, float('nan'), float('inf')):
        assert L.exotic_preparation_clips(clip, 1., ('a',)) == ()
