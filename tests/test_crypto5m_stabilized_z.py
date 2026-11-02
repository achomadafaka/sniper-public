import math
from sniper.algos.polymarket_crypto5m import stabilized_z as SZ


def test_w_prior_anchored_at_reference():
    # pinned: w_prior = 0.5 at (D_REF, S_REF) -- pre-fit z_v2 is not arbitrary
    assert abs(SZ.w_prior(SZ.D_REF, SZ.S_REF) - 0.5) < 1e-9


def test_w_prior_cedes_in_sharp_holds_in_calm():
    calm = SZ.w_prior(1.0, SZ.S_REF)      # divergence below ref -> trust ours more
    sharp = SZ.w_prior(6.0, SZ.S_REF)     # divergence far above ref -> cede to market
    assert calm > 0.5 > sharp
    assert 0.0 <= sharp < 0.2


def test_w_prior_wider_spread_trusts_ours():
    # a noisier (wider-spread) market -> var_market up -> weight on ours rises
    assert SZ.w_prior(SZ.D_REF, 0.04) > SZ.w_prior(SZ.D_REF, SZ.S_REF)


def test_jump_cap_decay_caps_then_relaxes():
    assert SZ.jump_cap_decay(None) == 1.0                 # no jump -> no cap
    assert SZ.jump_cap_decay(0) == SZ.jump_cap_decay(0)   # pure
    assert abs(SZ.jump_cap_decay(0) - 0.15) < 1e-9        # at the jump -> hard cap (cede)
    assert SZ.jump_cap_decay(0) < SZ.jump_cap_decay(4) < SZ.jump_cap_decay(8)  # relaxes (un-cede)
    assert SZ.jump_cap_decay(8) == 1.0                    # fully decayed
    assert SZ.jump_cap_decay(50) == 1.0


def _regime(divergence, *, vol_slow=1e-4, jump_flag=False, ticks_since_jump=None):
    return {"vol_slow": vol_slow, "vol_fast": vol_slow * divergence, "jump_flag": jump_flag,
            "ticks_since_jump": ticks_since_jump, "divergence": divergence}


def test_calm_primary_sharp_cede():
    # calm: z_v2 ~= z_ours (weight ~1). btc up a touch over a calm slow vol.
    calm = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(1.0), market_mid=0.5, spread=SZ.S_REF)
    assert calm["weight"] > 0.7
    assert abs(calm["z_v2"] - calm["z_ours"]) < abs(calm["z_ours"]) * 0.5
    # sharp: z_v2 pulled toward z_market (weight low)
    sharp = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=0.5, spread=SZ.S_REF)
    assert sharp["weight"] < 0.2
    assert abs(sharp["z_v2"] - sharp["z_market"]) < abs(sharp["z_v2"] - sharp["z_ours"])


def test_post_jump_not_overconfident():
    # THE test: a jumped numerator over a (jump-excluded) calm slow vol would make z_ours extreme;
    # z_v2 must NOT be extreme -- the jump cap forces a cede to the (mid=0.5 -> z_market=0) market.
    out = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(8.0, jump_flag=True, ticks_since_jump=0),
                          market_mid=0.5, spread=SZ.S_REF)
    assert out["weight"] <= 0.15
    assert abs(out["z_v2"]) < abs(out["z_ours"])          # tempered, not overconfident


def test_un_cede_after_jump_decays():
    # a few seconds after a single jump (no new jumps) z_v2 returns toward z_ours: NOT a one-way valve
    fresh = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(8.0, jump_flag=True, ticks_since_jump=0),
                            market_mid=0.5, spread=SZ.S_REF)
    later = SZ.stabilized_z(60240.0, 60000.0, 100, _regime(1.2, jump_flag=False, ticks_since_jump=8),
                            market_mid=0.5, spread=SZ.S_REF)
    assert later["weight"] > fresh["weight"]
    assert abs(later["z_v2"] - later["z_ours"]) < abs(fresh["z_v2"] - fresh["z_ours"])


def test_blend_is_in_z_space():
    out = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(3.0), market_mid=0.4, spread=SZ.S_REF)
    w = out["weight"]
    assert abs(out["z_v2"] - (w * out["z_ours"] + (1 - w) * out["z_market"])) < 1e-12


def test_degenerate_split():
    # our inputs bad -> None
    assert SZ.stabilized_z(0, 60000.0, 100, _regime(2.0), market_mid=0.5, spread=SZ.S_REF) is None
    assert SZ.stabilized_z(60050.0, 60000.0, 100, None, market_mid=0.5, spread=SZ.S_REF) is None
    # market thin/missing -> z_v2 == z_ours, weight 1 (NOT None)
    nomkt = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=None, spread=None)
    assert nomkt is not None and nomkt["weight"] == 1.0 and nomkt["z_v2"] == nomkt["z_ours"]
    # market_mid at the bound -> clamped, no blow-up
    clamped = SZ.stabilized_z(60050.0, 60000.0, 100, _regime(8.0), market_mid=1.0, spread=SZ.S_REF)
    assert clamped is not None and math.isfinite(clamped["z_v2"])
