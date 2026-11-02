import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


MANIFEST_PATH = Path("sniper/execution/lifi_4663_manifest.json")
CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"


def _activated_conn(release, manifest_hash):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=5,
        reserved_at="2026-08-30T20:00:00+00:00", network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        conn, pending_id, chain_id="4663", custody_wallet=CUSTODY, token=TOKEN,
        release_sha=release, manifest_hash=manifest_hash,
        created_at="2026-08-30T20:00:00+00:00",
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="exit_settled",
        event_type="exit_settled", created_at="2026-08-30T20:00:00+00:00",
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="exit_settled", new_state="allowance_zero",
        event_type="allowance_zero", created_at="2026-08-30T20:00:00+00:00",
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="allowance_zero", new_state="position_closed",
        event_type="position_closed", created_at="2026-08-30T20:00:00+00:00",
    )
    assert m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(TOKEN, CUSTODY, "0")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7200,
        created_at="2026-08-30T20:00:00+00:00",
    )
    assert m.activate_lifi_release(
        conn, release_sha=release, manifest_hash=manifest_hash, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=[(TOKEN, CUSTODY)], quota_limit=12000,
        quota_remaining=11990, quota_reset=7200,
        activated_at="2026-08-30T20:00:00+00:00",
    )
    return conn


def test_release_manifest_has_exact_chain_authority():
    manifest, digest = cfg.load_lifi_manifest(MANIFEST_PATH)

    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    assert digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert manifest["chain_id"] == "4663"
    assert manifest["allow_exchanges"] == ["nordstern"]
    assert manifest["top_level"]["type"] == "lifi"
    assert manifest["top_level"]["tool"] == "nordstern"
    assert [(step["tool"], step["type"]) for step in manifest["steps"]] == [
        ("feeCollection", "protocol"),
        ("nordstern", "swap"),
    ]
    assert manifest["fee"] == {
        "denominator": 10_000,
        "integrator_fee": 0,
        "numerator": 25,
    }


def test_release_manifest_pins_all_executable_authority():
    manifest, _digest = cfg.load_lifi_manifest(MANIFEST_PATH)

    assert manifest["top_level"] == {
        "abi": "swapTokensMultipleV3ERC20ToERC20(bytes32,string,string,address,uint256,(address,address,address,address,uint256,bytes,bool)[])",
        "code_hash": "0xca0fd158089c97d9e77828a4254bc7037be8ee6f784e45d80204df0e56013101",
        "facet": "0xb129ce9c3fcd55726ff314a2764d3937fa496071",
        "facet_code_hash": "0xdb3f706ca7f78237a197ccab9300628db168f0bc6c509835771200f9c163daa5",
        "selector": "0x5fd9ae2e",
        "spender": "0xb477751b76cf82d00a686a1232f5fcd772414af3",
        "target": "0xb477751b76cf82d00a686a1232f5fcd772414af3",
        "tool": "nordstern",
        "types": [
            "bytes32", "string", "string", "address", "uint256",
            "(address,address,address,address,uint256,bytes,bool)[]",
        ],
        "type": "lifi",
    }
    assert manifest["steps"] == [
        {
            "abi": "forwardERC20Fees(address,(address,uint256)[])",
            "approve_to": "0xf4bffe4dfc693f37715a47c15bda8af9ed8f7cf1",
            "code_hash": "0x7ee455a6853068874bfd201f93d6383ed6d88934a922316db5575b057e2ebe74",
            "selector": "0x332d746b",
            "spender": "0xb477751b76cf82d00a686a1232f5fcd772414af3",
            "target": "0xf4bffe4dfc693f37715a47c15bda8af9ed8f7cf1",
            "tool": "feeCollection",
            "types": ["address", "(address,uint256)[]"],
            "type": "protocol",
        },
        {
            "abi": "IceCreamSwap()",
            "approve_to": "0x603206d6105217dd972e4ab30676a220ca393346",
            "code_hash": "0x5c9dd55aa2b736702f8cc44aef400fca06ea1b708f35f32978bc4ba37ec4d30f",
            "executor": "0x2ca37ff95caf25366ef16fc2e655b78a165d125f",
            "executor_code_hash": "0x56bb014a053e10949003ac985e730a9e51e18f4734ad492841824941020ab20a",
            "selector": "0x3f0bde25",
            "spender": "0xb477751b76cf82d00a686a1232f5fcd772414af3",
            "target": "0x603206d6105217dd972e4ab30676a220ca393346",
            "tool": "nordstern",
            "types": [],
            "type": "swap",
        },
    ]


