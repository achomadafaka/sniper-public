#!/usr/bin/env python3
"""Activate one exact LI.FI release after a compatible completed canary."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))


def _state_root():
    configured = os.getenv("SNIPER_STATE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    runtime = next((parent for parent in CODE_ROOT.parents if parent.name == ".runtime"), None)
    return runtime.parent if runtime is not None else CODE_ROOT


STATE_ROOT = _state_root()
os.environ.setdefault("SNIPER_STATE_ROOT", str(STATE_ROOT))

from sniper.util.envfiles import load_env_files
load_env_files(STATE_ROOT)
os.environ["DB_PATH"] = str(STATE_ROOT / "sniper_tracker.db")

from sniper.execution.evm_wallet import CHAIN_QUOTE, load_keypair
from sniper.execution.lifi_executor import LifiClient
from sniper.execution.lifi_runtime import build_lifi_request
from sniper.execution.lifi_route import QuotaGuard
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection
from sniper.services.memecoin_runner import acquire_run_lock
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso
from sniper.util.state_paths import state_path

PROOF_RPC = "https://rpc.mainnet.chain.robinhood.com"


def _release_sha(value):
    release_sha = str(value or "").strip().lower()
    if len(release_sha) != 40 or any(char not in "0123456789abcdef" for char in release_sha):
        raise SystemExit("release SHA must be a full lowercase commit SHA")
    actual = subprocess.run(
        ["git", "-C", str(CODE_ROOT), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=True,
    ).stdout.strip().lower()
    if release_sha != actual:
        raise SystemExit("release SHA does not match the activation code")
    return release_sha


def _set_env(values):
    path = STATE_ROOT / ".env"
    lines = path.read_text().splitlines()
    pending = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in pending:
            out.append(key + "=" + pending.pop(key))
        else:
            out.append(line)
    out.extend(key + "=" + value for key, value in pending.items())
    temporary = path.with_name(".env.lifi.tmp")
    temporary.write_text("\n".join(out) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)



def probe_quota_with_retry(quote_fn, *, tries=3, sleep_s=2.0, sleep_fn=None):
    """Run the activation quota probe up to `tries` times; return the first OK result or the last.

    On 2026-09-05 a single probe failed on a transport blip, the script printed "quota cannot admit
    one funded position" and "restored" the same release, while the runtime LI.FI heartbeat showed
    11,998 of 12,000 quota. One read is not evidence about a quota. A probe that is OK ends the
    loop at once; a refusal is retried after `sleep_s`; the last result is returned unchanged so
    the caller still refuses on a persistent failure, now with the failure named.
    """
    import time as _time
    sleep = sleep_fn or _time.sleep
    result = None
    for attempt in range(max(1, int(tries))):
        result = quote_fn()
        if result.ok or quota_readable(result):
            return result
        if attempt + 1 < tries:
            sleep(sleep_s)
    return result


def quota_readable(result):
    """True when LI.FI answered with its quota headers, whatever it said about the route.

    The probe exists to read the authenticated quota. It quotes the CANARY token, and on
    2026-09-22 that 21-day-old token had no route (`no_route`, quota 11,959 of 12,000 in the same
    reply), so four deploys in a row bounced the funded runner and rolled back. A token that lost
    its pool says nothing about the key or the quota; only a reply with no quota headers does.
    """
    quota = getattr(result, "quota", None) or {}
    return isinstance(quota.get("limit"), int) and isinstance(quota.get("remaining"), int)

def main():
    parser = argparse.ArgumentParser(description="Activate one completed LI.FI canary")
    parser.add_argument("--canary-lifecycle", required=True)
    parser.add_argument("--release-sha")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("use --confirm for the separate activation action")

    run_lock = acquire_run_lock(str(state_path("data", "memecoin_runner.lock")))
    if run_lock is None:
        raise SystemExit("stop the funded runner before LI.FI activation")
    current_release = Path(os.path.realpath(STATE_ROOT / ".runtime/memecoin/current")).name
    release_sha = _release_sha(args.release_sha or current_release)
    manifest, manifest_hash = cfg.load_lifi_manifest()
    account = load_keypair(cfg.MEMECOIN_EVM_KEYPAIR)
    conn = get_connection()
    latest = m.latest_lifi_lifecycle(conn, args.canary_lifecycle)
    if latest is None or latest["state"] != "canary_complete":
        raise SystemExit("the selected canary is not complete")
    detail = json.loads(latest["detail_json"] or "{}")
    allowances = detail.get("allowances") or []
    mint = next((item["token"] for item in allowances
                 if item["token"].lower() != CHAIN_QUOTE["4663"]["token"].lower()), None)
    if not mint:
        raise SystemExit("the canary token is absent")

    client = LifiClient(
        os.getenv("MEMECOIN_LIFI_API_URL", "https://li.quest/v1/quote"),
        api_key=os.getenv("MEMECOIN_LIFI_API_KEY", ""), manifest=manifest,
        corrected_now=real_utc_now_iso,
    )
    result = probe_quota_with_retry(
        lambda: client.quote(
            build_lifi_request(
                "buy", 10**15, mint=mint, sell_token=CHAIN_QUOTE["4663"]["token"],
                custody=account.address, manifest=manifest,
            ),
            role="entry", stage="activation_quota_probe",
        ))
    quota = result.quota
    if not quota_readable(result) or not QuotaGuard(
            remaining=quota.get("remaining") or 0).admit_entry(0).allowed:
        raise SystemExit(
            "authenticated LI.FI quota cannot admit one funded position "
            "(probe ok=%s failure=%s remaining=%s)"
            % (result.ok, result.failure_type, quota.get("remaining")))
    expected = [(item["token"], item["spender"]) for item in allowances]
    activation = m.activate_lifi_release(
        conn, release_sha=release_sha, manifest_hash=manifest_hash, chain_id="4663",
        custody_wallet=account.address, canary_lifecycle_id=args.canary_lifecycle,
        expected_allowances=expected, quota_limit=quota["limit"],
        quota_remaining=quota["remaining"], quota_reset=quota["reset"],
        activated_at=real_utc_now_iso(),
    )
    if activation is None:
        raise SystemExit("LI.FI activation record failed")
    _set_env({
        "MEMECOIN_LIFI_MODE": "authenticated_funded",
        "MEMECOIN_LIFI_API_URL": "https://li.quest/v1/quote",
        "MEMECOIN_LIFI_PROOF_RPC_URL_4663": PROOF_RPC,
        "MEMECOIN_LIFI_ACTIVE_RELEASE_SHA": release_sha,
        "MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH": manifest_hash,
        "SNIPER_MEMECOIN_DEPLOY_MANIFEST": str(STATE_ROOT / "data/memecoin_deploy.json"),
    })
    print("activated release=" + release_sha)
    print("canary_lifecycle=" + args.canary_lifecycle)
    print("quota_limit=" + str(quota["limit"]))
    print("quota_remaining=" + str(quota["remaining"]))


if __name__ == "__main__":
    main()
