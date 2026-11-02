"""An exact source exclusion can repair one floor, without bypassing other gates."""
import pytest
from sniper.services.memecoin_real_runner import volume_alive_decision as decide
from sniper.services import memecoin_volume_writer as w


def test_lower_bound_repairs_the_floor_without_another_source_subtraction():
    args=dict(abs_floor=0,turnover_floor=0,vol_m5=2394.6,m5_floor=.02,signal_usd=1987.71)
    assert decide(10000,133874.49,**args)[0] is False
    assert decide(10000,133874.49,**args,m5_floor_lower_bound=3450)[0] is True


def test_lower_bound_preserves_hourly_and_transaction_gates():
    args=dict(abs_floor=0,turnover_floor=.01,vol_m5=100,m5_floor=.02,m5_floor_lower_bound=3450)
    assert decide(1,133874.49,**args)[0] is False
    assert decide(10000,133874.49,**args,m5_txns=0,m5_txns_floor=2)[2]=='m5_txns_under_floor'


def test_lower_bound_does_not_replace_raw_volume_for_the_ceiling():
    assert decide(10000,1000,abs_floor=0,turnover_floor=0,vol_m5=900,m5_floor=.02,
                  m5_ceiling=.5,m5_floor_lower_bound=100)[2]=='m5_turnover_over_ceiling'


