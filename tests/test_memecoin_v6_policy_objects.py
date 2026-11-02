from sniper.services.memecoin_v6_engine import (
    POLICY_V2,
    policy_document,
    policy_hash,
)


V2_HASH = "a11a0d279d9cd1c29974faac5ae6df7799af39b6b28acbbbfa182080f3cb9902"


def test_v2_policy_hash_is_pinned():
    assert POLICY_V2.hash() == V2_HASH
    assert policy_hash() == V2_HASH


def test_v2_policy_document_is_unchanged():
    assert policy_document() == dict(POLICY_V2.document)
    assert POLICY_V2.policy_id == "v6-position-engine-v2"


def test_v2_required_evidence_is_the_five_field_set():
    assert POLICY_V2.required_evidence == (
        "chain_vol_p90",
        "route_degraded",
        "source_wallet_exited",
        "wallet_tier_adjustment",
        "executable_price",
    )


def test_v2_ordered_rules_match_the_document():
    assert list(POLICY_V2.ordered_rules) == POLICY_V2.document["ordered_rules"]


def test_step_accepts_an_explicit_policy_and_defaults_to_v2():
    from sniper.services.memecoin_v6_engine import (
        RULE_EVALUATORS,
        EvidenceIdentity,
        V6PositionState,
        make_observation,
        step,
    )

    state = V6PositionState(
        position_id="p1",
        remaining_qty_atomic=1_000_000,
        entry_unit_price="1.0",
        opened_at="2026-08-11T00:00:00+00:00",
    )
    payload = {
        "is_break": False,
        "held_s": 10.0,
        "arrival_rate": 0.0,
        "buy_share": 0.1,
        "realized_vol": 0.0,
        "prior_low": 1.0,
        "price": 1.0,
        "net": 1.0,
        "chain_vol_p90": 1.0,
        "route_degraded": False,
        "source_wallet_exited": False,
        "wallet_tier_adjustment": 0,
        "executable_price": 1.0,
        "quote_status": "healthy",
        "tape_complete": True,
        "depth_pull_flag": False,
    }
    observation = make_observation(
        observation_id="o1",
        observed_at="2026-08-11T00:00:10+00:00",
        network_id="solana",
        payload=payload,
        evidence=EvidenceIdentity(market="m1", quote="q1"),
    )
    default = step(state, observation)
    explicit = step(state, observation, policy=POLICY_V2)
    assert default.policy_hash == explicit.policy_hash == POLICY_V2.hash()
    assert set(POLICY_V2.ordered_rules) <= set(RULE_EVALUATORS)
