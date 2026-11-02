"""Office pool-state fallback: a capped, fail-closed exact pool read, used only when the tape cannot prove price."""
import json
from types import SimpleNamespace

import pytest

from sniper.office import desk_near_pass as N
from sniper.office import pool_state_rpc as R
from sniper.office.tools import sheet as SH
from sniper.services.memecoin_desk_asof import StaleSnapshot

WSOL = "So11111111111111111111111111111111111111112"
GOOD = {"pool": "P", "facts": {"base_mint": "M", "quote_mint": WSOL, "virtual_quote": 7}, "base_reserve": 5,
        "quote_reserve": 6, "slot": 100}


def reader(tmp_path, read=None, cap=1000, calls=2, now=1_800_000_000.0):
    def fake(pool, *, rpc):
        for _ in range(calls):
            rpc("getMultipleAccounts", [])
        return read(pool) if callable(read) else (GOOD if read is None else read)
    return R.PoolStateReader("http://x", tmp_path / "c.json", cap_credits=cap, credits_per_call=5, clock=lambda: now,
                             post=lambda url, body: {"result": {}}, read_pool_state=fake)


def test_a_good_read_is_returned_and_charged_per_actual_call(tmp_path):
    r = reader(tmp_path)
    assert r.read("P", "M") == {"base_reserve": 5, "quote_reserve": 6, "virtual_quote": 7, "slot": 100,
                                "read_at": 1_800_000_000.0}
    assert json.loads((tmp_path / "c.json").read_text()) == {"month": "2027-01", "credits": 10}


def test_the_cap_refuses_before_any_call_and_never_resets_on_a_bad_counter(tmp_path):
    r = reader(tmp_path, cap=14)                       # worst case 3 calls x 5 = 15 > 14
    assert r.read("P", "M") == {"unknown": "rpc_cap"} and not (tmp_path / "c.json").exists()
    (tmp_path / "c.json").write_text("{not json")
    assert reader(tmp_path).read("P", "M") == {"unknown": "rpc_counter_unreadable"}


def test_identity_and_quote_mint_are_checked(tmp_path):
    assert reader(tmp_path).read("P", "OTHER") == {"unknown": "rpc_pool_mismatch"}
    bad = dict(GOOD, facts=dict(GOOD["facts"], quote_mint="USDC"))
    assert reader(tmp_path, read=bad).read("P", "M") == {"unknown": "rpc_pool_mismatch"}
    assert reader(tmp_path, read=lambda p: None).read("P", "M") == {"unknown": "rpc_pool_unusable"}


def test_a_transport_error_is_unknown_and_still_charged(tmp_path):
    def boom(pool):
        raise TimeoutError()
    r = reader(tmp_path, read=boom)
    assert r.read("P", "M") == {"unknown": "rpc_error:TimeoutError"}
    assert json.loads((tmp_path / "c.json").read_text())["credits"] == 10


# ---- near_pass_sheet fallback wiring
KNOWN = "2027-01-15T08:00:00+00:00"


def ticket():
    ev = {"known_at": KNOWN, "checks": {}, "market_cut": {"receipt_cut": 5.0, "cut_slot": 9}}
    return {"candidate": {"mint": "M", "pool": "P"}, "cohort_id": "D", "opportunity_id": "o",
            "stages": [{"stage": "hard_filter", "verdict": "SKIP", "reason": "unknown:x", "ts": KNOWN,
                        "inputs": {"evidence": ev}}]}


def run(monkeypatch, reasons, reader_out, naps=None):
    seen = []
    monkeypatch.setattr(SH, "sheet", lambda cand, **kw: seen.append(("sheet", kw["now"])) or {"state": "known"})
    store = SimpleNamespace(get_ticket=lambda c, i: ticket(), get_cohort=lambda c, i: {"policy": {"hard_filter": {}}},
                            get_intake_source_receipt=lambda *a, **k: {})
    queue = list(reasons)

    def refresh(c, **k):
        seen.append(("refresh", k.get("rpc_state") is not None))
        why = queue.pop(0)
        if isinstance(why, tuple):
            failure = StaleSnapshot(why[0]);failure.pending_gaps = why[1]
            raise failure
        if why:
            raise StaleSnapshot(why)
        return c, {}
    rd = SimpleNamespace(read=lambda pool, mint: reader_out)
    out = N.near_pass_sheet(None, "T", store_conn="S", desk_dir="/d", now=1000.0, clock_sample={}, buyer_ledger=[],
                            refresh=refresh, store=store, pool_state_reader=rd, clock=lambda: 1003.0,
                            sleep=lambda s: (naps if naps is not None else []).append(s))
    return out, seen


