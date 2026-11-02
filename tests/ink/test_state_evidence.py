from sniper.settings import memecoin as cfg


def test_ink_route_revert_is_not_cleared_by_token_pull_proof():
    from sniper.execution.evm_token_safety import token_safety_ok_evm
    ok, reason = token_safety_ok_evm(
        "0x" + "b" * 40, code_fn=lambda _m: "0x6080",
        sell_route_fn=lambda _m: True,
        sell_sim_fn=lambda _m: "reverted_unknown|token_pull_ok=true",
        allow_pull_only=False)
    assert ok is False
    assert "sell_sim_reverted_unknown" in reason


def test_ink_bound_safety_uses_strict_route_verdict(monkeypatch):
    from sniper.execution import evm_token_safety as safety
    from sniper.execution import evm_wallet
    seen = []
    monkeypatch.setattr(evm_wallet, "eth_get_code", lambda *_a, **_k: "0x6080")
    def verdict(_mint, **kwargs):
        seen.append(kwargs["allow_pull_only"])
        return False, "route refused"
    monkeypatch.setattr(safety, "token_safety_ok_evm", verdict)
    bound = safety.build_evm_token_safety(
        chain_id="57073", rpc_url="unused", taker="0x" + "1" * 40,
        quote_fn=lambda *_a, **_k: {"transaction": {"to": "0x" + "2" * 40}},
        decimals_fn=lambda _mint: 18)
    assert bound("0x" + "3" * 40, bought_amount_atomic=10**18) == (False, "route refused")
    assert seen == [False]


def test_uncertified_ink_manifest_blocks_entry_even_when_flags_are_enabled():
    reason = cfg.ink_capability_reason()
    assert reason is not None
    assert "ink" in reason.lower()


def test_capability_refusal_precedes_every_money_seam(monkeypatch, ink_flow):
    monkeypatch.setattr(cfg, "ink_capability_reason", lambda: "ink_activation_blocked")
    result = ink_flow("capability_blocked")
    assert result["result"] == {"status": "rejected", "reason": "ink_activation_blocked"}
    assert (result["approve_calls"], result["sign_calls"], result["send_calls"]) == (0, 0, 0)
    assert result["pending_rows"] == 0


def test_manifest_capability_requires_route_and_market_safety_proofs():
    manifest = {"chain_id": 57073, "funded_activation": "ready",
                "required_capabilities": {"market_data": "unsupported",
                                          "state_coherence": "supported_tested",
                                          "token_safety": "supported_tested",
                                          "canonical_receipts": "supported_tested",
                                          "source_coverage": "supported_tested",
                                          "route_full_exit": "supported_tested",
                                          "recovery": "supported_tested"},
                "venues": [{"route_class": "uniswap_v3", "state": "supported_tested",
                            "entry_proof": "supported_tested", "exit_proof": "supported_tested",
                            "simulation_proof": "supported_tested"}]}
    assert cfg.ink_capability_reason(manifest) == "ink_market_data_unsupported"
    manifest["required_capabilities"]["market_data"] = "supported_tested"
    assert cfg.ink_capability_reason(manifest) == "ink_fee_accounting_unsupported"
    manifest["required_capabilities"]["fee_accounting"] = "supported_tested"
    assert cfg.ink_capability_reason(manifest) == "ink_manual_recovery_unsupported"
    manifest["required_capabilities"]["manual_recovery"] = "supported_tested"
    assert cfg.ink_capability_reason(manifest) is None
    assert cfg.ink_capability_reason(
        manifest, preconfirmed=True) == "ink_preconfirmed_admission_unsupported"
