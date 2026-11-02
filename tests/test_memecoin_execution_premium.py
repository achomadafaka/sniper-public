"""Execution-premium gate: our route quote against the same-instant chart mid.

The chase caps reference the TRADER'S FILL, so they mix the coin's move with our own slippage.
On a nano coin the second term dominates: 8 of the 9 chase refusals since 2026-08-01 fired on
coins trading at or BELOW the price the trader paid. This gate measures only the route cost.
"""
import pytest

from sniper.services.memecoin_market_state import execution_premium_decision
from sniper.settings import memecoin as cfg


def _classification(display_price):
    return {"facts": {"display_price": display_price}}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 2.0)


def test_cap_off_fails_open(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 0)
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=9.9e-05)
    assert d.allow is True
    assert d.reason == "cap_off"


def test_missing_display_price_fails_open(armed):
    d = execution_premium_decision(_classification(None), exact_quote_price=2.4e-05)
    assert d.allow is True
    assert d.reason == "no_display_price"


def test_unpriceable_quote_fails_open(armed):
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=None)
    assert d.allow is True
    assert d.reason == "unpriceable_quote"


def test_clean_route_passes(armed):
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=1.05e-05)
    assert d.allow is True
    assert d.reason == "ok"
    assert d.ratio == pytest.approx(1.05)


def test_at_the_cap_passes(armed):
    """The cap is the cliff edge, not an exclusive bound — 2.00x is still the -5.38% cell."""
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=2.0e-05)
    assert d.allow is True


def test_ocrxa_refused(armed):
    """Live case 2026-08-23T21:07:52Z, mint BtMAd6os…pump, $10.2k mcap.

    display 1.0160e-05, exact route 2.3931e-05 -> 2.355x. The coin was trading 18% BELOW ocrxa's
    own fill (1.2330e-05), so nothing was chasing; the whole gap was route cost.
    """
    d = execution_premium_decision(
        _classification(1.0160e-05), exact_quote_price=2.3931182e-05)
    assert d.allow is False
    assert d.reason == "execution_premium_over_cap"
    assert d.ratio == pytest.approx(2.355, abs=0.001)


def test_genuine_run_with_a_clean_route_is_admitted(armed):
    """The case the source-referenced cap got wrong. Live 2026-08-23, $12.9k mcap: the coin HAD
    run +14% past the trader, total quote/source 1.42x tripped the 30% chase cap, but our route
    was only 1.25x the mid. Market move does not predict our loss; the premium does."""
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=1.25e-05)
    assert d.allow is True


def test_scales_across_bands_without_a_per_band_constant(armed):
    """A nano coin and a large coin at the same route cost get the same answer — the whole point
    of referencing the same instant's mid instead of a stale fill."""
    nano = execution_premium_decision(_classification(1.0e-08), exact_quote_price=2.5e-08)
    large = execution_premium_decision(_classification(1.0e-02), exact_quote_price=2.5e-02)
    assert nano.allow is False and large.allow is False
    assert nano.ratio == pytest.approx(large.ratio)


def test_non_dict_classification_fails_open(armed):
    assert execution_premium_decision(None, exact_quote_price=2.4e-05).allow is True


# --- executor wiring -------------------------------------------------------------------------
# The pure function above cannot catch the wiring defect: a configured cap with no display price
# must NOT supersede the source-referenced check. These run the real executor.

import sqlite3

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m


class _Bal:
    def __init__(self, human, decimals=6):
        self.human, self.decimals = human, decimals
        self.atomic = int(round(human * (10 ** decimals)))


class _Deps:
    """Quotes a $0.02 expected route price."""
    def __init__(self):
        self.calls = []
    def quote_fn(self, mint, usd):
        return {"inAmount": "10000000", "outAmount": "500000000",
                "otherAmountThreshold": "470000000", "priceImpactPct": 0.01}
    def reverse_quote_fn(self, mint, amount):
        return {"inAmount": str(amount), "outAmount": "9800000",
                "otherAmountThreshold": "9800000"}
    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0
    def token_fn(self, mint):
        return _Bal(500.0) if "sent" in self.calls else _Bal(0.0)
    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123")
    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True
    def confirm_fn(self, sig):
        return "confirmed"


