"""Cohort D sheet contract (prereg 8a2162552 + amendments): a desk-judge-v29 / pumpswap-facts-v3 cohort binds
every D component's {version, definition_sha256}; the v3 sheet always carries the D blocks, validated with closed
key sets; no other sheet may carry them. v1/v2 sheets are unchanged."""
import copy

import pytest

from test_memecoin_desk_judge import databases  # noqa: F401

from sniper.services import memecoin_desk_buyer_facts as B
from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_history_facts as H
from sniper.services import memecoin_desk_ps_facts as F
from test_desk_ps_inputs_contract import enabled
from test_desk_ps_model_facts import facts, tape


def d_policy():
    p = enabled()
    p.update(ps_inputs="pumpswap-facts-v3", judge_playbook_sha256="d" * 64, judge_model="claude-opus-5-5",
             **K.d_components())
    p.pop("judge_provider", None), p.pop("judge_effort", None)
    return p


def blocks(f):
    buyers = {"state": "unknown", "reason": "grade_asof_unknown", "version": B.VERSION}
    hist = H.creator_history_fact("/nonexistent", f["mint"], sheet_as_of=f["as_of"], decision_at=f["as_of"],
                                  pass_persist_at=f["as_of"])
    age = H.coin_age_fact("/nonexistent", f["mint"], sheet_as_of=f["as_of"], decision_at=f["as_of"],
                          pass_persist_at=f["as_of"])["coin"]
    out = {"buyers": buyers, "creator_history": hist, "coin_age": age, "clock": {"offset_s": 0.05, "sampled_at": 1.0}}
    for name in K.D_CONTEXT_MODULES:        # Epsilon's readers on an empty desk dir: their typed unknowns
        out[name] = getattr(K._context_module(name), name + "_for")({"mint": f["mint"], "pool": f.get("pool")},
                                                                    desk_dir="/nonexistent", as_of=f["as_of"])
    return out


