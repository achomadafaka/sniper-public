"""Task 1 — validated free-direct submission configuration.

``auto_free`` is the zero-configuration default for both chains; ``pooled_rpc`` and ``rpc`` are the
only rollback modes. The removed paid ``sender`` / ``jito`` / ``fast_fanout`` / EVM ``fanout`` modes
fail startup. A non-positive/unbounded deadline or timeout, or a non-finite/non-positive warm
interval, also fails startup. Status output never contains a URL, key, or credential.
"""
import pytest

from sniper.settings import memecoin_submission as C


def test_auto_free_is_zero_configuration_default():
    assert C.solana_submit_config(env={}).mode == "auto_free"
    assert C.evm_submit_config(env={}).mode == "auto_free"
    C.validate_submission_config(env={}, funded_evm_chains=("8453", "4663"))


@pytest.mark.parametrize("name,value", [
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "sender"),
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "jito"),
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "fast_fanout"),
    ("MEMECOIN_EVM_SUBMIT_MODE", "fanout"),
])
def test_removed_modes_fail_startup(name, value):
    with pytest.raises(C.SubmissionConfigError):
        C.validate_submission_config(env={name: value}, funded_evm_chains=("8453",))


def test_defaults_use_shared_transport():
    scfg = C.solana_submit_config(env={})
    assert scfg.mode == "auto_free" and scfg.is_shared is True
    ecfg = C.evm_submit_config(env={})
    assert ecfg.mode == "auto_free" and ecfg.is_shared is True
    C.validate_submission_config(env={}, funded_evm_chains=["8453", "4663"])


def test_pooled_rpc_and_rpc_rollback_modes_validate():
    C.validate_submission_config(
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "pooled_rpc",
             "MEMECOIN_EVM_SUBMIT_MODE": "pooled_rpc"}, funded_evm_chains=["8453"])
    # rpc is the legacy no-backend rollback; it skips the fanout deadline/timeout validation entirely.
    C.validate_submission_config(
        env={"MEMECOIN_SOLANA_SUBMIT_MODE": "rpc", "MEMECOIN_EVM_SUBMIT_MODE": "rpc"},
        funded_evm_chains=["8453", "4663"])
    assert C.solana_submit_config(env={"MEMECOIN_SOLANA_SUBMIT_MODE": "rpc"}).is_shared is False


def test_approval_pipeline_requires_explicit_funded_chain_allowlist():
    with pytest.raises(C.SubmissionConfigError, match="APPROVAL_PIPELINE_CHAINS"):
        C.validate_submission_config(
            env={"MEMECOIN_EVM_APPROVAL_PIPELINE": "1"},
            funded_evm_chains=["8453", "4663"],
        )

    C.validate_submission_config(
        env={"MEMECOIN_EVM_APPROVAL_PIPELINE": "1",
             "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS": "4663"},
        funded_evm_chains=["8453", "4663"],
    )


def test_approval_pipeline_rejects_non_funded_or_malformed_chain_ids():
    for chains in ("1", "4663,bad", "8453,4663,999999"):
        with pytest.raises(C.SubmissionConfigError, match="APPROVAL_PIPELINE_CHAINS"):
            C.validate_submission_config(
                env={"MEMECOIN_EVM_APPROVAL_PIPELINE": "1",
                     "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS": chains},
                funded_evm_chains=["8453", "4663"],
            )


def test_status_is_url_free_and_numbers_only():
    status = C.solana_submit_config(env={}).status()
    assert set(status) == {"mode", "deadline_ms", "timeout_ms"}
    assert "https" not in repr(status)
    estatus = C.evm_submit_config(env={}).status()
    assert set(estatus) == {"mode", "deadline_ms", "timeout_ms"}


@pytest.mark.parametrize("override,_why", [
    ({"MEMECOIN_SOLANA_SUBMIT_DEADLINE_MS": "0"}, "non-positive deadline"),
    ({"MEMECOIN_SOLANA_SUBMIT_DEADLINE_MS": "999999"}, "unbounded deadline"),
    ({"MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS": "0"}, "non-positive timeout"),
    ({"MEMECOIN_SOLANA_WARM_INTERVAL_S": "nan"}, "non-finite warm interval"),
    ({"MEMECOIN_SOLANA_WARM_INTERVAL_S": "0"}, "non-positive warm interval"),
    ({"MEMECOIN_SOLANA_SUBMIT_MODE": "bogus"}, "bad solana mode"),
    ({"MEMECOIN_EVM_SUBMIT_MODE": "bogus"}, "bad evm mode"),
    ({"MEMECOIN_EVM_SUBMIT_DEADLINE_MS": "0"}, "non-positive evm deadline"),
])
def test_invalid_config_fails(override, _why):
    with pytest.raises(C.SubmissionConfigError):
        C.validate_submission_config(env=override, funded_evm_chains=["8453"])
