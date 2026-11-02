"""Desk-only cluster funder trace: the wrapped-SOL close rule and the page cap. The funded gate's parser stays byte-for-byte as it was."""
import io
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_cluster_rpc as R
from sniper.services.memecoin_cluster_rpc import DESK_MAX_PAGES, DESK_PARSER_VERSION, DeskClusterRpc, ResearchClusterRpc

FIX = json.loads((Path(__file__).parent / "fixtures" / "cluster_wrap_funding.json").read_text())
URL = "https://h.invalid/?api-key=k"


def _http(sigs_pages, tx):
    """A fake opener: getSignaturesForAddress answers the pages in order, getTransaction answers `tx`."""
    pages = iter(sigs_pages)
    calls = []

    def http(request, timeout=8):
        body = json.loads(request.data)
        calls.append(body["method"])
        if body["method"] == "getSignaturesForAddress":
            result = next(pages)
        else:
            result = tx
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode())
    http.calls = calls
    return http


def _expected(kind):
    case = FIX[kind]
    info = case["seed_info"] if kind == "A_seed" else case["create_info"]
    return case["owner"], info["source"], case


@pytest.mark.parametrize("kind", ["A_seed", "B_create"])
def test_the_desk_trace_reads_a_wrapped_sol_close_as_the_payer_and_the_funded_parser_does_not(kind):
    owner, payer, case = _expected(kind)
    sigs = [[{"signature": s["signature"]} for s in case["sigs"]]]
    http = _http(sigs, case["tx"])
    desk = DeskClusterRpc(URL, http=http)
    assert desk.trace_first_funder(owner) == (payer, "resolved") and desk.last_source_signature == case["signature"]
    assert http.calls == ["getSignaturesForAddress", "getTransaction"]                 # zero extra calls
    funded = ResearchClusterRpc(URL, http=_http(sigs, case["tx"]))
    assert funded.trace_first_funder(owner) == (None, "unresolved")                    # the funded gate is unchanged


def test_the_seeded_shape_names_the_system_source_not_the_seed_base():
    owner, payer, case = _expected("A_seed")
    assert case["seed_info"]["source"] != case["seed_info"]["base"] == case["close_info"]["owner"]
    instructions = list(case["tx"]["transaction"]["message"]["instructions"])
    for group in case["tx"]["meta"]["innerInstructions"]:
        instructions.extend(group["instructions"])
    assert R.wrap_close_funder(instructions, owner) == payer


def test_the_wrap_rule_is_narrow():
    owner, payer, case = _expected("B_create")
    ixs = list(case["tx"]["transaction"]["message"]["instructions"])
    for group in case["tx"]["meta"]["innerInstructions"]:
        ixs.extend(group["instructions"])
    assert R.wrap_close_funder(ixs, owner) == payer
    assert R.wrap_close_funder(ixs, "SomeoneElse") is None                              # a close that pays another wallet is not this owner's funder
    assert R.wrap_close_funder([], owner) is None
    no_credit = [i for i in ixs if (i.get("parsed") or {}).get("type") not in ("transfer", "createAccount")]
    assert R.wrap_close_funder(no_credit, owner) is None                                # a close with no visible credit into it: unknown, not a guess
    self_funded = json.loads(json.dumps(ixs))
    for i in self_funded:
        if (i.get("parsed") or {}).get("info", {}).get("source"):
            i["parsed"]["info"]["source"] = owner
    assert R.wrap_close_funder(self_funded, owner) is None                              # the owner funding itself is no funder


def test_an_owner_with_more_signatures_than_the_page_cap_ends_deep_after_that_many_calls():
    full = [{"signature": "s%d" % i} for i in range(1000)]
    http = _http([full] * 30, None)
    desk = DeskClusterRpc(URL, http=http)
    assert DESK_MAX_PAGES == 2
    assert desk.trace_first_funder("owner") == (None, "deep") and http.calls == ["getSignaturesForAddress"] * 2 and desk.deep == 1
    funded_http = _http([full] * 30, None)
    assert ResearchClusterRpc(URL, http=funded_http).trace_first_funder("owner") == (None, "deep")
    assert funded_http.calls == ["getSignaturesForAddress"] * 20                       # the funded gate still pages 20 times


