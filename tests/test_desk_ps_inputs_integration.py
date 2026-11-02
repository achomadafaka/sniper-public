"""New-cohort inputs freeze once across call/record/restart; legacy calls stay unchanged."""
import base64
import copy
import hashlib
import json
import zlib
from contextlib import nullcontext
from datetime import datetime, timezone

import pytest

from scripts import memecoin_desk_judge_worker as W
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk_judge as J
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_paper as P
from sniper.services import memecoin_desk_ps_facts as F
from test_desk_ps_inputs_contract import enabled
from test_desk_ps_model_facts import NOW, CUT, tape
from test_memecoin_desk_contract import rehash
from test_memecoin_desk_judge import databases, model_returning, enter  # noqa: F401


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Clock:
    def __init__(self):
        self.t = datetime.fromtimestamp(NOW + 1, timezone.utc)

    def __call__(self):
        return self.t


def ready(db, tmp, *, new=True):
    c, _ = tape(tmp)
    c["snapshot"]["fields"]["pool_state"]["value"].update(virtual_quote_known_at=NOW - 5,
        virtual_quote_slot=7, virtual_quote_source="pumpswap_grpc:buy_event_virtual_quote")
    listing = NOW - 30
    source = I.open_day(tmp / "intake", "pumpfun", listing)
    I.record_poll(source, "pumpfun", "active", listing,
        [{"network_id": "solana", "chain_raw": "solana", "mint": c["mint"], "venue": "pumpswap", "raw": {}}], offset_s=0)
    source.close()
    c["intake"] = {"feed": "pumpfun", "filter": "active", "provider_rank": 1, "rank_kind": "list_position",
        "observed_at": listing, "host_received_at": listing, "clock_offset_s": 0,
        "intake_row_ref": iso(listing)[:10] + ":pumpfun:1:0"}
    rehash(c)
    policy = enabled()
    if not new:
        policy.pop("ps_inputs")
        policy["hard_filter"].pop("intake_source_version")
    cid = J.desk.provision_cohort(*db, producer="U", policy=policy,
        prompt_version="desk-judge-v27" if new else "desk-judge-v26", created_at=iso(NOW - 3600))
    ticket = J.desk.ingest_candidate(db[0], c, cohort_id=cid, now_iso=iso(NOW),
        intake_dir=tmp / "intake", source_now_fn=lambda: NOW)["ticket_id"]
    D.record_stage(db[0], ticket_id=ticket, stage="hard_filter", seat="code", verdict="PASS",
                   reason="fixture", inputs={"evidence": {"known_at": iso(NOW)}}, output={}, ts=iso(NOW))
    return ticket, cid


def refresh(c, ts, **_):
    c = copy.deepcopy(c)
    c["emitted_at"] = iso(ts)
    c["snapshot"]["emission_at"] = ts
    return rehash(c), {"receipt_cut": NOW, "cut_slot": CUT, "as_of": ts}


def block(ticket, candidate, meta, ts, *, tmp):
    f = F.facts_for(candidate, desk_dir=tmp, as_of=ts, receipt_cut=meta["receipt_cut"],
                    cut_slot=meta["cut_slot"], ours=frozenset(("ours",)))
    safe = {"authority_mint": "closed", "authority_freeze": "closed", "cluster_hit": None,
            "lp_locked_or_burned": None, "top10_pct": None, "creator_pct": None,
            "sell_probe": "ok", "sells_exist_60s": True}
    proof = {k: ticket["candidate"][k] for k in ("network_id", "mint", "pool")}
    proof.update(emission_snapshot_hash=ticket["snapshot_hash"], known_at=NOW,
                 probe_receipt_cut=meta["receipt_cut"], probe_cut_slot=meta["cut_slot"])
    return {"ps_facts": f, "safety": safe, "safety_proof": proof}


def forbidden(*_, **__):
    raise AssertionError("unexpected model, ledger or independent structure read")


def run(db, tmp, *, clock=None, calls=None, ps_inputs_fn=None, **kw):
    clock = clock or Clock()
    calls = [] if calls is None else calls
    return J.run_once(db[0], lambda: nullcontext(db[1]),
        call_model=model_returning(enter, calls=calls, clock=clock), system_prompt="fixture",
        worker="judge-test", now_fn=clock, calls_per_hour=40, asof_fn=refresh,
        structure_fn=forbidden, ps_inputs_fn=ps_inputs_fn, **kw)


