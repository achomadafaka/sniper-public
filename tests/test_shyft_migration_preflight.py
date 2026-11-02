"""Offline trust-boundary checks for a sole funded EU provider allocation."""
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from sniper.services.memecoin_solana_provider import validate_shyft_capacity


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
TOKEN = "private-grpc-token-DO-NOT-PRINT"
RPC_KEY = "private-rpc-key-DO-NOT-PRINT"


def candidate_env():
    return {
        "MEMECOIN_SOLANA_INFRA_PROVIDER": "shyft", "MEMECOIN_FUNDED_ONLY": "1",
        "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://rpc.fra.shyft.to?api_key=" + RPC_KEY,
        "MEMECOIN_SOLANA_RPC_URL": "",
        "ONCHAIN_WATCH_SOLANA_RPC_URL": "https://rpc.ams.shyft.to?api_key=" + RPC_KEY,
        "MEMECOIN_SOLANA_READ_RPC_URL": "https://rpc.ams.shyft.to?api_key=" + RPC_KEY,
        "MEMECOIN_SOL_TAPE_RPC_URL": "https://rpc.ams.shyft.to?api_key=" + RPC_KEY,
        "MEMECOIN_TPU_RPC_URL": "https://rpc.fra.shyft.to?api_key=" + RPC_KEY,
        "MEMECOIN_SHYFT_LIVE_ENABLED": "1", "MEMECOIN_SHYFT_GRPC_TOKEN": TOKEN,
        "MEMECOIN_SHYFT_GRPC_URLS": "https://grpc.ams.shyft.to,https://grpc.fra.shyft.to",
        "MEMECOIN_SHYFT_PREPARE_ENABLED": "1",
        "MEMECOIN_SHYFT_PREPARE_GRPC_URL": "https://rabbitstream.fra.shyft.to/",
        "MEMECOIN_SLOT_LANE_RABBIT_TAPE": "0",  # The legacy nine-stream fixture explicitly omits it.
        "MEMECOIN_SOLANA_KEYPAIR": "data/solana_keypair.json",
        "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT": "35",
    }


def allocation_record():
    return {
        "usage": "funded_only",
        "allocations": [{
            "role": "funded", "account_label": "shyft-build-main",
            "quota_scope_label": "account-wide", "region": "eu",
            "rpc_rps": 100, "send_rps": 20, "grpc_connections": 10,
            "grpc_filters": None, "verified_at": NOW.isoformat(),
            "allocation_evidence": "authenticated Build+ dashboard; research disabled",
        }],
        "rabbitstream_entitled": True,
        "research_active": False,
        "grpc_token_fingerprint": hashlib.sha256(TOKEN.encode()).hexdigest()[:12],
        "rpc_key_fingerprint": hashlib.sha256(RPC_KEY.encode()).hexdigest()[:12],
        "capabilities": {
            "observed_at": NOW.isoformat(),
            "http": [{"region": region, "method": method, "available": True,
                       "mainnet_genesis_match": True, "transaction_version": 0,
                       "raw_v0_available": True, "json_parsed_v0_available": True,
                       "alt_owner_program_verified": True, "alt_layout_verified": True,
                       "alt_address_count": 191,
                       "rtt_ms": 60.0}
                      for region in ("ams", "fra")
                      for method in ("getGenesisHash", "getSlot", "getSlotLeaders",
                                     "getClusterNodes", "getLatestBlockhash",
                                     "getTransaction", "getMultipleAccounts")],
            "streams": [{"role": role, "host": host, "authenticated": True,
                         "subscription_acknowledged": True, "head_age_s": .1,
                         "requested_count": 33, "acknowledged_count": 33}
                        for role, host in (
                            ("processed", "grpc.ams.shyft.to"),
                            ("processed", "grpc.fra.shyft.to"),
                            ("confirmed", "grpc.ams.shyft.to"),
                            ("confirmed", "grpc.fra.shyft.to"),
                            ("accounts", "grpc.ams.shyft.to"),
                            ("observer", "grpc.fra.shyft.to"),
                            ("source_accounts", "grpc.ams.shyft.to"),
                            ("rh_deposit", "grpc.fra.shyft.to"),
                            ("prepare", "rabbitstream.fra.shyft.to"))],
        },
        "topology": {"steady_connections": 9, "peak_connections": 10,
                     "reconnect_reserve": 1, "research_connections": 0,
                     "handover": "phased_primary_before_secondary", "account_connections": None,
                     "rpc_rps_demand": 40, "send_rps_demand": 5},
    }


