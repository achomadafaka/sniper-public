"""Real-entry mcap gate: resolve true mcap from same-mint feed history before fail-closed-skipping.

Regression for the 2026-07-11 bug where ~5% of approved buys omit their_market_cap and were
skipped as if too-big, silently dropping small-cap Solana winners (jester +68, PrimeT +39)."""
import sqlite3
import types

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as RR


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ev(eid, mint, mc, uid="other", network_id="solana"):
    return {"fomo_event_id": eid, "source_user_id": uid, "source_handle": "o", "mint": mint,
            "ticker": "T", "side": "buy", "their_price": 1.0, "their_market_cap": mc,
            "their_size_usd": 1.0, "is_first_buy": 0, "created_at": eid, "received_at": eid,
            "lag_s": 0.1, "network_id": network_id}


def _seed_readings(c, mint, mcaps):
    for i, mc in enumerate(mcaps):
        m.insert_event(c, _ev(f"{mint}-{i}", mint, mc))


# ── resolver ────────────────────────────────────────────────────────────────
def test_resolve_mint_mcap_median_of_nonzero():
    c = _conn()
    _seed_readings(c, "MINT", [1_000_000, 0, 2_000_000, 3_000_000])   # 0 ignored
    assert m.resolve_mint_mcap(c, "MINT") == 2_000_000                # median of [1M,2M,3M]
    assert m.resolve_mint_mcap(c, "NOPE") is None
    assert m.resolve_mint_mcap(c, None) is None


# ── pure gate decision ───────────────────────────────────────────────────────
def test_mcap_entry_decision_branches():
    C = 30_000_000
    assert RR.mcap_entry_decision(1_000_000, None, C)[0] is True      # small event mcap -> fire
    assert RR.mcap_entry_decision(50_000_000, None, C)[0] is False    # big event mcap -> skip
    assert RR.mcap_entry_decision(None, 900_000, C)[0] is True        # FIX: missing but resolved small
    assert RR.mcap_entry_decision(None, 50_000_000, C) == (False, 50_000_000, "over_ceiling")
    assert RR.mcap_entry_decision(None, None, C) == (False, None, "mcap_unresolved")
    assert RR.mcap_entry_decision(None, None, 0)[0] is True           # ceiling disabled


# ── hook integration ─────────────────────────────────────────────────────────
# venue contexts keyed by resolved chain; the hook's guard only checks MEMBERSHIP (unroutable
# chains are refused before the mcap/arming gates), so placeholder values are fine here.
_VENUES = {"solana": object(), "4663": object(), "8453": object()}


def _armed_hook(c, monkeypatch, status="filled"):
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"],
                               force=True)
    assert "u" in m.real_approved_user_ids(c, network_id="solana")
    monkeypatch.setattr(RR, "real_entry_hook", lambda *a, **k: {"status": status})
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    return RR.make_real_hook(None, _VENUES, now_fn=lambda: "t", trade_size=10.0, max_age_s=None)


def test_hook_FIRES_on_missing_mcap_when_resolvable_small(monkeypatch):
    c = _conn()
    _seed_readings(c, "SMALL", [600_000, 700_000, 650_000])           # priced small elsewhere
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint="SMALL")
    assert hook(c, ev)["status"] == "filled"                         # was skipped_mcap before the fix


def test_hook_resolves_missing_mcap_on_the_signal_chain(monkeypatch):
    c = _conn()
    m.insert_event(c, _ev("base", "0xSame", 2_000_000, network_id="8453"))
    m.insert_event(c, _ev("robin", "0xSame", 80_000_000, network_id="4663"))
    hook = _armed_hook(c, monkeypatch)
    # The EVM gate prices history at this buy (2026-10-07), so the event carries the source price.
    ev = types.SimpleNamespace(
        side="buy", source_user_id="u", their_market_cap=None, mint="0xSame",
        network_id="8453", their_price=1.0,
    )

    assert hook(c, ev)["status"] == "filled"


def test_hook_still_skips_missing_mcap_when_resolvable_big(monkeypatch):
    c = _conn()
    _seed_readings(c, "BIG", [200_000_000, 210_000_000])
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint="BIG")
    res = hook(c, ev)
    # The HOOK renders the numbers (2026-09-09); the GATE still returns the bare identifier,
    # which line 46 pins. `over_ceiling` was the largest armed-wallet refusal and carried no
    # mcap and no ceiling, so a $343k coin and a $50M coin were indistinguishable.
    assert res["status"] == "skipped_mcap"
    assert res["reason"].endswith("(over_ceiling)") and "over ceiling $" in res["reason"]


