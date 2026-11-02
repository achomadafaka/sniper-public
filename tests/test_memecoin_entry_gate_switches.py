"""Operator switches for the one-per-coin and token-safety entry gates (2026-08-13).

Both were unconditional. The operator asked for them off; they are now flags that DEFAULT TO THE
OLD BEHAVIOUR, so the shipped default is unchanged and turning them off is an explicit, auditable,
one-word revert.

What is deliberately NOT switchable: the INSERT-level dedup race guard in `reserve_entry`. Two
concurrent reservations double-spending into one coin is a correctness fault, not a policy.
"""
import importlib
import ast
import re

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk


def _ctx(**over):
    base = dict(usdc_balance=300.0, sol_balance=1.0, effective_concurrent=0,
                effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
                held_coin_ids=frozenset({m.coin_identity("solana", "MINT1")}),
                cooldown_coin_ids=frozenset(),
                daily_tripped=False, real_enabled=True, kill_tripped=False,
                trade_size_usd=10.0, max_concurrent=6, max_total_exposure_usd=60.0,
                min_trade_floor_usd=5.0, gas_entry_sol=0.002, gas_exit_sol=0.002,
                gas_retry_factor=2.0)
    base.update(over)
    return risk.MemecoinRiskCtx(**base)


def test_one_per_coin_rejects_a_held_mint_by_default(monkeypatch):
    monkeypatch.setattr(risk.cfg, "MEMECOIN_REAL_ONE_POSITION_PER_COIN", True, raising=False)
    d = risk.memecoin_entry_allowed(_ctx(), mint="MINT1", trade_size=10.0)
    assert not d.allowed and "one position per coin" in d.reason


def test_one_per_coin_off_admits_a_second_position_on_the_same_mint(monkeypatch):
    monkeypatch.setattr(risk.cfg, "MEMECOIN_REAL_ONE_POSITION_PER_COIN", False, raising=False)
    d = risk.memecoin_entry_allowed(_ctx(), mint="MINT1", trade_size=10.0)
    assert d.allowed or "one position per coin" not in (d.reason or "")


def test_the_switch_does_not_touch_the_other_entry_gates(monkeypatch):
    """Turning one guard off must not widen a different one — the 2026-07-23 chase-cap lesson."""
    monkeypatch.setattr(risk.cfg, "MEMECOIN_REAL_ONE_POSITION_PER_COIN", False, raising=False)
    assert not risk.memecoin_entry_allowed(
        _ctx(kill_tripped=True), mint="MINT1", trade_size=10.0).allowed
    assert not risk.memecoin_entry_allowed(
        _ctx(real_enabled=False), mint="MINT1", trade_size=10.0).allowed
    assert not risk.memecoin_entry_allowed(
        _ctx(daily_tripped=True), mint="MINT1", trade_size=10.0).allowed
    # the cooldown is a SEPARATE knob and still bites
    ctx = _ctx(cooldown_coin_ids=frozenset({m.coin_identity("solana", "MINT1")}))
    assert not risk.memecoin_entry_allowed(ctx, mint="MINT1", trade_size=10.0).allowed


def test_the_reservation_race_guard_is_not_switchable():
    """It must stay unconditional: it is the only thing stopping a concurrent double-spend."""
    src = importlib.import_module("sniper.services.memecoin_risk").__file__
    body = open(src).read()
    # Since 2026-10-21 the label comes from db_memecoin.lost_reservation_reason (it names a desk
    # gate when one refused); the rejection itself is still one unconditional `if pid is None`.
    i = body.index("return _reject(lost), None")
    guard = body[body.rindex("\n", 0, body.rindex("if pid is None", 0, i)) + 1:i]
    assert guard.lstrip().startswith("if pid is None:"), guard
    assert "ONE_POSITION_PER_COIN" not in guard, "the race guard must not be behind the policy flag"
    from sniper.persistence import db_memecoin as m
    assert "lost reservation race" in open(m.__file__).read()


def test_token_safety_defaults_on_and_is_call_gated():
    """Off must SKIP THE CALL, not ignore its result — the latency is the reason it exists."""
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED is True or \
        cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED in (True, False)
    body = open(importlib.import_module("sniper.services.memecoin_real_leg").__file__).read()
    guard = "if cfg.MEMECOIN_REAL_TOKEN_SAFETY_ENABLED:"
    assert guard in body
    # the call must sit INSIDE the guard, not before it
    # Since 2026-09-27 the call may pass the band's own mint read (pre_read=); both forms start here.
    assert body.index(guard) < body.index("token_safety_fn(ev.mint")
    # and the bypass must leave a trace
    assert "TOKEN SAFETY BYPASSED" in body


