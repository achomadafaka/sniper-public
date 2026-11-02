"""Desk cluster check (operator 821a42b0: desk-only cluster gate). One row per new U ticket, in the
hard filter's checks.cluster shape; research RPC only; per-minute cap; unknown never becomes pass."""
import io
import json

import pytest

from sniper.services import memecoin_desk_cluster as dc
from sniper.services.memecoin_cluster_admission import ClusterDecision


def _decision(allowed, reason):
    return ClusterDecision(decision_id="d-" + reason, allowed=allowed, reason=reason, policy_id="p1",
                           snapshot_at="2026-10-21T13:00:00+00:00",
                           completed_at="2026-10-21T13:00:01+00:00", metrics={"owners": 5})


@pytest.mark.parametrize("allowed,reason,status", [
    (True, "allowed", "pass"), (True, "pass_thin_owners", "pass"), (False, "cluster_share_high", "kill"),
    (False, "cluster_top_funder_high", "kill"), (False, "cluster_too_few_owners", "kill"),
    (False, "cluster_no_holders", "kill"), (False, "cluster_unknown_quota", "unknown"),
    (False, "cluster_unknown_deadline", "unknown"), (False, "cluster_unknown_unresolved", "unknown"),
    (False, "cluster_unknown_trace_error", "unknown"), (False, "cluster_no_rpc", "unknown"),
    (False, "cluster_store_unavailable", "unknown"), (False, "", "unknown")])
def test_only_an_allowed_decision_passes_and_only_a_final_block_kills(allowed, reason, status):
    check = dc.check_from_decision(_decision(allowed, reason))
    assert check["status"] == status and set(check) == {"status", "reason"}
    assert check["reason"]


VALID = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" /
                    "desk_cluster_valid_u_emission.json").read_text())
# Codex's fixture passes validate_candidate; Lambda's identity check also binds the pool to the
# mint's bonding-curve PDA (pool is outside the ticket id and the snapshot hash).
VALID["pool"] = __import__("sniper.services.memecoin_desk_safety",
                           fromlist=["curve_address"]).curve_address(VALID["mint"])
EPOCH = VALID["snapshot"]["emission_at"] + 5.0           # 5 s after emission
NOW = "2026-10-21T13:00:05+00:00"


def _emit(path, rows, torn=False):
    text = "".join(json.dumps(r) + "\n" for r in rows) + ('{"ticket_id": "torn' if torn else "")
    path.write_text(text)


def _u(i, **kw):
    """A genuine producer-U emission (Codex's fixture built with the real Field/build_snapshot), with
    its ticket re-derived for trigger ordinal i, so each is distinct and passes verify_u_emission."""
    from sniper.services.memecoin_desk_universe import ticket_id
    row = json.loads(json.dumps(VALID))
    row["trigger_ordinal"] = i
    row["ticket_id"] = ticket_id(row["network_id"], row["mint"], row["band_entry_slot"],
                                 row["trigger_signature"], i)
    row.update(kw)
    return row


def test_pending_takes_genuine_fresh_u_emissions_only(tmp_path):
    em = tmp_path / "em"
    em.mkdir()
    forged = _u(2, ticket_id="desk:U:" + "0" * 24)
    rehash = _u(3)
    rehash["snapshot"] = dict(rehash["snapshot"], missing=["x"])          # altered without a rehash
    _emit(em / "2026-10-21.jsonl", [_u(1), _u(1), forged, rehash, _u(5, producer="H"),
                                    _u(6)], torn=True)
    refused = {}
    got = dc.pending_candidates(em, done={_u(6)["ticket_id"]}, now_epoch=EPOCH, max_age_s=60,
                                refused=refused)
    assert [r["ticket_id"] for r in got] == [_u(1)["ticket_id"]]
    assert set(refused) >= {"emission_ticket_id", "emission_snapshot_hash"}
    old = dc.pending_candidates(em, done=set(), now_epoch=EPOCH + 3600, max_age_s=60, refused={})
    assert old == []                                                      # nothing past max age


