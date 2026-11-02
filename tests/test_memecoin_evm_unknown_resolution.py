"""Resolve `evm-unknown` positions from the feed's own event history.

A Robinhood 4663 mint delivered without a networkId can't be attributed by DexScreener
(no pair), so the position opens 'evm-unknown' -> never priced -> dies via the 24h guard
instead of its arm's exit policy. But the feed itself carries the concrete chain on other
events for the same mint. Three layers use one cheap events->chain lookup:
  1. entry-time  (copy_trader._open_arm) — don't create evm-unknown when a prior event knew the chain
  2. sweep self-heal (bracket_pass)      — heal a position once a concrete event lands
  3. one-time backfill                    — same bulk heal, run once on the live DB
"""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


def _ev(c, fomo_id, mint, network_id):
    c.execute("INSERT INTO memecoin_events(fomo_event_id, mint, network_id) VALUES (?,?,?)",
              (fomo_id, mint, network_id))
    c.commit()


class _Ev:
    def __init__(self, mint, network_id):
        self.side = "buy"; self.source_user_id = "u1"; self.source_handle = "alice"
        self.mint = mint; self.ticker = "T"; self.their_price = 0.005
        self.their_market_cap = 1e6; self.their_size_usd = 3000.0; self.is_first_buy = True
        self.fomo_event_id = "trigger"; self.created_at = "t"; self.received_at = "t"
        self.lag_s = 0.1; self.signal_source = "global_feed"; self.network_id = network_id


# --- the shared lookup ---

def test_events_mint_is_indexed():
    # resolve_network_from_events filters memecoin_events by mint every ~15s sweep — an unindexed
    # scan of the never-pruned firehose would also delay the real exit sweep that runs right after.
    c = _c()
    idx = {r["name"] for r in c.execute("PRAGMA index_list('memecoin_events')")}
    assert "idx_mc_events_mint" in idx


def test_resolve_network_from_events_unambiguous():
    c = _c()
    _ev(c, "e1", "0xMINT", "4663")
    _ev(c, "e2", "0xMINT", "4663")            # repeated concrete chain is still unambiguous
    assert m.resolve_network_from_events(c, "0xMINT") == "4663"


def test_resolve_network_from_events_refuses_ambiguous_and_empty():
    c = _c()
    _ev(c, "a1", "0xAMB", "4663"); _ev(c, "a2", "0xAMB", "8453")   # conflicting -> refuse
    assert m.resolve_network_from_events(c, "0xAMB") is None
    _ev(c, "u1", "0xUNK", "evm-unknown"); _ev(c, "u2", "0xUNK", "")  # only non-concrete
    assert m.resolve_network_from_events(c, "0xUNK") is None
    assert m.resolve_network_from_events(c, "0xNONE") is None        # no events at all


def test_concrete_event_networks_exposes_ambiguity_without_losing_indexed_lookup():
    c = _c()
    _ev(c, "a1", "0xAMB", "8453")
    _ev(c, "a2", "0xAMB", "4663")
    _ev(c, "a3", "0xAMB", "8453")
    _ev(c, "u1", "0xUNK", "evm-unknown")

    assert m.concrete_event_networks(c, "0xAMB") == ("4663", "8453")
    assert m.concrete_event_networks(c, "0xUNK") == ()


# --- Fix 1: entry-time resolution ---

def test_entry_resolves_evm_unknown_from_prior_event():
    c = _c()
    _ev(c, "prior", "0xCAFE", "4663")          # the feed already attributed this mint on another event
    ct.copy_event_to_arms(c, _Ev("0xCAFE", "evm-unknown"), 100.0, "t0",
                          arms=("control",), fill_fn=lambda *a, **k: (0.005, "jupiter"))
    pos = m.get_open_position(c, "u1", "0xCAFE", "control", network_id="4663")
    assert pos is not None and pos["network_id"] == "4663"


def test_entry_leaves_truly_unresolvable_as_evm_unknown():
    c = _c()                                   # no concrete event for this mint anywhere
    ct.copy_event_to_arms(c, _Ev("0xDEAD", "evm-unknown"), 100.0, "t0",
                          arms=("control",), fill_fn=lambda *a, **k: (0.005, "jupiter"))
    pos = m.get_open_position(c, "u1", "0xDEAD", "control")
    assert pos is not None and pos["network_id"] == "evm-unknown"


# --- Fix 2: sweep self-heal + Fix 3: bulk backfill (same helper) ---