def test_hook_assumes_micro_on_solana_when_triple_unresolvable(monkeypatch):
    """2026-07-16: event mcap, feed history, AND live DexScreener all empty on Solana = a
    minutes-old micro by construction (big caps always have a DS pair or feed history).
    Fail-closed here refused ~16 armed buys/day — all in the sub-$1M pocket (58% hit20),
    the exact 07-11 bug class one resolver layer deeper. Bounded by the micro-tier impact cap."""
    from sniper.settings import memecoin as cfg
    c = _conn()
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"],
                               force=True)
    seen = {}

    def _spy(conn, ev, contexts, **kw):
        seen.update(kw)
        return {"status": "filled"}

    monkeypatch.setattr(RR, "real_entry_hook", _spy)
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    hook = RR.make_real_hook(None, _VENUES, now_fn=lambda: "t", trade_size=10.0, max_age_s=None)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint="GHOST")
    assert hook(c, ev)["status"] == "filled"
    # impact cap must be the MICRO tier (assumption is micro), not the flat unknown-mcap base
    assert seen.get("impact_cap_pct") == cfg.entry_impact_cap_for_mcap(1.0)


def test_hook_unresolvable_still_skips_when_assume_micro_off(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MCAP_ASSUME_MICRO", False)
    c = _conn()
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None, mint="GHOST")
    res = hook(c, ev)
    assert res["status"] == "skipped_mcap" and res["reason"] == "mcap_unresolved"


def test_hook_unresolvable_still_skips_on_evm_chains(monkeypatch):
    """assume-micro needs an exhausted established-coin index: Solana has DexScreener, blind
    chains (4663) have GT+totalSupply. DS-covered EVM chains (8453) keep the conservative skip."""
    c = _conn()
    m.insert_event(c, _ev("seed", "0xGhost", 0, network_id="8453"))   # identity resolves; mcap doesn't
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None,
                               mint="0xGhost", network_id="8453")
    res = hook(c, ev)
    assert res["status"] == "skipped_mcap" and res["reason"] == "mcap_unresolved"


def _armed_blind_hook(c, monkeypatch, blind_mcap_fn):
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"],
                               force=True)
    monkeypatch.setattr(RR, "real_entry_hook", lambda *a, **k: {"status": "filled"})
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    return RR.make_real_hook(None, _VENUES, now_fn=lambda: "t", trade_size=10.0,
                             max_age_s=None, blind_mcap_fn=blind_mcap_fn)


def test_hook_resolves_blind_chain_mcap_via_gt_lookup(monkeypatch):
    """4663 has no DexScreener — its live-mcap source is GT price x on-chain totalSupply.
    A GT-resolved BIG cap must still be refused (honest resolution beats assumption)."""
    c = _conn()
    m.insert_event(c, _ev("seed", "0xBig", 0, network_id="4663"))
    hook = _armed_blind_hook(c, monkeypatch, lambda chain, mint: 45_000_000.0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None,
                               mint="0xBig", network_id="4663")
    res = hook(c, ev)
    # The HOOK renders the numbers (2026-09-09); the GATE still returns the bare identifier,
    # which line 46 pins. `over_ceiling` was the largest armed-wallet refusal and carried no
    # mcap and no ceiling, so a $343k coin and a $50M coin were indistinguishable.
    assert res["status"] == "skipped_mcap"
    assert res["reason"].endswith("(over_ceiling)") and "over ceiling $" in res["reason"]


def test_hook_assumes_micro_on_blind_chain_when_gt_also_empty(monkeypatch):
    """The capodon 0xce57… case (2026-07-16 19:55): fast lane caught a 14.8s 4663 buy, funded
    gate refused mcap_unresolved. With feed history AND the GT/totalSupply lookup both empty,
    the coin is minutes-old — proceed as micro, same construction argument as Solana."""
    c = _conn()
    m.insert_event(c, _ev("seed", "0xFresh", 0, network_id="4663"))
    hook = _armed_blind_hook(c, monkeypatch, lambda chain, mint: None)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=None,
                               mint="0xFresh", network_id="4663")
    assert hook(c, ev)["status"] == "filled"


def test_hook_still_skips_present_big_mcap(monkeypatch):
    c = _conn()
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u",
                               their_market_cap=277_000_000, mint="ANSEM")
    assert hook(c, ev)["status"] == "skipped_mcap"                   # big-cap policy unchanged


def test_hook_alerts_operator_when_mcap_gate_skips(monkeypatch):
    c = _conn()
    fired = []
    hook = _armed_hook(c, monkeypatch)
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: fired.append((a, k)))
    ev = types.SimpleNamespace(
        side="buy", source_user_id="u", source_handle="alice", ticker="ANSEM",
        their_market_cap=207_000_000, mint="ANSEM",
    )
    result = hook(c, ev)
    assert result["status"] == "skipped_mcap"
    assert len(fired) == 1
    assert fired[0][0][0] == "ANSEM"
    assert fired[0][0][2]["mcap"] == 207_000_000
    assert fired[0][1]["handle"] == "alice" and fired[0][1]["ticker"] == "ANSEM"
    # the wallet is armed for this chain, so the refusal still reaches the operator
    assert fired[0][1]["armed"] is True


