"""Desk funded-by fallback (parser_version 3): only for an owner the parser leaves unresolved or deep; every provider failure is a
distinct outcome and never 'no funder'; a frozen list of shared-infrastructure funders resolves its owner without clustering it."""
import io
import json
from urllib.error import HTTPError

import pytest

from sniper.services import memecoin_cluster_rpc as R
from sniper.services.memecoin_cluster_admission import decide_cluster
from sniper.services.memecoin_cluster_rpc import DeskClusterRpc, ResearchClusterRpc

URL = "https://h.invalid/?api-key=secret-key"


def _addr(n):
    """A real Solana address (base58 of 32 bytes), distinct per n."""
    num = int.from_bytes(bytes([n]) * 32, "big")
    out = ""
    while num:
        num, r = divmod(num, 58)
        out = R._B58[r] + out
    return out


OWNER = _addr(2)
FUNDER = _addr(1)
EXCH = next(iter(sorted(R.known_funders()["addresses"])))


def _tx(instructions):
    return {"transaction": {"message": {"instructions": instructions}}, "meta": {"innerInstructions": []}}


RESOLVING_TX = _tx([{"parsed": {"type": "transfer", "info": {"source": FUNDER, "destination": OWNER, "lamports": 5}}}])
NO_FUNDER_TX = _tx([{"parsed": {"type": "transfer", "info": {"source": "S" * 43, "destination": "D" * 43, "lamports": 5}}}])


class Http:
    """A fake opener recording method + host; `funded_by` is a list of answers (a dict body, an int HTTP code, or an Exception)."""
    def __init__(self, sigs, tx, funded_by=()):
        self.sigs, self.tx, self.fb = iter(sigs), tx, iter(funded_by)
        self.calls = []

    def __call__(self, request, timeout=8):
        if "funded-by" in request.full_url:
            self.calls.append("funded_by")
            assert request.get_header("X-api-key") == "secret-key" and "secret-key" not in request.full_url   # the key is a header
            ans = next(self.fb)
            if isinstance(ans, Exception):
                raise ans
            if isinstance(ans, int):
                raise HTTPError(request.full_url, ans, "x", {}, io.BytesIO(b"{}"))
            return io.BytesIO(json.dumps(ans).encode())
        body = json.loads(request.data)
        self.calls.append(body["method"])
        result = next(self.sigs) if body["method"] == "getSignaturesForAddress" else self.tx
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode())


ONE_SIG = [[{"signature": "s1"}]]
FULL = [{"signature": "s%d" % i} for i in range(1000)]
OK = {"funder": FUNDER, "funderType": "Centralized Exchange", "slot": 9, "date": "2026-10-22T00:00:00Z", "signature": "sig"}


def rpc(http, **kw):
    return DeskClusterRpc(URL, http=http, funded_by=True, **kw)


def test_a_parser_resolved_owner_never_calls_funded_by_even_if_it_would_disagree():
    http = Http(ONE_SIG, RESOLVING_TX, funded_by=[OK])
    r = rpc(http)                                                                       # audit off: no comparison call either
    assert r.trace_first_funder(OWNER) == (FUNDER, "resolved") and http.calls == ["getSignaturesForAddress", "getTransaction"]
    assert r.last_source_info["funder_source"] == "direct_tx" and r.extras()["funder_sources"] == {"direct_tx": 1}


def test_an_unresolved_owner_calls_funded_by_once_and_records_the_source_and_type():
    http = Http(ONE_SIG, NO_FUNDER_TX, funded_by=[OK])
    r = rpc(http)
    assert r.trace_first_funder(OWNER) == (FUNDER, "resolved")
    assert http.calls == ["getSignaturesForAddress", "getTransaction", "funded_by"]
    assert r.last_source_info == {"funder_source": "funded_by", "funder_type": "centralized_exchange",
                                  "detail": {"slot": 9, "date": "2026-10-22T00:00:00Z", "signature": "sig"}}
    assert r.extras()["funded_by"]["funded_by_calls"] == 1 and r.extras()["funder_sources"] == {"funded_by": 1}


