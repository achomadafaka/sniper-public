"""One native-equity rule for the early quote and the gate (2026-10-15, Alpha; operator "whatever
would be fastest"; MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS).

The gate valued spare native in sizing equity for every entry except the fast lanes (rh_fast_lane,
bsc_fast_lane, relay_prebuy); the early quote valued it for all of them. So every fast-lane entry
discarded its early quote with clip_mismatch: 4663 position 73895 (RSI, 20:49Z) sized 102.86 early
and 51.40 at the gate (WETH 102.80 over 2 slots). Both now call _sizing_native_price. The knob lets a
chain's fast lanes count spare native too; empty or malformed keeps today's exclusion."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_evm_risk as risk
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg

FRESH = {"quote_price_usd": 2460.01, "quote_price_age_ms": 777}
FAST = ("rh_fast_lane", "bsc_fast_lane", "relay_prebuy")


def _ev(source):
    return SimpleNamespace(signal_source=source)


@pytest.fixture(autouse=True)
def knob_off(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS", raising=False)


# --- the rule ----------------------------------------------------------------------------------

@pytest.mark.parametrize("source", FAST)
def test_knob_off_keeps_todays_fast_lane_exclusion(source):
    assert runner._sizing_native_price(FRESH, _ev(source), "4663") is None
    assert runner._sizing_native_price(FRESH, _ev("onchain_watch"), "4663") == 2460.01


@pytest.mark.parametrize("raw, chain, want", [
    ("4663", "4663", 2460.01),
    ("4663", "56", None),          # 56 stays excluded unless listed
    ("4663,56", "56", 2460.01),
    ("4663,x", "4663", None),      # malformed = exclusion
    ("", "4663", None),
])
def test_a_listed_chain_lets_its_fast_lanes_count_native(monkeypatch, raw, chain, want):
    monkeypatch.setenv("MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS", raw)
    assert runner._sizing_native_price(FRESH, _ev("rh_fast_lane"), chain) == want


@pytest.mark.parametrize("fields", [
    {}, {"quote_price_usd": 2460.0}, {"quote_price_usd": 2460.0, "quote_price_age_ms": 1001},
    {"quote_price_usd": float("nan"), "quote_price_age_ms": 5},
    {"quote_price_usd": 0, "quote_price_age_ms": 5}, {"quote_price_usd": True, "quote_price_age_ms": 5},
    {"quote_price_usd": "x", "quote_price_age_ms": 5}, None,
])
def test_an_unknown_or_stale_price_adds_no_native(fields, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS", "4663")
    assert runner._sizing_native_price(fields, _ev("onchain_watch"), "4663") is None


# --- both sizers go through it ------------------------------------------------------------------

def _gate_price(monkeypatch, ev, chain="4663"):
    """The native price the gate hands the leg (and so the reservation's risk envelope)."""
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    monkeypatch.setattr(runner, "_entry_prereads_for", lambda *a: None)
    calls = []
    monkeypatch.setattr(leg, "evm_real_entry", lambda c, e, **kw: calls.append(kw) or {"status": "unknown"})
    ctx = {"usdc_fn": lambda: 102.80, "native_fn": lambda: 0.049729,
           "quote_price_peek_fn": lambda: {"px": FRESH["quote_price_usd"],
                                           "age_ms": FRESH["quote_price_age_ms"]},
           "token_safety_fn": lambda *a: None, "execute_fn": lambda *a: None}
    runner._make_evm_entry_fn({chain: ctx})(None, ev, route=chain, day="2026-10-15",
                                            now_iso="2026-10-15T20:49:00+00:00", trade_size=100)
    return calls[0]["native_quote_price_usd"]


def _early_price(monkeypatch, tmp_path, ev, chain="4663"):
    """The native price the early quote's sizing hands the same risk envelope."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_arming as arming, memecoin_wallet_caps as caps
    from test_memecoin_entry_identity import _approve
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _approve(conn, chains=(chain,))
    conn.close()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(arming, "real_entries_allowed", lambda *a: True)
    monkeypatch.setattr(arming, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(runner, "_trial_for_entry", lambda *a: None)
    monkeypatch.setattr(caps, "entry_cap_usd", lambda *a, **k: 175.0)
    seen = {}

    def context(c, **kw):
        seen.update(kw)
        return SimpleNamespace(trade_size_usd=1.0)
    monkeypatch.setattr(risk, "build_evm_risk_context", context)
    monkeypatch.setattr(risk, "evm_entry_allowed", lambda *a, **kw: SimpleNamespace(allowed=True))
    pre = runner._EntryPreReads.__new__(runner._EntryPreReads)
    pre.generation, pre.chain, pre.context = 1, chain, {}
    pre.balances = lambda: (102.80, 0.049729, False, 1.0)
    pre.stale = lambda **kw: False
    ctx = {"quote_price_peek_fn": lambda: {"px": FRESH["quote_price_usd"],
                                           "age_ms": FRESH["quote_price_age_ms"]}}
    ev.source_user_id, ev.mint, ev.network_id = "u1", "0xce4c9418b8e9bcad3e4ba2bd84123a49a6911e18", chain
    runner._early_evm_quote_clip(ev, pre, str(db), ctx, chain)
    return seen["native_quote_price_usd"]


@pytest.mark.parametrize("source", ("rh_fast_lane", "bsc_fast_lane", "relay_prebuy", "onchain_watch"))
@pytest.mark.parametrize("knob", ("", "4663"))
def test_the_early_quote_and_the_gate_value_native_identically(monkeypatch, tmp_path, source, knob):
    """Alpha's check: the same inputs through both sizers give the same native price, so the same
    clip. Before, the fast-lane rows differed (early 2460.01, gate None)."""
    monkeypatch.setenv("MEMECOIN_EVM_FAST_LANE_NATIVE_EQUITY_CHAINS", knob)
    gate = _gate_price(monkeypatch, SimpleNamespace(mint="0xabc", signal_source=source))
    early = _early_price(monkeypatch, tmp_path, SimpleNamespace(signal_source=source))
    assert gate == early
    assert gate == (None if source in FAST and not knob else 2460.01)


def test_both_sizers_call_the_one_rule(monkeypatch, tmp_path):
    """Divergence needs a second rule; a sentinel from the one rule must reach both sizers."""
    seen = []

    def rule(fields, ev, chain):
        seen.append((ev.signal_source, chain))
        return 1234.5
    monkeypatch.setattr(runner, "_sizing_native_price", rule)
    assert _gate_price(monkeypatch, SimpleNamespace(mint="0xabc", signal_source="rh_fast_lane")) == 1234.5
    assert _early_price(monkeypatch, tmp_path, SimpleNamespace(signal_source="rh_fast_lane")) == 1234.5
    assert seen == [("rh_fast_lane", "4663"), ("rh_fast_lane", "4663")]


def test_the_runner_has_no_other_native_price_rule():
    from pathlib import Path
    text = Path(runner.__file__).read_text()
    assert text.count("native_quote_price_usd=native_price") == 2
    assert text.count("native_price = _sizing_native_price(") == 2
    assert '"bsc_fast_lane",\n' not in text.split("def _sizing_native_price")[0]


# --- what the knob does to the 4663 clip of 73895 -------------------------------------------------

@pytest.fixture
def ledger(monkeypatch):
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"4663": 2})
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"4663": 175})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN", {})
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


@pytest.mark.parametrize("price, want", [(None, 51.40), (2460.01, 102.80)])
def test_the_knob_turns_the_73895_clip_from_half_the_weth_into_the_weth(ledger, price, want):
    """WETH 102.80 and native 0.049729 at 2460.01: excluded, equity is the WETH alone (51.40 over 2
    slots); counted, equity ~221 over 2 slots is clamped to the spendable WETH, 102.80. The float
    cover and the pilot clip ($100) still bound the send."""
    ctx = risk.build_evm_risk_context(ledger, usdc_balance=102.80, native_balance=0.049729,
                                      native_quote_price_usd=price, day="2026-10-15",
                                      network_id="4663")
    assert ctx.trade_size_usd == pytest.approx(want)