def check(env=None, record=None):
    # A missing checker is the intentional red witness, not an import error.
    from scripts.research import shyft_migration_preflight as p
    assert callable(getattr(p, "check_preflight", None)), "preflight checker missing"
    return p.check_preflight(env or candidate_env(), record or allocation_record(), now=NOW)


def test_sole_funded_account_accepts_undocumented_filter_limit_without_fake_reservation():
    assert validate_shyft_capacity(allocation_record()) == ()


def test_sole_funded_mode_rejects_research_or_multiple_quota_allocations():
    record = allocation_record()
    record["allocations"].append(copy.deepcopy(record["allocations"][0]))
    record["allocations"][-1]["role"] = "research"
    assert "funded_only_allocation_invalid" in validate_shyft_capacity(record)


def test_complete_europe_graph_is_read_only_and_redacted(monkeypatch):
    import sqlite3
    import urllib.request
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("DB access"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("network access"))
    result = check()
    assert result["ok"] and result["reasons"] == []
    assert result["topology"]["steady_connections"] == 9
    assert result["allocation"]["grpc_filters"] is None
    encoded = json.dumps(result)
    assert TOKEN not in encoded and RPC_KEY not in encoded and "api_key=" not in encoded
    assert "sendTransaction" not in encoded


def test_unknown_remote_account_clients_remain_a_coverage_limit():
    record = allocation_record()
    record["topology"]["account_connections"] = None
    result = check(record=record)
    assert result["ok"]
    assert result["topology"]["inventory_scope"] == "observed_local"
    assert result["topology"]["remote_account_clients"] == "unknown"
    assert result["topology"]["account_connections"] is None


def _stream_inventory(*, tape=None, prepare=True, limit=11):
    env, record = candidate_env(), allocation_record()
    if tape is None:
        env.pop("MEMECOIN_SLOT_LANE_RABBIT_TAPE")
    else:
        env["MEMECOIN_SLOT_LANE_RABBIT_TAPE"] = tape
    if prepare is None:
        env.pop("MEMECOIN_SHYFT_PREPARE_ENABLED")
    else:
        env["MEMECOIN_SHYFT_PREPARE_ENABLED"] = "1" if prepare else "0"
    enabled = tape is None or tape.strip().lower() not in ("0", "false", "off", "no")
    streams = record["capabilities"]["streams"]
    if not prepare:
        streams[:] = [row for row in streams if row["role"] != "prepare"]
        record["rabbitstream_entitled"] = False
    if enabled:
        streams.append(dict(streams[0], role="rabbit_tape"))
    expected = 8 + int(bool(prepare)) + int(enabled)
    record["allocations"][0]["grpc_connections"] = limit
    record["topology"].update(steady_connections=expected, peak_connections=expected + 1)
    return env, record, expected


@pytest.mark.parametrize("prepare", [None, False, True])
@pytest.mark.parametrize("tape", [None, "", "1", "TRUE", "yes", "0", " false ", "OFF", "no"])
def test_pool_tape_inventory_matches_default_and_runtime_flag_semantics(tape, prepare):
    env, record, expected = _stream_inventory(tape=tape, prepare=prepare)
    result = check(env=env, record=record)
    assert result["ok"], result["reasons"]
    assert result["topology"]["configured_steady_connections"] == expected
    assert result["topology"]["available_connection_headroom"] == 11 - expected
    assert len(result["capabilities"]["streams"]) == expected


def test_default_pool_tape_refuses_stale_nine_stream_capacity_claim():
    env, record = candidate_env(), allocation_record()
    env.pop("MEMECOIN_SLOT_LANE_RABBIT_TAPE")
    result = check(env=env, record=record)
    assert not result["ok"]
    assert "topology_mismatch" in result["reasons"]
    assert "grpc_capacity_insufficient" in result["reasons"]
    assert result["topology"]["configured_steady_connections"] == 10
    assert result["topology"]["available_connection_headroom"] == 0