def test_a_deep_owner_calls_funded_by_after_the_page_cap():
    http = Http([FULL, FULL], None, funded_by=[OK])
    r = rpc(http)
    assert r.trace_first_funder(OWNER) == (FUNDER, "resolved")
    assert http.calls == ["getSignaturesForAddress"] * 2 + ["funded_by"] and r.deep == 1


@pytest.mark.parametrize("answer,stat", [
    (404, "absent"), ({"funder": None}, "absent"), ({"funder": OWNER}, "absent"),     # the provider knows there is no funding / a self funder
    (429, "dependency"), ({"funder": "short"}, "dependency"), ({"no": "funder key"}, "absent"), ([1, 2], "dependency"),
    (OSError("timeout"), "dependency"),
    ({"funder": " " * 32}, "dependency"), ({"funder": "!" * 32}, "dependency"), ({"funder": "0" + _addr(1)[1:]}, "dependency"),   # not base58
    ({"funder": "O" + _addr(1)[1:]}, "dependency"), ({"funder": "I" + _addr(1)[1:]}, "dependency"), ({"funder": "l" + _addr(1)[1:]}, "dependency"),
    ({"funder": _addr(1)[:31]}, "dependency"), ({"funder": _addr(1) + "1" * 14}, "dependency")])             # 31 / 45 characters
def test_every_funded_by_failure_leaves_the_owner_unresolved_and_is_never_clean(answer, stat):
    paused = []
    http = Http(ONE_SIG, NO_FUNDER_TX, funded_by=[answer, answer])
    r = rpc(http, on_quota=lambda: paused.append(1))
    assert r.trace_first_funder(OWNER) == (None, "unresolved")
    assert r.extras()["funded_by"][stat] == 1 and r.last_source_info is None
    assert paused == ([1] if answer == 429 else [])                                     # a 429 pauses the shared pacer


def test_a_transient_failure_is_retried_once_by_the_metered_opener_not_twice_a_429_and_a_404_are_not():
    from sniper.services import memecoin_desk_cluster as dc
    # the worker hands DeskClusterRpc the METERED opener: it owns the one paced, counted retry of a transient (Gamma LOW 1)
    http = Http(ONE_SIG, NO_FUNDER_TX, funded_by=[503, OK])
    meter, opener = dc.metered(http, 20)
    r = rpc(opener)
    assert r.trace_first_funder(OWNER) == (FUNDER, "resolved") and http.calls.count("funded_by") == 2 and r.extras()["funded_by"]["funded_by_calls"] == 1
    http = Http(ONE_SIG, NO_FUNDER_TX, funded_by=[503, 503, OK, OK])
    meter, opener = dc.metered(http, 20)
    r = rpc(opener)
    assert r.trace_first_funder(OWNER) == (None, "unresolved") and http.calls.count("funded_by") == 2    # at most two attempts, never four
    for code in (404, 429):
        http = Http(ONE_SIG, NO_FUNDER_TX, funded_by=[code, OK])
        meter, opener = dc.metered(http, 20)
        r = rpc(opener, on_quota=lambda: None)
        r.trace_first_funder(OWNER)
        assert http.calls.count("funded_by") == 1


@pytest.mark.parametrize("code", [401, 403])
def test_a_plan_or_auth_error_is_a_hard_stop_for_the_pass(code, tmp_path):
    http = Http(ONE_SIG * 3, NO_FUNDER_TX, funded_by=[code, OK])
    r = rpc(http)
    assert r.trace_first_funder(OWNER) == (None, "unresolved") and r.fatal == "funded_by_auth"
    assert r.extras()["fatal"] == "funded_by_auth"
    before = http.calls.count("funded_by")
    assert r.trace_first_funder(_addr(9)) == (None, "unresolved") and http.calls.count("funded_by") == before   # no further provider call once fatal
    # run_pass stops the pass after the row that carries the fatal
    import test_memecoin_desk_cluster as T
    from sniper.services import memecoin_desk_cluster as dc
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    T._emit(em / "2026-10-21.jsonl", [T._u(1), T._u(2)])

    def factory(call_budget):
        meter = {"calls": 0, "over": False, "extra": lambda: {"fatal": "funded_by_auth"}}
        return meter, T.FakeService(T._decision(False, "cluster_unknown_unresolved"), meter)
    stats = T._pass(em, out, factory, iter([60.0 + i * 0.01 for i in range(50)]).__next__)
    assert stats["decided"] == 1 and stats["stop"] == "funded_by_auth" and stats["capped"] == 1