class _StaleChartDeps(_Deps):
    """A $10 route at $0.00008 per token, with a normal 2% round trip."""
    def quote_fn(self, mint, usd):
        return {"inAmount": "10000000", "outAmount": "125000000000",
                "otherAmountThreshold": "117500000000", "priceImpactPct": 0.01}


def _conn_with_contract(display_price, source_price):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    attempt_id = "attempt-premium"
    m.start_entry_attempt(c, attempt_id=attempt_id, decision_started_at="t",
                          network_id="solana", mint="MINT")
    payload, digest = m.canonical_funded_json({
        "contract_version": 1,
        "identity": {"network_id": "solana", "mint": "MINT"},
        "classification": {"facts": {"display_price": display_price}},
    })
    m.set_entry_attempt_funded_contract(
        c, attempt_id, funded_contract_json=payload, funded_contract_hash=digest,
        funded_contract_version=1)
    pid = m.reserve_pending(
        c, mint="MINT", source_user_id="u1", strategy="alpha_b", notional=10.0,
        reserved_at="t", network_id="solana", attempt_id=attempt_id,
        funded_contract_version=1, funded_contract_hash=digest, funded_contract_json=payload)
    c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?", (source_price, pid))
    c.commit()
    return c, pid


def _run(c, pid, d, chase_max_pct=30.0):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-08-23T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
        impact_cap_pct=12.0, chase_max_pct=chase_max_pct)


def test_the_solana_premium_gate_is_retired_and_the_chase_check_owns_the_refusal(armed):
    """RETIRED 2026-09-22. The same prices that used to trip the chart-mid premium gate are now
    judged by the source-referenced chase check alone.

    The route still cannot cost more than MEMECOIN_ENTRY_IMPACT_CAP_PCT (5%), which is a stricter
    execution-cost ceiling than the 1.50x premium cap ever was: 5% impact is 1.0526x, so the
    premium cap could not bind under it. The entry is still refused and nothing is signed; the
    reason now names the coin's move instead of an execution premium it never measured.
    """
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    d = _Deps()
    r = _run(c, pid, d)
    assert r["status"] == "skipped_chase"
    assert "above source price" in r["reason"], "the chase gate should own this"
    assert "the chart mid" not in r["reason"], "the retired gate must not speak again"
    assert "sign" not in d.calls and "send" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)          # reservation released


def test_executor_uses_the_source_price_when_the_chart_price_lags(monkeypatch):
    """AI, 2026-08-31. The chart read was 0.00004, the source paid 0.00006, and the exact
    route was 0.00008. The old chart-only premium rejected a route inside the 40% source chase
    limit and the round-trip cap. Removing the source reference must refuse it again."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.95)
    c, pid = _conn_with_contract(display_price=0.00004, source_price=0.00006)
    d = _StaleChartDeps()
    r = _run(c, pid, d, chase_max_pct=40.0)
    assert r["status"] == "filled"
    assert "sign" in d.calls and "send" in d.calls


def test_a_clean_route_still_faces_the_chase_check(armed):
    """SUPERSEDED BEHAVIOUR REMOVED 2026-08-25. This test previously asserted the opposite -- that
    a clean premium lets a +100%-past-the-trader entry through -- and that assertion was the bug:
    44 fills bypassed the chase cap, 6 above their real cap, net -$28.46.

    A clean premium (1.25x) means our EXECUTION is sane. It says nothing about whether we are
    buying the trader's exit liquidity. Both gates run; chase refuses this one."""
    c, pid = _conn_with_contract(display_price=0.016, source_price=0.01)
    r = _run(c, pid, _Deps())
    assert r["status"] == "skipped_chase" and "exact quote" in r["reason"]


def test_a_cap_without_a_display_price_falls_back_to_the_chase_check(armed):
    """REGRESSION. A configured cap that cannot compute a ratio must not switch the
    source-referenced check off and put nothing in its place — the shape of the gate that priced
    to None and skipped itself on every first buy until 08-19. 8 of 345 Solana reservations since
    08-01 carry no display price."""
    c, pid = _conn_with_contract(display_price=None, source_price=0.01)
    r = _run(c, pid, _Deps())                        # route 0.02 = +100% past the trader
    assert r["status"] == "skipped_chase"
    assert "exact quote" in r["reason"]              # the OLD check refused it, not the premium
    assert "execution_premium" not in r


