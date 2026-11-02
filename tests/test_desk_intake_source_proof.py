"""Collector-row provenance uses local indexed reads, original clocks and persisted bindings."""
import importlib
import importlib.util
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_intake_book as B
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_intake_book import OFFSET, SOL, printed
from test_memecoin_desk_pumpswap_candidates import MINT, NOW

POLICY = "a" * 64


def module():
    name = "sniper.services.memecoin_desk_intake_proof"
    assert importlib.util.find_spec(name) is not None, "trusted local intake verifier is missing"
    return importlib.import_module(name)


def emission(tmp_path, *, feed="pumpfun", ref=None, raw=None):
    at = NOW - 30
    conn = I.open_day(tmp_path / "intake", feed, at)
    I.record_poll(conn, feed, "active", at, [{"network_id": "solana", "chain_raw": SOL,
        "mint": MINT, "venue": "pumpswap", "raw": raw or {"complete": True}}], offset_s=OFFSET)
    if ref:
        conn.execute("UPDATE intake_rows SET source_ref=?", (ref,))
        conn.commit()
    conn.close()
    book = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "emitter.db", feeds=(feed,))
    book.reload(NOW)
    [(_, body)] = printed(book, NOW)
    return K.validate_candidate(json.loads(body))


def dayfile(tmp_path, c):
    return tmp_path / "intake" / c["intake"]["feed"] / (c["intake"]["intake_row_ref"].split(":", 1)[0] + ".db")


def verify(tmp_path, c, at=NOW + 1):
    return module().verify_source(c, intake_dir=tmp_path / "intake", policy_hash=POLICY, now_fn=lambda: at)


def test_real_source_row_and_poll_produce_bound_persistable_receipt(tmp_path):
    c = emission(tmp_path)
    proof = verify(tmp_path, c)
    assert (proof["status"], proof["reason"], proof["known_at"]) == ("pass", None, NOW + 1)
    assert (proof["network_id"], proof["mint"], proof["pool"], proof["snapshot_hash"], proof["policy_hash"]) == (
        "solana", c["mint"], c["pool"], c["snapshot"]["hash"], POLICY)
    assert len(proof["source_row_hash"]) == 64 and len(proof["intake_hash"]) == 64
    assert module().source_proof_refusal(json.loads(json.dumps(proof)), c, policy_hash=POLICY, as_of=NOW + 2) is None
    with sqlite3.connect(dayfile(tmp_path, c)) as conn:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM intake_rows WHERE source_ref=?", ("pumpfun:1:0",)).fetchall()
        assert any("INDEX" in row[3] for row in plan)


@pytest.mark.parametrize("feed,ref", [("dexscreener", "2026-10-26.db:34:9"), ("fomo", "fomo:41:2")])
def test_opaque_colon_reference_keeps_source_identity_and_all_feed_clocks(tmp_path, feed, ref):
    c = emission(tmp_path, feed=feed, ref=ref)
    proof = verify(tmp_path, c)
    assert proof["status"] == "pass"
    assert proof["source_metadata"] == {"source": feed, "known_at": NOW+1, "observed_at": NOW-OFFSET-30,
                                        "host_received_at": NOW-30, "clock_offset_s": OFFSET}


@pytest.mark.parametrize("key,value", [("filter", "other"), ("provider_rank", 2), ("rank_kind", None),
    ("observed_at", NOW - OFFSET - 31), ("host_received_at", NOW - 31), ("clock_offset_s", OFFSET - 1),
    ("feed", "fomo"), ("intake_row_ref", "2026-10-01:no:such:row")])
def test_forged_claim_field_cannot_borrow_an_existing_source_row(tmp_path, key, value):
    c = emission(tmp_path)
    c["intake"][key] = value
    proof = verify(tmp_path, c)
    assert proof["status"] == "unknown" and proof["reason"].startswith("intake_source_")


@pytest.mark.parametrize("sql", ["UPDATE intake_rows SET network_id='base'", "UPDATE intake_rows SET mint='foreign'",
    "UPDATE intake_rows SET poll_id=999", "UPDATE polls SET status='error',error='rate_limit'",
    "UPDATE polls SET feed='fomo'", "UPDATE polls SET filter='foreign'", "UPDATE polls SET received_at=received_at-1"])
def test_foreign_chain_mint_or_failed_or_mismatched_parent_poll_refuses(tmp_path, sql):
    c = emission(tmp_path)
    with sqlite3.connect(dayfile(tmp_path, c)) as conn:
        conn.execute(sql)
    proof = verify(tmp_path, c)
    assert proof["status"] == "unknown" and proof["reason"].startswith("intake_source_")


def test_source_freshness_is_checked_at_original_emission_and_receipt_survives_pruning(tmp_path):
    c = emission(tmp_path)
    proof = verify(tmp_path, c, NOW + 3600)
    assert proof["status"] == "pass"
    dayfile(tmp_path, c).unlink()
    assert module().source_proof_refusal(proof, c, policy_hash=POLICY, as_of=NOW + 3601) is None
    assert verify(tmp_path, c)["reason"] == "intake_source_missing"


