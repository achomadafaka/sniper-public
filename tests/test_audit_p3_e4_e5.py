"""Engineering-audit fixes, 2026-09-18: P3, E4 and E5.

P3  the cutoff study counted an RPC ERROR as a page of zero signatures, while the live probe
    returns `unknown` for the same document. A study that turns a provider fault into evidence
    biases every cutoff it is used to choose.
E4  a search stopped by OUR OWN deadline or request budget classified as `no_route`, which reads
    as proved route absence and can send the caller to a fallback venue.
E5  the documented 32-read liquidity cap was applied inside the PER-PAIR scan, so one multihop
    decision that examined two pairs issued 64 reads against the endpoint the cap exists to spare.
"""
import importlib.util
import pathlib

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_v4_multihop as MH

STUDY = pathlib.Path(__file__).resolve().parents[1] / "scripts/research/solana_hourly_activity_cutoff_study.py"
SIG = "5" * 88
MINT = "B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6"


def _study():
    spec = importlib.util.spec_from_file_location("cutoff_study", STUDY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _item(**kw):
    out = {"kind": "funded_fill", "position_id": 1, "mint": MINT, "ticker": "T",
           "decision_at": "2026-09-16T16:19:56.996491+00:00", "source_signature": SIG,
           "outcome": "won"}
    out.update(kw)
    return out


def _doc(response, *, mint=MINT, before=SIG, fetched_at="2026-09-16T16:25:00+00:00"):
    return {"item": {"mint": mint}, "fetched_at": fetched_at, "rtt_ms": 120,
            "request": {"method": "getSignaturesForAddress",
                        "params": [mint, {"limit": 1000, "commitment": "confirmed",
                                          "before": before}]},
            "response": response}


# --- P3 -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("response,reason", [
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}, "error_body"),
    ({"jsonrpc": "2.0", "id": 1}, "no_result"),
    ({"jsonrpc": "2.0", "id": 1, "result": None}, "no_result"),
    ({"jsonrpc": "2.0", "id": 1, "result": "nonsense"}, "no_result"),
    ("not a document", "malformed"),
])
def test_a_provider_fault_is_unknown_not_a_zero_count(response, reason):
    """The live ActivityProbe returns `unknown` for each of these; the study must agree."""
    row = _study().count_item(_item(), _doc(response))
    assert row["count_state"] == "unknown", row
    assert row.get("unknown_reason") == reason
    assert "valid_signature_count" not in row


def test_a_real_page_is_still_counted_with_the_live_function():
    page = [{"signature": "a" * 88, "slot": 5, "err": None, "confirmationStatus": "finalized",
             "blockTime": 1789590000}]
    row = _study().count_item(_item(decision_at="2026-09-16T16:20:00+00:00"), _doc(
        {"jsonrpc": "2.0", "id": 1, "result": page}))
    assert row["count_state"] == "counted" and row["returned_count"] == 1


@pytest.mark.parametrize("doc,reason", [
    (_doc({"result": []}, mint="OTHERMINT"), "mint_mismatch"),
    (_doc({"result": []}, before="9" * 88), "anchor_mismatch"),
    (_doc({"result": []}, fetched_at="2026-09-16T16:10:00+00:00"), "page_before_decision"),
])
def test_a_page_that_does_not_belong_to_the_item_is_not_counted(doc, reason):
    """A page fetched for another mint, another anchor, or before the decision cannot describe the
    window being studied."""
    row = _study().count_item(_item(), doc)
    assert row["count_state"] == "unknown" and row["unknown_reason"] == reason


# --- W2/W3/W4 (inherited review findings, closed 2026-09-23) -------------------------------------

@pytest.mark.parametrize("doc,reason", [
    ({"response": {"result": []}, "fetched_at": "2026-09-16T16:25:00+00:00"}, "request_unverified"),
    (_doc({"result": []}) | {"request": {"method": "getProgramAccounts",
                                         "params": [MINT, {"before": SIG, "limit": 1000,
                                                           "commitment": "confirmed"}]}},
     "request_unverified"),
    (_doc({"result": []}) | {"request": {"method": "getSignaturesForAddress",
                                         "params": [MINT, {"limit": 1000, "commitment": "confirmed"}]}},
     "request_unverified"),
    (_doc({"result": []}) | {"request": {"method": "getSignaturesForAddress",
                                         "params": [MINT, {"before": SIG, "limit": 10,
                                                           "commitment": "confirmed"}]}},
     "request_unverified"),
    (_doc({"result": []}) | {"fetched_at": None}, "page_time_unknown"),
])
def test_an_empty_page_is_zero_only_when_the_request_that_produced_it_is_proven(doc, reason):
    """W2: an empty result counted as zero without checking what was asked. A page counts only
    when its saved request names the method, the mint, the before-anchor, the full page size and
    a commitment, and the fetch time is an aware instant."""
    row = _study().count_item(_item(), doc)
    assert row["count_state"] == "unknown" and row["unknown_reason"] == reason