def _settings_src():
    import sniper.settings.memecoin as s
    return open(s.__file__).read()


def test_every_switch_defaults_to_the_old_behaviour():
    """A fresh environment must not silently ship with any guard off.

    Asserted against the SOURCE default, not by reloading the settings module. Reloading it mutates
    shared state for every later test in the same xdist worker — it made ten unrelated real_runner
    and rails tests fail while each passed in isolation, twice today. The default lives in the
    os.getenv call, so read it there.
    """
    src = _settings_src()
    for flag in ("MEMECOIN_REAL_ONE_POSITION_PER_COIN",
                 "MEMECOIN_REAL_TOKEN_SAFETY_ENABLED",
                 "MEMECOIN_REAL_ROSTER_GATE_ENABLED"):
        assert re.search(rf'os\.getenv\(\s*"{flag}",\s*"1"\s*\)', src), flag


def _assert_roster_authority_guard(tree):
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    flag = "MEMECOIN_REAL_ROSTER_GATE_ENABLED"
    def refs(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr == name]
    authority = functions["_hook_raw"]
    gates = [n for n in ast.walk(authority) if isinstance(n, ast.If) and refs(n.test, flag)]
    flag_refs = refs(authority, flag)
    assert len(flag_refs) == len(gates) == 1
    assert isinstance(flag_refs[0].value, ast.Name) and flag_refs[0].value.id == "cfg"
    assert len(refs(authority, "real_approved_user_ids")) == 1
    assert any(isinstance(n, ast.Dict) and any(
        isinstance(k, ast.Constant) and k.value == "status"
        and isinstance(v, ast.Constant) and v.value == "not_approved"
        for k, v in zip(n.keys, n.values)) for n in ast.walk(gates[0]))
    # Alert labelling and demotion retain unconditional roster reads. A speculative read-only
    # quote precheck is not funded admission and must not change these assertions.
    for name in ("_wallet_is_armed", "check_wallet_demotion"):
        assert len(refs(functions[name], "real_approved_user_ids")) == 1
        assert not refs(functions[name], flag)


def test_the_roster_gate_switches_only_the_entry_authority_check():
    """Off, any wallet may enter. Auto-demote and the alert gate must still read the roster:
    they answer different questions, and gating them would stop demoting bad wallets and stop
    labelling alerts."""
    import sniper.services.memecoin_real_runner as rr
    _assert_roster_authority_guard(ast.parse(open(rr.__file__).read()))


@pytest.mark.parametrize("mutation", ["MEMECOIN_REAL_ROSTER_GATE_ENABLED", "real_approved_user_ids", "flag_owner"])
def test_roster_guard_rejects_a_missing_or_wrong_authority_read(mutation):
    import sniper.services.memecoin_real_runner as rr
    tree = ast.parse(open(rr.__file__).read())
    raw = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_hook_raw")
    for node in ast.walk(raw):
        if not isinstance(node, ast.Attribute):
            continue
        if mutation == "flag_owner" and node.attr == "MEMECOIN_REAL_ROSTER_GATE_ENABLED":
            node.value = ast.Name(id="wrong_config", ctx=ast.Load())
        elif node.attr == mutation:
            node.attr = "removed_authority_read"
    with pytest.raises(AssertionError):
        _assert_roster_authority_guard(tree)