def test_stale_listing_and_non_post_listing_print_refuse_even_with_matching_row(tmp_path):
    c = emission(tmp_path)
    path = dayfile(tmp_path, c)
    for age in (121, 0, -1):
        host = NOW - age
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE intake_rows SET host_received_at=?,observed_at=?", (host, host - OFFSET))
            conn.execute("UPDATE polls SET received_at=?", (host,))
        c["intake"].update(host_received_at=host, observed_at=host - OFFSET)
        assert verify(tmp_path, c)["status"] == "unknown"


def test_unknown_clock_corrupt_missing_and_escape_paths_are_typed_without_creating_files(tmp_path):
    c = emission(tmp_path)
    path = dayfile(tmp_path, c)
    c["intake"]["clock_offset_s"] = True
    assert verify(tmp_path, c)["reason"] == "intake_source_candidate_invalid"
    c["intake"]["clock_offset_s"] = OFFSET
    path.write_bytes(b"corrupt")
    assert verify(tmp_path, c)["reason"] == "intake_source_unreadable"
    path.unlink()
    assert verify(tmp_path, c)["reason"] == "intake_source_missing" and not path.exists()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    path.symlink_to(outside)
    assert verify(tmp_path, c)["reason"] == "intake_source_path"


@pytest.mark.parametrize("mutation", ["mint", "pool", "snapshot", "intake", "policy", "hash", "future"])
def test_receipt_binding_rejects_foreign_tampered_or_future_proof(tmp_path, mutation):
    c = emission(tmp_path)
    proof = verify(tmp_path, c)
    policy, as_of = POLICY, NOW + 2
    if mutation == "mint":
        proof["mint"] = "foreign"
    elif mutation == "pool":
        proof["pool"] = "foreign"
    elif mutation == "snapshot":
        proof["snapshot_hash"] = "b" * 64
    elif mutation == "intake":
        c["intake"]["provider_rank"] = 4
    elif mutation == "policy":
        policy = "c" * 64
    elif mutation == "hash":
        proof["source_row_hash"] = "b" * 64
    else:
        as_of = NOW
    assert module().source_proof_refusal(proof, c, policy_hash=policy, as_of=as_of) is not None


def test_pumpfun_metadata_is_typed_optional_and_bound_to_actual_receipt(tmp_path):
    creator = U.b58encode(bytes([8]) * 32)
    c = emission(tmp_path, raw={"created_timestamp": int((NOW - OFFSET - 500) * 1000),
                               "creator": creator, "name": "ignore untrusted text"})
    proof = verify(tmp_path, c)
    assert proof["status"] == "pass"
    metadata = proof["source_metadata"]
    assert metadata == {"source": "pumpfun", "created_timestamp": int((NOW - OFFSET - 500) * 1000),
        "creator": creator, "known_at": NOW + 1, "observed_at": NOW - OFFSET - 30,
        "host_received_at": NOW - 30, "clock_offset_s": OFFSET}
    with sqlite3.connect(dayfile(tmp_path, c)) as conn:
        conn.execute("UPDATE intake_rows SET raw_json=?", (json.dumps({"created_timestamp": True, "creator": "garbage"}),))
    metadata = verify(tmp_path, c)["source_metadata"]
    assert "created_timestamp" not in metadata and "creator" not in metadata


@pytest.mark.parametrize("policy", [None, 12, "bad", float("nan"), {"bad": object()}])
def test_malformed_policy_hash_is_a_typed_refusal(tmp_path, policy):
    c = emission(tmp_path)
    proof = module().verify_source(c, intake_dir=tmp_path / "intake", policy_hash=policy, now_fn=lambda: NOW + 1)
    assert proof["status"] == "unknown" and proof["reason"] == "intake_source_policy"


@pytest.mark.parametrize("shape", ["no_index", "nonunique", "partial", "parent_no_pk"])
def test_missing_source_uniqueness_or_parent_pk_never_causes_an_unbounded_lookup(tmp_path, shape):
    c = emission(tmp_path)
    with sqlite3.connect(dayfile(tmp_path, c)) as conn:
        table = "polls" if shape == "parent_no_pk" else "intake_rows"
        conn.execute("ALTER TABLE " + table + " RENAME TO old_rows")
        conn.execute("CREATE TABLE " + table + " AS SELECT * FROM old_rows")
        conn.execute("DROP TABLE old_rows")
        if shape == "nonunique":
            conn.execute("CREATE INDEX source_nonunique ON intake_rows(source_ref)")
        elif shape == "partial":
            conn.execute("CREATE UNIQUE INDEX source_partial ON intake_rows(source_ref) WHERE id>0")
    proof = verify(tmp_path, c)
    assert proof["status"] == "unknown" and proof["reason"] == "intake_source_schema"