class FakeService:
    def __init__(self, decision, meter, calls=3, raises=None):
        self.decision, self.meter, self.calls, self.raises = decision, meter, calls, raises

    def decide(self, mint, *, source_path, requested_at):
        assert source_path == "desk"
        self.meter["calls"] += self.calls
        if self.raises:
            raise self.raises
        return self.decision


def _pass(em, out, factory, clock, now=NOW, **kw):
    caps = dict(per_min=3, calls_per_candidate=40, calls_per_min=300, calls_per_day=20000)
    caps.update(kw)
    return dc.run_pass(factory, em, out, now_fn=lambda: now, clock=clock, max_age_s=60.0,
                       epoch_fn=lambda: EPOCH, **caps)


def _rows_of(out):
    return [json.loads(line) for path in sorted(out.glob("*.jsonl"))
            for line in path.read_text().splitlines()]


def test_a_pass_writes_one_identity_bound_row_per_ticket_within_the_minute_cap(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(i) for i in range(1, 6)])
    budgets = []

    def factory(call_budget):
        budgets.append(call_budget)
        meter = {"calls": 0, "over": False}
        return meter, FakeService(_decision(True, "pass_thin_owners"), meter)
    clock = iter([60.0 + i * 0.01 for i in range(100)]).__next__
    stats = _pass(em, out, factory, clock)
    assert stats == {"pending": 5, "decided": 3, "capped": 2, "stop": "candidates_per_min",
                     "refused": {}, "interrupted": 0}
    rows = _rows_of(out)
    assert [r["ticket_id"] for r in rows] == [_u(i)["ticket_id"] for i in (1, 2, 3)]
    first = rows[0]
    assert (first["schema"], first["mint"], first["pool"], first["snapshot_hash"]) == (
        "desk-cluster/v1", VALID["mint"], VALID["pool"], VALID["snapshot"]["hash"])
    assert first["check"] == {"status": "pass", "reason": "pass_thin_owners"}   # stratum kept
    assert first["known_at"] == "2026-10-21T13:00:01+00:00" and first["rpc_calls"] == 3
    assert budgets == [40, 40, 40] and first["call_budget"] == 40
    assert _pass(em, out, factory, clock)["decided"] == 0                       # same minute
    assert _pass(em, out, factory, clock, now="2026-10-21T13:01:05+00:00")["decided"] == 2
    assert len(_rows_of(out)) == 5                                              # never twice


def test_a_trace_past_its_budget_is_unknown_budget_and_the_day_cap_stops(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(i) for i in range(1, 4)])

    def factory(call_budget):
        meter = {"calls": 0, "over": True}              # the opener refused a call past the budget
        return meter, FakeService(_decision(False, "cluster_unknown_trace_error"), meter, calls=25)
    clock = iter([60.0 + i * 0.01 for i in range(100)]).__next__
    stats = _pass(em, out, factory, clock, calls_per_day=30)
    rows = _rows_of(out)
    assert rows[0]["check"] == {"status": "unknown", "reason": "cluster_unknown_budget"}
    assert rows[0]["call_budget"] == 30                 # min(candidate 40, day 30)
    assert stats["decided"] == 2 and stats["stop"] == "calls_per_day"   # 25 + 25 >= 30 spent


def test_a_raising_decision_writes_one_worker_error_row_and_is_never_respent(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(1)])

    def factory(call_budget):
        meter = {"calls": 0, "over": False}
        return meter, FakeService(None, meter, raises=RuntimeError("boom"))
    clock = iter([60.0 + i for i in range(100)]).__next__
    _pass(em, out, factory, clock)
    _pass(em, out, factory, clock)
    rows = _rows_of(out)
    assert len(rows) == 1 and rows[0]["check"] == {"status": "unknown",
                                                   "reason": "cluster_unknown_worker_error"}


def test_the_metered_opener_paces_counts_refuses_past_budget_and_pauses_on_429():
    events = []

    class Pacer:
        def acquire(self):
            events.append("acquire")

        def pause(self, s):
            events.append(("pause", s))

    class Http429(Exception):
        code = 429
    replies = iter([b"ok", Http429()])

    def http(*a, **k):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return r
    meter, opener = dc.metered(http, 2, pacer=Pacer())
    assert opener("req") == b"ok"
    with pytest.raises(Http429):
        opener("req")
    with pytest.raises(dc.BudgetExceeded):
        opener("req")
    assert meter == {"calls": 2, "over": True, "last_error": "http_429"}
    assert events == ["acquire", "acquire", ("pause", 60.0)]