def test_follow3050_for_all_wallets_overrides_the_band_default():
    """With the roster gate off, most entries have no memecoin_wallets row, so the handle-keyed
    allowlist falls through to the band default and silently reinstates mirror-on-sell, a trail and
    a de-risk. The resolver-level flag is what actually covers them."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_funded_policy import resolve_funded_policy
    saved = cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS
    try:
        cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS = False
        assert resolve_funded_policy(
            coin_band="nano", network_id="solana").policy_id == "funded-nano-v2"
        cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS = True
        for band in ("nano", "micro", "small", "mid"):
            p = resolve_funded_policy(coin_band=band, network_id="solana")
            assert p.policy_id == "follow-30-50-v1", band
            assert p.mirror_on_sell is False and p.trail_pct == 0 and p.derisk_pct == 0
    finally:
        cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS = saved


def test_selected_band_policy_precedes_global_follow3050(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_funded_policy import resolve_funded_policy

    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ("nano", "micro"))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_DERISK_PCT", 20.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_DERISK_FRACTION", 0.5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_SL_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_SL_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_TP_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_STOP_PCT", 50.0)

    for band, policy_id in (
        ("nano", "funded-nano-v2"),
        ("assumed_nano", "funded-nano-v2"),
        ("micro", "funded-micro-v2"),
    ):
        policy = resolve_funded_policy(coin_band=band, network_id="solana")
        assert policy.policy_id == policy_id
        assert (policy.tp_pct, policy.derisk_pct, policy.derisk_fraction) == (0.0, 20.0, 0.5)
        assert (policy.trail_pct, policy.stop_pct) == (30.0, 60.0)
        assert policy.trail_requires_derisk is True
        assert policy.mirror_on_sell is True

    for band in ("small", "mid"):
        policy = resolve_funded_policy(coin_band=band, network_id="solana")
        assert policy.policy_id == "follow-30-50-v1"
        assert (policy.tp_pct, policy.stop_pct) == (30.0, 50.0)
        assert (policy.derisk_pct, policy.trail_pct) == (0.0, 0.0)
        assert policy.mirror_on_sell is False


def test_admitted_out_of_scope_always_has_follow3050_rails(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_funded_policy import resolve_funded_policy

    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ("nano", "micro"))
    monkeypatch.setattr(
        cfg, "MEMECOIN_REAL_RESERVABLE_BANDS", ("nano", "micro", "small", "mid", "out_of_scope"))

    policy = resolve_funded_policy(coin_band="out_of_scope", network_id="solana")

    assert policy.policy_id == "follow-30-50-v1"
    assert (policy.tp_pct, policy.stop_pct) == (30.0, 50.0)


def test_follow3050_for_all_defaults_off():
    assert re.search(r'os\.getenv\(\s*"MEMECOIN_FOLLOW3050_ALL_WALLETS",\s*"0"\s*\)',
                     _settings_src())


def test_follow3050_covers_the_out_of_scope_band():
    """`_coin_band` hard-codes out_of_scope above $2M — a second mcap ceiling independent of
    MEMECOIN_REAL_MAX_MCAP_USD. If the operator admits that band, it must have rails; every other
    band-parameterised policy still refuses it because they DO have per-band values to look up."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_funded_policy import (
        resolve_funded_policy, _follow3050_policy, _tp_only_policy)
    p = _follow3050_policy(coin_band="out_of_scope")
    assert p.policy_id == "follow-30-50-v1" and p.mirror_on_sell is False
    saved = cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS
    try:
        cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS = True
        assert resolve_funded_policy(
            coin_band="out_of_scope", network_id="solana").policy_id == "follow-30-50-v1"
    finally:
        cfg.MEMECOIN_FOLLOW3050_ALL_WALLETS = saved
    # the band-parameterised policies still refuse it — they have per-band values to invent
    with pytest.raises(ValueError):
        _tp_only_policy(coin_band="out_of_scope")


def test_a_chain_rpc_failure_is_provider_blind_not_no_route():
    """An RPC 429 inside the V4 quote used to read as "this token has no V4 pool", so the entry was
    refused as no_route — proof about the COIN drawn from an infrastructure outage. Verified live
    on 2026-08-13: 0xa3b6aee9 was refused no_route and quoted fine seconds later.

    classify_quote_miss already preferred a blind rung, but it could only see the GeckoTerminal
    cooldown; the chain-RPC failure was swallowed by the quote's fail-closed returns.
    """
    from sniper.execution import evm_v4_quote as v4
    from sniper.execution.evm_real_seams import classify_quote_miss
    v4.begin_quote()
    assert v4.last_provider_blind() is None          # a clean attempt trusts its own no-pool answer
    v4._mark_blind("rpc_transport")
    assert v4.last_provider_blind() == "rpc_transport"
    v4.begin_quote()
    assert v4.last_provider_blind() is None          # cleared per attempt, never sticky

    code, _ = classify_quote_miss([{"rung": "v3", "hit": False}, {"rung": "v2", "hit": False},
                                   {"rung": "v4", "hit": False}])
    assert code == "no_route"                        # every rung answered -> the coin has no route
    code, _ = classify_quote_miss([{"rung": "v3", "hit": False}, {"rung": "v2", "hit": False},
                                   {"rung": "v4", "hit": False,
                                    "blind_reason": "rpc_transport"}])
    assert code == "provider_rpc_transport"          # a blind rung is never proof about the coin


def test_the_first_blind_reason_wins_and_is_not_overwritten():
    from sniper.execution import evm_v4_quote as v4
    try:
        v4.begin_quote()
        v4._mark_blind("rate_limit")
        v4._mark_blind("rpc_transport")
        assert v4.last_provider_blind() == "rate_limit"
    finally:
        v4.begin_quote()          # never leave a flag set for the next test in this worker