def test_new_inputs_read_once_and_record_the_exact_serialized_sheet(databases, tmp_path):
    ticket, _ = ready(databases, tmp_path / "tape")
    reads, calls = [], []
    def inputs(*args):
        reads.append(args)
        return block(*args, tmp=tmp_path / "tape")
    run(databases, tmp_path / "tape", calls=calls, ps_inputs_fn=inputs)
    vet = next(s for s in D.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert len(reads) == len(calls) == 1
    assert calls[0] == vet["inputs"]["sheet"]
    assert vet["inputs"]["sheet"]["safety"]["sell_probe"] == "ok"
    result = databases[0].execute("SELECT result_json FROM desk_judge_calls").fetchone()[0]
    result = json.loads(result)
    exact = zlib.decompress(base64.b64decode(result["sheet_zb64"])).decode()
    assert hashlib.sha256(exact.encode()).hexdigest() == result["sheet_sha256"]
    assert json.loads(exact) == calls[0]


def test_new_inputs_recovery_uses_saved_blocks_without_tape_or_source_reads(databases, tmp_path):
    ticket, _ = ready(databases, tmp_path / "tape")
    calls, reads, clock = [], [], Clock()
    def inputs(*args):
        reads.append(True)
        return block(*args, tmp=tmp_path / "tape")
    def crash(*_, **__):
        raise RuntimeError("saved before record")
    with pytest.raises(RuntimeError, match="saved before record"):
        run(databases, tmp_path / "tape", calls=calls, clock=clock, ps_inputs_fn=inputs, record_fn=crash)
    (tmp_path / "tape" / "pumpswap" / "tape.db").unlink()
    J.recover(databases[0], lambda: nullcontext(databases[1]), now_fn=clock)
    vet = next(s for s in D.get_ticket(databases[0], ticket)["stages"] if s["stage"] == "vet")
    assert len(reads) == len(calls) == 1 and calls[0] == vet["inputs"]["sheet"]


@pytest.mark.parametrize("case,reason", [("missing", "ps_inputs_required"), ("bad", "ps_inputs_invalid"),
                                         ("killed", "desk_cohort_killed")])
def test_new_input_or_cohort_refusal_precedes_a_model_claim(databases, tmp_path, case, reason):
    ticket, cid = ready(databases, tmp_path / "tape")
    if case == "killed":
        P.ensure(databases[0])
        databases[0].execute("INSERT INTO desk_paper_cohort_trips VALUES(?,?,?,?,?)",
                            (cid, iso(NOW), -200, -200, "cumulative_paper_loss"))
        databases[0].commit()
    def bad(*args):
        b = block(*args, tmp=tmp_path / "tape")
        b["ps_facts"]["snapshot_hash"] = "0" * 64
        return b
    out = J.run_once(databases[0], forbidden, call_model=forbidden, system_prompt="fixture", worker="w",
        now_fn=Clock(), calls_per_hour=40, asof_fn=refresh,
        ps_inputs_fn=None if case == "missing" else bad)
    assert out[0]["reason"] == reason
    assert databases[0].execute("SELECT COUNT(*) FROM desk_judge_calls").fetchone()[0] == 0
    assert D.get_ticket(databases[0], ticket)["state"] == "skipped"


def test_legacy_cohort_does_not_use_new_callback(databases, tmp_path):
    ready(databases, tmp_path / "tape", new=False)
    calls = []
    J.run_once(databases[0], lambda: nullcontext(databases[1]),
        call_model=model_returning(enter, calls=calls, clock=Clock()), system_prompt="fixture", worker="w",
        now_fn=Clock(), calls_per_hour=40, asof_fn=refresh, ps_inputs_fn=forbidden)
    assert len(calls) == 1 and "ps_facts" not in calls[0]


def test_worker_factory_uses_trusted_original_source_and_one_fresh_probe(databases, tmp_path, monkeypatch):
    ticket, _ = ready(databases, tmp_path / "tape")
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    seen = []
    def safety(original, evidence, **kw):
        seen.append((original["snapshot"]["hash"], evidence["known_at"], kw["as_of"]))
        return block(t, c, meta, NOW + 1, tmp=tmp_path / "tape")["safety"]
    monkeypatch.setattr(J.contract, "safety_from_evidence", safety)
    out = W.make_ps_inputs_fn(databases[0], tmp_path / "tape", lambda _: ["fixture4"])(t, c, meta, NOW + 1)
    assert seen == [(t["snapshot_hash"], iso(NOW), NOW + 1)]
    assert out["ps_facts"]["snapshot_hash"] == c["snapshot"]["hash"]
    assert out["ps_facts"]["flow_60s"]["buy_usd"] == 80
    assert out["safety_proof"]["emission_snapshot_hash"] == t["snapshot_hash"]
    assert out["safety"]["sell_probe"] == "ok" and out["safety"]["sells_exist_60s"] is True


def test_worker_factory_never_spends_on_unknown_or_killed_fresh_probe(databases, tmp_path, monkeypatch):
    from sniper.services import memecoin_desk_ps_evidence as E
    ticket, _ = ready(databases, tmp_path / "tape")
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    monkeypatch.setattr(J.contract, "safety_from_evidence", lambda *a, **kw: {})
    fn = W.make_ps_inputs_fn(databases[0], tmp_path / "tape", lambda _: [])
    monkeypatch.setattr(E, "round_trip", lambda **kw: {"loss_pct": 9})
    with pytest.raises(ValueError, match="ps_probe_kill"):
        fn(t, c, meta, NOW + 1)
    def unavailable(**kw):
        raise ValueError("unknown reserves")
    monkeypatch.setattr(E, "round_trip", unavailable)
    with pytest.raises(ValueError, match="ps_probe_unavailable"):
        fn(t, c, meta, NOW + 1)