def _decide(owners, funders, thin):
    from sniper.services.memecoin_cluster_admission import decide_cluster
    return decide_cluster(owners, {}, trace_fn=lambda o: (funders.get(o), "resolved" if funders.get(o)
                                                                       else "unresolved"),
                          workers=1, deadline_s=5.0, thin_owner_pass=thin)


@pytest.mark.parametrize("owners,funders,reason", [
    ([("A", 100)], {"A": "F1"}, "pass_thin_owners"),                     # a lone owner is not a cluster
    ([("A", 60), ("B", 40)], {"A": "F1", "B": "F2"}, "pass_thin_owners"),
    ([("A", 60), ("B", 40)], {"A": "F1", "B": "F1"}, "cluster_share_high"),   # a shared funder
    ([("A", 60), ("B", 40), ("C", 1)], {"A": "F1", "B": "F2"}, "cluster_unknown_unresolved")])
def test_the_thin_book_rule_blocks_only_on_a_shared_funder(owners, funders, reason):
    assert _decide(owners, funders, thin=True).reason == reason


def test_the_funded_gate_default_is_unchanged_for_a_thin_book():
    assert _decide([("A", 60), ("B", 40)], {"A": "F1", "B": "F2"}, thin=False).reason == \
        "cluster_too_few_owners"
    from sniper.services.memecoin_cluster_service import ClusterAdmissionService
    svc = ClusterAdmissionService(None, None)
    assert (svc.workers, svc.deadline_s, svc.thin_owner_pass) == (8, 12.0, False)


def test_the_worker_uses_only_the_plural_research_key(monkeypatch, capsys):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_desk_cluster_worker as w
    monkeypatch.setattr(w, "load_env_files", lambda *a, **k: None)
    monkeypatch.delenv("MEMECOIN_RESEARCH_HELIUS_RPC_URLS", raising=False)
    monkeypatch.setenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL", "https://spent.invalid/?api-key=spent")
    assert w.main(["--once"]) == 2 and "MEMECOIN_RESEARCH_HELIUS_RPC_URLS is unset" in capsys.readouterr().out


def test_the_cap_knob_parses_to_a_safe_integer(monkeypatch):
    from sniper.settings import memecoin as cfg
    for raw, want in (("6", 6), ("0", 0), ("-3", 0), ("x", 0)):
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_PER_MIN", raw)
        assert cfg.memecoin_desk_cluster_per_min() == want
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_CALLS_PER_DAY", raw)
        assert cfg.memecoin_desk_cluster_calls("DAY") == want
    monkeypatch.delenv("MEMECOIN_DESK_CLUSTER_CALLS_PER_CANDIDATE", raising=False)
    assert cfg.memecoin_desk_cluster_calls("CANDIDATE") == 40


def test_the_real_store_persists_funders_through_the_service_name(tmp_path):
    """ClusterAdmissionService.decide calls store.record_funder; the real ClusterStore lacked it."""
    from sniper.services.memecoin_cluster_store import ClusterStore
    store = ClusterStore(tmp_path / "c.db")
    store.record_funder("Owner1", "Funder1", "resolved", source_signature="sig", parser_version=1)
    assert store.cached_funders(["Owner1"], 1) == {"Owner1": "Funder1"}


# ---------------------------------------------------------------- Codex's adversarial witness (d87abea8c)

def test_a_restarted_worker_in_the_same_minute_never_exceeds_the_caps(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(i) for i in range(12)])

    def factory(call_budget):
        meter = {"calls": 0, "over": False}
        return meter, FakeService(_decision(True, "allowed"), meter, calls=40)
    clock = lambda: 60.0
    for _ in range(2):                               # two fresh workers, same wall minute
        _pass(em, out, factory, clock, per_min=6)
    rows = _rows_of(out)
    assert len(rows) <= 6 and sum(r["rpc_calls"] for r in rows) <= 300


