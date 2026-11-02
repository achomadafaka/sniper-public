"""Validated configuration for the free-direct submission backend.

``auto_free`` is the production default for both chains. The legacy profile fans exact signed bytes
across custody RPC, chain-owner public endpoints and a local Solana TPU sidecar. The Shyft profile
uses authenticated Shyft custody RPC and local TPU, plus explicitly enabled Frankfurt Nozomi.
``pooled_rpc`` (custody RPC) and ``rpc``
(the one-request send seam) are the rollback modes.

Any other value — including the removed paid accelerated Solana modes (Helius Sender, Jito, and the
tipped multi-provider variant) and the EVM multi-endpoint mode — fails startup (never silently
degrades). The legacy-profile exception is the slot lane's Sender A/B (operator YES 2026-10-01):
``MEMECOIN_SLOT_LANE_SENDER_AB=1`` adds a Sender provider that posts only tipped slot-lane entries
(sniper/execution/solana_sender_ab.py), and it needs the shared transport, so ``rpc`` mode refuses it.
``MEMECOIN_SLOT_LANE_NOZOMI_AB=1`` (operator YES 2026-10-09, same seam) adds Nozomi the same way, with
the same refusal. A non-positive/unbounded deadline or timeout, or a non-positive/non-finite warm interval,
also fails startup. Status output exposes the mode + bounded numbers only — never a URL, key, or
credential.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass


class SubmissionConfigError(RuntimeError):
    """Submission mode configuration is missing or inconsistent."""


_SOLANA_MODES = ("auto_free", "pooled_rpc", "rpc")
_EVM_MODES = ("auto_free", "pooled_rpc", "rpc")
_MAX_DEADLINE_MS = 60_000                 # a fanout deadline above a minute is a config error

# Fixed free-direct safety constants (design §11). These are NOT env-tunable — they are code-pinned so
# a health/quarantine change requires review + a pinned deploy, never a runtime knob.
FREE_HEALTH_INTERVAL_S = 30.0             # check free public endpoints at most every 30s (rate-limit safe)
FREE_HEALTH_FAIL_THRESHOLD = 3           # consecutive failures before quarantine
FREE_HEALTH_QUARANTINE_S = 60.0          # quarantine window for a failing free endpoint
TPU_FUTURE_LEADER_SLOTS = 4              # target the current leader plus four upcoming slots
TPU_SOCKET_MODE = 0o600                  # owner-only local sidecar socket


def validate_free_direct_constants():
    """Assert the fixed safety constants hold their reviewed values. Called from startup validation so
    an accidental edit fails loudly instead of silently loosening health/quarantine behavior."""
    if (FREE_HEALTH_INTERVAL_S != 30.0 or FREE_HEALTH_FAIL_THRESHOLD != 3
            or FREE_HEALTH_QUARANTINE_S != 60.0 or TPU_FUTURE_LEADER_SLOTS != 4
            or TPU_SOCKET_MODE != 0o600):
        raise SubmissionConfigError("free-direct safety constants were altered from reviewed values")


def _src(env):
    return os.environ if env is None else env


def _str(env, name, default=""):
    return str(_src(env).get(name, default) or "").strip()


def _int(env, name, default):
    raw = _str(env, name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SubmissionConfigError("%s must be an integer" % name) from None


def _float(env, name, default):
    raw = _str(env, name, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise SubmissionConfigError("%s must be numeric" % name) from None


@dataclass(frozen=True)
class SolanaSubmitConfig:
    mode: str
    deadline_ms: int
    timeout_ms: int
    warm_interval_s: float

    @property
    def is_shared(self):
        """True for every mode that routes through the shared same-bytes transport."""
        return self.mode != "rpc"

    def status(self):
        """URL-free status for logs/heartbeat."""
        return {"mode": self.mode, "deadline_ms": self.deadline_ms, "timeout_ms": self.timeout_ms}


@dataclass(frozen=True)
class EvmSubmitConfig:
    mode: str
    deadline_ms: int
    timeout_ms: int

    @property
    def is_shared(self):
        return self.mode != "rpc"

    def status(self):
        return {"mode": self.mode, "deadline_ms": self.deadline_ms, "timeout_ms": self.timeout_ms}


def solana_submit_config(env=None) -> SolanaSubmitConfig:
    return SolanaSubmitConfig(
        mode=_str(env, "MEMECOIN_SOLANA_SUBMIT_MODE", "auto_free").lower(),
        deadline_ms=_int(env, "MEMECOIN_SOLANA_SUBMIT_DEADLINE_MS", 2000),
        timeout_ms=_int(env, "MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS", 1500),
        warm_interval_s=_float(env, "MEMECOIN_SOLANA_WARM_INTERVAL_S", 5.0))


def evm_submit_config(env=None) -> EvmSubmitConfig:
    return EvmSubmitConfig(
        mode=_str(env, "MEMECOIN_EVM_SUBMIT_MODE", "auto_free").lower(),
        deadline_ms=_int(env, "MEMECOIN_EVM_SUBMIT_DEADLINE_MS", 1500),
        timeout_ms=_int(env, "MEMECOIN_EVM_SUBMIT_TIMEOUT_MS", 1000))


def evm_approval_pipeline_chains(env=None):
    """Validated chain IDs explicitly opted into nonce-ordered approval pipelining."""
    raw = _str(env, "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS")
    if not raw:
        return ()
    chains = tuple(part.strip() for part in raw.split(",") if part.strip())
    if (not chains or any(not chain.isdigit() or int(chain) <= 0 for chain in chains)
            or len(set(chains)) != len(chains)):
        raise SubmissionConfigError(
            "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS must be unique positive chain IDs")
    if "1" in chains:
        # Chain 1 submits through Flashbots Protect, which simulates before inclusion: a swap
        # signed against a pending approval simulates as a revert and is held, never mined
        # (BUNKER 72163, 2026-10-08). Chain 1 waits for the approval's receipt.
        raise SubmissionConfigError(
            "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS cannot include chain 1 (Protect)")
    return chains


def _validate_deadline(value, name):
    if value <= 0 or value > _MAX_DEADLINE_MS:
        raise SubmissionConfigError(
            "%s must be positive and <= %d ms" % (name, _MAX_DEADLINE_MS))


def _validate_solana(cfg: SolanaSubmitConfig, env=None):
    if cfg.mode not in _SOLANA_MODES:
        raise SubmissionConfigError("MEMECOIN_SOLANA_SUBMIT_MODE must be one of %s" %
                                    (_SOLANA_MODES,))
    from sniper.services.memecoin_solana_provider import infra_provider
    try:
        shyft = infra_provider(env) == "shyft"
    except ValueError:
        raise SubmissionConfigError("Solana infrastructure provider is invalid") from None
    from sniper.execution.solana_sender_ab import knob_on, nozomi_knob_on, nozomi_fee_split_on, with_shyft_on
    split_flag = _str(env, "MEMECOIN_NOZOMI_FEE_SPLIT_AB", "0").lower()
    if split_flag not in ("", "0", "false", "off", "no", "1", "true", "on", "yes"):
        raise SubmissionConfigError("MEMECOIN_NOZOMI_FEE_SPLIT_AB is invalid")
    try:
        nozomi_fee_split_on(env)
    except ValueError:
        raise SubmissionConfigError("MEMECOIN_NOZOMI_FEE_SPLIT_AB requires complete Nozomi bid activation") from None
    if shyft and not with_shyft_on(env):
        if knob_on(env):
            raise SubmissionConfigError(
                "MEMECOIN_SLOT_LANE_SENDER_AB conflicts with Shyft-only submission")
        if _str(env, "MEMECOIN_SLOT_LANE_SENDER_ALL").lower() in ("ab", "all"):
            raise SubmissionConfigError(
                "MEMECOIN_SLOT_LANE_SENDER_ALL conflicts with Shyft-only submission")
    if cfg.mode == "rpc":
        for name, on in (("MEMECOIN_SLOT_LANE_SENDER_AB", knob_on(env)),
                         ("MEMECOIN_SLOT_LANE_NOZOMI_AB", nozomi_knob_on(env))):
            if on:
                raise SubmissionConfigError(
                    "%s needs the shared transport (auto_free or pooled_rpc)" % name)
        return
    if shyft and nozomi_knob_on(env) and not _str(env, "MEMECOIN_NOZOMI_API_KEY"):
        raise SubmissionConfigError("MEMECOIN_NOZOMI_API_KEY is required for Nozomi submission")
    _validate_deadline(cfg.deadline_ms, "MEMECOIN_SOLANA_SUBMIT_DEADLINE_MS")
    _validate_deadline(cfg.timeout_ms, "MEMECOIN_SOLANA_SUBMIT_TIMEOUT_MS")
    if (not math.isfinite(cfg.warm_interval_s)
            or cfg.warm_interval_s <= 0 or cfg.warm_interval_s > 3600):
        raise SubmissionConfigError(
            "MEMECOIN_SOLANA_WARM_INTERVAL_S must be positive and <= 3600 seconds")


def _validate_evm(cfg: EvmSubmitConfig, funded_evm_chains=(), env=None):
    # funded_evm_chains / env retained for call-site signature compatibility; auto_free and pooled_rpc
    # both require only the already-configured custody RPC, so no per-chain endpoint validation runs.
    if cfg.mode not in _EVM_MODES:
        raise SubmissionConfigError("MEMECOIN_EVM_SUBMIT_MODE must be one of %s" % (_EVM_MODES,))
    if cfg.mode == "rpc":
        return
    _validate_deadline(cfg.deadline_ms, "MEMECOIN_EVM_SUBMIT_DEADLINE_MS")
    _validate_deadline(cfg.timeout_ms, "MEMECOIN_EVM_SUBMIT_TIMEOUT_MS")


def validate_submission_config(env=None, *, funded_evm_chains=()):
    """Startup gate. Raises SubmissionConfigError on any inconsistent submission configuration. The
    zero-configuration ``auto_free`` default requires no extra provider credentials."""
    validate_free_direct_constants()
    _validate_solana(solana_submit_config(env), env)
    _validate_evm(evm_submit_config(env), funded_evm_chains, env)
    pipeline = _str(env, "MEMECOIN_EVM_APPROVAL_PIPELINE", "0").lower() in (
        "1", "on", "true", "yes")
    chains = evm_approval_pipeline_chains(env)
    if pipeline and not chains:
        raise SubmissionConfigError(
            "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS is required when approval pipelining is enabled")
    funded = {str(chain) for chain in funded_evm_chains}
    if pipeline and not set(chains).issubset(funded):
        raise SubmissionConfigError(
            "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS must be a subset of funded EVM chains")
