"""Order B (operator 2026-10-28, plan docs/superpowers/plans/2026-10-28-desk-intake-evidence.md): an intake cohort that
freezes require_collected refuses a ticket whose holders, cluster or dump_group fact is still unknown; it never
turns an absent fact into a pass. Cohorts without the key keep today's soft rule."""
import base64
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services.memecoin_desk_dump_group import POLICY_HASH, VERSION
from test_memecoin_desk_contract import AT
from test_memecoin_desk_intake_filter import ev, intake_candidate, pol

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_desk_intake_assembler as IA  # noqa: E402

NOW = datetime.fromisoformat(AT).timestamp()
COLLECTED = ["chart", "cluster", "dump_group", "holders"]


def dump_pass(c):
    return dict(status="pass", reason="dump_group_clear", version=VERSION, policy_hash=POLICY_HASH, mint=c["mint"],
                pool=c["pool"], known_at=NOW - 1, cutoff=NOW - 2, expires_at=NOW + 2, buyer_share=0.0,
                buy_sol_share=0.0)


def collected(c, **override):
    e = ev(c)
    e["checks"].update(holders={"status": "pass", "reason": "largest_accounts", "top10_pct": 20.0, "creator_pct": 0.0,
                                "creator_basis": "short_page", "exclusions_verified": True},
                       cluster={"status": "pass", "reason": "pass_clean"}, dump_group=dump_pass(c),
                       chart={"status": "pass", "reason": "chart_complete"})
    e["checks"].update(override)
    return e


def decide(c, e, **rule):
    return K.hard_filter(c, evidence=e, policy=pol(**rule), evaluated_at=AT)["reason"]


def test_all_collected_passes():
    c = intake_candidate()
    assert decide(c, collected(c), require_collected=COLLECTED) == "pass"


@pytest.mark.parametrize("name", COLLECTED)
def test_each_absent_fact_refuses_with_its_name(name):
    c = intake_candidate()
    e = collected(c, **{name: {"status": "unknown", "reason": "not_collected"}})
    assert decide(c, e, require_collected=COLLECTED) == "unknown:%s:%s" % (
        name, "not_collected" if name != "dump_group" else "cache_identity")


@pytest.mark.parametrize("name", COLLECTED)
def test_a_missing_fact_refuses(name):
    c = intake_candidate()
    e = collected(c)
    del e["checks"][name]
    assert decide(c, e, require_collected=COLLECTED).startswith("unknown:%s:" % name)


def test_without_the_key_unknown_soft_facts_still_pass():
    c = intake_candidate()
    assert decide(c, ev(c)) == "pass"            # today's intake-v1 cohorts are unchanged


@pytest.mark.parametrize("bad", [["holders"], ["cluster", "dump_group", "holders"], ["chart", "holders", "cluster", "dump_group"], [], "all"])
def test_a_malformed_requirement_refuses(bad):
    c = intake_candidate()
    assert decide(c, collected(c), require_collected=bad) == "unknown:policy:intake"


# ---- holders reader

MINT_C = intake_candidate()


def token_account(mint, owner, amount):
    raw = bytes(S._pk(mint)) + bytes(S._pk(owner)) + struct.pack("<Q", amount) + bytes(93)
    return {"data": [base64.b64encode(raw).decode(), "base64"]}


class FakeRpc:
    timeout_s = 1.0
    clock = staticmethod(lambda: NOW)

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, params, *, deadline=None):
        self.calls.append(method)
        return self.replies.pop(0)


CREATOR = "11111111111111111111111111111112"
WALLET = "SysvarRent111111111111111111111111111111111"