def test_an_owner_program_429_is_unknown_dependency_never_a_thin_pass(tmp_path):
    import io as _io
    from urllib.error import HTTPError
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc
    from sniper.services.memecoin_cluster_service import ClusterAdmissionService
    from sniper.services.memecoin_cluster_store import ClusterStore

    def http(request, timeout):
        payload = json.loads(request.data)
        method, params = payload["method"], payload["params"]
        if method == "getTokenLargestAccounts":
            result = {"value": [{"address": "ATA", "amount": "100"}]}
        elif method == "getMultipleAccounts" and params[0] == ["ATA"]:
            result = {"value": [{"data": {"parsed": {"info": {"owner": "Wallet"}}}}]}
        elif method == "getMultipleAccounts" and params[0] == ["Wallet"]:
            raise HTTPError(request.full_url, 429, "quota", {}, None)
        elif method == "getSignaturesForAddress":
            result = [{"signature": "oldest"}]
        else:
            result = {"transaction": {"message": {"instructions": [{"parsed": {
                "type": "transfer", "info": {"destination": "Wallet", "source": "Funder"}}}]}}}
        return _io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))
    meter, opener = dc.metered(http, 40)
    rpc = ResearchClusterRpc("https://research.invalid/?api-key=R", http=opener)
    svc = ClusterAdmissionService(rpc, ClusterStore(tmp_path / "c.db"), now_fn=lambda: NOW, workers=1,
                                  deadline_s=5.0, thin_owner_pass=True,
                                  holder_failure_reason="cluster_unknown_dependency")
    check = dc.check_from_decision(svc.decide("Mint", source_path="desk", requested_at=NOW))
    assert check == {"status": "unknown", "reason": "cluster_unknown_dependency:quota"}   # desk names the cause
    funded = ClusterAdmissionService(rpc, ClusterStore(tmp_path / "f.db"), now_fn=lambda: NOW)
    assert funded.decide("Mint", source_path="funded", requested_at=NOW).reason == \
        "cluster_unknown_quota"                       # the funded gate blocks too, under its own name


# ---------------------------------------------------------------- Codex's witness on 85f4fdcbb

def test_a_process_that_dies_mid_trace_has_still_spent_its_budget(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(1), _u(2)])
    provider = []
    crash = [True]

    def factory(call_budget):
        meter, http = dc.metered(lambda: provider.append(1), call_budget)

        class Service:
            def decide(self, mint, **kw):
                for _ in range(40):
                    http()
                if crash and crash.pop():
                    raise SystemExit("died after the provider served the reads")
                return _decision(True, "allowed")
        return meter, Service()
    clock = lambda: 60.0
    with pytest.raises(SystemExit):
        _pass(em, out, factory, clock, calls_per_day=40)
    stats = _pass(em, out, factory, clock, calls_per_day=40)
    assert len(provider) == 40                                # the restart spent nothing more
    rows = _rows_of(out)
    assert [(r["ticket_id"], r["check"]["reason"], r["rpc_calls"]) for r in rows] == [
        (_u(1)["ticket_id"], "cluster_unknown_interrupted", 40)]
    assert stats["interrupted"] == 1 and stats["stop"] == "calls_per_day"
    _pass(em, out, factory, clock, calls_per_day=40)
    assert len(_rows_of(out)) == 1                            # settled once, never re-traced


