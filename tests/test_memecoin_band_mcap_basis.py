"""Solana band mcap: the DexScreener fall-through can be replaced by source price x mint supply.

2026-09-27, operator ("get these DexScreener prices out") via Alpha, option (B). make_real_hook
resolves the band mcap as: event mcap -> same-mint history -> DexScreener -> our route x supply ->
assume_micro. Only stage 3 is replaced, and only behind MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain
(default dexscreener). Measured over funded non-slot Solana attempts since 09-20: 58 bands came from
DexScreener, whose price lagged the source's fill by ~14% at the median; 6 of 52 would change band.

The supply comes from the token-safety mint read, MOVED ahead of the band, never a second read: the
leg's safety gate reuses it. With the default basis nothing moves and no read is added. The basis
is recorded on the trace and in the funded classification facts in both modes.
"""
import types

import pytest

from sniper.execution import solana_token_safety as TS
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

SUPPLY_ATOMIC = "1000000000000000"      # 1e9 tokens at 6 decimals


def _mint_account(supply=SUPPLY_ATOMIC, decimals=6, freeze=None, mint_auth=None):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {"parsed": {"info": {"supply": supply, "decimals": decimals,
                                         "freezeAuthority": freeze, "mintAuthority": mint_auth}}}}


@pytest.fixture
def book():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana"], force=True)
    yield c
    c.close()


def _hook(monkeypatch, reads, *, basis, ds_mcap=40_000.0, read_result=None):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_BAND_MCAP_BASIS", basis)
    seen = {}

    def spy(conn, ev, contexts, **kw):
        seen.update(kw)
        seen["ev"] = ev
        return {"status": "filled"}

    monkeypatch.setattr(RR, "real_entry_hook", spy)
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)

    def mint_read_fn(mint):
        reads.append(mint)
        return read_result if read_result is not None else (_mint_account(), None)

    contexts = {"solana": {"mint_read_fn": mint_read_fn}}
    stats = (lambda mint, network_id=None: {"market_cap": ds_mcap, "price": 3.0e-05,
                                            "liquidity": 9000.0})
    return RR.make_real_hook(None, contexts, now_fn=lambda: "t", trade_size=10.0,
                             max_age_s=None, stats_fn=stats), seen


def _ev(**kw):
    return types.SimpleNamespace(side="buy", source_user_id="u", mint="FRESH",
                                 their_market_cap=None, their_price=2.0e-05, **kw)


def test_the_default_basis_keeps_dexscreener_and_adds_no_read(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="dexscreener")
    assert hook(book, _ev())["status"] == "filled"
    ctx = seen["classification_context"]
    assert ctx["effective_mcap"] == 40_000.0 and ctx["mcap_basis"] == "dexscreener"
    assert reads == [], "the default mode must not move or add the mint read"
    assert not hasattr(seen["ev"], "mint_account_read")


def test_onchain_prices_the_band_from_the_source_fill_and_the_mint_supply(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain")
    assert hook(book, _ev())["status"] == "filled"
    ctx = seen["classification_context"]
    assert ctx["effective_mcap"] == pytest.approx(2.0e-05 * 1e9)
    assert ctx["mcap_basis"] == "onchain_source_fill"
    assert ctx["market_stats"]["liquidity"] == 9000.0, "the other DexScreener fields stay"
    assert reads == ["FRESH"]
    assert seen["ev"].mint_account_read == (_mint_account(), None), "the safety gate reuses it"


def test_a_failed_mint_read_falls_back_to_dexscreener_and_is_not_reused(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain", read_result=(None, TS.READ_FAILED))
    hook(book, _ev())
    ctx = seen["classification_context"]
    assert ctx["effective_mcap"] == 40_000.0 and ctx["mcap_basis"] == "dexscreener_fallback"
    assert not hasattr(seen["ev"], "mint_account_read"), "a failure must be re-read, not replayed"


def test_no_source_price_means_no_read_and_dexscreener(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain")
    ev = _ev()
    ev.their_price = None
    hook(book, ev)
    assert reads == [] and seen["classification_context"]["mcap_basis"] == "dexscreener_fallback"


def test_the_event_mcap_and_history_stages_are_untouched(book, monkeypatch):
    reads = []
    hook, seen = _hook(monkeypatch, reads, basis="onchain")
    ev = _ev()
    ev.their_market_cap = 31_000.0
    hook(book, ev)
    assert seen["classification_context"]["effective_mcap"] == 31_000.0
    assert seen["classification_context"]["mcap_basis"] == "event"
    assert reads == []


def test_an_unusable_supply_is_no_onchain_mcap():
    assert RR._onchain_source_mcap(2e-5, (_mint_account(supply="0"), None)) is None
    assert RR._onchain_source_mcap(2e-5, (_mint_account(decimals=None), None)) is None
    assert RR._onchain_source_mcap(0.0, (_mint_account(), None)) is None
    assert RR._onchain_source_mcap(2e-5, (None, TS.READ_FAILED)) is None
    assert RR._onchain_source_mcap(2e-5, (_mint_account(supply="5000000", decimals=0), None)) \
        == pytest.approx(2e-5 * 5_000_000)


# --- the safety gate: the same verdict, from the same read -------------------------------------

@pytest.mark.parametrize("account", [
    _mint_account(), _mint_account(freeze="F" * 44), _mint_account(mint_auth="M" * 44),
    {"owner": "x", "data": {"parsed": {"info": "not a dict"}}}, {"owner": "x", "data": {}},
])
def test_the_verdict_from_a_pre_read_equals_a_fresh_check(monkeypatch, account):
    monkeypatch.setattr(TS, "read_mint_account", lambda mint, rpc, http: (account, None))
    fresh = TS.token_safety_ok("M", "rpc", max_transfer_fee_bps=300, allow_live_fee_authority=False)
    assert TS.token_safety_verdict(account, None, max_transfer_fee_bps=300,
                                   allow_live_fee_authority=False) == fresh


def test_a_failed_read_verdict_is_the_failure():
    assert TS.token_safety_verdict(None, TS.READ_FAILED, max_transfer_fee_bps=300,
                                   allow_live_fee_authority=False) == (False, TS.READ_FAILED)


def test_the_leg_hands_the_pre_read_to_the_safety_gate_only_when_one_exists():
    import ast
    import inspect
    from sniper.services import memecoin_real_leg as leg
    tree = ast.parse(inspect.getsource(leg))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "token_safety_fn"]
    assert any(k.arg == "pre_read" for c in calls for k in c.keywords)
    assert any(not c.keywords for c in calls), "the default call stays token_safety_fn(ev.mint)"


# --- the basis is recorded -------------------------------------------------------------------

def test_the_basis_lands_in_the_funded_classification_facts(book):
    from sniper.services.memecoin_market_state import classify_funded_entry
    kw = dict(network_id="solana", mint="MINT", signal_source="follow_activity",
              decision_at="2026-09-27T05:00:00Z", effective_mcap=20_000.0, assumed_micro=False,
              base_clip_usd=None, consensus_n_60m=None, market_stats={})
    assert classify_funded_entry(book, mcap_basis="onchain_source_fill", **kw).facts[
        "mcap_basis"] == "onchain_source_fill"
    assert "mcap_basis" not in classify_funded_entry(book, **kw).facts


@pytest.mark.parametrize("raw,want", [(None, "dexscreener"), ("onchain", "onchain"),
                                      (" OnChain ", "onchain"), ("gmgn", "dexscreener")])
def test_the_flag_parses_to_a_known_basis(monkeypatch, raw, want):
    assert cfg.solana_band_mcap_basis(raw) == want