def test_gate_adapter_is_off_without_reads(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',set(),raising=False)
    monkeypatch.setattr(w,'read_for_gate',lambda *a,**k:pytest.fail('unexpected database read'))
    assert w.gate_evidence('4663','mint','swap:uuid') is None


def test_unknown_transaction_cannot_claim_source_exclusion(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',{'4663'},raising=False)
    monkeypatch.setattr(w,'read_for_gate',lambda *a,**k:pytest.fail('unknown source identity'))
    assert w.gate_evidence('4663','mint','swap:uuid')['reason']=='source_tx_unknown'


@pytest.mark.parametrize('prefix',['swap:raybot:','onchain:'])
def test_gate_adapter_passes_the_exact_source_hash(monkeypatch,prefix):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',{'4663'})
    tx='0x'+'Ab'*32;calls=[]
    monkeypatch.setattr(w,'read_for_gate',lambda *a,**kw:calls.append((a,kw)) or {'usd':123})
    assert w.gate_evidence('4663','mint',prefix+tx+':wallet')['usd']==123
    assert calls==[(('4663','mint'),{'exclude_tx':tx,'source_tokens':None})]


# ONE NUMBER FOR BOTH LANES (2026-09-28). DopamineFeenFr1's 4663 buy 0x2285b544... was refused by
# the on-chain lane (attempt 99043164) on "h1 organic volume $0" and, 0.2 s later, by J7 (28211dc9)
# on "$2171": only J7 got the tape bound, because the M5 chains parsed `swap:raybot:<tx>` and
# `onchain:<tx>` but not the on-chain lane's composite `onchain:<chain>:<tx>:<mint>`.
_ROW_TX = '0x2285b544f3d8b193ce39eedb76f0962bfb572695ef529c29796543f4f3f6c3f3'
_ROW_MINT = '0xfaf8ff44495f4b53ef4439344630f2a6d3d3e29e'
_ROW_ORIGINS = {
    '99043164 onchain_watch': 'onchain:4663:' + _ROW_TX + ':' + _ROW_MINT,
    '28211dc9 follow_activity': 'swap:raybot:' + _ROW_TX + ':0xbdce13a1cadea23a61013fbfb470c7eef4ba9520:' + _ROW_MINT,
}


@pytest.mark.parametrize('attempt', sorted(_ROW_ORIGINS))
def test_both_lanes_read_the_same_tape_bound_for_the_same_buy(monkeypatch, attempt):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS', {'4663', '8453'})
    calls = []
    monkeypatch.setattr(w, 'read_for_gate', lambda *a, **kw: calls.append((a, kw)) or {'usd': 2171.33})
    assert w.gate_evidence('4663', _ROW_MINT, _ROW_ORIGINS[attempt])['usd'] == 2171.33
    assert calls == [(('4663', _ROW_MINT), {'exclude_tx': _ROW_TX, 'source_tokens': None})]


@pytest.mark.parametrize('origin', [
    'onchain:8453:' + _ROW_TX + ':' + _ROW_MINT,                  # another chain's detection
    'onchain:4663:' + _ROW_TX + ':0x' + '11' * 20,                # another coin's detection
])
def test_a_composite_for_another_chain_or_coin_stays_unknown(monkeypatch, origin):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS', {'4663', '8453'})
    monkeypatch.setattr(w, 'read_for_gate', lambda *a, **k: pytest.fail('foreign source identity'))
    assert w.gate_evidence('4663', _ROW_MINT, origin)['reason'] == 'source_tx_unknown'


def test_revival_check_uses_the_same_source_excluded_floor(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner,'volume_gate_args',lambda *a,**k:(True,0,0,dict(
        abs_floor=0,turnover_floor=0,vol_m5=10,m5_floor=.1)))
    calls=[]
    monkeypatch.setattr(w,'gate_evidence',lambda *a,**kw:calls.append((a,kw)) or {'usd':200})
    event=SimpleNamespace(mint='mint',fomo_event_id='onchain:source',their_size_usd=0)
    assert runner.revival_watch_volume_cleared({'volume_h1':1000},event,mcap=1000,network_id='4663')
    assert calls==[(('4663','mint','onchain:source'), {'stats': {'volume_h1':1000}, 'source_tokens': None})]


@pytest.mark.parametrize('required',[False,True])
def test_insufficient_partial_bound_preserves_unknown_provider_volume(required):
    args=dict(abs_floor=0,turnover_floor=0,vol_m5=None,m5_floor=.1,m5_required=required)
    assert decide(1000,1000,**args,m5_floor_lower_bound=50)==decide(1000,1000,**args)


def test_sufficient_partial_bound_can_prove_the_floor_without_provider_volume():
    assert decide(1000,1000,abs_floor=0,turnover_floor=0,vol_m5=None,m5_floor=.1,
                  m5_required=True,m5_floor_lower_bound=150)[0] is True


# THE PROPERTY THAT MAKES A PARTIAL SUM SAFE (2026-09-16). The on-chain number skips pools it
# cannot price and windows it has not covered, so it is a LOWER bound and nothing more. That is
# only safe while a bound can never turn an admission into a refusal. If this test fails, the
# partial sums in memecoin_onchain_volume / memecoin_volume_writer become a refusing input: fix
# the gate, never this test.
@pytest.mark.parametrize('mcap',[None,0,5_000,133_874.49,3_000_000])
@pytest.mark.parametrize('vol_m5',[None,0,50,2394.6,90_000])
@pytest.mark.parametrize('extra',[
    dict(),
    dict(m5_required=True),
    dict(m5_ceiling=.5),
    dict(m5_band_max_mcap=2_000_000,m5_floor_over_band=.03,m5_ceiling_max_mcap=150_000,m5_ceiling=.5),
    dict(m5_txns=1,m5_txns_floor=2,m5_txns_required=True),
    dict(signal_usd=1987.71,source_age_s=.15),
])
def test_a_lower_bound_can_admit_but_never_refuse(mcap,vol_m5,extra):
    base=dict(abs_floor=3000,turnover_floor=.01,vol_m5=vol_m5,m5_floor=.02,**extra)
    for vol_h1 in (None,0,3573.83,1_000_000):
        allowed=decide(vol_h1,mcap,**base)[0]
        for bound in (None,-1,float('nan'),0,1,7.23,3450.42,1e12):
            with_bound=decide(vol_h1,mcap,**base,m5_floor_lower_bound=bound)[0]
            assert with_bound or not allowed, (vol_h1,bound)


# --- the quote token must be priceable, or the chain is half blind (2026-09-22) -----------------
#
# GIGABYTE ref 51a1a1b6, 19:10:08Z: onchain_m5 saw 51 swaps in a 4663 V4 pool and priced NONE of
# them, so the gate fell back to the provider's $309 and refused on the m5 floor. Cause: this
# priced only the CHAIN_QUOTE token and the native coin, so every USDG-quoted pool answered None.
# Measured on pool_meta for 4663: 31,849 of 66,875 pools priceable before, 49,220 after — 47.6% to
# 73.6%, a gain of 17,371 pools.

def test_a_configured_stable_quote_is_priced_at_one_dollar():
    from sniper.services.memecoin_onchain_volume import _default_price
    USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    assert _default_price("4663", USDG) == 1.0
    assert _default_price("4663", USDG.upper()) == 1.0, "address case must not decide a price"


def test_a_tokenized_stock_quote_is_NOT_priced():
    """0x86923f96... reads symbol "AMD", 18 decimals — a tokenized equity, not a stable and not a
    wrapper. Its USD value moves with a stock we do not track, so any number here is invented.
    Unknown stays None; unknown is never zero."""
    from sniper.services.memecoin_onchain_volume import _default_price
    assert _default_price("4663", "0x86923f96303d656e4aa86d9d42d1e57ad2023fdc") is None


def test_an_unconfigured_chains_stable_is_not_borrowed():
    """A stable configured for one chain must not price a token on another."""
    from sniper.services.memecoin_onchain_volume import _default_price
    assert _default_price("56", "0x5fc5360d0400a0fd4f2af552add042d716f1d168") is None


def test_the_stable_list_comes_from_config_not_a_literal(monkeypatch):
    from sniper.services import memecoin_onchain_volume as ov
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_USD_STABLES", {"4663": ("0xABCDEF",)})
    assert ov._default_price("4663", "0xabcdef") == 1.0
    assert ov._default_price("4663", "0x5fc5360d0400a0fd4f2af552add042d716f1d168") is None


# --- swaps we saw but could not price make the FLOOR undecidable (2026-09-22) -------------------
#
# GIGABYTE ref 51a1a1b6: our lane saw 51 swaps in a 4663 pool and priced NONE of them, so the gate
# fell back to the provider's $309 and refused on the m5 floor. That provider is measured to
# UNDERCOUNT — on 2026-09-16 it reported ONE of six swaps, $2,394.60 against a chain truth of
# $3,450 — so refusing a floor on it while holding unpriceable evidence of trading is treating an
# unknown as a zero. 4663 carries ~17,000 pools quoted in tokenized equities (NVDA, META, HOOD,
# SPCX, AMD) for which no honest USD price exists, so this is a standing class, not one coin.

def _decide(**kw):
    from sniper.services.memecoin_real_runner import volume_alive_decision
    base = dict(abs_floor=0.0, turnover_floor=0.0, mcap=100_000.0, vol_h1=50_000.0,
                vol_m5=100.0, m5_floor=0.10, m5_required=False, signal_usd=0.0)
    base.update(kw)
    mcap = base.pop("mcap")
    vol_h1 = base.pop("vol_h1")
    return volume_alive_decision(vol_h1, mcap, **base)


def test_an_unpriceable_lane_makes_the_m5_floor_undecidable_not_a_refusal():
    refused = _decide()
    assert refused[0] is False and refused[2].startswith("m5_turnover_under")

    undecidable = _decide(m5_floor_undecidable=True)
    assert undecidable[0] is True, "a floor we cannot measure must not refuse"
    assert undecidable[2] == "onchain_unpriced_undecidable"


def test_the_absolute_m5_floor_takes_the_same_rule():
    refused = _decide(vol_m5=10.0, m5_abs_floor_usd=500.0, m5_floor=0.0)
    assert refused[0] is False and refused[2] == "m5_volume_under_abs_floor"

    undecidable = _decide(vol_m5=10.0, m5_abs_floor_usd=500.0, m5_floor=0.0,
                          m5_floor_undecidable=True)
    assert undecidable[0] is True and undecidable[2] == "onchain_unpriced_undecidable"


def test_the_ceiling_is_untouched_by_an_unpriceable_lane():
    """A ceiling refusal on an UNDER-measurement is conservative: the real number can only be
    higher, so it is even further over the bound. Loosening it would be the unsafe direction."""
    ceiling = _decide(vol_m5=10_000_000.0, m5_ceiling=1.5, m5_ceiling_max_mcap=10_000_000.0,
                      m5_floor=0.0, m5_floor_undecidable=True)
    assert ceiling[0] is False, "the ceiling must still refuse"
    assert ceiling[2] != "onchain_unpriced_undecidable"


def test_an_undecidable_m5_floor_still_lets_the_hourly_floor_refuse():
    """Codex review 2026-09-23 A1: the undecidable m5 floor returned an overall allow and skipped
    the legs after it. Only the m5 FLOOR is unknown; the hourly floor has its own inputs."""
    out = _decide(vol_h1=0.0, mcap=10_000.0, abs_floor=100.0, turnover_floor=0.01, vol_m5=10.0,
                  m5_floor=0.10, m5_floor_undecidable=True)
    assert out[0] is False and out[2] == "turnover_under_floor"
    control = _decide(vol_h1=0.0, mcap=10_000.0, abs_floor=100.0, turnover_floor=0.01,
                      vol_m5=10.0, m5_floor=0.0, m5_floor_undecidable=True)
    assert control[2] == "turnover_under_floor"


def test_an_undecidable_m5_floor_still_lets_the_ceiling_refuse():
    """Same finding, the other leg: organic m5 under the floor (undecidable) while RAW m5 sits
    over the ceiling. The ceiling reads the raw number and must still refuse."""
    out = _decide(vol_h1=100_000.0, mcap=10_000.0, vol_m5=20_000.0, signal_usd=19_990.0,
                  m5_floor=0.10, m5_ceiling=1.5, m5_floor_undecidable=True)
    assert out[0] is False and out[2] == "m5_turnover_over_ceiling"


def test_an_undecidable_abs_floor_still_lets_the_hourly_floor_refuse():
    out = _decide(vol_h1=0.0, mcap=10_000.0, turnover_floor=0.01, vol_m5=10.0,
                  m5_abs_floor_usd=500.0, m5_floor=0.0, m5_floor_undecidable=True)
    assert out[0] is False and out[2] == "turnover_under_floor"


def test_a_passing_floor_is_not_relabelled():
    """The flag only rescues a floor that WOULD have refused; it never rewrites a pass."""
    ok = _decide(vol_m5=50_000.0, m5_floor_undecidable=True)
    assert ok[0] is True and ok[2] != "onchain_unpriced_undecidable"


def test_a_blind_onchain_lane_makes_the_m5_floor_undecidable():
    """JOLLYBOT d058ca73 (2026-09-22): the observer had just reset its window, the lane answered
    window_incomplete, and the gate refused on the provider's $0.49 while the coin ran +98%.
    A lane that did not measure cannot refuse; the label names the blindness."""
    undecidable = _decide(m5_floor_undecidable=True)
    assert undecidable[0] is True


# --- a quiet coin whose source transaction never reached the tape (2026-09-23) -------------------

def test_a_quiet_coin_with_an_absent_source_transaction_reaches_the_floor():
    """ILY (4663, 16:21:38Z) is the case. Our lane summed $0, the provider's m5 was $5.13 and that
    single transaction WAS the source's own buy, so nothing traded anywhere. Promoting the sum to
    the lower bound must let the floor bind, not label the lane blind: on current behaviour 4 of 4
    EVM attempts after the 15:29Z deploy read `source_tx_absent` and the m5 floor bound on none."""
    refused = _decide(vol_m5=5.13, m5_floor_lower_bound=0.0, m5_floor=0.10, mcap=100_000.0)
    assert refused[0] is False
    assert refused[2] == "m5_turnover_under_floor"


def test_a_venue_gap_still_refuses_to_decide():
    """MRLN's shape: the lane stays blind, so the floor cannot refuse on a sum from the wrong
    venue. `m5_floor_undecidable` is what the runner sets when the verdict is a venue gap."""
    undecidable = _decide(vol_m5=0.11, m5_floor_undecidable=True, m5_floor=0.10)
    assert undecidable[0] is True


# --- the on-chain sum bounds the HOUR from below too (FEEDME, 2026-09-23) -----------------------

def test_feedme_would_have_been_admitted_by_its_own_tape():
    """FEEDME (4663, 0de1e0bb, 18:23:24Z) was refused "h1 organic volume $1 under floor $3000"
    while our tape read 55 swaps and $5,502.47 organic in the same five minutes. The provider row
    was a stub for a seven-minute-old Pons-curve coin it had not indexed."""
    args = dict(abs_floor=3000.0, turnover_floor=0.0, mcap=100_000.0, vol_h1=0.62,
                vol_m5=5502.47, m5_floor=0.0, signal_usd=0.0)
    assert _decide(**args)[0] is False, 'the provider alone still refuses'
    assert _decide(**args, h1_floor_lower_bound=5502.47)[0] is True


def test_the_bound_may_only_raise_the_hour_never_lower_it():
    args = dict(abs_floor=3000.0, turnover_floor=0.0, mcap=100_000.0, vol_h1=9000.0,
                vol_m5=100.0, m5_floor=0.0, signal_usd=0.0)
    assert _decide(**args)[0] is True
    assert _decide(**args, h1_floor_lower_bound=1.0)[0] is True, 'a small bound cannot refuse'


def test_a_window_that_measured_nothing_carries_no_hourly_bound():
    """`hash_unverified` and `protocol_lagging` both return `usd` None, and the explicit reason test
    means a future reason code cannot leak in either."""
    from sniper.services.memecoin_real_runner import onchain_h1_lower_bound as bound

    assert bound({"reason": "ok", "usd": 5502.47, "unverified_swaps": 0}) == pytest.approx(5502.47)
    assert bound({"reason": "ok", "usd": 0.0, "unverified_swaps": 0}) == 0.0
    assert bound({"reason": "hash_unverified", "usd": None, "unverified_swaps": 3}) is None
    assert bound({"reason": "protocol_lagging", "usd": None}) is None
    assert bound({"reason": "ok", "usd": 5502.47, "unverified_swaps": 2}) is None
    assert bound({"reason": "source_tx_absent", "usd": 5502.47}) is None
    assert bound({"reason": "ok", "usd": None}) is None
    assert bound({"reason": "ok", "usd": -1.0}) is None
    assert bound(None) is None


def test_a_missing_provider_hour_is_still_missing():
    """OUT OF SCOPE, deliberately: the bound only ever RAISES a number the provider published. All
    three observed refusals of this class carried a provider figure (0.62, 0.0, 1240.21), none
    None. Substituting a 300 s sum for an absent HOUR is a larger claim and is not made here."""
    args = dict(abs_floor=3000.0, turnover_floor=0.0, mcap=100_000.0, vol_h1=None,
                vol_m5=100.0, m5_floor=0.0, signal_usd=0.0)
    before = _decide(**args)
    after = _decide(**args, h1_floor_lower_bound=5502.47)
    assert before == after


def test_the_ceiling_never_reads_the_on_chain_bound():
    """The bound is a floor instrument only; a safety bound must not be loosened by it."""
    args = dict(abs_floor=0.0, turnover_floor=0.0, mcap=1000.0, vol_h1=10_000.0, vol_m5=900.0,
                m5_floor=0.02, m5_ceiling=0.5, signal_usd=0.0)
    assert _decide(**args)[2] == 'm5_turnover_over_ceiling'
    assert _decide(**args, h1_floor_lower_bound=5502.47)[2] == 'm5_turnover_over_ceiling'


def test_both_gate_sites_wire_the_hourly_bound():
    """A SOURCE GUARD: the defect would be one of the call sites keeping only the m5 line. Four
    sites: the entry hook, the revival watch, m5_floor_evidence (the paper record of the gate,
    2026-10-07), which must read the gate the same way, and the J7 tape bound read on a would-be
    refusal (_m5_redecide_with_source_unknown_bound, 2026-10-15)."""
    from pathlib import Path
    src = Path("sniper/services/memecoin_real_runner.py").read_text()
    assert src.count('h1_floor_lower_bound"] = onchain_h1_lower_bound(') == 4


# ---- rule 3 per venue needs the source's token count at the gate (ULTRONICS, 2026-10-07) ----

def test_the_source_token_count_prefers_the_receipt_proof_over_the_j7_estimate():
    import types
    from sniper.services import memecoin_real_runner as RR
    proof = types.SimpleNamespace(their_size_usd=887.0, their_price=3.75e-05, source_evidence={
        "source_fill": {"acquired_atomic": str(23_643_884 * 10 ** 18), "token_decimals": 18}})
    assert RR._source_tokens(proof) == pytest.approx(23_643_884.0)
    j7 = types.SimpleNamespace(their_size_usd=887.0, their_price=3.75e-05, source_evidence=None)
    assert RR._source_tokens(j7) == pytest.approx(887.0 / 3.75e-05)
    for bad in ({"their_size_usd": 0, "their_price": 1.0}, {"their_size_usd": 5.0, "their_price": None},
                {"their_size_usd": float("nan"), "their_price": 1.0}, {}):
        assert RR._source_tokens(types.SimpleNamespace(**bad)) is None


def test_gate_evidence_hands_the_source_token_count_to_the_tape_read(monkeypatch):
    from sniper.services import memecoin_volume_writer as VW
    seen = {}
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663"})
    monkeypatch.setattr(VW, "_gate_source_tx", lambda chain, mint, origin: "0xsrc")
    monkeypatch.setattr(VW, "read_for_gate", lambda chain, mint, **k: seen.update(k) or {"usd": 1.0})
    VW.gate_evidence("4663", "0x" + "ab" * 20, "onchain:4663:0xsrc:m", source_tokens=12.5)
    assert seen == {"exclude_tx": "0xsrc", "source_tokens": 12.5}


# ---- Gamma on 485333d25: the gate must HEAR source_venue_untaped (writer and reader must meet) ----

def _gate_through_hook(monkeypatch, tmp_path, evidence, name):
    """One coin through the real entry hook: the provider's m5 ($10 on a $100k coin) is under a
    0.10 in-band turnover floor, and our own tape answers `evidence`."""
    import sqlite3
    from types import SimpleNamespace
    from test_memecoin_entry_identity import NOW, FakeClock, _arm, _event, _solana_context
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_volume_writer as vw
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"solana"}, raising=False)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.10)
    monkeypatch.setattr(vw, "gate_evidence", lambda *a, **k: dict(evidence))
    from sniper.services import memecoin_market_state as MS
    # The volume gate reads the band's mcap from the classification (None without a context).
    monkeypatch.setattr(R, "_classify_funded_draft", lambda conn, ev, identity, at, ctx, **k:
                        MS.EntryClassification(1, "solana", ev.mint, "onchain_watch", at, "micro",
                                               "live", "normal", "flat", "normal", "complete", 1.0,
                                               (), {"effective_mcap": 100_000.0}))
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / ("%s.db" % name)))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected", "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R, "chase_impact_allowance", lambda *a, **k: (None, "test"))
    subs = {}
    trace = SimpleNamespace(attempt_id=None, mark=lambda *a, **k: True, finish=lambda *a, **k: True,
                            mark_substage=lambda n, **k: subs.setdefault(n, k) or True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=100_000.0)
    res = R.real_entry_hook(conn, ev, {"solana": _solana_context({})}, now_fn=lambda: NOW,
                            trade_size=10.0, max_age_s=120, trace=trace, trace_clock=FakeClock(),
                            stats_fn=lambda mint, network_id=None: {
                                "price": 0.25, "market_cap": 100_000.0, "volume_m5": 10.0,
                                "volume_h1": 50_000.0, "txns_m5_buys": 5, "txns_m5_sells": 5})
    return res, subs