def test_a_malformed_ticket_id_never_stops_the_pass(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    bad = _u(2)
    bad["ticket_id"] = []
    _emit(em / "2026-10-21.jsonl", [bad, _u(1)])
    refused = {}
    got = dc.pending_candidates(em, done=set(), now_epoch=EPOCH, max_age_s=60, refused=refused)
    assert [r["ticket_id"] for r in got] == [_u(1)["ticket_id"]]
    assert refused == {"ticket_id_invalid": 1}


@pytest.mark.parametrize("program_values", [[None], []])
def test_an_unreadable_owner_program_is_unknown_never_a_thin_pass(tmp_path, program_values):
    import io as _io
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc
    from sniper.services.memecoin_cluster_service import ClusterAdmissionService
    from sniper.services.memecoin_cluster_store import ClusterStore

    def http(request, timeout):
        payload = json.loads(request.data)
        method, params = payload["method"], payload["params"]
        if method == "getTokenLargestAccounts":
            result = {"value": [{"address": "ATA", "amount": "100"}]}
        elif method == "getMultipleAccounts" and params[0] == ["ATA"]:
            result = {"value": [{"data": {"parsed": {"info": {"owner": "Wallet"}}}}]}
        elif method == "getMultipleAccounts" and params[0] == ["Wallet"]:
            result = {"value": program_values}
        elif method == "getSignaturesForAddress":
            result = [{"signature": "oldest"}]
        else:
            result = {"transaction": {"message": {"instructions": [{"parsed": {
                "type": "transfer", "info": {"destination": "Wallet", "source": "Funder"}}}]}}}
        return _io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))
    _meter, opener = dc.metered(http, 40)
    rpc = ResearchClusterRpc("https://research.invalid/?api-key=R", http=opener)
    svc = ClusterAdmissionService(rpc, ClusterStore(tmp_path / "c.db"), now_fn=lambda: NOW, workers=1,
                                  deadline_s=5.0, thin_owner_pass=True,
                                  holder_failure_reason="cluster_unknown_dependency")
    check = dc.check_from_decision(svc.decide("Mint", source_path="desk", requested_at=NOW))
    assert check == {"status": "unknown", "reason": "cluster_unknown_dependency:owner_program"}
    funded = ClusterAdmissionService(rpc, ClusterStore(tmp_path / "f.db"), now_fn=lambda: NOW)
    assert funded.decide("Mint", source_path="funded", requested_at=NOW).reason != \
        "cluster_unknown_dependency"                  # the funded gate's handling is unchanged


def test_a_second_worker_never_settles_an_in_flight_claim(tmp_path):
    import fcntl
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(1)])
    claims = out / "claims"
    claims.mkdir(parents=True)
    (claims / "2026-10-21.jsonl").write_text(json.dumps(
        {"ticket_id": _u(2)["ticket_id"], "call_budget": 40, "written_at": NOW}) + "\n")
    traced = []

    def factory(call_budget):
        traced.append(call_budget)
        meter = {"calls": 0, "over": False}
        return meter, FakeService(_decision(True, "allowed"), meter)
    with open(out / "worker.lock", "a") as held:                # the first worker, mid-trace
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stats = _pass(em, out, factory, lambda: 60.0)
    assert stats["stop"] == "locked" and traced == [] and _rows_of(out) == []
    assert _pass(em, out, factory, lambda: 60.0)["interrupted"] == 1   # lock gone: settles once


# ---------------------------------------------------------------- Codex's witness on 669b0b0a4

def _held_trace_service(tmp_path, actual, started, release, finished, allowance, thin):
    from sniper.services.memecoin_cluster_rpc import EligibleHolder
    from sniper.services.memecoin_cluster_service import ClusterAdmissionService
    from sniper.services.memecoin_cluster_store import ClusterStore
    meter, opener = dc.metered(lambda: actual.append("read"), allowance)
    stops = []

    class Rpc:
        def eligible_holders(self, mint):
            opener()
            return [EligibleHolder("ata" + o, o, 25) for o in "ABCD"]

        def trace_first_funder(self, owner, should_stop=None):
            opener()
            started.set()
            try:
                assert release.wait(2), "test did not release the provider"
                stops.append(should_stop is not None and should_stop())
                if stops[-1]:
                    return None, "aborted"
                opener()                                  # the next page
                return "funder" + owner, "resolved"
            finally:
                finished.set()
    svc = ClusterAdmissionService(Rpc(), ClusterStore(tmp_path / ("c%d.db" % thin)), now_fn=lambda: NOW,
                                  workers=1, deadline_s=0.03, thin_owner_pass=thin)
    return meter, svc, stops


