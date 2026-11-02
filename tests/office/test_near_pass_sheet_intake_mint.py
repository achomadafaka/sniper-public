"""near_pass_sheet must hand ps_asof the ticket's own mint read (its SKIP hard_filter row has token_safety pass) and
must name the StaleSnapshot reason instead of the bare class. Live 2026-10-29: 59/59 sheets were
unknown refresh_failed:StaleSnapshot because intake_mint_of only reads a PASS stage."""
from types import SimpleNamespace

from sniper.office import desk_near_pass as N
from sniper.office.tools import sheet as SH
from sniper.services.memecoin_desk_asof import StaleSnapshot

NOW = 1_800_000_000.0
KNOWN = "2027-01-15T08:00:00+00:00"
SAFETY = {"status": "pass", "reason": "mint_read", "authority_mint": "closed", "authority_freeze": "closed",
          "decimals": 6, "mint_context_slot": 77, "supply_atomic": "1000000"}


def ticket(**over):
    cand = {"network_id": "solana", "mint": "m", "pool": "p", "intake": {"feed": "pumpfun"}, "snapshot": {"hash": "h"}}
    ev = {"network_id": "solana", "mint": "m", "pool": "p", "snapshot_hash": "h", "known_at": KNOWN,
          "checks": {"token_safety": dict(SAFETY)}, "market_cut": {"receipt_cut": 5.0, "cut_slot": 9}}
    ev.update(over.pop("ev", {}))
    t = {"candidate": cand, "cohort_id": "D", "opportunity_id": "o",
         "stages": [{"stage": "hard_filter", "verdict": "SKIP", "reason": "unknown:chart:x", "ts": KNOWN,
                     "inputs": {"evidence": ev}}]}
    t.update(over)
    return t


def run(t, refresh, monkeypatch):
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: {"state": "known"})
    store = SimpleNamespace(get_ticket=lambda c, i: t, get_cohort=lambda c, i: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {})
    return N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=NOW, clock_sample={}, buyer_ledger=[],
                             refresh=refresh, store=store)


def test_refresh_gets_the_ticket_own_mint_read(monkeypatch):
    seen = {}
    run(ticket(), lambda c, **k: seen.update(k) or (c, {}), monkeypatch)
    assert seen["integrity"] is True and seen["mint_max_age_s"] == N.MINT_MAX_AGE_S and seen["accept_curve_derived"] is True
    assert seen["intake_mint"] == {"supply_atomic": "1000000", "decimals": 6, "authority_mint": "closed",
                                   "known_at": _ts(), "context_slot": 77}


def _ts():
    from datetime import datetime
    return datetime.fromisoformat(KNOWN).timestamp()


def test_unbound_or_failed_safety_gives_no_mint_read(monkeypatch):
    for t in (ticket(ev={"mint": "other"}), ticket(ev={"snapshot_hash": "x"}),
              ticket(ev={"checks": {"token_safety": dict(SAFETY, status="unknown")}})):
        seen = {}
        run(t, lambda c, **k: seen.update(k) or (c, {}), monkeypatch)
        assert seen["intake_mint"] is None


def test_the_stale_reason_is_named(monkeypatch):
    def boom(c, **k):
        raise StaleSnapshot("tape_stale")
    r = run(ticket(), boom, monkeypatch)
    assert r["state"] == "unknown" and r["reason"] == "refresh_failed:StaleSnapshot:tape_stale"


def test_mint_age_bound_is_separate_from_the_sol_bound():
    import pytest
    from sniper.services import memecoin_desk_ps_asof as PA
    rec = {"supply_atomic": "1000000", "decimals": 6, "authority_mint": "closed", "known_at": 1000.0, "context_slot": 50}
    src = {"slot": 40}
    with pytest.raises(StaleSnapshot, match="intake_mint_unusable"):
        PA._intake_facts(rec, src, 1600.0, 900.0, 120.0)                       # SOL bound: 600 s old is refused
    assert PA._intake_facts(rec, src, 1600.0, 900.0, N.MINT_MAX_AGE_S)["metadata_known_at"] == 1000.0


def test_the_sheet_cut_is_the_refresh_cut_so_facts_match_the_candidate_state(monkeypatch):
    """Live 2026-10-29: 13/30 PASS sheets were sheet_unavailable:ps_facts_candidate. facts_for needs
    candidate pool_state.cut_slot == the cut_slot it is given, but the sheet passed the ticket's older persisted slot."""
    seen = {}
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: seen.update(kw) or {"state": "known"})
    store = SimpleNamespace(get_ticket=lambda c, i: ticket(), get_cohort=lambda c, i: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {})
    N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=NOW, clock_sample={}, buyer_ledger=[],
                      refresh=lambda c, **k: (c, {"receipt_cut": NOW - 2.0, "cut_slot": 123}), store=store)
    assert seen["hard_filter_row"]["market_cut"] == {"receipt_cut": NOW - 2.0, "cut_slot": 123}


def test_the_sheet_call_time_is_never_before_the_snapshot(monkeypatch):
    """Live 2026-10-29: 12/30 sheets refused snapshot_before_pass_or_future; the snapshot as_of was rounded 0.4 us past now."""
    seen = {}
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: seen.update(kw) or {"state": "known"})
    store = SimpleNamespace(get_ticket=lambda c, i: ticket(), get_cohort=lambda c, i: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {})
    N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=NOW, clock_sample={}, buyer_ledger=[],
                      refresh=lambda c, **k: (c, {"as_of": NOW + 4e-7}), store=store)
    assert seen["now"] >= NOW + 4e-7
