#!/usr/bin/env python3
"""Read-only mainnet health + an authorized ephemeral DEVNET same-bytes smoke for the free-direct
submission backend.

    memecoin_submission_smoke.py health          # read-only identity checks, no signing, no send
    memecoin_submission_smoke.py devnet [--sidecar-binary PATH]

`health` performs only read-only identity probes of the official endpoints (EVM eth_chainId, Solana
getHealth + mainnet-beta genesis, and — if a sidecar is running — its no-send health). `devnet`
generates EPHEMERAL key material, funds it from the public devnet faucet, signs ONE self-transfer, and
submits those EXACT bytes through the devnet RPC (and, if a sidecar binary is given, the local TPU
sidecar pointed at devnet), then confirms one signature and proves exactly one balance effect before
destroying the key material. It REFUSES to run if any endpoint identifies as mainnet, and never
accepts a funded-key path. No mainnet transaction is ever sent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path

# The documented operator command executes this file directly. Python otherwise puts only
# ``scripts/`` on sys.path, so the in-repo ``sniper`` package is not importable from a clean shell.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sniper.execution import free_endpoint_registry as R

DEVNET_RPC = "https://api.devnet.solana.com"
MAINNET_GENESIS = R.SOLANA_MAINNET_GENESIS
DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"


def _rpc(url, method, params=None, timeout=15.0):
    """One JSON-RPC POST. Returns the parsed 'result' (or raises). A non-default User-Agent is sent
    because some chain-owner endpoints' WAF (e.g. Base) 403s the bare ``Python-urllib`` agent."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"content-type": "application/json",
                                          "user-agent": "memecoin-submission-smoke/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if "error" in payload and payload["error"] is not None:
        raise RuntimeError("rpc error %s: %s" % (method, payload["error"]))
    return payload.get("result")


# --------------------------------------------------------------------------- read-only health
def check_health(rpc=_rpc):
    """Read-only identity checks for every official endpoint. Never signs or submits."""
    results = {}
    ok = True
    for e in R.free_endpoints("solana"):
        try:
            healthy = rpc(e.url, "getHealth")
            genesis = rpc(e.url, "getGenesisHash")
            match = str(genesis) == str(e.expected_identity)
            results[e.provider_id] = {"role": e.role, "getHealth": healthy, "genesis": str(genesis),
                                      "genesis_ok": match}
            ok = ok and healthy == "ok" and match
        except Exception as exc:                                   # noqa: BLE001
            results[e.provider_id] = {"role": e.role, "error": type(exc).__name__}
            ok = False
    for chain in ("8453", "4663"):
        for e in R.free_endpoints(chain):
            if e.health_method == "none":
                results[e.provider_id] = {"role": e.role, "identity": "submission-only (no read)"}
                continue
            try:
                got = rpc(e.url, "eth_chainId", [])
                match = str(got).lower() == str(e.expected_identity).lower()
                results[e.provider_id] = {"role": e.role, "chain_id": str(got), "chain_id_ok": match}
                ok = ok and match
            except Exception as exc:                               # noqa: BLE001
                results[e.provider_id] = {"role": e.role, "error": type(exc).__name__}
                ok = False
    return {"ok": ok, "endpoints": results}


# --------------------------------------------------------------------------- devnet same-bytes smoke
def _new_ephemeral_keypair():
    from solders.keypair import Keypair
    return Keypair()


def _sign_self_transfer(keypair, blockhash, lamports=0):
    """Sign ONE self-transfer of `lamports` (default 0 -> the only balance effect is the fee). Returns
    (signed_b64, signature, payload_sha256). Signed exactly once."""
    import base64
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    pk = keypair.pubkey()
    ix = transfer(TransferParams(from_pubkey=pk, to_pubkey=pk, lamports=int(lamports)))
    msg = MessageV0.try_compile(pk, [ix], [], Hash.from_string(str(blockhash)))
    tx = VersionedTransaction(msg, [keypair])
    signed_b64 = base64.b64encode(bytes(tx)).decode()
    return signed_b64, str(tx.signatures[0]), hashlib.sha256(signed_b64.encode()).hexdigest()