def test_holders_excludes_the_pool_vault_and_reads_the_creator():
    c = MINT_C
    largest = [{"address": "A" * 32}, {"address": "B" * 32}]
    accounts = [token_account(c["mint"], c["pool"], 900), token_account(c["mint"], WALLET, 100)]
    rpc = FakeRpc([({"value": largest}, None), ({"value": accounts}, None)])
    got = IA.read_holders(c, rpc, deadline=1, supply_atomic="1000", creator=CREATOR)
    assert rpc.calls == ["getTokenLargestAccounts", "getMultipleAccounts"]
    assert got["status"] == "pass" and got["top10_pct"] == 10.0          # the pool's 900 is excluded
    assert got["creator_basis"] == "short_page" and got["creator_pct"] == 0.0


@pytest.mark.parametrize("replies,reason", [
    ([(None, "rate_limited")], "rate_limited"),                                    # a 429 is unknown, never empty
    ([(None, "timeout")], "timeout"),
    ([({"value": []}, None)], "largest_malformed"),
    ([({"value": [{"address": "A" * 32}]}, None), (None, "rate_limited")], "rate_limited"),
    ([({"value": [{"address": "A" * 32}]}, None), ({"value": []}, None)], "accounts_malformed"),
])
def test_holders_failures_are_typed_unknown(replies, reason):
    got = IA.read_holders(MINT_C, FakeRpc(replies), deadline=1, supply_atomic="1000", creator=CREATOR)
    assert got == {"status": "unknown", "reason": reason}


def test_holders_without_creator_or_supply_spend_no_call():
    rpc = FakeRpc([])
    assert IA.read_holders(MINT_C, rpc, deadline=1, supply_atomic="1000", creator=None)["reason"] == "creator_unknown"
    assert IA.read_holders(MINT_C, rpc, deadline=1, supply_atomic=None, creator=CREATOR)["reason"] == "supply_unknown"
    assert rpc.calls == []


def test_a_stop_during_collection_never_persists_a_verdict():
    def collect(_evidence):
        raise IA.Stopped
    with pytest.raises(IA.Stopped):
        IA.evaluate(MINT_C, "t", desk_conn=None, desk_dir=None, rpc=FakeRpc([(None, "timeout")]), clip_usd=25,
                    hard_policy={}, now_fn=lambda: NOW, evidence_fn=lambda *a, **k: {"checks": {}}, collect=collect)


# ---- cluster worker input

def test_cluster_accepts_a_genuine_intake_emission_only(tmp_path):
    from sniper.services import memecoin_desk_cluster as C
    from sniper.services.memecoin_desk_intake_book import opportunity_id
    from test_memecoin_desk_contract import rehash
    c = intake_candidate()
    c["ticket_id"] = opportunity_id("solana", c["mint"], c["intake"]["feed"], c["intake"]["filter"],
                                    c["intake"]["intake_row_ref"])
    c["pool"] = S.pumpswap_pool_address(c["mint"])
    c = rehash(c)
    forged = rehash({**json.loads(json.dumps(c)), "ticket_id": "desk:U:intake:" + "0" * 32})
    (tmp_path / "2026-10-28.jsonl").write_text(json.dumps(c) + "\n" + json.dumps(forged) + "\n")
    emitted = c["snapshot"]["emission_at"]
    refused = {}
    got = C.pending_candidates(tmp_path, set(), now_epoch=emitted + 1, max_age_s=60, refused=refused, intake=True)
    assert [r["ticket_id"] for r in got] == [c["ticket_id"]] and refused == {"emission_ticket_id": 1}
    assert C.pending_candidates(tmp_path, set(), now_epoch=emitted + 1, max_age_s=60) == []   # band mode refuses PS


# ---- chart facts (flow_60s, price_vs_60s high/low, age) from our own PumpSwap tape

def facts_stub(**override):
    out = {"flow_60s": {"buy_n": 0, "sell_n": 0, "buy_usd": 0, "sell_usd": 0, "makers": 0, "largest_buy": 0},
           "price_vs_60s_low_pct": 0.0, "price_vs_60s_high_pct": 0.0,
           "coverage": {"60s": {"state": "complete", "reason": None}},
           "ages": {k: {"state": "unknown", "age_s": None} for k in ("coin", "pool", "provider_reported", "observed")}}
    out["ages"]["coin"] = {"state": "known", "age_s": 10.0}
    out.update(override)
    return out