def test_no_contract_leaves_the_chase_check_untouched(armed):
    """Pre-contract reservations return {} from the validator and must behave exactly as before."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?", (0.01, pid))
    c.commit()
    r = _run(c, pid, _Deps())
    assert r["status"] == "skipped_chase" and "exact quote" in r["reason"]


def test_the_premium_gate_does_not_supersede_the_chase_check(armed):
    """REGRESSION 2026-08-25. Shipping premium as a REPLACEMENT for the chase check let 44 fills
    bypass the chase cap; 6 were above the cap they would actually have faced, net -$28.46
    (GRILA -21.29 at +49.7% drift, CAVIAR -8.50 at +92.3%). The two gates measure different axes --
    premium is route-vs-CHART, chase is route-vs-TRADER -- so a trade must satisfy BOTH.

    Here the premium is clean (1.25x, under the 2.0x cap) while the route sits +100% above the
    trader's fill. Premium allows; chase must still refuse."""
    c, pid = _conn_with_contract(display_price=0.016, source_price=0.01)   # route 0.02
    r = _run(c, pid, _Deps(), chase_max_pct=30.0)
    assert r["status"] == "skipped_chase"
    assert "exact quote" in r["reason"], "the CHASE check must be the one that refused"
    assert m.pending_totals(c) == (0, 0.0)


# --- a unit fault is not a premium (2026-09-02) -------------------------------------------------
#
# Measured over 7 days of 4663 premium refusals: 17 of 20 were plausible route cost (1.97x-9.87x)
# and THREE were 4,280x, 380,148x and 395,149x. VEX carried quote_price 2487.26 against a source
# price of 0.0065 -- 2487 is the ETH/USD price in a token-price column, the WETH/USD defect class
# that produced $3.47M of fake PnL on 2026-07-30. Both outcomes refuse; only the name changes.


def test_an_absurd_ratio_is_named_a_unit_fault_not_a_premium(armed):
    got = execution_premium_decision(_classification(0.0065), exact_quote_price=2487.26)
    assert got.allow is False
    assert got.reason == "quote_unit_fault", (
        "a broken quote reported as a route premium pollutes every measurement over this column")


def test_the_widest_REAL_premium_ever_observed_is_still_a_premium(armed):
    """9.87x (LAUNCHCOIN, 2026-09-01) must not be reclassified. The guard is two orders above it."""
    got = execution_premium_decision(_classification(1.0), exact_quote_price=9.87)
    assert got.reason == "execution_premium_over_cap" and got.allow is False


def test_the_fault_bound_sits_between_the_two_measured_populations():
    """Empty band by measurement: widest real 9.87x, smallest fault 4,280x."""
    from sniper.services.memecoin_market_state import _QUOTE_UNIT_FAULT_RATIO
    assert 9.87 < _QUOTE_UNIT_FAULT_RATIO < 4280.0


def test_a_unit_fault_still_REFUSES(armed):
    """The point is the label. An unusable quote must never fund a transaction."""
    for exact in (2487.26, 30385.13, 1e9):
        assert execution_premium_decision(_classification(0.0065), exact_quote_price=exact).allow is False


# --- the source floor is nano-only on Solana (2026-09-13, MPAIR 56875) ---

def _mpair(band, network="solana"):
    return {"network_id": network, "coin_band": band, "facts": {"display_price": 0.0001589}}