def _open_paper(c, mint, network_id, strategy="control"):
    return m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                           entry_price=0.2, entry_notional=100.0, entry_fill_source="chain",
                           entry_lag_s=0, their_size_usd=0, opened_at="2026-07-15T00:00:00+00:00",
                           strategy=strategy, execution_mode="paper", network_id=network_id)


def test_bulk_heal_resolves_only_the_unambiguous():
    c = _c()
    good = _open_paper(c, "0xGOOD", "evm-unknown"); _ev(c, "g", "0xGOOD", "4663")
    amb = _open_paper(c, "0xAMB", "evm-unknown"); _ev(c, "a1", "0xAMB", "4663"); _ev(c, "a2", "0xAMB", "8453")
    none = _open_paper(c, "0xNONE", "evm-unknown")           # no concrete event
    sol = _open_paper(c, "SoLmint", "solana")                # non-0x, untouched
    n = m.heal_open_evm_unknown_from_events(c)
    assert n == 1
    assert m.get_position(c, good)["network_id"] == "4663"
    assert m.get_position(c, amb)["network_id"] == "evm-unknown"     # ambiguous -> refused
    assert m.get_position(c, none)["network_id"] == "evm-unknown"    # unresolvable -> left
    assert m.get_position(c, sol)["network_id"] == "solana"          # untouched


def test_sweep_self_heals_then_prices_on_the_corrected_chain(monkeypatch):
    from sniper.services import memecoin_runner as R
    c = _c()
    pid = _open_paper(c, "0xEvmMint", "evm-unknown")
    _ev(c, "later", "0xEvmMint", "4663")                    # feed attributed the chain after we opened
    captured = {}
    monkeypatch.setattr("sniper.services.memecoin_exits.sweep_exits",
                        lambda conn, price_map, now_iso, **kw: captured.update(price_map) or [])
    R.bracket_pass(c, price_fn=lambda mints: {("4663", "0xEvmMint"): {"price": 0.25, "liquidity": 1e6}},
                   now_fn=lambda: "2026-07-15T00:01:00+00:00")
    assert m.get_position(c, pid)["network_id"] == "4663"           # healed before keys were built
    assert ("4663", "0xEvmMint") in captured                        # priced on the correct chain


def test_sweep_heal_collision_is_stable_and_does_not_gate_funded_exits(monkeypatch, caplog):
    from sniper.services import memecoin_real_runner as RR
    from sniper.services import memecoin_runner as R

    c = _c()
    unknown = _open_paper(c, "0xCOLLIDE", "evm-unknown", strategy="alpha_b")
    concrete = _open_paper(c, "0xCOLLIDE", "4663", strategy="alpha_b")
    _ev(c, "resolved", "0xCOLLIDE", "4663")

    healed = m.heal_open_evm_unknown_from_events(c)
    assert healed == 0
    assert healed.collisions == ({
        "position_id": unknown,
        "conflicting_position_id": concrete,
        "network_id": "4663",
        "mint": "0xCOLLIDE",
        "reason": "open_coin_identity_collision",
    },)
    assert m.get_position(c, unknown)["network_id"] == "evm-unknown"
    assert m.get_position(c, concrete)["network_id"] == "4663"

    monkeypatch.setattr("sniper.services.memecoin_exits.sweep_exits", lambda *args, **kwargs: [])
    funded_sweeps = []
    monkeypatch.setattr(RR, "real_exit_sweep", lambda *args, **kwargs: funded_sweeps.append(1))
    for _ in range(2):
        R.bracket_pass(c, price_fn=lambda _keys: {},
                       now_fn=lambda: "2026-07-15T00:01:00+00:00")
        RR.real_exit_sweep(c, {})

    assert funded_sweeps == [1, 1]
    assert "open_coin_identity_collision" in caplog.text
    assert m.get_position(c, unknown)["network_id"] == "evm-unknown"
    assert m.get_position(c, concrete)["network_id"] == "4663"


def test_bulk_heal_continues_after_a_collision():
    c = _c()
    unknown = _open_paper(c, "0xCOLLIDE", "evm-unknown", strategy="alpha_b")
    _open_paper(c, "0xCOLLIDE", "4663", strategy="alpha_b")
    healable = _open_paper(c, "0xHEAL", "evm-unknown", strategy="alpha_b")
    _ev(c, "collision", "0xCOLLIDE", "4663")
    _ev(c, "healable", "0xHEAL", "8453")

    result = m.heal_open_evm_unknown_from_events(c)

    assert result == 1
    assert result.collisions[0]["position_id"] == unknown
    assert m.get_position(c, healable)["network_id"] == "8453"
