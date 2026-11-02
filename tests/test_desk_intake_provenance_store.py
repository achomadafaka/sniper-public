"""New input cohorts authenticate source receipts in the same transaction as admission."""
import copy
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from test_desk_intake_source_proof import emission, dayfile
from test_memecoin_desk_contract import evidence, policy
from test_memecoin_desk_pipeline import databases, POLICY  # noqa: F401
from test_memecoin_desk_intake_assembler_runner import run  # noqa: F401
from test_memecoin_desk_pumpswap_candidates import NOW


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def setup(tmp_path, *, marked=True, cid="intake"):
    c = emission(tmp_path)
    conn = D.connect(tmp_path / "desk.db")
    hard = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 1}}
    if marked:
        hard["intake_source_version"] = "desk-intake-source-v1"
    D.register_cohort(conn, cohort_id=cid, producer="U", prompt_version="desk-judge-v27" if marked else "desk-judge-v26",
        policy={"venue": "pumpswap", "hard_filter": hard}, created_at=iso(NOW - 3600))
    return conn, c, hard


def admit(conn, c, tmp_path, **kw):
    return desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 2),
        intake_dir=tmp_path / "intake", source_now_fn=lambda: NOW + 1, **kw)


def receipt(conn, c, hard, *, at=NOW + 3, cid="intake"):
    fn = getattr(D, "get_intake_source_receipt", None)
    assert callable(fn), "trusted source receipt retrieval is missing"
    return fn(conn, c["ticket_id"], cohort_id=cid, hard_policy=hard, candidate=c, as_of=at)


def test_marked_admission_cannot_use_an_unverified_candidate(tmp_path):
    conn, c, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="intake_source"):
        desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 2))
    assert conn.execute("SELECT count(*) FROM desk_ticket_heads").fetchone()[0] == 0


def test_receipt_and_admission_are_atomic_immutable_and_bound(tmp_path):
    conn, c, hard = setup(tmp_path)
    got = admit(conn, c, tmp_path)
    proof = receipt(conn, c, hard)
    assert got["is_primary"] is True and proof["status"] == "pass"
    assert proof["candidate_hash"] == hashlib.sha256(D._json(K.validate_candidate(c)).encode()).hexdigest()
    assert proof["policy_hash"] == hashlib.sha256(D._json(hard).encode()).hexdigest()
    assert conn.execute("SELECT count(*) FROM desk_intake_source_receipts").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM desk_intake_source_receipts")
    conn.rollback()
    assert receipt(conn, c, {**hard, "round_trip_loss_pct_max": 9}) is None
    assert receipt(conn, c, hard, cid="foreign") is None
    assert receipt(conn, c, hard, at=NOW) is None


def test_failed_receipt_insert_rolls_back_snapshot_head_and_emission(tmp_path):
    conn, c, _ = setup(tmp_path)
    conn.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON desk_intake_source_receipts "
                 "BEGIN SELECT RAISE(ABORT,'receipt_write_failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="receipt_write_failed"):
        admit(conn, c, tmp_path)
    assert all(conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0 for table in
               ("desk_snapshots", "desk_ticket_heads", "desk_candidate_emissions", "desk_intake_source_receipts"))


def test_replay_after_source_prune_reuses_only_trusted_receipt(tmp_path):
    conn, c, hard = setup(tmp_path)
    admit(conn, c, tmp_path)
    proof = receipt(conn, c, hard)
    dayfile(tmp_path, c).unlink()
    got = desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 3600))
    assert got["is_primary"] is False and got["reason"] == "duplicate"
    assert receipt(conn, c, hard, at=NOW + 3601) == proof
    conn.execute("DROP TABLE desk_intake_source_receipts")  # simulate missing legacy/corrupt storage, never caller JSON
    with pytest.raises(ValueError, match="intake_source"):
        desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 3602))
    assert conn.execute("SELECT count(*) FROM desk_candidate_emissions").fetchone()[0] == 1


def test_new_admission_rejects_forged_listing_and_failed_poll_before_any_store_write(tmp_path):
    conn, c, _ = setup(tmp_path)
    c["intake"]["provider_rank"] += 1
    with pytest.raises(ValueError, match="intake_source_mismatch"):
        admit(conn, c, tmp_path)
    c["intake"]["provider_rank"] -= 1
    with sqlite3.connect(dayfile(tmp_path, c)) as source:
        source.execute("UPDATE polls SET status='error',error='timeout'")
    with pytest.raises(ValueError, match="intake_source_poll"):
        admit(conn, c, tmp_path)
    assert conn.execute("SELECT count(*) FROM desk_candidate_emissions").fetchone()[0] == 0


