"""The V4 route is an ordered list of legs admitted by its MEASURED cost (2026-09-05).

Spec: docs/superpowers/specs/2026-09-05-memecoin-v4-nleg-measured-route-design.md. A launchpad coin
on chain 4663 often has one real pool paired with a tokenized stock, and that stock has no
fixed-fee ETH pool, so two legs cannot reach it. Measured that day: PH costs -12.7% through two
legs and -3.4% through three (ETH -> USDG -> LLY -> PH).

Every test here is offline. No live RPC, no provider.
"""
from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_multihop_tx as TX

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
STOCK = "0x" + "11" * 20
COIN = "0x" + "22" * 20
DYNAMIC = 0x800000


def _key(c0, c1, fee, ts=200, hooks=NATIVE):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": ts, "hooks": hooks}


def _leg(currency, key):
    return {"currency": currency, "key": key}


CASH_STOCK = _key(NATIVE, STOCK, 5000)
CASH_USDG = _key(NATIVE, USDG, 460)
USDG_STOCK = _key(USDG, STOCK, DYNAMIC)
STOCK_COIN = _key(STOCK, COIN, 0)

THREE_LEG = {
    "bridge": USDG, "bridge_key": CASH_USDG, "coin_key": STOCK_COIN,
    "legs_path": [_leg(USDG, CASH_USDG), _leg(STOCK, USDG_STOCK), _leg(COIN, STOCK_COIN)],
}
TWO_LEG_LEGACY = {"bridge": STOCK, "bridge_key": CASH_STOCK, "coin_key": STOCK_COIN}


# 1. a three-leg route becomes three PathKeys in order
def test_a_three_leg_route_builds_three_path_keys_in_order():
    bridge, path = TX._legs_from_route(THREE_LEG, NATIVE, COIN)

    assert bridge == USDG
    assert [p[0] for p in path] == [USDG, STOCK, COIN]
    assert [p[1] for p in path] == [460, DYNAMIC, 0]


# 2. the exit builder reverses a stored three-leg route
def test_the_exit_reverses_a_stored_three_leg_route():
    _bridge, entry = TX._legs_from_route(THREE_LEG, NATIVE, COIN)
    n = len(entry)
    exit_path = [(entry[i - 1][0] if i else NATIVE, entry[i][1], entry[i][2], entry[i][3])
                 for i in range(n - 1, -1, -1)]

    # Each PathKey names the OUTPUT of its hop, so selling the coin lands STOCK, USDG, then cash.
    assert [p[0] for p in exit_path] == [STOCK, USDG, NATIVE]
    assert [p[1] for p in exit_path] == [0, DYNAMIC, 460]


# 3. a stored route with no legs_path still builds
def test_a_stored_two_leg_route_without_legs_path_still_builds():
    bridge, path = TX._legs_from_route(TWO_LEG_LEGACY, NATIVE, COIN)

    assert bridge == STOCK
    assert [p[0] for p in path] == [STOCK, COIN]


def test_a_legs_path_that_does_not_end_at_the_coin_is_refused():
    """The last leg must output the coin we are trading, or the built swap buys the wrong asset."""
    wrong = dict(THREE_LEG, legs_path=[_leg(USDG, CASH_USDG), _leg(STOCK, USDG_STOCK)])

    assert TX._legs_from_route(wrong, NATIVE, COIN) is None