def test_a_deadlined_trace_stops_and_its_row_is_charged_the_whole_reservation(tmp_path):
    import threading
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(1)])
    actual, ev = [], [threading.Event() for _ in range(3)]
    started, release, finished = ev
    box = {}

    def factory(allowance):
        meter, svc, box["stops"] = _held_trace_service(tmp_path, actual, started, release,
                                                       finished, allowance, True)
        return meter, svc
    try:
        _pass(em, out, factory, lambda: 60.0, calls_per_day=40)
        (row,) = _rows_of(out)
        assert row["check"]["reason"] == "cluster_unknown_deadline"
        assert (row["rpc_calls"], row["rpc_calls_at_write"]) == (40, 2)   # reservation, not count
    finally:
        release.set()
    assert finished.wait(2)
    assert box["stops"] == [True] and len(actual) == 2 <= row["rpc_calls"]  # stopped, no 3rd read


def test_the_funded_gate_stops_a_deadlined_trace_with_the_same_decision(tmp_path):
    import threading
    actual, ev = [], [threading.Event() for _ in range(3)]
    started, release, finished = ev
    _meter, svc, stops = _held_trace_service(tmp_path, actual, started, release, finished, 40, False)
    try:
        decision = svc.decide("Mint", source_path="funded", requested_at=NOW)
        assert (decision.allowed, decision.reason) == (False, "cluster_unknown_deadline")
    finally:
        release.set()
    assert finished.wait(2) and stops == [True] and len(actual) == 2