def test_chart_complete_quiet_window_passes():
    assert IA.chart_check(facts_stub()) == {"status": "pass", "reason": "chart_complete"}


def test_chart_absent_flow_refuses_with_coverage_reason():
    f = facts_stub(flow_60s=None, coverage={"60s": {"state": "partial", "reason": "tape_loss"}})
    assert IA.chart_check(f) == {"status": "unknown", "reason": "flow_60s_tape_loss"}


@pytest.mark.parametrize("key", ["price_vs_60s_low_pct", "price_vs_60s_high_pct"])
def test_chart_absent_price_edge_refuses(key):
    assert IA.chart_check(facts_stub(**{key: None})) == {"status": "unknown", "reason": key + "_unproven"}


def test_chart_age_falls_back_to_provider_then_pool_and_refuses_when_all_unknown():
    f = facts_stub()
    f["ages"]["coin"] = {"state": "unknown", "age_s": None}
    assert IA.chart_check(f) == {"status": "unknown", "reason": "age_unknown"}
    f["ages"]["pool"] = {"state": "known", "age_s": 80.0}
    assert IA.chart_check(f)["status"] == "pass"


def test_chart_unreadable_facts_is_unknown_never_pass():
    assert IA.chart_check(None) == {"status": "unknown", "reason": "facts_unavailable"}


def test_a_hard_kill_stays_a_kill_when_collected_facts_are_unknown():
    c = intake_candidate()
    e = collected(c, holders={"status": "unknown", "reason": "not_collected"})
    e["checks"]["token_safety"]["authority_mint"] = "open"
    assert decide(c, e, require_collected=COLLECTED) == "kill:authority_mint"


def test_tape_unreadable_never_escapes_read_chart(monkeypatch):
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.services import memecoin_desk_ps_tape as PT
    from sniper.persistence import db_memecoin_desk as store

    def boom(*a, **k):
        raise PT.TapeUnreadable("ps_swaps:null_field")
    monkeypatch.setattr(F, "facts_for", boom)
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda *a, **k: None)
    c = K.validate_candidate(intake_candidate())   # as the assembler holds it (opportunity_id)
    got = IA.read_chart(c, {"market_cut": {"receipt_cut": 1.0, "cut_slot": 1}}, desk_dir=None, desk_conn=None,
                        cohort_id="x", hard_policy={}, now=NOW)
    assert got == {"status": "unknown", "reason": "facts_unavailable:tape_unreadable:ps_swaps:null_field"}


def test_a_collect_error_is_unknown_and_stopped_still_propagates():
    def boom():
        raise OSError("cache")
    assert IA.guarded("dump_group", boom) == {"status": "unknown", "reason": "collect_error"}

    def stop():
        raise IA.Stopped
    with pytest.raises(IA.Stopped):
        IA.guarded("cluster", stop)


