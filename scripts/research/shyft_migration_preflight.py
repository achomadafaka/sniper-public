#!/usr/bin/env python3
"""Read-only EU migration checks. Evidence is captured separately; no signer or live calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EU_RPC = {"rpc.ams.shyft.to", "rpc.fra.shyft.to"}
EU_GRPC = {"grpc.ams.shyft.to", "grpc.fra.shyft.to"}
EU_RABBIT = {"rabbitstream.ams.shyft.to", "rabbitstream.fra.shyft.to"}
HTTP_METHODS = {"getGenesisHash", "getSlot", "getSlotLeaders", "getClusterNodes",
                "getLatestBlockhash", "getTransaction", "getMultipleAccounts"}
PROVIDER_KEYS = frozenset({
    "MEMECOIN_SOLANA_INFRA_PROVIDER", "MEMECOIN_FUNDED_ONLY",
    "MEMECOIN_SOLANA_RPC_POOL_URLS", "MEMECOIN_SOLANA_RPC_URL",
    "ONCHAIN_WATCH_SOLANA_RPC_URL", "MEMECOIN_SOLANA_READ_RPC_URL", "MEMECOIN_TPU_RPC_URL",
    "MEMECOIN_SOL_TAPE_RPC_URL", "MEMECOIN_SOL_TAPE_MIN_CALL_S",
    "MEMECOIN_RESEARCH_SOLANA_RPC_URLS", "MEMECOIN_SOL_COHORT_RPC_URL",
    "MEMECOIN_SHYFT_LIVE_ENABLED", "MEMECOIN_SHYFT_GRPC_TOKEN", "MEMECOIN_SHYFT_GRPC_URLS",
    "MEMECOIN_SHYFT_TAPE_CONNECT_IP",
    "MEMECOIN_SHYFT_PREPARE_ENABLED", "MEMECOIN_SHYFT_PREPARE_GRPC_URL",
    "MEMECOIN_SLOT_LANE_SENDER_AB", "MEMECOIN_SLOT_LANE_SENDER_ALL",
    "MEMECOIN_SLOT_LANE_NOZOMI_AB", "MEMECOIN_NOZOMI_ALL_LANES", "MEMECOIN_NOZOMI_ALL_B",
    "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED",
    "MEMECOIN_NOZOMI_FEE_SPLIT_AB",
    "MEMECOIN_SOL_COHORT_HELIUS_URL", "MEMECOIN_CAPTURE_ENABLED",
    "MEMECOIN_TP_CONTINUATION_CAPTURE", "MEMECOIN_EXIT_TAPE_FOLLOW_ENABLED",
    "MEMECOIN_COPY_QUOTE_ENABLED", "MEMECOIN_OPPORTUNITY_CAPTURE_ENABLED",
    "MEMECOIN_V6_PAPER_ENABLED", "MEMECOIN_V6_RADAR_CONSUMER_ENABLED",
    "MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_ENABLED", "MEMECOIN_ENTRY_CAPTURE_SOL_FLOW_ENABLED",
    "MEMECOIN_ENTRY_CAPTURE_HOLDERS_ENABLED", "MEMECOIN_REFUSAL_SHADOW_ENABLED",
    "MEMECOIN_PROMOTION_LANE_ENABLED", "MEMECOIN_CAPTURE_V2_ENABLED",
    "MEMECOIN_PUMP_CAPTURE_ENABLED", "MEMECOIN_THESIS_CAPTURE_ENABLED",
})


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    return values


def _non_provider_lines(path):
    # Preserve every policy/custody/roster assignment, including duplicate ordering.
    return tuple(line.strip() for line in Path(path).read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith("#")
                 and ("=" not in line or line.split("=", 1)[0].strip() not in PROVIDER_KEYS))


def validate_provider_stage(source_env, fomo_env, staged_env, report):
    reasons = []
    if not isinstance(report, dict) or report.get("ok") is not True:
        return ("provider_preflight_refused",)
    for name, path in (("source_env", source_env), ("fomo_env", fomo_env), ("staged_env", staged_env)):
        if file_digest(path) != report.get(name + "_sha256"):
            reasons.append(name + "_changed")
    if _non_provider_lines(source_env) != _non_provider_lines(staged_env):
        reasons.append("provider_stage_scope_changed")
    return tuple(reasons)


def _age(value, now):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return (now - parsed).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return None


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def check_preflight(env, record, *, now=None):
    """Validate recorded capabilities and the entire enabled graph without touching the network."""
    from sniper.services.memecoin_solana_provider import provider_urls, validate_shyft_capacity
    from sniper.settings import memecoin_submission as submission
    now = now or datetime.now(timezone.utc)
    record = record if isinstance(record, dict) else {}
    reasons = list(validate_shyft_capacity(record))
    if env.get("MEMECOIN_SOLANA_INFRA_PROVIDER") != "shyft":
        reasons.append("shyft_required")
    if env.get("MEMECOIN_FUNDED_ONLY") != "1":
        reasons.append("funded_only_required")
    if record.get("usage") != "funded_only" or record.get("research_active") is not False:
        reasons.append("research_active")
    selection_env = dict(env, MEMECOIN_SOLANA_INFRA_PROVIDER="shyft")
    roles = {}
    rpc_keys = set()
    for role in ("funded", "signal", "read", "tpu"):
        try:
            urls = provider_urls(role, selection_env)
        except ValueError as exc:
            reasons.append(str(exc))  # Selector exposes fixed codes only.
            continue
        roles[role] = []
        for url in urls:
            parsed = urlsplit(url)
            key = parse_qs(parsed.query).get("api_key", [""])[0]
            fingerprint = _fingerprint(key)
            rpc_keys.add(fingerprint)
            roles[role].append({"host": parsed.hostname, "credential_fingerprint": fingerprint})
            if parsed.hostname not in EU_RPC:
                reasons.append("region_not_europe")
    if env.get('MEMECOIN_SOL_TAPE_RPC_URL'):
        try:
            tape = provider_urls('read', dict(selection_env,
                MEMECOIN_SOLANA_READ_RPC_URL=env['MEMECOIN_SOL_TAPE_RPC_URL']))[0]
            parsed = urlsplit(tape)
            if parsed.hostname not in EU_RPC:
                reasons.append('region_not_europe')
            if _fingerprint(parse_qs(parsed.query)['api_key'][0]) not in rpc_keys:
                reasons.append('credential_evidence_mismatch')
        except ValueError:
            reasons.append('tape_url_invalid')
    token = env.get("MEMECOIN_SHYFT_GRPC_TOKEN", "").strip()
    if (not token or record.get("grpc_token_fingerprint") != _fingerprint(token)
            or rpc_keys != {record.get("rpc_key_fingerprint")}):
        reasons.append("credential_evidence_mismatch")
    urls = tuple(x.strip() for x in env.get("MEMECOIN_SHYFT_GRPC_URLS", "").split(",") if x.strip())
    try:
        hosts = {urlsplit(url).hostname for url in urls}
        tape_host = urlsplit(urls[0]).hostname if urls else None
        valid = len(urls) == 2 and hosts == EU_GRPC and all(
            urlsplit(url).scheme == "https" and urlsplit(url).port in (None, 443)
            and not urlsplit(url).username and not urlsplit(url).password
            and urlsplit(url).path in ("", "/") and not urlsplit(url).query
            and not urlsplit(url).fragment for url in urls)
    except ValueError:
        valid = False
        tape_host = None
    if not valid or env.get("MEMECOIN_SHYFT_LIVE_ENABLED") != "1":
        reasons.append("grpc_graph_invalid")
    prepare = env.get("MEMECOIN_SHYFT_PREPARE_ENABLED") == "1"
    rabbit_tape = env.get("MEMECOIN_SLOT_LANE_RABBIT_TAPE", "1").strip().lower() not in (
        "0", "false", "off", "no")
    tape_transport = None
    if env.get('MEMECOIN_SHYFT_TAPE_CONNECT_IP', '').strip():
        try:
            from sniper.services.memecoin_rabbit_tape import tape_transport as resolve_tape_transport
            tape_transport = resolve_tape_transport(urls[0], env['MEMECOIN_SHYFT_TAPE_CONNECT_IP'])
            if not rabbit_tape:
                reasons.append('grpc_tape_transport_inactive')
        except (ValueError, IndexError):
            reasons.append('grpc_tape_transport_invalid')
    rabbit_host = None
    if prepare:
        try:
            url = urlsplit(env.get("MEMECOIN_SHYFT_PREPARE_GRPC_URL", ""))
            rabbit_host = url.hostname
            valid = (url.scheme == "https" and url.hostname in EU_RABBIT and url.port in (None, 443)
                     and not url.username and not url.password and url.path in ("", "/")
                     and not url.query and not url.fragment)
        except ValueError:
            valid = False
        if not valid:
            reasons.append("rabbit_region_invalid")
        if record.get("rabbitstream_entitled") is not True:
            reasons.append("rabbit_not_entitled")
    if env.get("MEMECOIN_SLOT_LANE_SENDER_AB", "0").lower() not in ("0", "false", "off", ""):
        reasons.append("auxiliary_send_enabled")
    if env.get("MEMECOIN_SLOT_LANE_SENDER_ALL", "").lower() in ("ab", "all"):
        reasons.append("auxiliary_send_enabled")
    try:
        submission._validate_solana(submission.solana_submit_config(env), env)
    except submission.SubmissionConfigError:
        reasons.append("submission_config_invalid")
    allocations = record.get("allocations")
    allocation = allocations[0] if isinstance(allocations, list) and allocations else {}
    allocation = allocation if isinstance(allocation, dict) else {}
    age = _age(allocation.get("verified_at"), now)
    if age is None or not 0 <= age <= 86400:
        reasons.append("allocation_evidence_stale")
    topology = record.get("topology")
    topology = topology if isinstance(topology, dict) else {}
    expected = 8 + int(prepare) + int(rabbit_tape)
    limit = allocation.get("grpc_connections")
    steady, peak = (topology.get(k) for k in ("steady_connections", "peak_connections"))
    # The host inventory covers local clients. An account-wide remote inventory is unknown
    # unless independently supplied; entitlement and successful handshakes do not prove zero.
    account_count = topology.get("account_connections")
    reserve = topology.get("reconnect_reserve")
    if topology.get("steady_connections") != expected or topology.get("research_connections") != 0:
        reasons.append("topology_mismatch")
    if (type(limit) is not int or type(reserve) is not int or reserve < 1
            or type(steady) is not int or type(peak) is not int
            or steady < expected or steady + reserve > limit or peak < steady or peak > limit
            or (account_count is not None and (type(account_count) is not int
                or account_count < steady or account_count + reserve > limit))):
        reasons.append("grpc_capacity_insufficient")
    if topology.get("handover") != "phased_primary_before_secondary":
        reasons.append("handover_unbounded")
    for kind in ("rpc", "send"):
        field = kind + "_rps_demand"
        demand, budget = topology.get(field), allocation.get(kind + "_rps")
        # An explicit unknown is honest. A recorded estimate never proves an account-wide ceiling.
        if (field not in topology or not _number(budget)
                or demand is not None and (not _number(demand) or demand > budget)):
            reasons.append(kind + "_capacity_insufficient")
    capabilities = record.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    age = _age(capabilities.get("observed_at"), now)
    if age is None or not 0 <= age <= 3600:
        reasons.append("capability_evidence_stale")
    http = capabilities.get("http")
    http = http if isinstance(http, list) else []
    for region in ("ams", "fra"):
        for method in HTTP_METHODS:
            rows = [r for r in http if isinstance(r, dict) and r.get("region") == region
                    and r.get("method") == method]
            if len(rows) != 1 or rows[0].get("available") is not True:
                reasons.append("http_capability_missing")
                continue
            if method == "getGenesisHash" and rows[0].get("mainnet_genesis_match") is not True:
                reasons.append("genesis_mismatch")
            if method == "getTransaction" and (rows[0].get("transaction_version") != 0
                    or rows[0].get("raw_v0_available") is not True
                    or rows[0].get("json_parsed_v0_available") is not True):
                reasons.append("transaction_version_unverified")
            if method == "getMultipleAccounts" and (rows[0].get("alt_owner_program_verified") is not True
                    or rows[0].get("alt_layout_verified") is not True
                    or type(rows[0].get("alt_address_count")) is not int
                    or rows[0]["alt_address_count"] <= 0):
                reasons.append("transaction_version_unverified")
    expected_streams = {(role, host) for role in ("processed", "confirmed") for host in EU_GRPC}
    expected_streams.update((role, "grpc.ams.shyft.to")
                            for role in ("accounts", "source_accounts"))
    expected_streams.update((role, "grpc.fra.shyft.to")
                            for role in ("observer", "rh_deposit"))
    if prepare:
        expected_streams.add(("prepare", rabbit_host))
    if rabbit_tape:
        expected_streams.add(("rabbit_tape", tape_host))
    streams = capabilities.get("streams")
    streams = streams if isinstance(streams, list) else []
    observed_streams = {(r["role"], r["host"]) for r in streams if isinstance(r, dict)
                        and isinstance(r.get("role"), str) and isinstance(r.get("host"), str)}
    if observed_streams != expected_streams or len(streams) != len(expected_streams):
        reasons.append("grpc_stream_graph_mismatch")
    for row in streams:
        if not isinstance(row, dict):
            reasons.append("grpc_stream_invalid")
            continue
        if row.get("authenticated") is not True:
            reasons.append("grpc_auth_unverified")
        if row.get("subscription_acknowledged") is not True:
            reasons.append("grpc_ack_incomplete")
        if row.get('role') == 'rabbit_tape' and tape_transport is not None:
            if any(row.get(key) != value for key, value in tape_transport.items()):
                reasons.append('grpc_tape_transport_mismatch')
        if row.get("role") in ("processed", "confirmed"):
            if (type(row.get("requested_count")) is not int or row["requested_count"] < 1
                    or row.get("acknowledged_count") != row["requested_count"]):
                reasons.append("grpc_ack_incomplete")
            if not _number(row.get("head_age_s")) or row["head_age_s"] > 2:
                reasons.append("grpc_head_stale")
    reasons = list(dict.fromkeys(reasons))
    safe_http = [{"region": r["region"], "method": r["method"],
                  "available": r.get("available") is True,
                  "rtt_ms": r.get("rtt_ms") if _number(r.get("rtt_ms")) else None}
                 for r in http if isinstance(r, dict) and r.get("region") in ("ams", "fra")
                 and isinstance(r.get("method"), str) and r["method"] in HTTP_METHODS]
    safe_streams = [{"role": r["role"], "host": r["host"],
                     "authenticated": r.get("authenticated") is True,
                     "subscription_acknowledged": r.get("subscription_acknowledged") is True,
                     **({k: r.get(k) if tape_transport is not None
                            and r.get(k) == tape_transport[k] else None for k in
                        ('connect_ip', 'dial_target', 'tls_authority')}
                        if r['role'] == 'rabbit_tape' else {}),
                     **{k: r.get(k) if _number(r.get(k)) else None for k in
                        ("head_age_s", "requested_count", "acknowledged_count")}}
                    for r in streams if isinstance(r, dict) and isinstance(r.get("role"), str)
                    and isinstance(r.get("host"), str) and (r["role"], r["host"]) in expected_streams]
    return {"version": 1, "ok": not reasons, "reasons": reasons, "provider": "shyft",
            "checked_at": now.isoformat(), "provider_roles": roles,
            "allocation": {k: allocation.get(k) if _number(allocation.get(k)) else None for k in
                           ("rpc_rps", "send_rps", "grpc_connections", "grpc_filters")},
            "topology": {**{k: topology.get(k) if _number(topology.get(k)) else None for k in
                            ("steady_connections", "peak_connections", "reconnect_reserve", "account_connections",
                             "rpc_rps_demand", "send_rps_demand")},
                         "configured_steady_connections": expected,
                         "available_connection_headroom": (max(0, limit - expected)
                             if type(limit) is int and limit >= 0 else None),
                         "total_demand_proof": ("recorded_estimate" if all(_number(topology.get(k))
                            for k in ("rpc_rps_demand", "send_rps_demand")) else "unproved"),
                         "inventory_scope": "observed_local", "remote_account_clients": "unknown"},
            "capabilities": {"http": safe_http, "streams": safe_streams},
            "speed_proof": "unproved"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--allocation-record", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--env-file")
    args = parser.parse_args()
    state = Path(args.state_root).resolve()
    source = state / ".env"
    fomo = state / ".fomo.env"
    staged = Path(args.env_file).resolve() if args.env_file else source
    try:
        record_path = Path(args.allocation_record).resolve()
        env = read_env(staged)
        for key, value in read_env(fomo).items():
            env.setdefault(key, value)
        result = check_preflight(env, json.loads(record_path.read_text()))
        result.update(source_env_sha256=file_digest(source), fomo_env_sha256=file_digest(fomo),
                      staged_env_sha256=file_digest(staged), allocation_record=str(record_path),
                      allocation_record_sha256=file_digest(record_path))
    except (OSError, ValueError, TypeError, KeyError):
        result = {"version": 1, "ok": False, "reasons": ["preflight_input_invalid"]}
    # Caller chooses this report path; no ledger, configuration or signer is mutated.
    output = Path(args.output)
    with output.open("w") as handle:
        json.dump(result, handle, sort_keys=True)
        handle.write("\n")
    os.chmod(output, 0o600)
    print(json.dumps({"ok": result["ok"], "reasons": result["reasons"]}, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
