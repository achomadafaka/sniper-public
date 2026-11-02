from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
from sniper.services.memecoin_volume_death import BscPollState, _log_pool


MINT = "0x" + "ab" * 20
POOL = "0x" + "cd" * 32


def test_flap_binding_is_token_scoped_and_survives_rebind():
    state = BscPollState()
    key = "flap:" + MINT
    state.replace_bindings({key: MINT}, now_ts=1, covered=True)
    assert state.record_pool_activity(key, now_ts=2)
    state.replace_bindings({key: MINT}, now_ts=3)
    assert state.coverage_snapshot(3)["pools_covered"] == 1
    assert not state.record_pool_activity("flap:0x" + "ef" * 20, now_ts=3)


def test_rediscovered_flap_binding_rejects_malformed_token():
    state = BscPollState()
    state.bind_more({"flap:" + MINT, "flap:0x" + "zz" * 20}, MINT)
    assert state.record_pool_activity("flap:" + MINT, now_ts=2)
    assert not state.record_pool_activity("flap:0x" + "zz" * 20, now_ts=2)


def test_singleton_log_identity_uses_manager_and_swap_topic():
    manager = V4_POOL_MANAGERS["1"]
    assert _log_pool({"address": manager, "topics": [V4_SWAP, POOL]}) == POOL
    assert _log_pool({"address": manager, "topics": ["0xwrong", POOL]}) == manager
    assert _log_pool({"address": MINT, "topics": [V4_SWAP, POOL]}) == MINT