def test_a_v29_cohort_binds_every_component_exactly():
    p = d_policy()
    assert K.validate_ps_inputs(p, "desk-judge-v29") is True
    for key in ("buyers", "history", "chart", "d_sheet"):
        bad = copy.deepcopy(p)
        bad[key]["definition_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="cohort_policy:%s_mismatch" % key):
            K.validate_ps_inputs(bad, "desk-judge-v29")
        bad.pop(key)
        with pytest.raises(ValueError):
            K.validate_ps_inputs(bad, "desk-judge-v29")
    with pytest.raises(ValueError):
        K.validate_ps_inputs(p, "desk-judge-v28")                     # v3 inputs only with the v29 prompt


def test_the_v3_sheet_carries_validated_d_blocks_and_the_chart(tmp_path):
    c, _ = tape(tmp_path)
    f = facts(c, tmp_path, version=F.VERSION3)
    sheet = K.render_judge_sheet(c, ps_facts=f, d_blocks=blocks(f))
    assert sheet["chart"] == f["chart"] and sheet["buyers"]["state"] == "unknown"
    assert sheet["creator_history"]["definition_sha256"] == H.DEFINITION_SHA256
    assert sheet["coin_age"]["state"] == "unknown" and set(sheet["d_blocks"]) == K.d_block_keys()
    with pytest.raises(ValueError, match="d_blocks"):
        K.render_judge_sheet(c, ps_facts=f)                           # a v3 sheet without its blocks
    v2 = facts(c, tmp_path, version=F.VERSION)
    with pytest.raises(ValueError, match="d_blocks"):
        K.render_judge_sheet(c, ps_facts=v2, d_blocks=blocks(f))      # blocks on a non-D sheet
    assert "chart" not in K.render_judge_sheet(c, ps_facts=v2) and "buyers" not in K.render_judge_sheet(c, ps_facts=v2)


@pytest.mark.parametrize("poison", [
    lambda b: b.update(extra=1),
    lambda b: b["buyers"].update(reason="looks_fine"),
    lambda b: b["buyers"].update(state="known"),
    lambda b: b["creator_history"].update(definition_sha256="0" * 64),
    lambda b: b["creator_history"]["creator_prior_launches_lb"].update(lb=0),          # lb without a known status
    lambda b: b["creator_history"].update(as_of=1.0),                                   # another clock
    lambda b: b["coin_age"].update(age_s=10.0),                                         # unknown with a value
    lambda b: b["creator_history"]["field_unknown"].update(dev_wallet="invented"),
])
def test_validators_refuse_shapes_outside_the_frozen_definitions(tmp_path, poison):
    c, _ = tape(tmp_path)
    f = facts(c, tmp_path, version=F.VERSION3)
    bad = blocks(f)
    poison(bad)
    with pytest.raises(ValueError):
        K.render_judge_sheet(c, ps_facts=f, d_blocks=bad)


def worker_d_blocks(databases, tmp_path, monkeypatch):
    """make_ps_inputs_fn on a desk-judge-v29 cohort; returns its output (the real worker path, not a helper)."""
    from scripts import memecoin_desk_judge_worker as W
    from sniper.persistence import db_memecoin_desk as D
    from sniper.services import memecoin_desk_judge as J
    from test_desk_ps_inputs_integration import block, refresh
    from test_desk_ps_model_facts import NOW
    from test_desk_v28_judge_inputs import ready
    ticket, _ = ready(databases, tmp_path / "tape")
    t = D.get_ticket(databases[0], ticket)
    c, meta = refresh(t["candidate"], NOW + 1)
    safety = block(t, c, meta, NOW + 1, tmp=tmp_path / "tape")["safety"]
    monkeypatch.setattr(J.contract, "safety_from_evidence", lambda *a, **kw: copy.deepcopy(safety))
    real = D.get_cohort

    def cohort(conn, cid):
        row = real(conn, cid)
        row["policy"] = {k: v for k, v in row["policy"].items() if k not in ("judge_provider", "judge_effort")}
        row["policy"].update(ps_inputs="pumpswap-facts-v3", judge_model="claude-opus-5-5", **K.d_components())
        row["prompt_version"] = "desk-judge-v29"
        return row
    monkeypatch.setattr(W.store, "get_cohort", cohort)
    monkeypatch.setattr(F, "facts_for", lambda *a, **kw: {
        "version": kw["version"], "flow_60s": None, "as_of": NOW + 1, "receipt_cut": NOW, "cut_slot": 1000,
        "ages": {"pool": {"state": "unknown", "occurred_at": None}}})
    return W.make_ps_inputs_fn(databases[0], tmp_path / "tape", lambda _: [])(t, c, meta, NOW + 1)


def test_the_worker_builds_d_blocks_for_a_v29_cohort_only(databases, tmp_path, monkeypatch):
    out = worker_d_blocks(databases, tmp_path, monkeypatch)
    assert set(out) == {"ps_facts", "safety", "safety_proof", "d_blocks"}
    assert set(out["d_blocks"]) == K.d_block_keys() == K.D_BLOCK_KEYS | {"heat", "list_claims"}
    assert out["d_blocks"]["buyers"] == {"state": "unknown", "reason": "roster_unreadable", "version": B.VERSION}
    assert out["d_blocks"]["creator_history"]["state"] == "unknown"
    assert out["d_blocks"]["coin_age"]["state"] == "unknown"


def test_the_host_times_are_corrected_with_one_sampled_offset(tmp_path):
    # Gamma: a fetch 0.03 s before the PASS persist in corrected time is accepted, 0.03 s after is refused,
    # when host and corrected differ by 0.05 s; without an offset the blocks are clock_unknown
    from scripts import memecoin_desk_judge_worker as W
    import test_desk_history_facts as T
    host_pass = 1990.0
    for delta, known in ((-0.03, True), (0.03, False)):
        d, con = T.desk(tmp_path / str(delta))
        T.coin(con, fetched=host_pass - 0.05 + delta + T.OFF)          # stored host fetch; corrected = minus OFF
        history, age = W.history_blocks(d, "M1", None, sheet_as_of=2000.0, decision_at=2010.0,
                                        pass_persist_at=host_pass, offset=0.05, market_cut=1999.0)
        assert (age["state"] == "known") is known, (delta, age)
    history, age = W.history_blocks(d, "M1", None, sheet_as_of=2000.0, decision_at=2010.0,
                                    pass_persist_at=host_pass, offset=None, market_cut=1999.0)
    assert age["reason"] == history["reason"] == history["history_reason"] == "clock_unknown"


def test_the_clock_age_bound_is_hash_bound_and_used_by_the_worker():
    from scripts import memecoin_desk_judge_worker as W
    assert K.D_SHEET_DEFINITION["clock"]["max_age_s"] == 3600 and W.CLOCK_MAX_AGE_S == 3600.0
    assert K.d_components()["d_sheet"]["definition_sha256"] == K.D_SHEET_DEFINITION_SHA256



def _one_rule(obj):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def test_every_beta_definition_uses_the_one_d_hash_rule():
    assert F.CHART_DEFINITION_SHA256 == _one_rule(F.CHART_DEFINITION)
    assert K.D_SHEET_DEFINITION_SHA256 == _one_rule(K.D_SHEET_DEFINITION)
    assert B.DEFINITION_SHA256 == _one_rule(B.DEFINITION)


@pytest.mark.parametrize("change", [{"judge_model": "gpt-6-luna"}, {"judge_provider": "claude_sdk"},
                                    {"judge_effort": "low"}, {"judge_model": None}])
def test_a_d_policy_runs_only_the_opus_judge_without_gpt_keys(change):
    p = d_policy()
    p.update(change)
    with pytest.raises(ValueError, match="d_judge_model"):
        K.validate_ps_inputs(p, "desk-judge-v29")


def test_a_registered_context_block_is_bound_and_validated(tmp_path, monkeypatch):
    """Stub wiring for Epsilon's heat / list_claims: any module registered in D_CONTEXT_MODULES joins the
    policy binding and the sheet's closed key set; a block off its module's key sets or hash refuses the sheet."""
    import sys
    import types
    mod = types.ModuleType("fake_ctx")
    mod.definition = lambda: {"version": "fake-v1", "definition_sha256": "f" * 64}
    mod.KNOWN_KEYS = frozenset({"state", "reason", "definition_sha256", "as_of", "x", "field_unknown"})
    mod.UNKNOWN_KEYS = frozenset({"state", "reason", "definition_sha256", "as_of"})
    mod.REASONS, mod.FIELD_REASONS = frozenset({"stale"}), frozenset({"parse_cell"})
    mod.heat_for = lambda c, **kw: {"state": "unknown", "reason": "stale", "definition_sha256": "f" * 64, "as_of": 1.0}
    monkeypatch.setitem(sys.modules, "fake_ctx", mod)
    monkeypatch.setattr(K, "D_CONTEXT_MODULES", {"heat": "fake_ctx"})
    p = d_policy()
    assert p["heat"] == mod.definition() and K.validate_ps_inputs(p, "desk-judge-v29") is True
    p["heat"] = {"version": "fake-v1", "definition_sha256": "0" * 64}
    with pytest.raises(ValueError, match="cohort_policy:heat_mismatch"):
        K.validate_ps_inputs(p, "desk-judge-v29")
    c, _ = tape(tmp_path)
    f = facts(c, tmp_path, version=F.VERSION3)
    known = {"state": "known", "reason": None, "definition_sha256": "f" * 64, "as_of": 1.0, "x": 3,
             "field_unknown": {}}
    sheet = K.render_judge_sheet(c, ps_facts=f, d_blocks=dict(blocks(f), heat=known))
    assert sheet["heat"] == known and set(sheet["d_blocks"]) == K.D_BLOCK_KEYS | {"heat"}
    unknown = {"state": "unknown", "reason": "stale", "definition_sha256": "f" * 64, "as_of": 1.0}
    assert K.render_judge_sheet(c, ps_facts=f, d_blocks=dict(blocks(f), heat=unknown))["heat"] == unknown
    with pytest.raises(ValueError, match="d_blocks"):
        K.render_judge_sheet(c, ps_facts=f, d_blocks={k: v for k, v in blocks(f).items() if k != "heat"})  # missing
    for bad in (dict(known, definition_sha256="0" * 64), dict(known, reason="stale"), dict(unknown, reason="nope"),
                dict(known, field_unknown={"x": "nope"}), dict(unknown, x=1), dict(known, state="partial"), None):
        with pytest.raises(ValueError, match="d_heat"):
            K.render_judge_sheet(c, ps_facts=f, d_blocks=dict(blocks(f), heat=bad))


def test_clock_sample_is_one_read_across_a_forced_refresh(tmp_path, monkeypatch):
    """Zeta HOLD c3593cd13: an atomic refresh between two reads paired the old offset_s with the new 'at'. The
    sample is one JSON read: the old (stale) sample or the new one, never a mix."""
    import json
    import pathlib
    from scripts import memecoin_desk_judge_worker as W
    path = tmp_path / "clock_offset.json"
    path.write_text(json.dumps({"offset_s": 100.0, "at": 1.0}))
    real, reads = pathlib.Path.read_text, []

    def refreshing(self, *a, **k):
        text = real(self, *a, **k)
        reads.append(text)
        tmp = tmp_path / "new.json"
        tmp.write_text(json.dumps({"offset_s": 0.05, "at": 10000.0}))
        tmp.replace(path)                                   # the writer's atomic refresh lands right after the read
        return text
    monkeypatch.setattr(pathlib.Path, "read_text", refreshing)
    assert W.read_clock_sample(path, now=10000.0) == (None, None)       # old sample, 9999 s old: unknown
    assert len(reads) == 1
    monkeypatch.setattr(pathlib.Path, "read_text", real)
    assert W.read_clock_sample(path, now=10000.0) == (0.05, 10000.0)
    for doc in ({"offset_s": True, "at": 10000.0}, {"offset_s": 0.05, "at": float("nan")}, {"offset_s": 0.05},
                {"offset_s": "0.05", "at": 10000.0}):
        path.write_text(json.dumps(doc))
        assert W.read_clock_sample(path, now=10000.0) == (None, None)


def test_a_default_separators_digest_is_refused_for_every_d_component():
    """Amendment 16: one canonical hash rule (compact separators, sorted keys, UTF-8, ensure_ascii=False). A policy
    or block carrying the digest of the same definition under json.dumps defaults (0d4b5332 for buyers) is refused."""
    import hashlib
    import json
    canon = lambda d: hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False).encode("utf-8")).hexdigest()
    default = lambda d: hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
    defs = {"buyers": B.DEFINITION, "history": H.DEFINITION, "chart": F.CHART_DEFINITION,
            "d_sheet": K.D_SHEET_DEFINITION}
    assert default(B.DEFINITION).startswith("0d4b5332") and canon(B.DEFINITION).startswith("ffabe76c")
    for key, definition in defs.items():
        assert K.d_components()[key]["definition_sha256"] == canon(definition) != default(definition)
        bad = d_policy()
        bad[key]["definition_sha256"] = default(definition)
        with pytest.raises(ValueError, match="cohort_policy:%s_mismatch" % key):
            K.validate_ps_inputs(bad, "desk-judge-v29")