def test_a_micro_coin_is_judged_against_the_pre_buy_chart(monkeypatch):
    """MPAIR: route 0.000243, chart 0.0001589 (before the trader's buy), trader 0.000182.
    The floor read it as 1.33x the trader and passed a 1.5x cap; the chart says 1.53x."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    d = execution_premium_decision(_mpair("micro"), exact_quote_price=0.000243,
                                   source_price=0.000182)
    assert d.allow is False and d.reference_basis == "chart"
    assert d.ratio == pytest.approx(1.529, abs=0.01)


def test_a_nano_coin_keeps_the_trader_price_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    d = execution_premium_decision(_mpair("nano"), exact_quote_price=0.000243,
                                   source_price=0.000182)
    assert d.allow is True and d.reference_basis == "source"


@pytest.mark.parametrize("classification", [_mpair("micro", network="4663"),
                                            {"facts": {"display_price": 0.0001589}}])
def test_other_chains_and_unbanded_calls_keep_the_floor(monkeypatch, classification):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    d = execution_premium_decision(classification, exact_quote_price=0.000243,
                                   source_price=0.000182)
    assert d.allow is True and d.reference_basis == "source"


def test_the_nano_only_floor_has_an_env_rollback(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_EXEC_PREMIUM_SOLANA_FLOOR_NANO_ONLY", False)
    assert execution_premium_decision(_mpair("micro"), exact_quote_price=0.000243,
                                      source_price=0.000182).allow is True


def test_a_wallet_cap_override_replaces_the_global_cap(armed):
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=2.5e-05, cap_override=3.0)
    assert d.allow is True and d.cap == 3.0
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=3.5e-05, cap_override=3.0)
    assert d.allow is False


def test_a_wallet_override_never_switches_a_disabled_gate_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 0)
    d = execution_premium_decision(_classification(1.0e-05), exact_quote_price=9.9e-05, cap_override=2.0)
    assert d.allow is True and d.reason == "cap_off"


def test_the_per_wallet_knob_now_lifts_the_chase_cap_with_a_named_mapping(monkeypatch):
    """RE-HOMED 2026-09-22. MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET capped the retired chart-mid
    gate; it now lifts the SOURCE-REFERENCED chase cap, which is the surviving per-wallet lever.

    THE MAPPING, once and named: the knob stores a price RATIO and the chase gate speaks PERCENT
    ABOVE SOURCE, so ratio -> (ratio - 1) * 100. DegenCapitalLLC's configured value is 2.0, which
    maps to 100% against the roster default of 30%. It is a LIFT, never a tightening: the operator
    set it on 2026-09-17 to let that one wallet pay MORE than the roster (7 premium refusals, 4
    take-profits, 0 stops), not less.

    NOTHING IS CONFIGURED TODAY. The .env line is commented out and the parsed dict is empty, so
    this pins the capability and the conversion, not a live value.
    """
    # DegenCapitalLLC is fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb in the commented .env line; the
    # executor keys the knob by source_user_id, which these fixtures fix as "u1".
    DEGEN = "u1"
    assert (2.0 - 1.0) * 100.0 == 100.0                  # the mapping, stated in the test itself

    # A 30% roster cap refuses a route 186% above the source...
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    r = _run(c, pid, _Deps(), chase_max_pct=30.0)
    assert r["status"] == "skipped_chase" and "above source price" in r["reason"]

    # ...and the per-wallet 2.0 ratio lifts that wallet's cap to 100%, which still refuses 186%.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {f"solana:{DEGEN}": 2.0})
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    r = _run(c, pid, _Deps(), chase_max_pct=30.0)
    assert r["status"] == "skipped_chase", "100% is a lift, not a licence for any premium"

    # A ratio that maps ABOVE the gap does admit it, which is what "lift" means.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {f"solana:{DEGEN}": 3.0})
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    assert _run(c, pid, _Deps(), chase_max_pct=30.0)["status"] == "filled"

    # An unlisted wallet keeps the roster default.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:someone_else": 3.0})
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    assert _run(c, pid, _Deps(), chase_max_pct=30.0)["status"] == "skipped_chase"


def test_the_wallet_premium_cap_also_lifts_the_quote_chase_cap_to_match(monkeypatch):
    """SEND (2026-09-17) was a nano coin: after the premium, the quote chase check (cap 50%) would
    refuse a +79% route. A 2.0x wallet premium cap means the wallet may pay up to +100% over the
    trader, so its quote chase cap is at least that; other wallets keep the passed cap."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:u1": 3.0})
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)   # route 0.02: +186% vs source
    r = _run(c, pid, _Deps(), chase_max_pct=50.0)
    assert r["status"] == "filled"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:u1": 2.0})
    c, pid = _conn_with_contract(display_price=0.008, source_price=0.007)
    r = _run(c, pid, _Deps(), chase_max_pct=50.0)
    assert r["status"] == "skipped_chase"                                  # 2.5x over a 2.0x cap