def test_release_manifest_rejects_permit_authority():
    manifest, _digest = cfg.load_lifi_manifest(MANIFEST_PATH)

    assert set(manifest["reject"]["addresses"]) == {
        "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "0x8eabb4e117fb70b346592e013855f6d825f50af1",
    }
    assert set(manifest["reject"]["selectors"]) >= {
        "0x0193b9fc",
        "0x4561136e",
        "0xd505accf",
        "0xd7a08473",
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(chain_id="8453"),
        lambda value: value.update(allow_exchanges=[]),
        lambda value: value["steps"].append(dict(value["steps"][1])),
        lambda value: value["top_level"].pop("facet_code_hash"),
        lambda value: value["steps"][1].pop("executor"),
        lambda value: value["steps"][1].pop("executor_code_hash"),
        lambda value: value["reject"].update(selectors=[]),
    ],
)
def test_manifest_loader_rejects_incomplete_or_wrong_authority(tmp_path, change):
    value = json.loads(MANIFEST_PATH.read_text())
    change(value)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))

    with pytest.raises(RuntimeError):
        cfg.load_lifi_manifest(path)


def test_lifi_mode_defaults_off_and_rejects_unknown_value(monkeypatch):
    monkeypatch.delenv("MEMECOIN_LIFI_MODE", raising=False)
    assert cfg.memecoin_lifi_mode() == "disabled"

    monkeypatch.setenv("MEMECOIN_LIFI_MODE", "unsafe")
    with pytest.raises(RuntimeError):
        cfg.memecoin_lifi_mode()


def test_authenticated_mode_requires_static_authority(monkeypatch):
    monkeypatch.setenv("MEMECOIN_LIFI_MODE", "authenticated_funded")
    monkeypatch.delenv("MEMECOIN_LIFI_API_KEY", raising=False)
    monkeypatch.delenv("MEMECOIN_LIFI_PROOF_RPC_URL_4663", raising=False)

    with pytest.raises(RuntimeError):
        cfg.validate_lifi_config()


def test_authenticated_mode_binds_candidate_release(monkeypatch, tmp_path):
    _manifest, manifest_hash = cfg.load_lifi_manifest()
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "sha": "a" * 40,
        "lifi_manifest_sha256": manifest_hash,
    }))
    values = {
        "MEMECOIN_LIFI_MODE": "authenticated_funded",
        "MEMECOIN_LIFI_API_URL": "https://li.quest/v1/quote",
        "MEMECOIN_LIFI_API_KEY": "test-key",
        "MEMECOIN_LIFI_PROOF_RPC_URL_4663": "https://proof.example",
        "MEMECOIN_LIFI_ACTIVE_RELEASE_SHA": "a" * 40,
        "MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH": manifest_hash,
        "SNIPER_MEMECOIN_DEPLOY_MANIFEST": str(candidate),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="activation"):
        cfg.validate_lifi_config()

    conn = _activated_conn("a" * 40, manifest_hash)
    activation = cfg.validate_lifi_config(conn=conn, custody_wallet=CUSTODY)
    assert activation["quota_remaining"] == "11990"

    monkeypatch.setenv("MEMECOIN_LIFI_ACTIVE_RELEASE_SHA", "b" * 40)
    with pytest.raises(RuntimeError):
        cfg.validate_lifi_config()


def test_authenticated_mode_requires_https_api(monkeypatch, tmp_path):
    _manifest, manifest_hash = cfg.load_lifi_manifest()
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({
        "sha": "a" * 40,
        "lifi_manifest_sha256": manifest_hash,
    }))
    values = {
        "MEMECOIN_LIFI_MODE": "authenticated_funded",
        "MEMECOIN_LIFI_API_URL": "http://li.quest/v1/quote",
        "MEMECOIN_LIFI_API_KEY": "test-key",
        "MEMECOIN_LIFI_PROOF_RPC_URL_4663": "https://proof.example",
        "MEMECOIN_LIFI_ACTIVE_RELEASE_SHA": "a" * 40,
        "MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH": manifest_hash,
        "SNIPER_MEMECOIN_DEPLOY_MANIFEST": str(candidate),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError):
        cfg.validate_lifi_config()