@pytest.mark.parametrize("why", ["latest_swap_not_buy", "reserve_stale", "virtual_quote_unproven", "tape_gap_since_cut"])
def test_fallback_reads_once_and_resheets_at_the_read_time(monkeypatch, why):
    got = {"base_reserve": 1, "quote_reserve": 1, "virtual_quote": 0, "slot": 9, "read_at": 1002.5}
    out, seen = run(monkeypatch, [why, None], got)
    assert out["state"] == "known" and seen == [("refresh", False), ("refresh", True), ("sheet", 1003.0)]


def test_other_reasons_do_not_use_the_fallback(monkeypatch):
    out, seen = run(monkeypatch, ["unmapped_pool"], {"unknown": "x"})
    assert out["reason"] == "refresh_failed:StaleSnapshot:unmapped_pool" and seen == [("refresh", False)]


def test_a_failed_read_names_both_causes(monkeypatch):
    out, _ = run(monkeypatch, ["latest_swap_not_buy"], {"unknown": "rpc_cap"})
    assert out["state"] == "unknown" and out["reason"] == "refresh_failed:StaleSnapshot:latest_swap_not_buy;rpc_cap"


def test_a_flickering_heartbeat_is_retried_then_succeeds(monkeypatch):
    naps = []
    out, seen = run(monkeypatch, ["ps_commit_unproven", "tape_stale", None], None, naps)
    assert out["state"] == "known" and naps == [N.RETRY_GAP_S] * 2 and seen[-1] == ("sheet", 1003.0)


def test_retries_are_bounded_and_other_reasons_are_not_retried(monkeypatch):
    naps = []
    out, _ = run(monkeypatch, ["ps_commit_unproven"] * (N.RETRIES + 1), None, naps)
    assert out["reason"] == "refresh_failed:StaleSnapshot:ps_commit_unproven" and len(naps) == N.RETRIES
    naps.clear()
    run(monkeypatch, ["unmapped_pool"], None, naps)
    assert naps == []


GAP = "tape_gap_since_cut"


def test_a_tape_gap_with_a_pending_loss_retries_and_a_settled_gap_does_not(monkeypatch):
    naps = []
    out, seen = run(monkeypatch, [(GAP, {"decode_loss": 999.0}), None], None, naps)
    assert out["state"] == "known" and naps == [N.RETRY_GAP_S]
    naps = []
    out, seen = run(monkeypatch, [(GAP, {})], {"unknown": "rpc_cap"}, naps)
    assert naps == [] and out["reason"].endswith("rpc_cap") and seen == [("refresh", False)]          # at once to the existing fallback, no sleep


def test_a_failed_tape_gap_keeps_its_pending_state_for_the_event(monkeypatch):
    naps = []
    out, _ = run(monkeypatch, [(GAP, {"decode_loss": 999.0})] * (N.RETRIES + 1) + [(GAP, {"decode_loss": 1002.0})], {"base_reserve": 1, "quote_reserve": 1, "virtual_quote": 0, "slot": 9, "read_at": 1002.5}, naps)
    assert len(naps) == N.RETRIES and out["reason"] == "refresh_failed:StaleSnapshot:" + GAP and out["ps_pending_gaps"] == {"decode_loss": 1002.0}
    from sniper.office import sheet_display, seat_tools
    import json
    assert seat_tools._sheet_refs(json.dumps(sheet_display.project(out)))["ps_pending_gaps"] == {"decode_loss": 1002.0}


def test_the_real_refresh_accepts_every_argument_the_sheet_passes():
    """A faked refresh hid a TypeError that failed every sheet read at publish (2026-10-31)."""
    import inspect, re
    from pathlib import Path
    from sniper.services import memecoin_desk_asof as asof, memecoin_desk_ps_asof as ps
    source = Path(N.__file__).read_text()
    passed = set(re.findall(r'(\w+)=', ' '.join(re.findall(r'refresh\(([^)]*)\)', source)))) | {'intake_mint', 'integrity'}
    for fn in (asof.refresh, ps.refresh):
        assert passed <= set(inspect.signature(fn).parameters), (fn.__module__, passed - set(inspect.signature(fn).parameters))


def test_the_office_flag_reaches_the_pumpswap_refresh(monkeypatch):
    from sniper.services import memecoin_desk_asof as asof, memecoin_desk_ps_asof as ps
    seen = {}
    monkeypatch.setattr(ps, 'refresh', lambda c, **k: seen.update(k) or (c, {}))
    monkeypatch.setattr(asof.contract, 'validate_candidate', lambda c: c)
    asof.refresh(dict(producer='U', venue='pumpswap', intake={}), desk_dir='/d', now=1.0, resolve_zero_fees=True)
    assert seen.get('resolve_zero_fees') is True