def test_the_ordinary_transfer_funder_still_resolves_on_the_desk_path_first():
    tx = {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer", "info": {"source": _ADDR_F, "destination": "O", "lamports": 5}}}]}},
          "meta": {"innerInstructions": []}}
    desk = DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], tx))
    assert desk.trace_first_funder("O") == (_ADDR_F, "resolved")


def test_the_desk_parser_version_keys_its_own_cache_and_the_funded_default_is_unchanged(tmp_path):
    from sniper.services import memecoin_cluster_service as svc
    assert DESK_PARSER_VERSION == 3 and svc.PARSER_VERSION == 1
    assert svc.ClusterAdmissionService(None, None).parser_version == 1
    assert svc.ClusterAdmissionService(None, None, parser_version=DESK_PARSER_VERSION).parser_version == 3


def test_the_funded_parsers_source_is_byte_identical_to_main():
    import hashlib
    import inspect
    src = inspect.getsource(ResearchClusterRpc.trace_first_funder)
    # sha256 of ResearchClusterRpc.trace_first_funder as it stands on main (1d5938475): any edit to the funded trace changes this
    assert hashlib.sha256(src.encode()).hexdigest() == FUNDED_TRACE_SHA


FUNDED_TRACE_SHA = "4b45b390ded76a33e15ce5b245ceee5dc65d89f1ea9d32515b1e5ca53f9d1fec"


def test_every_cluster_row_records_the_whole_definition_and_the_deep_owners(tmp_path):
    import test_memecoin_desk_cluster as T
    from sniper.services import memecoin_desk_cluster as dc
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    T._emit(em / "2026-10-21.jsonl", [T._u(1)])

    def factory(call_budget):
        meter = {"calls": 0, "over": False, "extra": lambda: {"cluster_definition": R.desk_definition(True), "deep_owners": 7}}
        return meter, T.FakeService(T._decision(False, "cluster_unknown_unresolved"), meter)
    T._pass(em, out, factory, iter([60.0 + i * 0.01 for i in range(50)]).__next__)
    row = T._rows_of(out)[0]
    assert row["cluster_definition"] == R.desk_definition(True) and row["deep_owners"] == 7