def test_d_binds_heat_and_list_claims_and_never_dex_token():
    """Operator 2026-10-28 ~14:4xZ: dex_token (DexScreener UI, 403/challenge) is out of D. heat and list_claims are bound
    with their canonical hashes; a policy or a sheet carrying dex_token is refused; not_served is no state of D."""
    from sniper.services import memecoin_desk_heat_v2 as Heat
    from sniper.services import memecoin_desk_list_claims as L
    comps = K.d_components()
    assert comps["heat"] == Heat.definition() and comps["list_claims"] == L.definition() and "dex_token" not in comps
    assert set(comps) == {"buyers", "history", "chart", "d_sheet", "heat", "list_claims"}
    assert "dex_token" not in K.d_block_keys()
    p = d_policy()
    p["dex_token"] = {"version": "desk-dex-token-v1", "definition_sha256": "f" * 64}
    with pytest.raises(ValueError, match="cohort_policy:dex_token_retired"):
        K.validate_ps_inputs(p, "desk-judge-v29")
    for name, mod in (("heat", Heat), ("list_claims", L)):
        ns = {"state": "not_served", "reason": None, "definition_sha256": mod.definition()["definition_sha256"],
              "as_of": 1.0}
        with pytest.raises(ValueError, match="d_" + name):
            K.validate_context_block(name, ns)