def test_no_ceiling_still_resolves_mcap():
    # BRAINROT 2026-08-19: with the ceiling off, the gate must still hand downstream banding
    # the resolved mcap — returning the event's empty field banded fresh coins out_of_scope.
    assert RR.mcap_entry_decision(None, 9_300, 0) == (True, 9_300, "no_ceiling")
    assert RR.mcap_entry_decision(150_000, 9_300, 0) == (True, 150_000, "no_ceiling")
    assert RR.mcap_entry_decision(None, None, 0) == (True, None, "no_ceiling")


# ── per-wallet ceiling (2026-09-12, operator "exempt frankdegods") ───────────


def _ceiling_hook(c, monkeypatch, by_wallet):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", by_wallet)
    return _armed_hook(c, monkeypatch)


def _big(network_id="solana", mc=2_000_000):
    mint = "MID" if network_id == "solana" else "0x" + "ab" * 20
    return types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=mc,
                                 mint=mint, network_id=network_id)


def test_wallet_override_admits_its_own_chain_above_the_global_ceiling(monkeypatch):
    c = _conn()
    hook = _ceiling_hook(c, monkeypatch, {"solana:u": 6_000_000.0})
    assert hook(c, _big())["status"] == "filled"


def test_wallet_override_is_still_a_ceiling(monkeypatch):
    c = _conn()
    hook = _ceiling_hook(c, monkeypatch, {"solana:u": 6_000_000.0})
    res = hook(c, _big(mc=9_000_000))
    assert res["status"] == "skipped_mcap"
    assert "over ceiling $6,000,000" in res["reason"], "the refusal must print the bar that applied"


def test_wallet_override_does_not_reach_another_chain(monkeypatch):
    c = _conn()
    hook = _ceiling_hook(c, monkeypatch, {"solana:u": 6_000_000.0})
    assert hook(c, _big(network_id="8453"))["status"] == "skipped_mcap"


def test_wallet_override_does_not_reach_another_wallet(monkeypatch):
    c = _conn()
    hook = _ceiling_hook(c, monkeypatch, {"solana:someone-else": 6_000_000.0})
    assert hook(c, _big())["status"] == "skipped_mcap"


def test_wallet_override_parses_from_env_text():
    from sniper.settings import memecoin as cfg
    got = cfg._parse_source_buy_band_ratios("solana:AEFE-1=6000000, bad, 56:x=nan")
    assert got["solana:aefe-1"] == 6_000_000.0
    assert "56:x" not in got


def test_hook_prices_evm_mcap_from_supply_and_source_price_when_every_source_is_empty(monkeypatch):
    """C3S (BSC, 2026-09-17 11:21Z): feed mcap 0, no history, and the only DexScreener pair is
    quoted in another memecoin with no price, so the entry refused mcap_unresolved. The coin's
    on-chain supply x the trader's own fill price resolves it; the ceiling then applies as usual."""
    seen = []
    monkeypatch.setattr(RR, "_default_evm_source_mcap_fn",
                        lambda: (lambda chain, mint, price: seen.append((chain, mint, price)) or 90_000.0))
    c = _conn()
    m.insert_event(c, _ev("seed", "0xC3s", 0, network_id="8453"))
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=0.0,
                               their_price=3.047e-05, mint="0xC3s", network_id="8453")
    res = hook(c, ev)
    assert seen == [("8453", "0xC3s", 3.047e-05)]
    assert res["status"] == "filled"


def test_evm_source_priced_mcap_still_refuses_over_the_ceiling(monkeypatch):
    monkeypatch.setattr(RR, "_default_evm_source_mcap_fn",
                        lambda: (lambda chain, mint, price: 40_000_000.0))
    c = _conn()
    m.insert_event(c, _ev("seed", "0xBigEvm", 0, network_id="8453"))
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=0.0,
                               their_price=1.0, mint="0xBigEvm", network_id="8453")
    res = hook(c, ev)
    assert res["status"] == "skipped_mcap" and res["reason"].endswith("(over_ceiling)")


def test_evm_source_priced_mcap_needs_a_source_price(monkeypatch):
    called = []
    monkeypatch.setattr(RR, "_default_evm_source_mcap_fn",
                        lambda: (lambda chain, mint, price: called.append(1) or 90_000.0))
    c = _conn()
    m.insert_event(c, _ev("seed", "0xNoPx", 0, network_id="8453"))
    hook = _armed_hook(c, monkeypatch)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=0.0,
                               their_price=None, mint="0xNoPx", network_id="8453")
    res = hook(c, ev)
    assert called == [] and res["reason"] == "mcap_unresolved"