def test_the_audit_probes_a_stable_sample_of_resolved_owners_counts_agreement_and_never_overrides():
    owners = ["%043d" % i for i in range(40)]
    agree = disagree = calls = 0
    for o in owners:
        http = Http(ONE_SIG, _tx([{"parsed": {"type": "transfer", "info": {"source": FUNDER, "destination": o, "lamports": 5}}}]),
                    funded_by=[OK, OK])
        r = DeskClusterRpc(URL, http=http, funded_by=True, audit_every=4)
        got = r.trace_first_funder(o)
        assert got == (FUNDER, "resolved")                                              # the parser's answer, always
        st = r.extras()["funded_by"]
        calls += st["funded_by_calls"]
        agree += st["audit_agree"]
        disagree += st["audit_n"] - st["audit_agree"]
    assert 0 < calls < 40 and agree == calls and disagree == 0                          # ~1 in 4 probed; same funder here
    hit = next(o for o in owners if int(__import__("hashlib").sha256(o.encode()).hexdigest(), 16) % 4 == 0)
    other = Http(ONE_SIG, _tx([{"parsed": {"type": "transfer", "info": {"source": FUNDER, "destination": hit, "lamports": 5}}}]),
                 funded_by=[dict(OK, funder=_addr(3))] * 3)
    r = DeskClusterRpc(URL, http=other, funded_by=True, audit_every=4)
    assert r.trace_first_funder(hit) == (FUNDER, "resolved") and r.extras()["funded_by"]["audit_n"] == 1
    assert r.extras()["funded_by"]["audit_agree"] == 0                                  # a disagreement is counted, not applied
    low = DeskClusterRpc(URL, http=Http(ONE_SIG, RESOLVING_TX, funded_by=[OK]), funded_by=True, audit_every=1, calls_left=lambda: 3)
    low.trace_first_funder(OWNER)
    assert low.extras()["funded_by"]["funded_by_calls"] == 0                            # no spare budget: no audit call


def test_with_the_fallback_off_nothing_changes_for_an_unresolved_owner():
    http = Http(ONE_SIG, NO_FUNDER_TX)
    r = DeskClusterRpc(URL, http=http, funded_by=False)
    assert r.trace_first_funder(OWNER) == (None, "unresolved") and http.calls == ["getSignaturesForAddress", "getTransaction"]


# ---- the frozen list --------------------------------------------------------------------------------------------------------
def _decide(funders, known):
    owners = [("o%d" % i, 100) for i in range(len(funders))]
    cached = {"o%d" % i: f for i, f in enumerate(funders)}
    return decide_cluster(owners, cached, trace_fn=lambda o: (None, "unresolved"), workers=1, deadline_s=1.0, thin_owner_pass=True,
                          non_clusterable=known)


def test_a_listed_funder_resolves_its_owners_but_is_no_cluster_and_the_count_is_exposed():
    same = [EXCH] * 6
    plain = _decide(same, frozenset())
    assert plain.reason in ("cluster_share_high", "cluster_top_funder_high")            # an ordinary funder shared by 6 owners blocks
    listed = _decide(same, frozenset({EXCH}))
    assert listed.allowed and listed.metrics["excluded_known_funders"] == 6 and listed.metrics["clustered_supply"] == 0
    assert listed.metrics["resolved"] == 6                                               # resolved, not unknown
    mixed = _decide([EXCH, EXCH, "X" * 43, "X" * 43, "X" * 43, "Y" * 43], frozenset({EXCH}))
    assert mixed.metrics["excluded_known_funders"] == 2 and mixed.reason == "cluster_share_high"   # the unlisted trio still clusters
    assert "excluded_known_funders" not in _decide(same, frozenset()).metrics           # the funded gate's metrics are unchanged