def test_read_chart_feeds_facts_an_intake_candidate_at_the_evidence_cut(monkeypatch):
    """Initial intake emissions carry no cut_slot in pool_state (the cut is learned at evidence time, ps_evidence);
    read_chart must hand facts_for the evidence market_cut slot, else facts_for refuses every intake ticket with
    ps_facts_candidate and a chart-required cohort never PASSes (10-28 16:12Z..10-29 drought)."""
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.persistence import db_memecoin_desk as store
    real, seen = F.facts_for, {}

    def spy(cand, **kw):
        try:
            real(cand, **kw)
        except ValueError as exc:
            seen["error"] = str(exc)
        except Exception as exc:                          # past candidate validation: the tape read (no desk_dir here)
            seen["error"] = type(exc).__name__
        seen["cut_slot"] = cand["snapshot"]["fields"]["pool_state"]["value"].get("cut_slot")
        return facts_stub()
    monkeypatch.setattr(F, "facts_for", spy)
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda *a, **k: None)
    c = K.validate_candidate(intake_candidate())          # as the assembler holds it
    state = c["snapshot"]["fields"]["pool_state"]["value"]
    assert "cut_slot" not in state                         # the real intake shape
    cut = {"receipt_cut": c["snapshot"]["emission_at"], "cut_slot": state["slot"] + 1, "block_known_at": 0.0}
    got = IA.read_chart(c, {"market_cut": cut}, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert seen.get("error") != "ps_facts_candidate", seen
    assert seen["cut_slot"] == cut["cut_slot"] and got["status"] == "pass"
    assert "cut_slot" not in c["snapshot"]["fields"]["pool_state"]["value"]          # claim candidate untouched


def chart_v3(null_reason=None, state="known", prints_n=5, losses=0):
    win = {"prints_n": prints_n, "null_reason": null_reason, "own_pool_losses": losses, "unattributed_loss_receipts": 0,
           "partial": True, "lower_bound": True}
    if state == "unknown":
        return {"state": "unknown", "reason": "loss_read_failed"}
    return {"state": "known", "reason": None, "windows": {"15s": win, "60s": win, "300s": win}}


def test_v3_chart_passes_on_observed_prints_despite_partial_coverage_and_losses():
    """Operator 4f5ad1f2 (pumpswap-facts-v3): price-path facts from observed prints when prints_n >= 2, losses counted
    beside, unseen swaps never a veto; the strict top-level edges (complete coverage only) are not the v3 gate."""
    f = facts_stub(version="pumpswap-facts-v3", price_vs_60s_low_pct=None, price_vs_60s_high_pct=None,
                   chart=chart_v3(losses=3), coverage={"60s": {"state": "partial", "reason": "tape_loss"}})
    assert IA.chart_check(f) == {"status": "pass", "reason": "chart_complete"}


@pytest.mark.parametrize("chart,reason", [(chart_v3(null_reason="prints_lt_2", prints_n=1), "chart_60s_prints_lt_2"),
                                          (chart_v3(state="unknown"), "chart_loss_read_failed")])
def test_v3_chart_refuses_only_on_its_own_null_reasons(chart, reason):
    f = facts_stub(version="pumpswap-facts-v3", price_vs_60s_low_pct=None, price_vs_60s_high_pct=None, chart=chart)
    assert IA.chart_check(f) == {"status": "unknown", "reason": reason}


def test_v3_chart_still_needs_flow_and_an_age():
    f = facts_stub(version="pumpswap-facts-v3", chart=chart_v3(), flow_60s=None,
                   coverage={"60s": {"state": "partial", "reason": "tape_loss"}})
    assert IA.chart_check(f)["status"] == "unknown"
    f = facts_stub(version="pumpswap-facts-v3", chart=chart_v3())
    f["ages"]["coin"] = {"state": "unknown", "age_s": None}
    assert IA.chart_check(f) == {"status": "unknown", "reason": "age_unknown"}


def test_read_chart_asks_facts_for_the_cohort_ps_inputs_version(monkeypatch):
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.persistence import db_memecoin_desk as store
    seen = {}
    monkeypatch.setattr(F, "facts_for", lambda cand, **kw: seen.update(kw) or facts_stub())
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda *a, **k: None)
    c = K.validate_candidate(intake_candidate())
    cut = {"receipt_cut": c["snapshot"]["emission_at"], "cut_slot": 8, "block_known_at": 0.0}
    IA.read_chart(c, {"market_cut": cut}, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW,
                  ps_version="pumpswap-facts-v3")
    assert seen["version"] == "pumpswap-facts-v3"
    IA.read_chart(c, {"market_cut": cut}, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert seen["version"] == F.LEGACY_VERSION                                     # a cohort without ps_inputs: unchanged


def test_read_chart_gives_facts_the_evidence_mint_decimals_so_prints_have_prices(monkeypatch):
    """Without base_decimals the PS tape rows carry no price_usd, so every v3 window reads prints_n 0 (live replay
    10-29: flow 1-6 swaps, prints 0/0/0). The decimals come from the evidence's own mint read."""
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.persistence import db_memecoin_desk as store
    seen = {}
    monkeypatch.setattr(F, "facts_for", lambda cand, **kw: seen.update(
        state=cand["snapshot"]["fields"]["pool_state"]["value"]) or facts_stub())
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda *a, **k: None)
    c = K.validate_candidate(intake_candidate())
    c["snapshot"]["fields"]["pool_state"]["value"]["base_decimals"] = None          # the live intake shape (no mint read)
    cut = {"receipt_cut": c["snapshot"]["emission_at"], "cut_slot": 8, "block_known_at": 0.0}
    ev_ok = {"market_cut": cut, "checks": {"token_safety": {"status": "pass", "decimals": 9}}}
    ev_ok["known_at"] = "2026-10-21T12:00:03+00:00"
    got = IA.read_chart(c, ev_ok, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert seen["state"]["base_decimals"] == 9 and seen["state"]["cut_slot"] == 8
    assert got["reason"].endswith("_decimals_post_cut_immutable")       # read time = the evidence envelope's known_at
    assert set(seen["state"]) - set(c["snapshot"]["fields"]["pool_state"]["value"]) <= {"cut_slot"}   # only decimals change values
    ev_bad = {"market_cut": cut, "checks": {"token_safety": {"status": "unknown", "decimals": 9}}}
    got = IA.read_chart(c, ev_bad, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert seen["state"].get("base_decimals") is None and "decimals" not in got["reason"]   # unproven: never borrowed


def test_a_chart_row_carrying_decimals_provenance_still_decides_the_same():
    c = intake_candidate()
    chart = {"status": "pass", "reason": "chart_complete_decimals_post_cut_immutable"}
    assert decide(c, collected(c, chart=chart), require_collected=COLLECTED) == "pass"


def test_read_chart_asks_facts_for_the_decision_clock_and_labels_the_stratum(monkeypatch):
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.persistence import db_memecoin_desk as store
    seen = {}
    def facts(cand, **kw):
        seen.update(kw)
        f = facts_stub()
        f["ages"]["provider_reported"] = {"state": "known", "age_clock": "decision"}
        return f
    monkeypatch.setattr(F, "facts_for", facts)
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda *a, **k: None)
    c = K.validate_candidate(intake_candidate())
    cut = {"receipt_cut": c["snapshot"]["emission_at"], "cut_slot": 8, "block_known_at": 0.0}
    got = IA.read_chart(c, {"market_cut": cut}, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert seen["decision_as_of"] == NOW and got["reason"] == "chart_complete_ageclock_decision"


def test_evaluate_resolves_the_chart_at_the_one_persisted_decision_time(monkeypatch, tmp_path):
    from sniper.services import memecoin_desk as desk
    t, seen = [NOW], {}
    monkeypatch.setattr(IA, "read_mint", lambda *a, **k: (None, None))
    def collect(e):
        t[0] += 5                                        # collection takes time
        return {"chart": lambda decision: seen.setdefault("chart_at", decision) and {"status": "pass", "reason": "x"}}
    monkeypatch.setattr(desk, "evaluate_candidate", lambda *a, **k: seen.update(
        persisted=datetime.fromisoformat(k["now_iso"]).timestamp(), chart=k["evidence"]["checks"]["chart"])
        or {"verdict": "PASS", "reason": "pass"})
    IA.evaluate(intake_candidate(), "t", desk_conn=None, desk_dir=tmp_path, rpc=type("R", (), {"timeout_s": 1})(),
                clip_usd=25, hard_policy={}, now_fn=lambda: t[0], evidence_fn=lambda *a, **k: {"checks": {}},
                collect=collect)
    assert seen["chart_at"] == seen["persisted"] == NOW + 5 and seen["chart"] == {"status": "pass", "reason": "x"}


def test_g12_history_hook_runs_after_the_deferred_chart_is_resolved(monkeypatch, tmp_path):
    """Zeta HOLD 9c70794cd: the hook must see the RESOLVED chart dict (cheap_pass needs a dict), so an enabled G12
    history is never silently skipped; it runs after the one decision sample and before the hard filter."""
    from sniper.services import memecoin_desk as desk
    cand = intake_candidate()
    full = collected(cand)
    seen, order = [], []

    class G12:
        def enabled(self): return True
        def fetch(self, mint): order.append("fetch"); seen.append(mint); return {"status": "ok"}
        def spent_today(self): return 0
    monkeypatch.setattr(IA, "read_mint", lambda *a, **k: (None, None))
    monkeypatch.setattr(desk, "evaluate_candidate", lambda *a, **k: order.append("filter") or {"verdict": "PASS", "reason": "pass"})
    chart = full["checks"]["chart"]
    collect = lambda e: {**{k: full["checks"][k] for k in ("holders", "cluster", "dump_group")}, "chart": lambda d: chart}
    after = lambda ev: IA.history_hook(G12(), cand, ev["checks"],
                                       {k: ev["checks"][k] for k in ("holders", "cluster", "dump_group", "chart")},
                                       lambda event: None)
    IA.evaluate(cand, "t", desk_conn=None, desk_dir=tmp_path, rpc=type("R", (), {"timeout_s": 1})(), clip_usd=25,
                hard_policy={}, now_fn=lambda: NOW, evidence_fn=lambda *a, **k: {"checks": dict(full["checks"])},
                collect=collect, after_decision=after)
    assert seen == [cand["mint"]] and order == ["fetch", "filter"]


def test_facts_unavailable_reason_names_the_exception_and_passes_the_contract():
    from sniper.services.memecoin_desk_contract import _MACHINE
    for exc in (ValueError("ps_facts_candidate"), KeyError("Pool State"), RuntimeError()):
        r = IA._facts_unavailable(exc)
        assert r.startswith("facts_unavailable:") and _MACHINE.fullmatch(r), r
    assert IA._facts_unavailable(ValueError("ps_facts_candidate")) == "facts_unavailable:value_error:ps_facts_candidate"


def test_read_chart_takes_the_raw_emission_line_the_assembler_holds(monkeypatch):
    """Live 09:56Z: chart facts_unavailable:key_error:opportunity_id. The assembler passes the raw emission (json line),
    which has ticket_id but no opportunity_id; the source receipt is keyed by that id."""
    from sniper.services import memecoin_desk_ps_facts as F
    from sniper.persistence import db_memecoin_desk as store
    seen = {}
    monkeypatch.setattr(store, "get_intake_source_receipt", lambda conn, oid, **k: seen.setdefault("oid", oid) and None)
    monkeypatch.setattr(F, "facts_for", lambda cand, **kw: facts_stub())
    raw = intake_candidate()
    assert "opportunity_id" not in raw
    cut = {"receipt_cut": raw["snapshot"]["emission_at"], "cut_slot": 8, "block_known_at": 0.0}
    got = IA.read_chart(raw, {"market_cut": cut}, desk_dir=None, desk_conn=None, cohort_id="x", hard_policy={}, now=NOW)
    assert got["status"] == "pass" and seen["oid"] == raw["ticket_id"]

def _creator_tape(tmp_path, *, mint_col, status, pool):
    import sqlite3
    from sniper.services import memecoin_desk_ps_tape as PT
    path = tmp_path / "pumpswap" / "tape.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE swaps_ps(signature TEXT, ordinal INTEGER, pool TEXT, mint TEXT, map_status TEXT, "
                   "coin_creator TEXT)")
        db.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?)", ("SIG", 0, pool, mint_col, status, "Creator111"))
    return PT