def test_a_transient_read_failure_is_retried_once_inside_the_budget_and_never_a_429():
    class Pacer:
        def __init__(self):
            self.n = 0

        def acquire(self):
            self.n += 1

        def pause(self, s):
            self.n += 1000

    class Http503(Exception):
        code = 503

    class Http400(Exception):
        code = 400
    seq = iter([TimeoutError("slow"), b"ok", Http503(), Http503(), Http400()])

    def http(*a, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    pacer = Pacer()
    meter, opener = dc.metered(http, 4, pacer=pacer)
    assert opener("r") == b"ok" and meter["calls"] == 2 and pacer.n == 2          # the retry is paced and counted
    with pytest.raises(Http503):
        opener("r")                                                                # one retry only, then the failure stands
    assert meter["calls"] == 4 and meter["last_error"] == "http_503" and not meter["over"]
    meter2, opener2 = dc.metered(http, 3, pacer=Pacer())
    with pytest.raises(Http400):
        opener2("r")                                                               # a 4xx is the request's own fault
    assert meter2["calls"] == 1


def test_a_retry_never_passes_the_budget():
    def http(*a, **k):
        raise TimeoutError("slow")
    meter, opener = dc.metered(http, 1)
    with pytest.raises(dc.BudgetExceeded):
        opener("r")                                                                # attempt 1 spends the budget; its retry is refused
    assert meter["calls"] == 1 and meter["over"] is True


def test_concurrent_calls_never_exceed_the_budget():
    import sys
    import threading
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                      # force thread switches inside the check-then-count window
    try:
        meter, opener = dc.metered(lambda *a, **k: b"ok", 3000)
        over = []

        def go():
            for _ in range(1000):
                try:
                    opener("r")
                except dc.BudgetExceeded:
                    over.append(1)
        threads = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        sys.setswitchinterval(old)
    assert meter["calls"] == 3000 and len(over) == 5000


def test_an_unknown_row_names_the_last_read_failure(tmp_path):
    em, out = tmp_path / "em", tmp_path / "out"
    em.mkdir()
    _emit(em / "2026-10-21.jsonl", [_u(1)])

    def factory(call_budget):
        meter = {"calls": 0, "over": False, "last_error": "http_503"}
        return meter, FakeService(_decision(False, "cluster_unknown_dependency"), meter)
    _pass(em, out, factory, iter([60.0 + i * 0.01 for i in range(100)]).__next__)
    row = _rows_of(out)[0]
    assert row["check"]["reason"] == "cluster_unknown_dependency" and row["metrics"]["last_error"] == "http_503"


def test_the_gap_and_worker_knobs_are_safe(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.delenv("MEMECOIN_DESK_CLUSTER_GAP_S", raising=False)
    monkeypatch.delenv("MEMECOIN_DESK_CLUSTER_WORKERS", raising=False)
    assert (cfg.memecoin_desk_cluster_gap_s(), cfg.memecoin_desk_cluster_workers()) == (1.1, 1)
    for raw, gap, workers in (("0.3", 0.3, 3), ("0.001", 0.1, 99), ("x", 1.1, 1), ("nan", 1.1, 1), ("-2", 0.1, 0)):
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_GAP_S", raw)
        monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_WORKERS", str(workers) if raw != "x" else raw)
        assert cfg.memecoin_desk_cluster_gap_s() == gap
        assert cfg.memecoin_desk_cluster_workers() == (min(max(workers, 1), 4) if raw != "x" else 1)


def test_the_worker_builds_its_pacer_with_the_desk_gap(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import memecoin_desk_cluster_worker as w
    from sniper.services import memecoin_research_pacer as rp
    import scripts.research.slot_lane_send_monitor as mon
    seen = {}
    monkeypatch.setattr(w, "load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(mon, "pick_research_url", lambda env: "https://h.invalid/?api-key=k")
    monkeypatch.setattr(rp.SharedPacer, "for_url", classmethod(lambda cls, url, gap_s=rp.GAP_S: seen.setdefault("gap", gap_s)))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_CLUSTER_GAP_S", "0.3")
    assert w.main(["--once"]) == 0
    assert seen["gap"] == 0.3


def _rpc_reply(body):
    import io
    return lambda request, timeout=8: io.BytesIO(json.dumps(body).encode())


@pytest.mark.parametrize("code", [429, -32429, -32005])
def test_a_json_rpc_limit_body_at_http_200_is_quota_and_pauses_the_pacer(code):
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc, ResearchRpcQuotaError
    paused = []
    rpc = ResearchClusterRpc("https://h.invalid/?api-key=k", http=_rpc_reply({"error": {"code": code, "message": "Too many requests"}}),
                             on_quota=lambda: paused.append(1))
    with pytest.raises(ResearchRpcQuotaError):
        rpc.call("getSlot", [])
    assert paused == [1]


def test_another_json_rpc_error_is_not_quota_and_does_not_pause():
    from sniper.services.memecoin_cluster_rpc import ResearchClusterRpc
    paused = []
    rpc = ResearchClusterRpc("https://h.invalid/?api-key=k", http=_rpc_reply({"error": {"code": -32602, "message": "bad"}}),
                             on_quota=lambda: paused.append(1))
    with pytest.raises(RuntimeError):
        rpc.call("getSlot", [])
    assert paused == []


def test_a_missing_owner_account_is_classified_by_curve_membership(monkeypatch):
    """Live sample 2026-10-29: 11 of 11 owner_program blocks were owners with no account, all on-curve 0-SOL wallets.
    On-curve = an eligible wallet (funder-traced as usual); off-curve and unparseable stay unknown and are never
    excluded, so their balance stays in the concentration math."""
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from sniper.services import memecoin_cluster_rpc as R
    wallet = str(Keypair().pubkey())
    pda = str(Pubkey.find_program_address([b"vault"], Pubkey.from_string("11111111111111111111111111111112"))[0])
    rpc = R.ResearchClusterRpc("https://research.invalid/?api-key=R", http=lambda *a, **k: None)
    monkeypatch.setattr(rpc, "holder_accounts", lambda mint: [("a1", 10), ("a2", 10), ("a3", 10)])
    monkeypatch.setattr(rpc, "token_account_owners", lambda atas: {"a1": wallet, "a2": pda, "a3": "not-a-key"})
    monkeypatch.setattr(rpc, "owner_programs", lambda owners: {o: None for o in owners})
    by = {h.ata: h for h in rpc.eligible_holders("Mint")}
    assert (by["a1"].exclusion, by["a1"].program_unknown) == (None, False)
    assert (by["a2"].exclusion, by["a2"].program_unknown) == (None, True)           # off-curve: unknown, not excluded
    assert (by["a3"].exclusion, by["a3"].program_unknown) == (None, True)
    assert R.on_curve(wallet) is True and R.on_curve(pda) is False and R.on_curve("not-a-key") is None