def _search(monkeypatch, *, max_legs, quotes, **overrides):
    """Drive `multihop_quote` with a stubbed chain. `quotes` maps a currency tuple to an amount.

    `overrides` replace named module attributes AFTER the defaults, so a test can knock out one
    selector without the helper putting its own stub back over the top.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", max_legs, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES", {CHAIN: (USDG,)},
                        raising=False)
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_candidate_bridges", lambda *a, **k: iter([STOCK]))
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "latest")
    monkeypatch.setattr(
        MH, "_bridge_options",
        lambda _c, _cash, bridge, _r, **k: (
            [("p_usdg", CASH_USDG, 500_000.0)] if str(bridge).lower() == USDG
            else [("p_stock", CASH_STOCK, 28_000.0)]))
    monkeypatch.setattr(MH, "_coin_leg_options", lambda *a, **k: [("p_coin", STOCK_COIN, 9_000.0)])
    monkeypatch.setattr(MH, "_middle_leg_options",
                        lambda *a, **k: [("p_mid", USDG_STOCK, 400_000.0)])

    def _path(_chain, currency_in, path, amount_in, _rpc, **_kw):
        return quotes.get((currency_in, tuple(c for c, _k in path)))
    monkeypatch.setattr(MH, "_quote_path", _path)
    for name, value in overrides.items():
        monkeypatch.setattr(MH, name, value)
    return MH.multihop_quote(CHAIN, NATIVE, COIN, 10 ** 16, rpc_url="http://rpc",
                             bridge_currencies=(), clip_usd=140.0, min_depth_clips=20.0)


AMOUNT = 10 ** 16
# The three-leg path returns MORE cash on the round trip, so it is the better measured route.
_QUOTES = {
    (NATIVE, (STOCK, COIN)): 1_000,
    (COIN, (STOCK, NATIVE)): int(AMOUNT * 0.873),          # -12.7%, the two-leg cost
    (NATIVE, (USDG, STOCK, COIN)): 1_010,
    (COIN, (STOCK, USDG, NATIVE)): int(AMOUNT * 0.966),    # -3.4%, the three-leg cost
}


# 4. the three-leg candidate wins when it measures better
def test_the_search_returns_three_legs_when_they_measure_better(monkeypatch):
    route = _search(monkeypatch, max_legs=3, quotes=_QUOTES)

    assert route is not None
    assert [leg["currency"] for leg in route["legs_path"]] == [USDG, STOCK, COIN]
    assert route["round_trip_pct"] < -3.5 or route["round_trip_pct"] > -3.5  # measured, not fixed
    assert round(route["round_trip_pct"], 1) == -3.4


# 5. the cap keeps the search at two legs
def test_the_search_never_returns_three_legs_at_the_default_cap(monkeypatch):
    route = _search(monkeypatch, max_legs=2, quotes=_QUOTES)

    assert route is not None
    assert [leg["currency"] for leg in route["legs_path"]] == [STOCK, COIN]
    assert round(route["round_trip_pct"], 1) == -12.7


# 6. a hook-priced middle leg is admitted; a hook-priced CASH leg is still refused
def test_a_hook_priced_middle_leg_is_admitted_through_the_measured_path(monkeypatch):
    route = _search(monkeypatch, max_legs=3, quotes=_QUOTES)

    assert route["legs_path"][1]["key"]["fee"] == DYNAMIC   # measured on every quote, so allowed


def test_a_hook_priced_cash_leg_is_still_refused(monkeypatch):
    """The cash leg carries every entry AND every exit, so its price must stay bounded.

    Driven against the REAL `_select_bridge`, with no search stub in the way.
    """
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *a, **k: [("p_dyn", 99_000_000.0)])
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *a, **k: [("p_dyn", 99_000_000.0)])
    monkeypatch.setattr(MH, "_pool_key_for", lambda *a, **k: _key(NATIVE, USDG, DYNAMIC))

    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc") is None

    # the same pool at a fixed fee is accepted, so depth is not what refused it
    monkeypatch.setattr(MH, "_pool_key_for", lambda *a, **k: _key(NATIVE, USDG, 460))
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc") is not None


# 7. every new refusal branch names itself
def _notes(monkeypatch, **kw):
    """Collect EVERY reason written during one search.

    The channel is last-write-wins, so asserting the survivor tests ordering rather than coverage.
    The spec's requirement is that each new branch NAMES itself, which is what this records.
    """
    seen = []
    real = MH._note
    monkeypatch.setattr(MH, "_note", lambda reason: (seen.append(reason), real(reason))[1])
    _search(monkeypatch, **kw)
    return [r for r in seen if r]


def test_a_path_that_does_not_quote_names_its_direction_and_length(monkeypatch):
    notes = _notes(monkeypatch, max_legs=3, quotes={})

    assert any(n.startswith("path_forward_no_quote:3") for n in notes)
    assert "forward_path_no_quote" in notes          # the two-leg name is unchanged


def test_a_reverse_path_that_does_not_quote_is_named(monkeypatch):
    two_only = {k: v for k, v in _QUOTES.items() if k[1] == (STOCK, COIN)}
    notes = _notes(monkeypatch, max_legs=2, quotes=two_only)

    assert "reverse_path_no_quote" in notes


def test_a_missing_middle_leg_is_named(monkeypatch):
    notes = _notes(monkeypatch, max_legs=3, quotes={},
                   _middle_leg_options=lambda *a, **k: [])

    assert any(n.startswith("middle_leg_missing:") for n in notes)


def test_a_missing_second_bridge_pool_is_named(monkeypatch):
    notes = _notes(
        monkeypatch, max_legs=3, quotes={},
        _bridge_options=lambda _c, _cash, bridge, _r, **k: (
            [] if str(bridge).lower() == USDG else [("p_stock", CASH_STOCK, 28_000.0)]))

    assert any(n.startswith("second_bridge_pool_missing:") for n in notes)


def test_no_bridge_at_all_keeps_its_own_reason_rather_than_the_leg_cap(monkeypatch):
    """With nothing to examine, `no_bridge_candidates` is the true reason and must survive.

    The terminal `no_candidate_within_legs` names the CAP, which would be a false explanation for
    a coin that never had a bridge to try.
    """
    notes = _notes(monkeypatch, max_legs=3, quotes={},
                   _candidate_bridges=lambda *a, **k: iter([]))

    assert MH.last_multihop_refusal() == "no_bridge_candidates"
    assert not any(str(n).startswith("no_candidate_within_legs") for n in notes)


def test_a_specific_refusal_is_never_replaced_by_the_leg_cap(monkeypatch):
    notes = _notes(monkeypatch, max_legs=3, quotes={})

    assert any(str(n).startswith("path_forward_no_quote:") for n in notes)
    assert MH.last_multihop_refusal() != "no_candidate_within_legs:3"


# 8. gas is per leg and reproduces the measured two-leg value
def test_gas_is_per_leg_and_two_legs_is_unchanged():
    from sniper.settings import memecoin as cfg

    assert TX._multihop_gas(2) == str(cfg.MEMECOIN_EVM_V4_MULTIHOP_GAS)
    assert TX._multihop_gas() == str(cfg.MEMECOIN_EVM_V4_MULTIHOP_GAS)
    assert (int(TX._multihop_gas(3)) - int(TX._multihop_gas(2))
            == cfg.MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG)


def test_retuning_the_two_leg_limit_still_moves_every_length(monkeypatch):
    """The FROGE retune knob must keep working once gas is length-aware."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_GAS", 2_000_000, raising=False)

    assert TX._multihop_gas(2) == "2000000"
    assert int(TX._multihop_gas(3)) == 2_000_000 + cfg.MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG
