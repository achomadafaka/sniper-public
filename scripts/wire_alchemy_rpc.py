#!/usr/bin/env python3
"""Add an Alchemy endpoint to the funded EVM read pools (4663, 8453, 56, 5042), after proving it.

Operator order 2026-09-28: wire the Alchemy mainnet key for Robinhood, Arc, Base and BNB. The
operator runs this; the key is typed at a hidden prompt (or read from ALCHEMY_API_KEY) and never
printed, logged or passed on the command line.

WHAT IT CHANGES, and only with --confirm: `MEMECOIN_EVM_RPC_POOL_URLS_<chain>` gains the Alchemy
URL as its LAST member. The custody endpoint (`MEMECOIN_EVM_RPC_URL_<chain>`), the websockets, the
watchers and submission are untouched. The pool ranks members by measured latency plus head lag
(sniper/execution/evm_rpc_pool.py), so Alchemy carries reads when it measures better and covers
when another member is quarantined. Nothing takes effect until the runner restarts.

WHY IT PROVES A BALANCE FIRST. On 2026-09-05 the previous (free-tier) Alchemy 4663 member answered
a phantom native balance and was removed from the pool (.env comment at the pool line). A chain id
check cannot catch that. So each chain must return the SAME custody quote-token balance
(`balanceOf`, the input 4663 entry sizing reads) as a member already in the pool, at the same
block, before that chain is written. A chain that fails any check is skipped, never written.

    python3 scripts/wire_alchemy_rpc.py                  # probe only, prints the plan
    python3 scripts/wire_alchemy_rpc.py --confirm        # probe, back up .env, write
    python3 scripts/wire_alchemy_rpc.py --selftest

Rollback: copy the printed .env.bak.<UTC> back over .env and restart the runner.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CHAINS = {"4663": ("robinhood-mainnet", "0x1237"), "8453": ("base-mainnet", "0x2105"),
          "56": ("bnb-mainnet", "0x38"), "5042": ("arc-mainnet", "0x13b2")}
UA = "curl/8.5.0"            # publicnode refuses Python's default User-Agent with a bare 403


def alchemy_url(slug, key):
    return "https://%s.g.alchemy.com/v2/%s" % (slug, key)


def key_from_input(text):
    """The bare key from what the operator pasted: the key itself, or a whole Alchemy URL
    (https://<chain>.g.alchemy.com/v2/<key>). The dashboard copies the URL, not the key."""
    text = str(text or "").strip().strip("'\"")
    if "/v2/" in text:
        text = text.split("/v2/", 1)[1]
    return text.strip().strip("/").split("?", 1)[0].split("/", 1)[0]


def mask(text, key):
    return str(text).replace(key, "<KEY>") if key else str(text)


def host(url):
    return url.split("//", 1)[-1].split("/", 1)[0]


def plan_pool(current, new_url):
    """The new comma list: every current member in order, then `new_url` once."""
    members = [u.strip() for u in current if u and u.strip()]
    if new_url not in members:
        members.append(new_url)
    return ",".join(members)


def rewrite_env(lines, key, value):
    """Replace the single live `key=` line, or append one. Refuses two live lines (ambiguous)."""
    pat = re.compile(r"^\s*%s\s*=" % re.escape(key))
    hits = [i for i, ln in enumerate(lines) if pat.match(ln)]
    if len(hits) > 1:
        raise ValueError("%s is set %d times in .env; fix by hand first" % (key, len(hits)))
    new_line = "%s=%s\n" % (key, value)
    out = list(lines)
    if hits:
        out[hits[0]] = new_line
    else:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(new_line)
    return out


def rpc(url, method, params, timeout=8.0):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json",
                                                          "user-agent": UA})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        answer = json.loads(resp.read())
    ms = (time.perf_counter() - t0) * 1000.0
    if answer.get("error"):
        raise RuntimeError("rpc error %s" % answer["error"])
    return answer.get("result"), ms


def balance_of(url, token, owner, block_hex):
    data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
    result, _ = rpc(url, "eth_call", [{"to": token, "data": data}, block_hex])
    return int(result, 16)