def test_the_trigger_cohort_freezes_the_desk_cluster_definition_and_other_cohorts_do_not_change():
    import importlib.util
    spec = importlib.util.spec_from_file_location("prov3", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_provision_cohort.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from sniper.settings.memecoin import parse_desk_rail_bounds
    kw = dict(judge_p_admit=1.0, round_trip_loss_pct_max=6.0, hook_allowlist=[], rail_bounds=parse_desk_rail_bounds("sl_pct:10-40"))
    new, old = mod.frozen_policy(**kw, trigger=True), mod.frozen_policy(**kw)
    known = R.known_funders()
    assert new["cluster"] == {"parser_version": 3, "max_pages": 2, "wrap_close_rule": "largest-system-credit-source-v1",
                              "funded_by_fallback": "funded-by-v1",
                              "known_funders": {"version": known["version"], "sha256": known["sha256"]}}
    assert "cluster" not in old                                                         # a non-trigger cohort's policy (and id) is unchanged


# ---- the consumer compares the row's definition with the cohort's (the cluster worker is cohort-blind) -------------------------
def _row(**extra):
    return {"schema": "desk-cluster/v1", "check": {"status": "kill", "reason": "cluster_share_high"}, "metrics": {}, **extra}


TRIGGER_POLICY = {"cluster": R.desk_definition(True)}
OLD_POLICY = {"clip_usd": 25}                                                        # no cluster block = the funded definition


def test_a_cluster_row_is_used_only_under_the_definition_its_cohort_froze():
    from sniper.services.memecoin_desk_soft_facts import check_from_row, row_definition, cohort_definition
    v2 = _row(cluster_definition=R.desk_definition(True))
    assert check_from_row(v2, TRIGGER_POLICY)["reason"] == "cluster_share_high"                 # v2 row + trigger cohort: used
    assert check_from_row(v2, OLD_POLICY) == {"status": "unknown", "reason": "cluster_parser_mismatch"}   # v2 row + old cohort
    assert check_from_row(_row(), TRIGGER_POLICY) == {"status": "unknown", "reason": "cluster_parser_mismatch"}   # v1 row + trigger cohort
    assert check_from_row(_row(), OLD_POLICY)["reason"] == "cluster_share_high"                 # v1 row + old cohort: unchanged
    assert check_from_row(_row(cluster_definition=dict(R.desk_definition(True), max_pages=20)), TRIGGER_POLICY)["reason"] == "cluster_parser_mismatch"   # page cap differs
    assert check_from_row(v2)["reason"] == "cluster_share_high"                                 # no cohort given: the old call, unchanged
    assert row_definition(_row()) == {"parser_version": 1, "max_pages": 20} and cohort_definition(OLD_POLICY) == {"parser_version": 1, "max_pages": 20}


def test_the_judge_fact_and_the_assembler_both_read_the_row_through_the_cohort_definition(tmp_path):
    import json
    import sys
    from datetime import datetime, timezone
    from sniper.services import memecoin_desk_soft_facts as SF
    import test_memecoin_desk_contract as T
    cand = T.candidate()
    now = cand["snapshot"]["emission_at"] + 30
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    (tmp_path / "cluster_checks").mkdir()
    known = datetime.fromtimestamp(now - 5, timezone.utc).isoformat()
    row = _row(ticket_id=cand["ticket_id"], network_id=cand["network_id"], mint=cand["mint"], pool=cand["pool"],
               snapshot_hash=cand["snapshot"]["hash"], known_at=known, cluster_definition=R.desk_definition(True))
    (tmp_path / "cluster_checks" / (day + ".jsonl")).write_text(json.dumps(row) + "\n")
    assert SF.bound_cluster_check(cand, desk_dir=tmp_path, now=now, cohort_policy=OLD_POLICY)["reason"] == "cluster_parser_mismatch"
    assert SF.bound_cluster_check(cand, desk_dir=tmp_path, now=now, cohort_policy=TRIGGER_POLICY)["reason"] == "cluster_share_high"
    ticket = {"candidate": cand, "stages": [], "cohort_id": "c"}
    fact = SF.for_ticket(ticket, desk_dir=tmp_path, now=now, cohort_fn=lambda t: {"policy": OLD_POLICY})
    assert json.dumps(fact).count("cluster_parser_mismatch") >= 1                               # the JUDGE fact carries the reason
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_desk_assembler as AS
    ev = AS.assemble(cand, safety={}, cluster=row, probe={"status": "unknown", "reason": "x"}, now=now, cohort_policy=OLD_POLICY)
    assert ev["checks"]["cluster"] == {"status": "unknown", "reason": "cluster_parser_mismatch"}
    ev = AS.assemble(cand, safety={}, cluster=row, probe={"status": "unknown", "reason": "x"}, now=now, cohort_policy=TRIGGER_POLICY)
    assert ev["checks"]["cluster"]["reason"] == "cluster_share_high"


# ---- a FAILED first transaction moves no lamports (Codex P1, 3821d27b3) ---------------------------------------------------
def _addr(n):
    num, out = int.from_bytes(bytes([n]) * 32, "big"), ""
    while num:
        num, r = divmod(num, 58)
        out = R._B58[r] + out
    return out


_ADDR_F = _addr(7)


def _failed(tx):
    out = json.loads(json.dumps(tx))
    out["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    return out


@pytest.mark.parametrize("kind", ["A_seed", "B_create"])
def test_a_failed_first_transaction_is_unresolved_with_no_funder_and_no_source_signature(kind):
    owner, payer, case = _expected(kind)
    sigs = [[{"signature": s["signature"]} for s in case["sigs"]]]
    desk = DeskClusterRpc(URL, http=_http(sigs, _failed(case["tx"])))
    assert desk.trace_first_funder(owner) == (None, "unresolved") and desk.last_source_signature is None
    ok = DeskClusterRpc(URL, http=_http(sigs, case["tx"]))
    assert ok.trace_first_funder(owner) == (payer, "resolved")                         # the same shape, succeeded: still resolves


def test_a_failed_direct_transfer_and_a_missing_meta_are_unresolved_too():
    tx = {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer", "info": {"source": _ADDR_F, "destination": "O", "lamports": 5}}}]}},
          "meta": {"innerInstructions": [], "err": None}}
    assert DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], tx)).trace_first_funder("O") == (_ADDR_F, "resolved")
    assert DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], _failed(tx))).trace_first_funder("O") == (None, "unresolved")
    for no_meta in (None, "x", []):
        broken = {"transaction": tx["transaction"], "meta": no_meta}
        d = DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], broken))
        assert d.trace_first_funder("O") == (None, "unresolved") and d.last_source_signature is None
    gone = {"transaction": tx["transaction"]}                                           # no meta key at all
    assert DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], gone)).trace_first_funder("O") == (None, "unresolved")