def test_coin_creator_reads_an_unmapped_trigger_bound_by_the_canonical_pool(tmp_path, monkeypatch):
    """Live 2026-10-29: 37/37 holders creator_unknown had the trigger print in swaps_ps with coin_creator set but
    map_status 'unmapped' and mint NULL; the query demanded mint=?. The intake pool is the canonical PDA of the mint,
    which binds the identity (ps_evidence's intake_canonical_pda rule)."""
    from sniper.services import memecoin_desk_ps_tape as PT
    c = dict(K.validate_candidate(intake_candidate()), trigger_signature="SIG", trigger_ordinal=0)
    _creator_tape(tmp_path, mint_col=None, status="unmapped", pool=c["pool"])
    monkeypatch.setattr(PT, "canonical_pool", lambda mint: c["pool"] if mint == c["mint"] else "other")
    assert IA.coin_creator(tmp_path, c) == "Creator111"


def test_coin_creator_never_reads_a_row_of_another_mint(tmp_path, monkeypatch):
    from sniper.services import memecoin_desk_ps_tape as PT
    c = dict(K.validate_candidate(intake_candidate()), trigger_signature="SIG", trigger_ordinal=0)
    _creator_tape(tmp_path, mint_col="OtherMint", status="mapped", pool=c["pool"])
    monkeypatch.setattr(PT, "canonical_pool", lambda mint: c["pool"])
    assert IA.coin_creator(tmp_path, c) is None
    monkeypatch.setattr(PT, "canonical_pool", lambda mint: "not-this-pool")          # unmapped needs the canonical PDA
    _creator_tape(tmp_path / "b", mint_col=None, status="unmapped", pool=c["pool"])
    assert IA.coin_creator(tmp_path / "b", c) is None