def test_enabled_pool_tape_needs_its_own_stream_capability_on_the_selected_host():
    env, record, _ = _stream_inventory()
    streams = record["capabilities"]["streams"]
    tape = streams.pop()
    assert "grpc_stream_graph_mismatch" in check(env=env, record=record)["reasons"]
    env["MEMECOIN_SHYFT_GRPC_URLS"] = "https://grpc.fra.shyft.to,https://grpc.ams.shyft.to"
    streams.append(dict(tape, host="grpc.fra.shyft.to"))
    assert check(env=env, record=record)["ok"]


def _pinned_tape_inventory():
    env, record, _ = _stream_inventory()
    env['MEMECOIN_SHYFT_TAPE_CONNECT_IP'] = '84.32.186.182'
    tape = record['capabilities']['streams'][-1]
    tape.update(connect_ip='84.32.186.182', dial_target='ipv4:84.32.186.182:443',
                tls_authority='grpc.ams.shyft.to')
    return env, record, tape


def test_pinned_tape_preflight_preserves_exact_dial_and_tls_evidence():
    env, record, tape = _pinned_tape_inventory()
    result = check(env=env, record=record)
    assert result['ok'], result['reasons']
    observed = next(row for row in result['capabilities']['streams'] if row['role'] == 'rabbit_tape')
    for key in ('host', 'connect_ip', 'dial_target', 'tls_authority'):
        assert observed[key] == tape[key]


@pytest.mark.parametrize('field,value', [
    ('connect_ip', ''), ('dial_target', 'grpc.ams.shyft.to:443'),
    ('tls_authority', '84.32.186.182'), ('connect_ip', None),
])
def test_pin_requires_observed_transport_identity(field, value):
    env, record, tape = _pinned_tape_inventory()
    tape[field] = value
    result = check(env=env, record=record)
    assert not result['ok']
    assert 'grpc_tape_transport_mismatch' in result['reasons']


@pytest.mark.parametrize('pin', ['host.example', '::1', '84.32.186.182:443'])
def test_preflight_refuses_invalid_tape_pin(pin):
    env, record, _ = _pinned_tape_inventory()
    env['MEMECOIN_SHYFT_TAPE_CONNECT_IP'] = pin
    result = check(env=env, record=record)
    assert not result['ok'] and 'grpc_tape_transport_invalid' in result['reasons']


def test_preflight_refuses_pin_for_another_host():
    env, record, _ = _pinned_tape_inventory()
    env['MEMECOIN_SHYFT_GRPC_URLS'] = 'https://grpc.fra.shyft.to,https://grpc.ams.shyft.to'
    result = check(env=env, record=record)
    assert not result['ok'] and 'grpc_tape_transport_invalid' in result['reasons']


def test_invalid_transport_evidence_never_echoes_a_credential():
    env, record, tape = _pinned_tape_inventory()
    tape['dial_target'] = TOKEN
    result = check(env=env, record=record)
    assert not result['ok'] and TOKEN not in json.dumps(result)


@pytest.mark.parametrize("mutate", [
    lambda r: r["allocations"][0].update(grpc_connections=None),
    lambda r: r["topology"].update(account_connections=11),
    lambda r: r["topology"].update(reconnect_reserve=0),
])
def test_pool_tape_does_not_waive_unknown_entitlement_or_overlap_headroom(mutate):
    env, record, _ = _stream_inventory()
    mutate(record)
    result = check(env=env, record=record)
    assert not result["ok"] and "grpc_capacity_insufficient" in result["reasons"]
    assert result["topology"]["remote_account_clients"] == "unknown"


def test_explicit_unknown_total_rpc_and_send_demand_stays_unproved():
    record = allocation_record()
    record['topology'].update(rpc_rps_demand=None, send_rps_demand=None)
    result = check(record=record)
    assert result['ok'], result['reasons']
    assert result['topology']['rpc_rps_demand'] is None
    assert result['topology']['send_rps_demand'] is None
    assert result['topology']['total_demand_proof'] == 'unproved'