def test_a_measured_low_m5_refuses_through_the_real_gate(monkeypatch, tmp_path):
    res, subs = _gate_through_hook(monkeypatch, tmp_path, {
        "usd": 10.0, "reason": "ok", "coverage": "partial", "covered_s": 300.0, "pools": []}, "ok")
    assert res["status"] != "rejected" or res["reason"] != "stub", "the control must be refused"
    assert "m5" in str(res.get("reason")), res


def test_source_venue_untaped_is_blind_at_the_real_gate_and_cannot_refuse(monkeypatch, tmp_path):
    res, subs = _gate_through_hook(monkeypatch, tmp_path, {
        "usd": None, "reason": "source_venue_untaped", "coverage": "blind_source_venue_untaped",
        "covered_s": 300.0, "pools": [], "source_tokens_taped": 1.0}, "blind")
    assert subs["onchain_volume_m5"]["measurement_basis"] == "onchain_blind_undecidable"
    assert res == {"status": "rejected", "reason": "stub"}, res        # reached the entry, not m5


def test_the_revival_watch_treats_a_blind_tape_read_as_undecidable(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_volume_writer as vw
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.10)
    ev = SimpleNamespace(mint="mint", fomo_event_id="onchain:4663:0xsrc:mint", their_size_usd=0)
    st = {"volume_m5": 10.0, "volume_h1": 50_000.0}
    monkeypatch.setattr(vw, "gate_evidence", lambda *a, **k: {"usd": 10.0, "reason": "ok"})
    assert not R.revival_watch_volume_cleared(st, ev, mcap=100_000.0, network_id="4663")
    monkeypatch.setattr(vw, "gate_evidence",
                        lambda *a, **k: {"usd": None, "reason": "source_venue_untaped"})
    assert R.revival_watch_volume_cleared(st, ev, mcap=100_000.0, network_id="4663")