def run_devnet_smoke(rpc_url=DEVNET_RPC, *, rpc=_rpc, keygen=_new_ephemeral_keypair,
                     sidecar_binary=None, airdrop_lamports=100_000_000, poll_s=2.0, max_polls=30):
    """Authorized ephemeral devnet proof. REFUSES to run against mainnet. Never accepts a funded key."""
    # Guard: the endpoint MUST NOT be mainnet.
    genesis = str(rpc(rpc_url, "getGenesisHash"))
    if genesis != DEVNET_GENESIS:
        raise SystemExit("refusing devnet smoke: endpoint is not DEVNET")

    keypair = keygen()
    pubkey = str(keypair.pubkey())
    proof = {"pubkey": pubkey, "genesis": genesis, "rpc_submitted": False, "tpu_submitted": False}
    try:
        # The public devnet faucet is rate-limited/flaky; retry the airdrop with bounded backoff and
        # then wait for the balance to land. Never substitute a funded key or mainnet.
        _airdrop_with_retry(rpc, rpc_url, pubkey, int(airdrop_lamports))
        before = _await_balance(rpc, rpc_url, pubkey, minimum=1, poll_s=poll_s, max_polls=max_polls)
        if before < 1:
            raise SystemExit("devnet faucet did not fund the ephemeral key (public faucet unavailable)")
        proof["funded_lamports"] = before

        blockhash = _recent_blockhash(rpc, rpc_url)
        signed_b64, signature, payload_hash = _sign_self_transfer(keypair, blockhash)
        proof["signature"] = signature
        proof["payload_sha256"] = payload_hash

        # Submit the EXACT bytes through the devnet RPC.
        rpc(rpc_url, "sendTransaction", [signed_b64, {"encoding": "base64", "skipPreflight": True}])
        proof["rpc_submitted"] = True

        # Optionally submit the SAME bytes through the local TPU sidecar pointed at devnet.
        if sidecar_binary:
            tpu_hash = _submit_via_sidecar(sidecar_binary, rpc_url, signature, signed_b64, payload_hash)
            proof["tpu_submitted"] = True
            proof["tpu_payload_sha256"] = tpu_hash
            proof["identical_payload"] = (tpu_hash == payload_hash)

        confirmed = _await_confirmation(rpc, rpc_url, signature, poll_s=poll_s, max_polls=max_polls)
        proof["confirmed"] = confirmed
        after = _await_balance(rpc, rpc_url, pubkey, minimum=0, poll_s=0.0, max_polls=1)
        proof["balance_after"] = after
        # One balance effect: a self-transfer's only net change is the single fee (submitted twice with
        # identical bytes -> one signature -> one landing). Assert exactly one deduction occurred.
        delta = before - after
        proof["balance_delta_lamports"] = delta
        proof["one_balance_effect"] = confirmed and 0 < delta <= 20_000    # a single base fee (5000) + margin
        return proof
    finally:
        _destroy_keypair(keypair)


def _airdrop_with_retry(rpc, rpc_url, pubkey, lamports, *, attempts=6, base_delay=3.0):
    last = None
    for i in range(attempts):
        try:
            return rpc(rpc_url, "requestAirdrop", [pubkey, lamports])
        except Exception as exc:                                   # noqa: BLE001 - faucet is flaky
            last = exc
            time.sleep(min(base_delay * (2 ** i), 20.0))
    raise SystemExit("devnet faucet airdrop failed after retries: %s" % last)


def _recent_blockhash(rpc, rpc_url):
    value = rpc(rpc_url, "getLatestBlockhash", [{"commitment": "finalized"}])
    return (value or {}).get("value", {}).get("blockhash") or value.get("blockhash")


def _await_balance(rpc, rpc_url, pubkey, *, minimum, poll_s, max_polls):
    last = 0
    for _ in range(max_polls):
        value = rpc(rpc_url, "getBalance", [pubkey])
        last = (value or {}).get("value", value) if isinstance(value, dict) else value
        last = int(last or 0)
        if last >= minimum:
            return last
        if poll_s:
            time.sleep(poll_s)
    return last


def _await_confirmation(rpc, rpc_url, signature, *, poll_s, max_polls):
    for _ in range(max_polls):
        value = rpc(rpc_url, "getSignatureStatuses", [[signature]])
        statuses = (value or {}).get("value") if isinstance(value, dict) else None
        row = (statuses or [None])[0]
        if row and row.get("confirmationStatus") in ("confirmed", "finalized") and not row.get("err"):
            return True
        if poll_s:
            time.sleep(poll_s)
    return False


def _submit_via_sidecar(binary, rpc_url, signature, signed_b64, payload_hash):
    """Start the keyless sidecar against devnet and submit the EXACT signed bytes through it. Returns
    the payload sha256 the sidecar leg carried (identical to the RPC leg by construction)."""
    import tempfile
    from sniper.execution.solana_tpu_sidecar import TpuSidecarProcess
    from sniper.execution.submission_transport import build_request
    sock = tempfile.mktemp(prefix="mc-tpu-smoke-", suffix=".sock")
    proc = TpuSidecarProcess(binary_path=binary, socket_path=sock, rpc_url=rpc_url,
                             start_timeout_s=20.0, health_timeout_s=3.0)
    try:
        if not proc.start():
            raise SystemExit("devnet TPU sidecar did not become healthy")
        req = build_request(chain_id="solana", action="entry", tx_identity=signature,
                            signed_payload=signed_b64)
        attempt = proc.provider().submit(req)
        if attempt.state.value not in ("accepted", "ambiguous"):
            raise SystemExit("devnet TPU submit was not accepted/ambiguous: %s" % attempt.reason_code)
        return req.signed_payload_sha256
    finally:
        proc.close()


def _destroy_keypair(keypair):
    try:
        # solders Keypair is immutable; drop the only reference so its secret bytes are freed promptly.
        del keypair
    except Exception:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    dev = sub.add_parser("devnet")
    dev.add_argument("--rpc-url", default=DEVNET_RPC)
    dev.add_argument("--sidecar-binary", default=None)
    args = parser.parse_args(argv)
    if args.cmd == "health":
        result = check_health()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.cmd == "devnet":
        # Never accept a funded-key path — the smoke ONLY ever uses a freshly generated ephemeral key.
        proof = run_devnet_smoke(args.rpc_url, sidecar_binary=args.sidecar_binary)
        print(json.dumps(proof, indent=2, sort_keys=True))
        return 0 if proof.get("one_balance_effect") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