def test_g5_readers_get_corrected_utc_from_the_history_offset_and_no_read_when_it_is_unknown(monkeypatch):
    """Zeta HOLD e76b9881d: the G5 readers take corrected UTC. Host sheet_as_of 2000 with offset 100 reads at 1900
    (the host time would select a capture from the corrected future); an unknown offset reads nothing and every block is
    unknown:clock_unknown, and a sheet whose clock is unknown refuses any other context block."""
    from scripts import memecoin_desk_judge_worker as W
    seen = []
    for name in K.D_CONTEXT_MODULES:
        mod = K._context_module(name)
        monkeypatch.setattr(mod, name + "_for", lambda c, *, desk_dir, as_of, mod=mod, name=name:
                            seen.append((name, as_of)) or mod._unknown("no_capture", as_of))
    out = W.context_blocks({"mint": "M"}, desk_dir="d", sheet_as_of=2000.0, offset=100.0)
    assert sorted(seen) == sorted((n, 1900.0) for n in K.D_CONTEXT_MODULES) and set(out) == set(K.D_CONTEXT_MODULES)
    seen.clear()
    out = W.context_blocks({"mint": "M"}, desk_dir="d", sheet_as_of=2000.0, offset=None)
    assert seen == [] and all(b["state"] == "unknown" and b["reason"] == "clock_unknown" and b["as_of"] is None
                              for b in out.values())
    for name, b in out.items():
        assert K.validate_context_block(name, b) == b
    known_clock = {"offset_s": None, "sampled_at": None}
    for name in K.D_CONTEXT_MODULES:
        blk = K._context_module(name)._unknown("no_capture", 1900.0)
        with pytest.raises(ValueError, match="d_" + name):
            K._context_clock(blk, known_clock, name)