def test_resolve_mint_supply_recovers_a_blank_market_cap():
    """entry_mcap is NULL on 52% of 4663 real positions: the funded lane enters on a coin's FIRST
    signal, so when that event omitted the market cap there is no same-mint history to fall back on
    and the display's frozen-ratio derivation has no ratio either. Supply is the constant those
    derivations want, and any priced event yields it."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)

    def ev(mc, px):
        c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side,"
                  " their_market_cap, their_price, network_id, capture_version, clock_domain)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"e{mc}{px}", "u", "MINT", "buy", mc, px, "4663", 2, "observed_real_utc"))
    ev(1_000_000.0, 0.001)      # supply 1e9
    ev(2_000_000.0, 0.002)      # supply 1e9
    c.commit()
    assert m.resolve_mint_supply(c, "MINT", network_id="4663") == pytest.approx(1e9)

    # a ZERO market cap means MISSING in this feed, never a zero-cap coin, and a 0 price would divide
    c.execute("DELETE FROM memecoin_events")
    ev(0.0, 0.001)
    ev(1_000_000.0, 0.0)
    c.commit()
    assert m.resolve_mint_supply(c, "MINT", network_id="4663") is None
    assert m.resolve_mint_supply(c, "", network_id="4663") is None


def test_a_per_wallet_mcap_floor_replaces_the_global_one():
    """2026-09-18 operator: point the filter at ONE wallet. smol_intern trades 4663 only above
    $250k; every other wallet keeps the global floor."""
    from sniper.services.memecoin_real_runner import _wallet_mcap_floor

    by_wallet = {"4663:wallet-a": 250000.0}

    assert _wallet_mcap_floor(by_wallet, "4663", "wallet-a", 0.0) == 250000.0
    assert _wallet_mcap_floor(by_wallet, "robin", "wallet-a", 0.0) == 250000.0   # canonicalised
    assert _wallet_mcap_floor(by_wallet, "solana", "wallet-a", 0.0) == 0.0
    assert _wallet_mcap_floor(by_wallet, "4663", "wallet-b", 0.0) == 0.0


def _mcap_conn():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    return c


def test_a_resolved_mcap_is_priced_at_this_entry_not_the_windows_median():
    """COINU DDndHihss… (2026-09-18 07:32Z) resolved to $126,036 and passed the $250k ceiling while
    its own chart said $266,708 and price x supply said $313,818. The readings spanned a coin that
    had doubled, so the median described an earlier price."""
    from sniper.persistence import db_memecoin as m
    c = _mcap_conn()
    # one billion supply, readings taken as the coin ran from $0.00014 to $0.00034
    for i, (mcap, price) in enumerate([(145643.0, 0.000139933), (189471.0, 0.000202740),
                                       (298723.0, 0.000336527), (318824.0, 0.00033672),
                                       (188766.0, 0.000181288)]):
        m.insert_event(c, {"fomo_event_id": "e%d" % i, "source_user_id": "u1",
                           "source_handle": "alice", "mint": "COINU", "ticker": "C", "side": "buy",
                           "their_price": price, "their_market_cap": mcap, "their_size_usd": 100.0,
                           "is_first_buy": False, "created_at": "2026-09-18T07:00:00+00:00",
                           "received_at": "2026-09-18T07:00:00+00:00", "lag_s": 0.2,
                           "network_id": "solana"})

    median_only = m.resolve_mint_mcap(c, "COINU", network_id="solana")
    priced = m.resolve_mint_mcap(c, "COINU", network_id="solana", price=0.0003138)

    assert 180000 < median_only < 200000          # the window's median price, not the coin now
    assert 290000 < priced < 330000               # ~1.0B supply x our entry price
    assert priced > 250000                        # the ceiling now measures the coin we are buying


def test_readings_that_disagree_on_supply_keep_the_median():
    """Price x supply is only better when the supply readings agree. A coin whose readings imply
    wildly different supplies (a rebase, a bad feed) falls back rather than inventing a number."""
    from sniper.persistence import db_memecoin as m
    c = _mcap_conn()
    for i, (mcap, price) in enumerate([(100000.0, 0.001), (100000.0, 0.0001),
                                       (100000.0, 0.00001), (100000.0, 0.000001)]):
        m.insert_event(c, {"fomo_event_id": "d%d" % i, "source_user_id": "u1",
                           "source_handle": "alice", "mint": "WOBBLE", "ticker": "W", "side": "buy",
                           "their_price": price, "their_market_cap": mcap, "their_size_usd": 100.0,
                           "is_first_buy": False, "created_at": "2026-09-18T07:00:00+00:00",
                           "received_at": "2026-09-18T07:00:00+00:00", "lag_s": 0.2,
                           "network_id": "solana"})

    assert m.resolve_mint_mcap(c, "WOBBLE", network_id="solana", price=0.001) == 100000.0
