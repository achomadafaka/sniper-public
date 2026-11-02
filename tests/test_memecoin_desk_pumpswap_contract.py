"""PumpSwap identity, exact pool facts and venue isolation; no network or funded writes."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_universe as U
from sniper.services import memecoin_desk_cluster as C
from test_memecoin_desk_contract import candidate as curve_candidate, rehash, NOW, AT, MINT, SIG

ROOT = Path(__file__).resolve().parents[1]
WSOL = "So11111111111111111111111111111111111111112"
AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def pool(mint=MINT):
    m = bytes(Pubkey.from_string(mint))
    authority = bytes(Pubkey.find_program_address([b"pool-authority", m], Pubkey.from_string(U.PUMP_PROGRAM))[0])
    return str(Pubkey.find_program_address([b"pool", b"\0\0", authority, m, bytes(Pubkey.from_string(WSOL))],
                                          Pubkey.from_string(AMM))[0])


def ps_candidate():
    c = curve_candidate()
    c.update(schema="desk-candidate/pumpswap-v1", venue="pumpswap", pool=pool())
    key = f"solana|pumpswap|{c['pool']}|{MINT}|7|{SIG}|0"
    c["ticket_id"] = "desk:U:ps:" + hashlib.sha256(key.encode()).hexdigest()[:24]
    state = {"base_reserve_atomic": str(2**64 - 1), "quote_reserve_atomic": "2000000000", "quote_mint": WSOL,
             "base_decimals": 6, "supply_atomic": "1000000000000000", "virtual_quote_atomic": "0",
             "lp_fee_bps": 20, "protocol_fee_bps": 5, "creator_fee_bps": 30, "slot": 7, "map_known_at": NOW - 1}
    state.update(metadata_known_at=NOW - 1, metadata_slot=6, metadata_source="verified_mint:local")
    c["snapshot"]["fields"]["pool_state"] = {"value": state, "source": "pumpswap_grpc:pool_state",
        "known_at": NOW, "slot": 7, "completeness": "complete"}
    return rehash(c)


def store(tmp_path):
    conn = D.connect(tmp_path / "desk.db")
    for cid, venue in (("curve", None), ("ps", "pumpswap"), ("ps2", "pumpswap")):
        policy = {"clip_usd": 25}
        if venue:
            policy["venue"] = venue
        D.register_cohort(conn, cohort_id=cid, producer="U", prompt_version="v", policy=policy, created_at=AT)
    return conn


def ingest(conn, c, cid):
    c = K.validate_candidate(c)
    return D.record_candidate(conn, c, cohort_id=cid, now_iso=AT, cooldown_s=300)


def counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("desk_snapshots", "desk_ticket_heads", "desk_candidate_emissions"))


def test_pumpswap_envelope_and_uint64_facts_roundtrip_without_rewriting_curve():
    c = ps_candidate()
    assert K.validate_candidate(c)["snapshot"] == c["snapshot"]
    assert S.verify_u_emission(c, now=NOW + 1, max_age_s=60) == c
    sheet = K.render_judge_sheet(c)
    assert sheet["pool_state"] is None and sheet["verdict_price"] is None
    assert sheet["venue"] == "pumpswap"
    old = curve_candidate()
    assert K.validate_candidate(old)["snapshot"] == old["snapshot"]
    assert "venue" not in K.render_judge_sheet(old)


@pytest.mark.parametrize("key,value", [("venue", "pumpfun"), ("venue", None), ("producer", "H"),
                                       ("schema", "desk-candidate/v1")])
def test_pumpswap_envelope_refuses_mixed_or_unknown_versions(key, value):
    c = ps_candidate()
    c[key] = value
    with pytest.raises(ValueError):
        K.validate_candidate(c)


@pytest.mark.parametrize("key,value", [("base_reserve_atomic", "0"), ("base_reserve_atomic", 1),
    ("base_reserve_atomic", str(2**64)), ("quote_reserve_atomic", "-1"), ("supply_atomic", "0"),
    ("virtual_quote_atomic", "00"), ("base_decimals", True), ("base_decimals", 19),
    ("lp_fee_bps", -1), ("creator_fee_bps", 10000), ("slot", 0), ("slot", True),
    ("quote_mint", MINT), ("map_known_at", NOW + 1), ("map_known_at", True),
    ("virtual_quote_atomic", None), ("metadata_known_at", NOW + 1), ("metadata_slot", 8),
    ("metadata_slot", True), ("metadata_source", "untrusted free text")])
def test_complete_pool_state_refuses_invalid_or_unknown_values(key, value):
    c = ps_candidate()
    c["snapshot"]["fields"]["pool_state"]["value"][key] = value
    rehash(c)
    with pytest.raises(ValueError):
        K.validate_candidate(c)


def test_partial_pool_facts_can_be_retained_but_cannot_be_verified_as_an_emission():
    c = ps_candidate()
    row = c["snapshot"]["fields"]["pool_state"]
    row["value"]["virtual_quote_atomic"] = None
    row["completeness"] = "missing"
    c["snapshot"]["missing"] = ["pool_state"]
    rehash(c)
    assert K.validate_candidate(c)["snapshot"] == c["snapshot"]
    with pytest.raises(ValueError, match="emission_pool_state"):
        S.verify_u_emission(c, now=NOW + 1, max_age_s=60)


@pytest.mark.parametrize("change", ["pool", "ticket", "hash", "time", "missing_state"])
def test_pumpswap_verifier_binds_canonical_pool_ticket_snapshot_and_time(change):
    c = ps_candidate()
    if change == "pool":
        c["pool"] = S.curve_address(MINT)
        key = f"solana|pumpswap|{c['pool']}|{MINT}|7|{SIG}|0"
        c["ticket_id"] = "desk:U:ps:" + hashlib.sha256(key.encode()).hexdigest()[:24]
    elif change == "ticket":
        c["ticket_id"] = U.ticket_id("solana", MINT, 7, SIG, 0)
    elif change == "hash":
        c["snapshot"]["hash"] = "0" * 64
    elif change == "time":
        c["emitted_at"] = "2026-10-21T12:00:01+00:00"
    else:
        del c["snapshot"]["fields"]["pool_state"]
        rehash(c)
    with pytest.raises(ValueError):
        S.verify_u_emission(c, now=NOW + 2, max_age_s=60)


@pytest.mark.parametrize("pumpswap,cid", [(True, "curve"), (False, "ps")])
def test_wrong_venue_cohort_refuses_before_any_persistence(tmp_path, pumpswap, cid):
    conn = store(tmp_path)
    with pytest.raises(ValueError, match="venue_mismatch"):
        ingest(conn, ps_candidate() if pumpswap else curve_candidate(), cid)
    assert counts(conn) == (0, 0, 0)


def test_pumpswap_cannot_attach_to_another_cohort_active_or_cooldown(tmp_path):
    conn = store(tmp_path)
    ingest(conn, ps_candidate(), "ps")
    before = counts(conn)
    c = ps_candidate()
    c["ticket_id"] += "2"  # store accepts a validated opaque id; verifier checks source identity separately
    with pytest.raises(ValueError, match="cohort_attachment"):
        ingest(conn, c, "ps2")
    assert counts(conn) == before
    conn.execute("UPDATE desk_ticket_heads SET state='closed'")
    conn.commit()
    with pytest.raises(ValueError, match="cohort_attachment"):
        ingest(conn, c, "ps2")
    assert counts(conn) == before


def test_curve_and_pumpswap_same_mint_never_share_a_head(tmp_path):
    conn = store(tmp_path)
    ingest(conn, curve_candidate(), "curve")
    with pytest.raises(ValueError, match="cohort_attachment"):
        ingest(conn, ps_candidate(), "ps")
    assert counts(conn) == (1, 1, 1)


def test_curve_cluster_reader_refuses_pumpswap_before_budget(tmp_path):
    (tmp_path / "2026-10-21.jsonl").write_text(json.dumps(ps_candidate()) + "\n")
    refused = {}
    assert C.pending_candidates(tmp_path, set(), now_epoch=NOW + 1, max_age_s=60, refused=refused) == []
    assert refused == {"emission_venue": 1}


def test_provision_adds_venue_only_for_explicit_pumpswap():
    spec = importlib.util.spec_from_file_location("ps_provision", ROOT / "scripts/memecoin_desk_provision_cohort.py")
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    args = dict(judge_p_admit=1, round_trip_loss_pct_max=8,
                hook_allowlist=[], rail_bounds={"sl_pct": (10, 40)})
    old = p.frozen_policy(**args)
    assert "venue" not in old
    assert p.frozen_policy(**args, venue="pumpfun") == old
    ps = p.frozen_policy(**args, venue="pumpswap")
    assert ps["hard_filter"]["version"] == "hf2" and p.PROMPT_VERSION == "desk-judge-v18"
    assert ps["venue"] == "pumpswap"
    assert ps["admission_status"] == "unregistered"
    assert ps["ranker_version"] == "pumpswap-features-v1"
    assert ps["select_version"] == ps["label_version"] == "pumpswap-unregistered"
    assert ps["judge_p_admit"] == ps["sr_all_p"] == ps["sr_post_p"] == 0
    assert p.cohort_id(ps) != p.cohort_id(old)


def test_pumpswap_refuses_curve_progress_claim_even_with_a_valid_hash():
    c = ps_candidate()
    c["snapshot"]["fields"]["curve_pct"] = {"value": 75.0, "source": "pumpswap_grpc:pool_state",
        "known_at": NOW, "slot": 7, "completeness": "complete"}
    rehash(c)
    with pytest.raises(ValueError, match="field_value"):
        K.validate_candidate(c)


def test_curve_verifier_does_not_accept_a_venue_marker_on_v1():
    c = curve_candidate()
    c["pool"] = S.curve_address(MINT)
    c["ticket_id"] = U.ticket_id("solana", MINT, 7, SIG, 0)
    c["venue"] = "pumpswap"
    with pytest.raises(ValueError, match="emission_schema"):
        S.verify_u_emission(c, now=NOW + 1, max_age_s=60)


def test_curve_assembler_refuses_pumpswap_cohort_before_rpc_construction(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("ps_assembler", ROOT / "scripts/memecoin_desk_assembler.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    conn = store(tmp_path)
    monkeypatch.setenv("MEMECOIN_DESK_ASSEMBLER_ENABLED", "1")
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(a, "load_env_files", lambda *_: None)
    monkeypatch.setattr(a, "Rpc", lambda *_: pytest.fail("RPC was constructed before venue refusal"))
    with pytest.raises(SystemExit):
        a.main(["--desk-db", str(tmp_path / "desk.db"), "--cohort-id", "ps"], env_root=tmp_path)
    assert "emission_venue" in capsys.readouterr().err
    assert counts(conn) == (0, 0, 0)


def test_curve_assembler_refuses_pumpswap_row_without_ingest_or_safety_calls(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    spec = importlib.util.spec_from_file_location("ps_row_assembler", ROOT / "scripts/memecoin_desk_assembler.py")
    a = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(a)
    conn = D.connect(tmp_path / "desk.db")
    D.register_cohort(conn, cohort_id="curve", producer="U", prompt_version="v",
        policy={"evidence_wait_s": 2, "hard_filter": {}}, created_at=AT)
    day = datetime.now(timezone.utc).date().isoformat()
    directory = tmp_path / "data" / "desk" / "candidate_emissions"
    directory.mkdir(parents=True)
    (directory / f"{day}.jsonl").write_text(json.dumps(ps_candidate()) + "\n")
    monkeypatch.setenv("MEMECOIN_DESK_ASSEMBLER_ENABLED", "1")
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(a, "state_root", lambda: tmp_path)
    monkeypatch.setattr(a, "load_env_files", lambda *_: None)
    monkeypatch.setattr(a, "check_ticket", lambda *_a, **_k: pytest.fail("safety budget was spent"))
    assert a.main(["--desk-db", str(tmp_path / "desk.db"), "--cohort-id", "curve"],
                  env_root=tmp_path, rpc=object(), from_start=True) == 0
    rows = [json.loads(s) for s in (tmp_path / "data" / "desk" / "assembler" / f"{day}.jsonl").read_text().splitlines()]
    assert any(r.get("reason") == "emission_venue" for r in rows)
    assert counts(conn) == (0, 0, 0)


def test_unregistered_pumpswap_cohort_cannot_be_confirmed(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("ps_confirm_provision", ROOT / "scripts/memecoin_desk_provision_cohort.py")
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(D, "connect", lambda *_: pytest.fail("unregistered cohort opened a database"))
    with pytest.raises(SystemExit):
        p.main(["--judge-p-admit", "1", "--top10-pct-max", "50", "--creator-pct-max", "10",
                "--round-trip-loss-pct-max", "8", "--venue", "pumpswap", "--confirm"])
    assert "PumpSwap admission is unregistered" in capsys.readouterr().err


def test_pumpswap_emission_state_slot_must_match_its_trigger_slot():
    c = ps_candidate()
    c["snapshot"]["fields"]["pool_state"]["value"]["slot"] = 8
    c["snapshot"]["fields"]["pool_state"]["slot"] = 8
    rehash(c)
    with pytest.raises(ValueError, match="emission_pool_state"):
        S.verify_u_emission(c, now=NOW + 1, max_age_s=60)