def test_am41_readiness_waits_for_a_block_after_the_state_slot(monkeypatch, tmp_path):
    import sniper.services.memecoin_desk_capture as CAP
    cut, r = 1_800_000_000.000001, 1_800_000_000.0
    state = {"slot": 77, "reserve_known_at": r}
    c = {"pool": "P", "snapshot": {"emission_at": cut, "fields": {"pool_state": {"value": state}}}}
    monkeypatch.setattr(IA, "print_at", lambda cand: cut)
    monkeypatch.setattr(CAP, "capture_proof", lambda *a, **k: {"from": r - 1, "through": cut + 1})
    seen = {"value": None}
    monkeypatch.setattr(IA, "block_seen", lambda d, slot, now: seen.setdefault("slot", slot) and seen["value"])
    assert IA.committed_through_print(tmp_path, c, cut + 5, integrity=True) is False      # no block witness yet: wait
    seen["value"] = cut + 2
    assert IA.committed_through_print(tmp_path, c, cut + 5, integrity=True) is True
    assert seen["slot"] == state["slot"]
    seen["value"] = None
    assert IA.committed_through_print(tmp_path, c, cut + 5) is True                         # non-integrity unchanged


def test_am41_block_seen_needs_a_strictly_later_slot_received_before_now(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    (tmp_path / "pumpswap").mkdir()
    sqlite3.connect(tmp_path / "pumpswap" / "tape.db").close()
    day = datetime.fromtimestamp(1_800_000_000, timezone.utc).date().isoformat()
    (tmp_path / "blocks").mkdir()
    with sqlite3.connect(tmp_path / "blocks" / (day + ".db")) as b:
        b.execute("CREATE TABLE blocks(slot INTEGER PRIMARY KEY, received_at REAL)")
        b.executemany("INSERT INTO blocks VALUES(?,?)", [(100, 1_800_000_000.0), (101, 1_800_000_002.0)])
    assert IA.block_seen(tmp_path, 100, 1_800_000_003.0) == 1_800_000_002.0
    assert IA.block_seen(tmp_path, 100, 1_800_000_001.0) is None          # slot 101 not yet received at that time
    assert IA.block_seen(tmp_path, 101, 1_800_000_003.0) is None          # strictly later slot required