@pytest.mark.parametrize("offset", [100.0, None])
def test_the_worker_reads_g5_at_corrected_utc_or_not_at_all(databases, tmp_path, monkeypatch, offset):
    """Zeta HOLD e76b9881d, through make_ps_inputs_fn: with a 100 s offset every G5 reader gets sheet_as_of - 100 from
    the SAME sample the history blocks use (the host time would pick a capture from the corrected future); with no
    offset no G5 reader is called and every G5 block is unknown:clock_unknown (never host time, never an implicit 0)."""
    from scripts import memecoin_desk_judge_worker as W
    from test_desk_ps_model_facts import NOW
    monkeypatch.setattr(W, "read_clock_sample", lambda path, *, now: (offset, None if offset is None else now))
    seen = []
    for name in K.D_CONTEXT_MODULES:
        mod = K._context_module(name)
        monkeypatch.setattr(mod, name + "_for", lambda c, *, desk_dir, as_of, mod=mod, name=name:
                            seen.append((name, as_of)) or mod._unknown("no_capture", as_of))
    out = worker_d_blocks(databases, tmp_path, monkeypatch)["d_blocks"]
    assert out["clock"]["offset_s"] == offset
    if offset is None:
        assert seen == []
        assert all(out[n]["state"] == "unknown" and out[n]["reason"] == "clock_unknown" and out[n]["as_of"] is None
                   for n in K.D_CONTEXT_MODULES)
    else:
        assert sorted(seen) == sorted((n, NOW + 1 - offset) for n in K.D_CONTEXT_MODULES)


@pytest.mark.parametrize("offset", [100.0, None])
def test_build_d_blocks_matches_worker_output(databases, tmp_path, monkeypatch, offset):
    """Office Task 5: the worker's D blocks come from build_d_blocks with explicit inputs (desk_dir, ours, clock,
    ledger, no globals). Calling it again with the worker's exact arguments gives byte-identical JSON, for a nonzero
    and for a missing clock offset."""
    import json
    from scripts import memecoin_desk_judge_worker as W
    monkeypatch.setattr(W, "read_clock_sample", lambda path, *, now: (offset, None if offset is None else now))
    seen = []
    real = W.build_d_blocks

    def spy(*a, **kw):
        out = real(*a, **kw)
        seen.append((a, kw, out))
        return out
    monkeypatch.setattr(W, "build_d_blocks", spy)
    out = worker_d_blocks(databases, tmp_path, monkeypatch)["d_blocks"]
    (a, kw, got), = seen
    assert kw["clock"] == (offset, kw["clock"][1]) and set(kw) == {"desk_dir", "ours", "pass_persist_at", "decision_at",
                                                                   "clock", "ledger"}
    again = real(*a, **kw)
    dump = lambda d: json.dumps(d, sort_keys=True, separators=(",", ":"), default=repr).encode()
    assert dump(again) == dump(got) == dump(out)