def probe(chain, url, reference, token, owner, samples):
    """Returns (ok, facts). Every failure is a reason string; nothing here writes."""
    facts = {}
    cid, _ = rpc(url, "eth_chainId", [])
    if str(cid).lower() != CHAINS[chain][1]:
        return False, {"reason": "chain id %s, expected %s" % (cid, CHAINS[chain][1])}
    times = []
    for _ in range(samples):
        _, ms = rpc(url, "eth_blockNumber", [])
        times.append(ms)
        time.sleep(0.5)
    facts["blockNumber_ms_p50"] = round(statistics.median(times), 1)
    facts["blockNumber_ms_max"] = round(max(times), 1)
    head_a, _ = rpc(url, "eth_blockNumber", [])
    head_r, _ = rpc(reference, "eth_blockNumber", [])
    facts["head_lag_blocks_vs_reference"] = int(head_r, 16) - int(head_a, 16)
    block = hex(min(int(head_a, 16), int(head_r, 16)) - 2)
    a, r = balance_of(url, token, owner, block), balance_of(reference, token, owner, block)
    facts["balance_check_block"] = int(block, 16)
    facts["balance_matches_reference"] = a == r
    if a != r:
        return False, dict(facts, reason="custody balanceOf differs from %s at the same block "
                                          "(%d vs %d)" % (host(reference), a, r))
    try:
        rpc(url, "eth_getLogs", [{"address": token, "fromBlock": hex(int(block, 16) - 3),
                                  "toBlock": block}])
        facts["getLogs_address_filtered"] = "ok"
    except Exception as exc:                            # reported, not a gate: the pool routes around
        facts["getLogs_address_filtered"] = "refused: %s" % type(exc).__name__
    return True, facts


def custody_address():
    hb = json.loads((REPO / "data" / "memecoin_runner.heartbeat").read_text())
    addr = ((hb.get("lifi") or {}).get("custody_wallet") or "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise SystemExit("custody address not readable from the runner heartbeat; not probing")
    return addr


def selftest():
    assert plan_pool(["https://a", " https://b "], "https://c") == "https://a,https://b,https://c"
    assert plan_pool(["https://a", "https://c"], "https://c") == "https://a,https://c"
    assert rewrite_env(["X=1\n", "K=old\n"], "K", "new") == ["X=1\n", "K=new\n"]
    assert rewrite_env(["#K=commented\n", "X=1"], "K", "v") == ["#K=commented\n", "X=1\n", "K=v\n"]
    try:
        rewrite_env(["K=1\n", "K=2\n"], "K", "v")
        raise AssertionError("two live lines must refuse")
    except ValueError:
        pass
    assert mask("https://h/v2/SECRET", "SECRET") == "https://h/v2/<KEY>"
    assert key_from_input("  abcDEF_123-xyz456789  ") == "abcDEF_123-xyz456789"
    assert key_from_input("https://robinhood-mainnet.g.alchemy.com/v2/alch_AbC123dEf456gHi789/") == "alch_AbC123dEf456gHi789"
    assert key_from_input("'https://base-mainnet.g.alchemy.com/v2/k_1234567890abcdef?x=1'") == "k_1234567890abcdef"
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chains", default="4663,8453,56,5042")
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--confirm", action="store_true", help="back up .env and write the pool lines")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    import sniper.settings.memecoin as cfg
    from sniper.execution.evm_wallet import CHAIN_QUOTE

    key = os.environ.get("ALCHEMY_API_KEY") or getpass.getpass("Alchemy API key (hidden): ")
    key = key_from_input(key)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", key):
        raise SystemExit("that does not look like an Alchemy key; nothing probed")
    owner = custody_address()
    pools = cfg.MEMECOIN_EVM_RPC_POOL_URLS
    writes = {}
    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        if chain not in CHAINS:
            print("%s: not a supported chain, skipped" % chain)
            continue
        url = alchemy_url(CHAINS[chain][0], key)
        current = list(pools.get(chain) or ())
        # Cross-check against publicnode where the pool has it: the custody primary is the funded
        # lane's own endpoint and must not carry verification traffic.
        others = [u for u in current if "alchemy" not in u]
        reference = next((u for u in others if "publicnode" in u), others[0] if others else None)
        if not reference:
            print("%s: no current pool member to cross-check against; skipped" % chain)
            continue
        try:
            ok, facts = probe(chain, url, reference, CHAIN_QUOTE[chain]["token"], owner,
                              args.samples)
        except Exception as exc:
            ok, facts = False, {"reason": mask("%s: %s" % (type(exc).__name__, exc), key)}
        print("%s: %s %s" % (chain, "PASS" if ok else "SKIP", json.dumps(facts)))
        if ok:
            writes["MEMECOIN_EVM_RPC_POOL_URLS_%s" % chain] = plan_pool(current, url)
    if not writes:
        print("nothing to write")
        return 1
    for k, v in writes.items():
        print("plan: %s=%s" % (k, mask(v, key)))
    if not args.confirm:
        print("probe only. Re-run with --confirm to write .env, then ask Alpha to restart the runner.")
        return 0
    env_path = Path(str(state_root())) / ".env"
    backup = env_path.with_name(".env.bak.%s" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    shutil.copy2(env_path, backup)
    lines = env_path.read_text().splitlines(keepends=True)
    for k, v in writes.items():
        lines = rewrite_env(lines, k, v)
    env_path.write_text("".join(lines))
    print("wrote %d line(s); backup %s. Runner restart needed (Alpha)." % (len(writes), backup.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