def test_a_signature_row_that_already_says_failed_skips_the_transaction_read():
    tx = {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer", "info": {"source": _ADDR_F, "destination": "O", "lamports": 5}}}]}},
          "meta": {"innerInstructions": [], "err": None}}
    http = _http([[{"signature": "x", "err": {"InstructionError": [0, "Custom"]}}]], tx)
    d = DeskClusterRpc(URL, http=http)
    assert d.trace_first_funder("O") == (None, "unresolved") and http.calls == ["getSignaturesForAddress"]     # one call saved
    http = _http([[{"signature": "x", "err": None}]], tx)
    assert DeskClusterRpc(URL, http=http).trace_first_funder("O") == (_ADDR_F, "resolved") and len(http.calls) == 2


def test_the_funded_parser_has_the_same_flaw_and_is_left_untouched():
    """REPORT, not a change: the funded ResearchClusterRpc never reads meta.err, so a failed first tx still names a funder there."""
    owner, payer, case = _expected("B_create")
    sigs = [[{"signature": s["signature"]} for s in case["sigs"]]]
    funded = ResearchClusterRpc(URL, http=_http(sigs, _failed(_direct_tx(owner, "Fz" * 21))))
    assert funded.trace_first_funder(owner) == ("Fz" * 21, "resolved")                  # the failed transfer is believed (the flaw)


def _direct_tx(owner, source):
    return {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer",
            "info": {"source": source, "destination": owner, "lamports": 5}}}]}}, "meta": {"innerInstructions": []}}


# ---- the two remaining bypasses of the definition guard (Codex bd2b76cd4) ------------------------------------------------------
def _soft_setup(tmp_path, *, with_row=True, stored=None):
    import test_memecoin_desk_contract as T
    from datetime import datetime, timezone
    cand = T.candidate()
    now = cand["snapshot"]["emission_at"] + 30
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    (tmp_path / "cluster_checks").mkdir(parents=True, exist_ok=True)
    if with_row:
        known = datetime.fromtimestamp(now - 5, timezone.utc).isoformat()
        row = _row(ticket_id=cand["ticket_id"], network_id=cand["network_id"], mint=cand["mint"], pool=cand["pool"],
                   snapshot_hash=cand["snapshot"]["hash"], known_at=known, cluster_definition=R.desk_definition(True))
        (tmp_path / "cluster_checks" / (day + ".jsonl")).write_text(json.dumps(row) + "\n")
    stages = [] if stored is None else [{"stage": "hard_filter", "inputs": {"evidence": {"checks": {"cluster": stored}}}}]
    return {"candidate": cand, "stages": stages, "cohort_id": "c"}, now