def test_evaluate_uses_store_receipt_and_ignores_caller_supplied_pass_blob(tmp_path):
    conn, c, hard = setup(tmp_path)
    admit(conn, c, tmp_path)
    e = evidence(c)
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    e["checks"]["token_safety"].update(supply_atomic=state["supply_atomic"], decimals=state["base_decimals"])
    e["known_at"] = iso(NOW + 3)
    e["market_cut"] = {"receipt_cut": c["snapshot"]["emission_at"],
                       "cut_slot": state["slot"]+1, "block_known_at": NOW+3}
    e["intake_source"] = {"status": "pass", "mint": "forged"}
    before = copy.deepcopy(e)
    filtered = desk.evaluate_candidate(conn, c["ticket_id"], evidence=e, policy=hard, now_iso=iso(NOW + 4))
    assert filtered["verdict"] == "PASS", filtered["reason"]
    assert filtered["inputs"]["evidence"]["intake_source"] == receipt(conn, c, hard, at=NOW + 4)
    assert e == before  # no caller-owned global/dict mutation


def test_unmarked_cohort_keeps_existing_admission_behavior(tmp_path):
    conn, c, _ = setup(tmp_path, marked=False)
    dayfile(tmp_path, c).unlink()
    got = desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 2))
    assert got["is_primary"] is True
    assert conn.execute("SELECT count(*) FROM desk_intake_source_receipts").fetchone()[0] == 0


def test_invalid_v27_policy_refuses_before_either_registry_is_written(databases):
    with pytest.raises(ValueError, match="ps_inputs"):
        desk.provision_cohort(*databases, producer="U", policy=POLICY,
                              prompt_version="desk-judge-v27", created_at=iso(NOW))
    assert databases[0].execute("SELECT count(*) FROM desk_cohorts").fetchone()[0] == 0
    assert databases[1].execute("SELECT count(*) FROM memecoin_desk_cohorts").fetchone()[0] == 0


def test_actual_intake_assembler_passes_explicit_state_root_and_clock(run, monkeypatch):
    from test_desk_ps_loss_integration import provision
    provision(run["desk"])  # the stricter new cohort needs explicit loss-schema/index proof
    conn = D.connect(run["db"])
    original = D.get_cohort(conn, "intake")["policy"]
    marked = {**original, "hard_filter": {**original["hard_filter"], "intake_source_version": "desk-intake-source-v1"}}
    D.register_cohort(conn, cohort_id="marked", producer="U", prompt_version="desk-judge-v27", policy=marked,
                      created_at=iso(run["clock"]() - 3600))
    conn.close()
    monkeypatch.setenv("MEMECOIN_DESK_INTAKE_COHORT_ID", "marked")
    assert run["invoke"]() == 0
    conn = D.connect(run["db"])
    [stage] = conn.execute("SELECT verdict,reason,inputs_json FROM desk_tickets WHERE stage='hard_filter'").fetchall()
    assert (stage[0], stage[1]) == ("PASS", "pass")
    proof = json.loads(stage[2])["evidence"]["intake_source"]
    assert proof["status"] == "pass" and proof["known_at"] == run["clock"]()
    assert conn.execute("SELECT cohort_id FROM desk_intake_source_receipts").fetchone()[0] == "marked"


def test_admission_timestamp_cannot_precede_source_verification_completion(tmp_path):
    conn, c, hard = setup(tmp_path)
    got = desk.ingest_candidate(conn, c, cohort_id="intake", now_iso=iso(NOW + 2),
        intake_dir=tmp_path / "intake", source_now_fn=lambda: NOW + 5)
    assert got["created_at"] == iso(NOW + 5)
    assert got["emitted_at"] == c["emitted_at"]
    assert receipt(conn, c, hard, at=NOW + 6)["known_at"] == NOW + 5


def test_unknown_source_version_refuses_direct_registered_cohort(tmp_path):
    conn, c, hard = setup(tmp_path)
    hard = {**hard, "intake_source_version": "unregistered-v2"}
    D.register_cohort(conn, cohort_id="bad", producer="U", prompt_version="desk-judge-v27",
        policy={"venue": "pumpswap", "hard_filter": hard}, created_at=iso(NOW - 3600))
    with pytest.raises(ValueError, match="intake_source_version"):
        desk.ingest_candidate(conn, c, cohort_id="bad", now_iso=iso(NOW + 2))


def test_corrupt_receipt_json_shape_is_unknown_not_an_exception(tmp_path):
    conn, c, hard = setup(tmp_path)
    admit(conn, c, tmp_path)
    conn.execute("DROP TRIGGER desk_intake_source_receipts_no_update")
    bad = "[]"
    conn.execute("UPDATE desk_intake_source_receipts SET receipt_json=?,receipt_hash=?",
                 (bad, hashlib.sha256(bad.encode()).hexdigest()))
    conn.commit()
    assert receipt(conn, c, hard) is None