@pytest.mark.parametrize('kind', ['rpc', 'send'])
def test_absent_total_demand_is_not_explicit_unknown(kind):
    record = allocation_record()
    record['topology'].pop(kind + '_rps_demand')
    assert kind + '_capacity_insufficient' in check(record=record)['reasons']


@pytest.mark.parametrize('value', [float('inf'), float('nan'), -1, True, TOKEN])
@pytest.mark.parametrize('kind', ['rpc', 'send'])
def test_malformed_or_nonfinite_total_demand_is_refused(kind, value):
    record = allocation_record()
    record['topology'][kind + '_rps_demand'] = value
    result = check(record=record)
    assert kind + '_capacity_insufficient' in result['reasons']
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("method,field", [
    ("getTransaction", "raw_v0_available"), ("getTransaction", "json_parsed_v0_available"),
    ("getMultipleAccounts", "alt_owner_program_verified"), ("getMultipleAccounts", "alt_layout_verified")])
def test_v0_and_alt_capabilities_cannot_be_inferred_from_a_method_name(method, field):
    record = allocation_record()
    row = next(r for r in record["capabilities"]["http"] if r["method"] == method)
    row.pop(field)
    assert "transaction_version_unverified" in check(record=record)["reasons"]


def test_nested_secret_stream_fields_are_refused_without_echo_or_crash():
    record = allocation_record()
    record["capabilities"]["streams"][0]["role"] = {"secret": TOKEN}
    result = check(record=record)
    assert not result["ok"] and "grpc_stream_graph_mismatch" in result["reasons"]
    assert TOKEN not in json.dumps(result)


def test_preflight_report_retains_redacted_method_rtt_and_stream_ack_evidence():
    result = check()
    assert len(result["capabilities"]["http"]) == 14
    assert len(result["capabilities"]["streams"]) == 9
    assert result["capabilities"]["http"][0]["rtt_ms"] == 60
    assert result["capabilities"]["streams"][0]["acknowledged_count"] == 33


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r["allocations"][0].update(grpc_connections=8), "grpc_capacity_insufficient"),
    (lambda r: r.update(research_active=True), "research_active"),
    (lambda r: r.update(rabbitstream_entitled=False), "rabbit_not_entitled"),
    (lambda r: r["topology"].update(account_connections=10), "grpc_capacity_insufficient"),
    (lambda r: r["topology"].update(peak_connections=11), "grpc_capacity_insufficient"),
    (lambda r: r["topology"].update(rpc_rps_demand=101), "rpc_capacity_insufficient"),
    (lambda r: r["topology"].update(send_rps_demand=21), "send_capacity_insufficient"),
    (lambda r: r["topology"].update(handover="runner_before_watcher"), "handover_unbounded"),
    (lambda r: r["capabilities"]["http"][0].update(mainnet_genesis_match=False), "genesis_mismatch"),
    (lambda r: r["capabilities"]["http"][2].update(available=False), "http_capability_missing"),
    (lambda r: r["capabilities"]["http"][5].update(transaction_version=None), "transaction_version_unverified"),
    (lambda r: r["capabilities"]["streams"][0].update(authenticated=False), "grpc_auth_unverified"),
    (lambda r: r["capabilities"]["streams"][0].update(head_age_s=3), "grpc_head_stale"),
    (lambda r: r["capabilities"]["streams"][0].update(acknowledged_count=32), "grpc_ack_incomplete"),
    (lambda r: r["capabilities"].update(observed_at="2026-10-01T00:00:00Z"), "capability_evidence_stale"),
    (lambda r: r.update(grpc_token_fingerprint="wrong"), "credential_evidence_mismatch"),
])
def test_known_wrong_provider_evidence_is_refused(mutate, reason):
    record = allocation_record()
    mutate(record)
    assert reason in check(record=record)["reasons"]