def _fact(ticket, tmp_path, now, cohort_fn):
    from sniper.services import memecoin_desk_soft_facts as SF
    return SF.for_ticket(ticket, desk_dir=tmp_path, now=now, cohort_fn=cohort_fn)["cluster"]


def test_a_supplied_cohort_callback_that_yields_no_cohort_is_unproven_never_the_unguarded_path(tmp_path):
    ticket, now = _soft_setup(tmp_path)
    for none in (lambda t: None, lambda t: {}, lambda t: {"policy": None}, lambda t: "x"):
        fact = _fact(ticket, tmp_path, now, none)
        assert fact["state"] == "unknown" and fact["reason"] == "cluster_definition_unproven"
    from sniper.services import memecoin_desk_soft_facts as SF
    legacy = SF.for_ticket(ticket, desk_dir=tmp_path, now=now)["cluster"]                 # an OMITTED callback is the legacy API, unchanged
    assert legacy["state"] == "ready" and legacy["reason"] == "cluster_share_high"


def test_a_cohort_with_a_cluster_block_never_inherits_unstamped_stored_evidence(tmp_path):
    stored = {"status": "pass", "reason": "cluster_clear"}
    ticket, now = _soft_setup(tmp_path, with_row=False, stored=stored)
    trig = _fact(ticket, tmp_path, now, lambda t: {"policy": TRIGGER_POLICY})
    assert trig["state"] == "unknown" and trig["reason"] == "cluster_definition_unproven"      # no bound row + stored legacy PASS
    old = _fact(ticket, tmp_path, now, lambda t: {"policy": OLD_POLICY})
    assert old["state"] == "ready" and old["check"] == "pass"                                  # a legacy cohort keeps its stored evidence
    ticket2, now2 = _soft_setup(tmp_path / "other", with_row=False, stored=None)
    assert _fact(ticket2, tmp_path / "other", now2, lambda t: {"policy": TRIGGER_POLICY})["reason"] == "not_yet_ready"   # nothing stored: unchanged
    ticket3, now3 = _soft_setup(tmp_path / "third", with_row=True, stored=stored)
    assert _fact(ticket3, tmp_path / "third", now3, lambda t: {"policy": TRIGGER_POLICY})["reason"] == "cluster_share_high"   # a bound row still wins


def test_the_judge_worker_always_passes_the_cohort_callback():
    import ast
    src = (Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_judge_worker.py").read_text()
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "for_ticket"]
    assert calls and all(any(k.arg == "cohort_fn" for k in c.keywords) for c in calls)


def test_a_direct_source_that_is_not_an_address_never_resolves_and_the_wrap_payer_must_be_one_too():
    bad = ["", " " * 32, "!" * 32, "O" * 32, _addr(7)[:31], 12345, None]
    for source in bad:
        tx = {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer", "info": {"source": source, "destination": "O", "lamports": 5}}}]}},
              "meta": {"innerInstructions": [], "err": None}}
        d = DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], tx))
        assert d.trace_first_funder("O") == (None, "unresolved") and d.last_source_signature is None, source
    good = {"transaction": {"message": {"instructions": [{"parsed": {"type": "transfer", "info": {"source": _ADDR_F, "destination": "O", "lamports": 5}}}]}},
            "meta": {"innerInstructions": [], "err": None}}
    assert DeskClusterRpc(URL, http=_http([[{"signature": "x"}]], good)).trace_first_funder("O") == (_ADDR_F, "resolved")
    # the wrap rule: a payer that is not an address, or the owner itself, is None
    owner, payer, case = _expected("B_create")
    ixs = list(case["tx"]["transaction"]["message"]["instructions"])
    for group in case["tx"]["meta"]["innerInstructions"]:
        ixs.extend(group["instructions"])
    spoiled = json.loads(json.dumps(ixs))
    for i in spoiled:
        info = (i.get("parsed") or {}).get("info") or {}
        if info.get("source") == payer:
            info["source"] = "!" * 32
    assert R.wrap_close_funder(ixs, owner) == payer and R.wrap_close_funder(spoiled, owner) is None