def test_the_frozen_list_is_hash_pinned_and_seeded_from_trusted_label_types():
    k = R.known_funders()
    import hashlib
    assert k["version"] == "cluster-known-funders-v1" and k["sha256"] == hashlib.sha256(R.KNOWN_FUNDERS_PATH.read_bytes()).hexdigest()
    doc = json.loads(R.KNOWN_FUNDERS_PATH.read_text())
    assert doc["addresses"] and {v["type"] for v in doc["addresses"].values()} <= {"Centralized Exchange", "Authority"}


# ---- cache + persistence -----------------------------------------------------------------------------------------------------
def test_version_3_results_are_cached_apart_with_their_source_and_the_funded_columns_stay_untouched(tmp_path):
    from sniper.services.memecoin_cluster_store import ClusterStore
    store = ClusterStore(tmp_path / "c.db")
    store.ensure_source_columns()
    store.record_funder("O1", FUNDER, "resolved", source_signature="sig", parser_version=3, funder_source="funded_by",
                        source_detail={"funder_type": "centralized_exchange", "slot": 9})
    store.record_funder("O2", FUNDER, "resolved", source_signature="sig", parser_version=1)        # the funded call, unchanged
    assert store.cached_funders(["O1", "O2"], 3) == {"O1": FUNDER} and store.cached_funders(["O1", "O2"], 1) == {"O2": FUNDER}
    row = store.conn.execute("SELECT funder_source, source_detail FROM cluster_funder_cache WHERE owner='O1'").fetchone()
    assert row[0] == "funded_by" and json.loads(row[1]) == {"funder_type": "centralized_exchange", "slot": 9}
    assert store.conn.execute("SELECT funder_source FROM cluster_funder_cache WHERE owner='O2'").fetchone()[0] is None


def test_the_funded_traces_bytes_are_still_pinned():
    import hashlib
    import inspect
    from test_desk_cluster_parser import FUNDED_TRACE_SHA
    assert hashlib.sha256(inspect.getsource(ResearchClusterRpc.trace_first_funder).encode()).hexdigest() == FUNDED_TRACE_SHA