@pytest.mark.parametrize("name,value,reason", [
    ("MEMECOIN_SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com", "funded_url_invalid"),
    ("MEMECOIN_SOLANA_READ_RPC_URL", "https://rpc.shyft.to?api_key=" + RPC_KEY, "region_not_europe"),
    ("MEMECOIN_SHYFT_GRPC_URLS", "https://grpc.ny.shyft.to", "grpc_graph_invalid"),
    ("MEMECOIN_SHYFT_PREPARE_GRPC_URL", "https://rabbitstream.ny.shyft.to", "rabbit_region_invalid"),
    ("MEMECOIN_SLOT_LANE_SENDER_AB", "1", "auxiliary_send_enabled"),
    ("MEMECOIN_FUNDED_ONLY", "0", "funded_only_required"),
])
def test_hidden_legacy_route_or_wrong_region_refuses_without_echo(name, value, reason):
    env = candidate_env()
    env[name] = value
    result = check(env=env)
    assert not result["ok"] and reason in result["reasons"]
    assert TOKEN not in json.dumps(result) and RPC_KEY not in json.dumps(result)


def test_preflight_allows_explicit_nozomi_and_capped_bid_profile_without_echo():
    env = candidate_env()
    key = "private-nozomi-key-DO-NOT-PRINT"
    env.update(MEMECOIN_SLOT_LANE_NOZOMI_AB="1", MEMECOIN_NOZOMI_ALL_LANES="1",
               MEMECOIN_NOZOMI_ALL_B="1", MEMECOIN_NOZOMI_API_KEY=key,
               MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED="1")
    result = check(env=env)
    assert result["ok"] and result["reasons"] == []
    assert key not in json.dumps(result)
    assert result["provider"] == "shyft" and result["speed_proof"] == "unproved"


@pytest.mark.parametrize("key", [None, "", "  "])
def test_preflight_nozomi_missing_key_refuses_through_submission_config(key):
    env = candidate_env()
    env.update(MEMECOIN_SLOT_LANE_NOZOMI_AB="1", MEMECOIN_NOZOMI_API_KEY=key)
    result = check(env=env)
    assert not result["ok"] and "submission_config_invalid" in result["reasons"]


def test_provider_stage_can_activate_and_roll_back_bids_without_policy_changes(tmp_path):
    from scripts.research import shyft_migration_preflight as p
    source, staged, fomo = (tmp_path / name for name in ("source", "staged", "fomo"))
    policy = "MEMECOIN_REAL_ENABLED=1\nMEMECOIN_REAL_MAX_COPY_PREMIUM_PCT=35\n"
    fomo.write_text("FOMO_USER=unchanged\n")
    for before, after in (("0", "1"), ("1", "0")):
        source.write_text(policy + "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED=" + before + "\n")
        staged.write_text(policy + "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED=" + after + "\n")
        report = {"ok": True, **{name + "_sha256": p.file_digest(path) for name, path in
                  (("source_env", source), ("staged_env", staged), ("fomo_env", fomo))}}
        assert p.validate_provider_stage(source, fomo, staged, report) == ()
        staged.write_text(staged.read_text().replace("MEMECOIN_REAL_ENABLED=1", "MEMECOIN_REAL_ENABLED=0"))
        report["staged_env_sha256"] = p.file_digest(staged)
        assert "provider_stage_scope_changed" in p.validate_provider_stage(source, fomo, staged, report)


def test_stage_refuses_policy_custody_roster_change_and_source_drift(tmp_path):
    from scripts.research import shyft_migration_preflight as p
    assert callable(getattr(p, "validate_provider_stage", None)), "provider stage validator missing"
    source = tmp_path / ".env"
    staged = tmp_path / "candidate.env"
    fomo = tmp_path / ".fomo.env"
    source.write_text("MEMECOIN_SOLANA_KEYPAIR=data/key.json\nMEMECOIN_MAX_CONCURRENT=7\n")
    fomo.write_text("ONCHAIN_WATCH_SOLANA_OWNERS=a,b\n")
    staged.write_text(source.read_text() + "MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\n")
    proof = {"ok": True, "source_env_sha256": p.file_digest(source),
             "fomo_env_sha256": p.file_digest(fomo), "staged_env_sha256": p.file_digest(staged)}
    assert p.validate_provider_stage(source, fomo, staged, proof) == ()
    source.write_text(source.read_text() + "PEER_CHANGE=1\n")
    assert "source_env_changed" in p.validate_provider_stage(source, fomo, staged, proof)
    source.write_text("MEMECOIN_SOLANA_KEYPAIR=data/key.json\nMEMECOIN_MAX_CONCURRENT=7\n")
    for key, value in (("MEMECOIN_MAX_CONCURRENT", "8"), ("MEMECOIN_SOLANA_KEYPAIR", "data/other.json"),
                       ("ONCHAIN_WATCH_SOLANA_OWNERS", "different")):
        staged.write_text(source.read_text() + f"{key}={value}\n")
        proof["staged_env_sha256"] = p.file_digest(staged)
        assert "provider_stage_scope_changed" in p.validate_provider_stage(source, fomo, staged, proof)