@pytest.mark.parametrize("doc", [
    _doc({"result": []}) | {"request": "not-an-object"},
    _doc({"result": []}) | {"request": {"params": 100}},
    _doc({"result": []}) | {"request": {"method": "getSignaturesForAddress", "params": [MINT, 7]}},
    _doc({"result": []}) | {"fetched_at": 12345},
])
def test_a_malformed_saved_request_is_an_unknown_row_not_a_crash(doc):
    """W3: a string request or integer params raised and the batch died after earlier rows."""
    row = _study().count_item(_item(), doc)
    assert row["count_state"] == "unknown"
    assert row["unknown_reason"] in ("request_unverified", "malformed", "page_time_unknown")


def test_fetch_time_is_compared_as_an_instant_not_as_text():
    """W4: ISO strings with different UTC offsets compared lexically. Decision 16:19:56Z:
    18:00+02:00 is 16:00Z (too early, refused) and 12:30-04:00 is 16:30Z (valid, counted)."""
    study = _study()
    early = study.count_item(_item(), _doc({"result": []}, fetched_at="2026-09-16T18:00:00+02:00"))
    assert early["count_state"] == "unknown" and early["unknown_reason"] == "page_before_decision"
    late = study.count_item(_item(), _doc({"result": []}, fetched_at="2026-09-16T12:30:00-04:00"))
    assert late["count_state"] == "counted"
    naive = study.count_item(_item(), _doc({"result": []}, fetched_at="2026-09-16T16:30:00"))
    assert naive["count_state"] == "unknown" and naive["unknown_reason"] == "page_time_unknown"


def test_the_batch_emits_one_row_per_roster_item_whatever_the_pages_hold(tmp_path):
    """W3, cardinality: the CLI must print exactly one row per roster item even when a middle page
    is malformed."""
    import json
    import subprocess
    import sys
    roster = {"roster": [_item(position_id=i) for i in (1, 2, 3)]}
    (tmp_path / "roster.json").write_text(json.dumps(roster))
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "funded_fill_1.json").write_text(json.dumps(_doc({"result": []})))
    (pages / "funded_fill_2.json").write_text(json.dumps({"request": "x", "response": {"result": []}}))
    (pages / "funded_fill_3.json").write_text(json.dumps(_doc({"result": []})))
    out = subprocess.run([sys.executable, str(STUDY), "--roster", str(tmp_path / "roster.json"),
                          "--pages", str(pages)], capture_output=True, text=True)
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    assert out.returncode == 0 and len(rows) == 3
    assert [r["count_state"] for r in rows] == ["counted", "unknown", "counted"]


# --- E4 -----------------------------------------------------------------------------------------

def test_our_own_search_limit_is_not_proof_that_a_route_is_absent():
    rungs = [{"rung": "v4_multihop", "hit": False, "blind_reason": "search_limit"}]
    code, detail = S.classify_quote_miss(rungs)
    assert code == "provider_search_limit" and "search" in detail


def test_the_search_limit_maps_to_a_transport_failure_not_a_token_verdict():
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    assert S.QUOTE_MISS_FAILURES["provider_search_limit"] is QuoteFailure.PROVIDER_TIMEOUT


@pytest.mark.parametrize("note,expected", [
    ("search_stopped:discovery_limit", "search_limit"),
    ("search_stopped:request_budget", "search_limit"),
    # EX1 (2026-09-20, Codex review): a fixed block we could not read is the same blindness as a
    # search we stopped ourselves -- `_measure_paths` never runs without one -- and this exact,
    # suffix-free note reached `no_route` before the fix.
    ("decision_block_unavailable", "search_limit"),
    ("no_bridge_candidates", None),
    ("no_candidate_within_legs:3", None),
    (None, None),
])
def test_only_a_local_limit_claims_blindness(note, expected):
    assert S.multihop_blind_reason(note, gt_cold=False, discovery_observed=True) == expected


def test_a_cold_provider_still_claims_blindness():
    assert S.multihop_blind_reason("no_bridge_candidates", gt_cold=True,
                                   discovery_observed=False) == "discovery_cooldown"


# --- E5 -----------------------------------------------------------------------------------------

def test_the_liquidity_read_cap_is_spent_once_per_decision_not_once_per_pair():
    budget = MH._SearchBudget(deadline_s=30.0, max_requests=1000)
    assert budget.take_liquidity_reads(20) == 20
    assert budget.discovery_cut is False, "a full grant is not a cut search"
    assert budget.take_liquidity_reads(20) == 12, "the second pair may only use what is left of 32"
    # EX2 (2026-09-20, Codex review): a partial grant used to leave discovery_cut False, so the
    # ladder had no way to tell a starved second pair from one with no pool at all.
    assert budget.discovery_cut is True, "a short grant is the same blindness a refused request is"
    assert budget.take_liquidity_reads(5) == 0


def test_the_cap_still_applies_without_a_budget():
    assert MH._liquidity_reads_allowed(None, 50) == 32
    assert MH._liquidity_reads_allowed(None, 4) == 4


def test_a_budget_reports_what_the_decision_spent():
    budget = MH._SearchBudget(deadline_s=30.0, max_requests=1000)
    budget.take_liquidity_reads(30)
    assert budget.liquidity_reads_used == 30
