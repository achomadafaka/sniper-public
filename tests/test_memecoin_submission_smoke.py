"""Task 9 — submission smoke (offline via injected seams).

`health` is read-only identity probing. `devnet` REFUSES a mainnet endpoint, funds an EPHEMERAL key
from the faucet, signs ONE self-transfer, submits those exact bytes, and proves exactly one balance
effect. The real network run happens in Task 10; here every RPC is a fake so the logic is verified
without touching the network and without any funded key.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import memecoin_submission_smoke as smoke


def test_documented_direct_command_imports_repo_without_pythonpath():
    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "scripts/memecoin_submission_smoke.py", "--help"],
        cwd=repo, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def _real_keygen():
    from solders.keypair import Keypair
    return Keypair()


class FakeDevnetRpc:
    def __init__(self, *, genesis=None, fee=5000):
        genesis = smoke.DEVNET_GENESIS if genesis is None else genesis
        self.genesis = genesis
        self.fee = fee
        self.balance = 0
        self.sent = []
        self.confirmed = True

    def __call__(self, url, method, params=None, timeout=None):
        from solders.hash import Hash
        if method == "getGenesisHash":
            return self.genesis
        if method == "requestAirdrop":
            self.balance = 100_000_000
            return "airdropsig"
        if method == "getBalance":
            return {"value": self.balance}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default())}}
        if method == "sendTransaction":
            self.sent.append(params[0])
            self.balance -= self.fee                       # a single base fee deduction
            return "sig"
        if method == "getSignatureStatuses":
            status = {"confirmationStatus": "confirmed", "err": None} if self.confirmed else None
            return {"value": [status]}
        raise RuntimeError("unexpected method " + method)


def test_health_ok_when_identities_match():
    def rpc(url, method, params=None, timeout=None):
        if method == "getHealth":
            return "ok"
        if method == "getGenesisHash":
            return smoke.MAINNET_GENESIS                   # health checks mainnet endpoints
        if method == "eth_chainId":
            return "0x2105" if "base" in url else "0x1237"
        raise RuntimeError(method)
    out = smoke.check_health(rpc=rpc)
    assert out["ok"] is True
    assert out["endpoints"]["solana:public_rpc"]["genesis_ok"] is True
    assert out["endpoints"]["8453:base_standard"]["chain_id_ok"] is True
    # the submission-only sequencer is reported without a read probe
    assert "submission-only" in out["endpoints"]["4663:sequencer"]["identity"]


def test_health_flags_wrong_chain_id():
    def rpc(url, method, params=None, timeout=None):
        if method == "getHealth":
            return "ok"
        if method == "getGenesisHash":
            return smoke.MAINNET_GENESIS
        if method == "eth_chainId":
            return "0xdead"                                # wrong
        raise RuntimeError(method)
    out = smoke.check_health(rpc=rpc)
    assert out["ok"] is False


def test_devnet_refuses_mainnet_endpoint():
    def rpc(url, method, params=None, timeout=None):
        if method == "getGenesisHash":
            return smoke.MAINNET_GENESIS                   # endpoint is mainnet -> refuse
        raise AssertionError("must not proceed past the mainnet guard")
    with pytest.raises(SystemExit):
        smoke.run_devnet_smoke("https://api.mainnet-beta.solana.com", rpc=rpc, keygen=_real_keygen)


def test_devnet_refuses_any_non_devnet_genesis():
    def rpc(url, method, params=None, timeout=None):
        if method == "getGenesisHash":
            return "4uhcVJyU9pJkvQyS88uRDiswHXSCkY3zQawwpjk2NsNY"  # Solana testnet
        raise AssertionError("must not proceed past the devnet identity guard")
    with pytest.raises(SystemExit, match="not DEVNET"):
        smoke.run_devnet_smoke("https://api.testnet.solana.com", rpc=rpc, keygen=_real_keygen)


def test_devnet_same_bytes_proves_one_balance_effect():
    fake = FakeDevnetRpc()
    proof = smoke.run_devnet_smoke("https://api.devnet.solana.com", rpc=fake, keygen=_real_keygen,
                                   poll_s=0.0, max_polls=3)
    assert proof["rpc_submitted"] is True
    assert proof["confirmed"] is True
    assert proof["balance_delta_lamports"] == 5000        # exactly one base fee
    assert proof["one_balance_effect"] is True
    assert len(fake.sent) == 1                             # the exact signed bytes went out once
    assert proof["payload_sha256"] and proof["signature"]


def test_devnet_unconfirmed_is_not_one_effect():
    fake = FakeDevnetRpc()
    fake.confirmed = False
    proof = smoke.run_devnet_smoke("https://api.devnet.solana.com", rpc=fake, keygen=_real_keygen,
                                   poll_s=0.0, max_polls=2)
    assert proof["one_balance_effect"] is False