@pytest.mark.parametrize("field", ["allocations", "topology", "capabilities"])
def test_malformed_evidence_is_a_safe_refusal(field):
    record = allocation_record()
    record[field] = TOKEN
    result = check(record=record)
    assert not result["ok"] and TOKEN not in json.dumps(result)


def test_bad_capacity_fields_never_echo_a_secret():
    record = allocation_record()
    record["allocations"][0]["grpc_filters"] = TOKEN
    record["topology"]["peak_connections"] = RPC_KEY
    result = check(record=record)
    assert not result["ok"]
    assert TOKEN not in json.dumps(result) and RPC_KEY not in json.dumps(result)


def test_provider_stage_can_pace_tape_without_changing_trade_policy(tmp_path):
    from scripts.research import shyft_migration_preflight as p
    source, staged, fomo = (tmp_path / name for name in ('source', 'staged', 'fomo'))
    policy = 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT=35\nMEMECOIN_SOLANA_KEYPAIR=data/key.json\n'
    source.write_text(policy + 'MEMECOIN_SOL_TAPE_MIN_CALL_S=0.005\nMEMECOIN_SOL_TAPE_MIN_CALL_S=1.2\n')
    staged.write_text(policy + 'MEMECOIN_SOL_TAPE_MIN_CALL_S=0.1\nMEMECOIN_SOL_TAPE_MIN_CALL_S=0.1\n')
    fomo.write_text('FOMO_USER=unchanged\n')
    report = {'ok': True, **{name + '_sha256': p.file_digest(path) for name, path in
        [('source_env', source), ('staged_env', staged), ('fomo_env', fomo)]}}
    assert p.validate_provider_stage(source, fomo, staged, report) == ()
    assert p.read_env(staged)['MEMECOIN_SOL_TAPE_MIN_CALL_S'] == '0.1'
    staged.write_text(staged.read_text().replace('PREMIUM_PCT=35', 'PREMIUM_PCT=36'))
    report['staged_env_sha256'] = p.file_digest(staged)
    assert 'provider_stage_scope_changed' in p.validate_provider_stage(source, fomo, staged, report)


def test_provider_stage_can_pin_and_rollback_only_tape_transport(tmp_path):
    from scripts.research import shyft_migration_preflight as p
    source, staged, fomo = (tmp_path / name for name in ('source', 'staged', 'fomo'))
    policy = 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT=35\nMEMECOIN_SOLANA_KEYPAIR=data/key.json\n'
    fomo.write_text('FOMO_USER=unchanged\n')
    for before, after in (('', '84.32.186.182'), ('84.32.186.182', '')):
        source.write_text(policy + 'MEMECOIN_SHYFT_TAPE_CONNECT_IP=' + before + '\n')
        staged.write_text(policy + 'MEMECOIN_SHYFT_TAPE_CONNECT_IP=' + after + '\n')
        report = {'ok': True, **{name + '_sha256': p.file_digest(path) for name, path in
            [('source_env', source), ('staged_env', staged), ('fomo_env', fomo)]}}
        assert p.validate_provider_stage(source, fomo, staged, report) == ()
        staged.write_text(staged.read_text().replace('PREMIUM_PCT=35', 'PREMIUM_PCT=36'))
        report['staged_env_sha256'] = p.file_digest(staged)
        assert 'provider_stage_scope_changed' in p.validate_provider_stage(source, fomo, staged, report)