def test_the_kill_knob_and_audit_knob_parse(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_DESK_CLUSTER_FUNDED_BY", raising=False)
    assert cfg.memecoin_desk_cluster_funded_by() is True
    for raw in ("0", "false", "off", "no", ""):
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_FUNDED_BY", raw)
        assert cfg.memecoin_desk_cluster_funded_by() is False
    for raw, want in (("8", 8), ("0", 0), ("-3", 0), ("x", 0), ("99999", 1000)):
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_FUNDED_BY_AUDIT_N", raw)
        assert cfg.memecoin_desk_cluster_funded_by_audit() == want


def test_a_failed_or_unverifiable_first_transaction_goes_to_the_fallback_not_to_a_funder_of_its_own():
    failed = json.loads(json.dumps(RESOLVING_TX))
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    http = Http(ONE_SIG, failed, funded_by=[OK])
    r = rpc(http)
    assert r.trace_first_funder(OWNER) == (FUNDER, "resolved") and r.last_source_info["funder_source"] == "funded_by"   # the SUCCESSFUL funding
    off = DeskClusterRpc(URL, http=Http(ONE_SIG, failed), funded_by=False)
    assert off.trace_first_funder(OWNER) == (None, "unresolved") and off.last_source_info is None
    row_err = Http([[{"signature": "s1", "err": {"InstructionError": [0, "Custom"]}}]], RESOLVING_TX, funded_by=[OK])
    assert rpc(row_err).trace_first_funder(OWNER) == (FUNDER, "resolved") and "getTransaction" not in row_err.calls


def test_the_cluster_block_freezes_the_fallback_the_list_and_the_knob_blocks_a_mismatch(tmp_path):
    import importlib.util
    import test_memecoin_desk_preflight as PF
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("prov5", root / "scripts" / "memecoin_desk_provision_cohort.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from sniper.settings.memecoin import parse_desk_rail_bounds
    kw = dict(judge_p_admit=0.3, round_trip_loss_pct_max=5.0, hook_allowlist=[], rail_bounds=parse_desk_rail_bounds("sl_pct:10-40,tp_pct:15-150"))
    good = mod.frozen_policy(**kw, trigger=True)
    k = R.known_funders()
    assert good["cluster"] == {"parser_version": 3, "max_pages": 2, "wrap_close_rule": R.DESK_WRAP_RULE, "funded_by_fallback": "funded-by-v1",
                               "known_funders": {"version": k["version"], "sha256": k["sha256"]}}
    state = PF._ready_state(tmp_path / "ok", good)
    assert PF.P.check_cohort(PF.P.env_knobs(state / ".env"), state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")[0] == "PASS"
    for bad in ({"known_funders": {"version": k["version"], "sha256": "0" * 64}}, {"funded_by_fallback": "off"}, {"parser_version": 2}):
        pol = dict(good, cluster={**good["cluster"], **bad})
        st = PF._ready_state(tmp_path / ("bad%s" % list(bad)[0]), pol)
        got = PF.P.check_cohort(PF.P.env_knobs(st / ".env"), st / "data" / "memecoin_desk.db", st / "sniper_tracker.db")
        assert got[0] == "BLOCKED" and "cluster" in got[1], bad
    knobs = dict(PF.P.env_knobs(state / ".env"), MEMECOIN_DESK_CLUSTER_FUNDED_BY="0")
    got = PF.P.check_cohort(knobs, state / "data" / "memecoin_desk.db", state / "sniper_tracker.db")
    assert got[0] == "BLOCKED" and "funded_by knob" in got[1]


# ---- the excluded-known-funders share reaches the JUDGE fact (Gamma MEDIUM) -------------------------------------------------------
def test_the_excluded_share_is_in_the_metrics_the_check_dict_and_the_judge_fact():
    from sniper.services import memecoin_desk_contract as K
    from sniper.services.memecoin_desk_soft_facts import check_from_row
    d = _decide([EXCH, EXCH, "X" * 43, "Y" * 43, "Z" * 43, "W" * 43], frozenset({EXCH}))
    assert d.metrics["excluded_known_funders"] == 2 and d.metrics["excluded_known_funders_of_attempted"] == pytest.approx(2 / 6)
    assert "excluded_known_funders_of_attempted" not in _decide([EXCH] * 6, frozenset()).metrics         # the funded gate's metrics unchanged
    metrics = {"attempted_supply": 600, "resolved_supply": 600, "eligible": 6, "top_funder_of_attempted": 0.2,
               "clustered_of_attempted": 0.0, "excluded_known_funders_of_attempted": 2 / 6}
    row = {"check": {"status": "pass", "reason": "cluster_clear"}, "metrics": metrics}
    check = check_from_row(row)
    assert check["excluded_known_funders_pct"] == pytest.approx(33.3333)
    fact = K.soft_facts({"cluster": check})["cluster"]
    assert fact["excluded_known_funders_pct_of_attempted"] == pytest.approx(33.3333) and fact["state"] == "ready"
    # absent or invalid = null, never 0
    for bad in (None, -0.1, 1.5, "x", True):
        m = dict(metrics, excluded_known_funders_of_attempted=bad)
        got = check_from_row({"check": row["check"], "metrics": m})
        assert "excluded_known_funders_pct" not in got
    assert K.soft_facts({"cluster": {"status": "pass", "reason": "cluster_clear"}})["cluster"]["excluded_known_funders_pct_of_attempted"] is None


def test_a_recorded_v17_sheet_still_validates_under_its_own_prompt_version_and_a_v18_sheet_needs_the_field():
    from sniper.services import memecoin_desk_contract as K
    new = K.soft_facts({"cluster": {"status": "pass", "reason": "cluster_clear"}})
    old = {"cluster": {k: v for k, v in new["cluster"].items() if k != "excluded_known_funders_pct_of_attempted"}, "holders": new["holders"]}
    assert K.soft_shape("desk-judge-v17") == "v17" and K.soft_shape("desk-judge-v18") == "current" and K.soft_shape("desk-judge-v13") == "legacy"
    assert K.validate_soft_facts(old, shape="v17") == old                                     # replay of a recorded v17 row
    with pytest.raises(ValueError):
        K.validate_soft_facts(old)                                                            # a current sheet must carry the field
    with pytest.raises(ValueError):
        K.validate_soft_facts(new, shape="v17")                                               # ... and a v17 sheet must not
    assert K.validate_soft_facts(new) == new


def test_the_playbook_names_the_new_field_and_the_cohort_prompt_version_is_18():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-10-21-desk-judge-playbook.md").read_text()
    assert "prompt_version: `desk-judge-v18`" in text and "excluded_known_funders_pct_of_attempted" in text


def test_the_excluded_share_is_null_unless_the_trace_is_fully_resolved():
    from sniper.services.memecoin_desk_soft_facts import check_from_row
    exact = {"attempted_supply": 600, "resolved_supply": 600, "eligible": 6, "top_funder_of_attempted": 0.2, "clustered_of_attempted": 0.0,
             "excluded_known_funders_of_attempted": 0.5}
    row = lambda m, reason="cluster_clear": {"check": {"status": "pass", "reason": reason}, "metrics": m}
    assert check_from_row(row(exact))["excluded_known_funders_pct"] == 50.0
    partial = dict(exact, resolved_supply=400)                                       # a floor, not a number
    assert "excluded_known_funders_pct" not in check_from_row(row(partial))
    for reason in ("allowed_bound", "pass_thin_bound"):
        assert "excluded_known_funders_pct" not in check_from_row(row(exact, reason))
    assert "excluded_known_funders_pct" not in check_from_row(row(dict(exact, attempted_supply=0, resolved_supply=0)))


def test_a_403_on_an_audit_probe_stops_the_check_like_a_403_on_the_fallback():
    hit = next(o for o in ("%043d" % i for i in range(200)) if int(__import__("hashlib").sha256(o.encode()).hexdigest(), 16) % 4 == 0)
    tx = _tx([{"parsed": {"type": "transfer", "info": {"source": FUNDER, "destination": hit, "lamports": 5}}}])
    http = Http(ONE_SIG * 4, tx, funded_by=[403, OK, OK])
    r = DeskClusterRpc(URL, http=http, funded_by=True, audit_every=4)
    assert r.trace_first_funder(hit) == (FUNDER, "resolved") and r.fatal == "funded_by_auth" and r.extras()["fatal"] == "funded_by_auth"
    calls = http.calls.count("funded_by")
    other = next(o for o in ("%043d" % i for i in range(200, 400)) if int(__import__("hashlib").sha256(o.encode()).hexdigest(), 16) % 4 == 0)
    tx2 = _tx([{"parsed": {"type": "transfer", "info": {"source": FUNDER, "destination": other, "lamports": 5}}}])
    http.tx = tx2
    assert r.trace_first_funder(other) == (FUNDER, "resolved") and http.calls.count("funded_by") == calls     # no further probe after a fatal


def test_every_row_stamps_the_whole_definition_built_from_the_same_constants_the_cohort_freezes():
    r = DeskClusterRpc(URL, http=Http(ONE_SIG, NO_FUNDER_TX), funded_by=True)
    assert r.extras()["cluster_definition"] == R.desk_definition(True)
    k = R.known_funders()
    assert R.desk_definition(True) == {"parser_version": 3, "max_pages": 2, "wrap_close_rule": R.DESK_WRAP_RULE,
                                       "funded_by_fallback": "funded-by-v1", "known_funders": {"version": k["version"], "sha256": k["sha256"]}}
    off = DeskClusterRpc(URL, http=Http(ONE_SIG, NO_FUNDER_TX), funded_by=False, parser_version=2)
    assert off.extras()["cluster_definition"] == R.desk_definition(False) and R.desk_definition(False)["funded_by_fallback"] is None


def test_the_consumer_compares_the_whole_definition_not_parser_and_pages_only():
    from sniper.services.memecoin_desk_soft_facts import MISMATCH, check_from_row
    good = R.desk_definition(True)
    row = lambda d: {"check": {"status": "kill", "reason": "cluster_share_high"}, "metrics": {}, **({"cluster_definition": d} if d else {})}
    cohort = {"cluster": good}
    assert check_from_row(row(good), cohort)["reason"] == "cluster_share_high"                         # same definition: used
    lists = dict(good, known_funders={"version": good["known_funders"]["version"], "sha256": "0" * 64})
    for changed in (lists, dict(good, known_funders={"version": "cluster-known-funders-v2", "sha256": good["known_funders"]["sha256"]}),
                    dict(good, wrap_close_rule="another-rule"), dict(good, funded_by_fallback="funded-by-v2"),
                    dict(good, funded_by_fallback=None), R.desk_definition(False)):
        assert check_from_row(row(changed), cohort)["reason"] == MISMATCH, changed                    # list A row + list B cohort etc.
    assert check_from_row({"check": row(None)["check"], "metrics": {}, "cluster_parser_version": 3, "max_pages": 2}, cohort)["reason"] == MISMATCH
    assert check_from_row(row(None), cohort)["reason"] == MISMATCH                                     # an unstamped row never serves a cluster cohort
    assert check_from_row(row(good), {"clip_usd": 25})["reason"] == MISMATCH                          # nor a legacy cohort
    assert check_from_row(row(None), {"clip_usd": 25})["reason"] == "cluster_share_high"               # the funded definition serves a legacy cohort


# ---- the frozen list must be TRACKED, valid, pinned and survive packaging (Codex P1) -------------------------------------------
FROZEN_LIST_SHA = "6835e4fd5f69cc7e40c67296559be44f3836e46b8fbb62f2c05f6371b8917bf5"


def test_the_known_funder_list_is_tracked_not_ignored_pinned_and_valid():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    rel = "sniper/services/memecoin_cluster_known_funders.json"
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", rel], cwd=root).returncode   # 0 = the rules ignore it
    assert ignored == 1, "the frozen list is git-ignored (.gitignore *.json needs the negation)"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel], cwd=root, capture_output=True).returncode
    assert tracked == 0, "the frozen list is not tracked"
    k = R.known_funders()
    assert k["sha256"] == FROZEN_LIST_SHA and k["version"] == "cluster-known-funders-v1"       # a change is a new list version, loudly
    doc = json.loads(R.KNOWN_FUNDERS_PATH.read_text())
    addrs = list(doc["addresses"])
    assert len(addrs) == len(set(addrs)) == 9 and all(R.is_address(a) for a in addrs)
    from sniper.research.memecoin_opportunity.state import MAYHEM_AGENT_IDS
    assert not set(addrs) & set(MAYHEM_AGENT_IDS)


def test_the_list_survives_release_packaging_a_fresh_git_archive_imports_it(tmp_path):
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    if head.returncode != 0:
        pytest.skip("not a git checkout")
    arch = subprocess.run(["git", "archive", "HEAD", "sniper/services/memecoin_cluster_rpc.py",
                           "sniper/services/memecoin_cluster_known_funders.json"], cwd=root, capture_output=True)
    assert arch.returncode == 0, arch.stderr
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=arch.stdout, check=True)
    probe = tmp_path / "sniper" / "services"
    assert (probe / "memecoin_cluster_known_funders.json").exists()                            # in the HEAD tree, so in a release worktree
    body = (probe / "memecoin_cluster_known_funders.json").read_bytes()
    import hashlib
    assert hashlib.sha256(body).hexdigest() == FROZEN_LIST_SHA
