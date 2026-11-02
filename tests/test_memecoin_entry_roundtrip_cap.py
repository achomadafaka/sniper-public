"""Entry round-trip cap. The mcap-tiered impact cap bounds what the entry PAYS to get in, and
before 2026-08-12 nothing bounded what the pool RETURNS on the way out. The exit ladder widens
tolerance until the sell lands, so a one-sided pool was uncapped in the only direction that empties
the bag.

KALMAN (funded position 36192, 2026-08-12): the buy quoted inside its 15% nano cap, and 86 s later
the whole $36.95 bag sold back for $17.02. Its pool held 3.886 SOL and our own sell raised the
token reserve 22.1% in one transaction; the mint also carries an immutable 3% Token-2022 transfer
fee charged on both legs. The chart showed no such candle because neither price was a candle.
"""
import sqlite3

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m
from sniper.settings.memecoin import (
    MEMECOIN_ENTRY_IMPACT_CAP_PCT,
    entry_roundtrip_cap_pct,
)


# --- cap policy ---------------------------------------------------------------------------------

def test_the_cap_scales_with_the_one_way_cap_it_complements():
    """A factor, not an absolute number, so the two gates can never contradict each other."""
    assert entry_roundtrip_cap_pct(15.0) == 30.0    # nano tier
    assert entry_roundtrip_cap_pct(5.0) == 10.0


def test_an_unusable_one_way_cap_falls_back_to_the_base(monkeypatch):
    assert entry_roundtrip_cap_pct(None) == MEMECOIN_ENTRY_IMPACT_CAP_PCT * 2
    assert entry_roundtrip_cap_pct("nan") == MEMECOIN_ENTRY_IMPACT_CAP_PCT * 2


def test_a_malformed_absolute_knob_never_disables_the_gate(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT", "abc")
    assert entry_roundtrip_cap_pct(15.0) == 30.0


def test_the_absolute_knob_wins_when_the_operator_measures_one(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_CAP_PCT", "18")
    assert entry_roundtrip_cap_pct(15.0) == 18.0


# --- executor -----------------------------------------------------------------------------------

def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    return c, pid


class _Deps:
    def __init__(self, quote_impact=0.01, usd_back=None, raises=False):
        self.calls = []
        self._impact = quote_impact
        self._usd_back = usd_back
        self._raises = raises
    def quote_fn(self, mint, usd):
        self.calls.append("quote"); return {"outAmount": "500", "otherAmountThreshold": "450",
                "priceImpactPct": self._impact}
    def reverse_quote_fn(self, mint, token_atomic):
        self.calls.append("roundtrip")
        assert token_atomic == 450       # `otherAmountThreshold`, the signer's minimum bound
        if self._raises:
            raise RuntimeError("provider exploded")
        if self._usd_back is None:
            return None
        # the reverse quote prices the PLANNED (minimum) quantity; the gate scales its rate onto
        # the expected 500, so state it here at the planned quantity's own rate
        out = int(round(self._usd_back * 1e6 * (450 / 500)))
        return {"inAmount": 450, "outAmount": out, "otherAmountThreshold": max(1, out - 1)}
    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0
    def token_fn(self, mint):
        return 500.0 if "sent" in self.calls else 0.0
    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123")
    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True
    def confirm_fn(self, sig):
        self.calls.append("confirm"); return "confirmed"


def _run(c, pid, d, impact_cap_pct=15.0):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-08-12T00:00:00Z",
        quote_fn=d.quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=impact_cap_pct,
        reverse_quote_fn=d.reverse_quote_fn)


def test_a_kalman_shaped_pool_aborts_before_it_signs():
    """The buy passes its cap and the sell returns 46% of the clip. Never sign."""
    c, pid = _c(); d = _Deps(quote_impact=0.05, usd_back=4.60)   # 54% round trip vs a 30% cap
    r = _run(c, pid, d)
    assert r["status"] == "aborted"
    assert "sign" not in d.calls
    assert round(r["roundtrip_cost_pct"], 1) == 54.0
    assert "round trip 54.00% over 30.0% cap" in r["reason"]
    assert m.pending_totals(c) == (0, 0.0)      # reservation released


def test_a_normal_round_trip_still_fills():
    """25 real Solana funded closes since 2026-08-05 have a median round trip of -0.8%."""
    c, pid = _c(); d = _Deps(quote_impact=0.05, usd_back=9.92)
    r = _run(c, pid, d)
    assert r["status"] == "filled"
    assert "sign" in d.calls
    assert round(r["roundtrip_cost_pct"], 1) == 0.8     # carried on the FILL for calibration


def test_an_absent_sell_route_is_still_main_s_route_failed_gate():
    """This cap prices a sell route; it never has to decide whether one EXISTS. That verdict
    already belongs to the planned-quantity check that runs immediately before it."""
    c, pid = _c(); d = _Deps(quote_impact=0.05, usd_back=None)
    r = _run(c, pid, d)
    assert r["status"] == "route_failed"
    assert "sign" not in d.calls


def test_a_raising_reverse_quote_never_signs():
    c, pid = _c(); d = _Deps(quote_impact=0.05, raises=True)
    r = _run(c, pid, d)
    assert r["status"] == "route_failed"
    assert "sign" not in d.calls


def test_the_one_way_cap_still_fires_first():
    """Impact over the cap aborts without ever asking for the sell quote."""
    c, pid = _c(); d = _Deps(quote_impact=0.25, usd_back=9.99)
    r = _run(c, pid, d)
    assert r["status"] == "aborted"
    assert "price impact" in r["reason"]
    assert "roundtrip" not in d.calls


def test_the_gate_costs_no_extra_request():
    """`a6cd2db2` already fetches the reverse quote to prove a sell route exists. The cap reads
    that same answer, so it adds no round trip to the entry critical path."""
    c, pid = _c(); d = _Deps(quote_impact=0.05, usd_back=9.92)
    _run(c, pid, d)
    assert d.calls.count("roundtrip") == 1


# --- route provenance ---------------------------------------------------------------------------

def test_the_classification_records_which_pair_its_liquidity_describes():
    """KALMAN recorded liquidity $22,040.91 and pair_age_s 54,842 -- exact facts about a $24,508
    Raydium pool -- while the swap routed a $356 Meteora pool. Both numbers were right about a
    pool we did not trade, and nothing on the row said so. The batch already returns the pair
    address and the pair count, so recording them costs no request."""
    import sqlite3

    from sniper.services import memecoin_market_state as ms

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    cls = ms.classify_funded_entry(
        conn, network_id="solana", mint="9TPDSL", signal_source="follow_activity",
        decision_at="2026-08-12T15:04:37Z", effective_mcap=38_173.0, assumed_micro=False,
        base_clip_usd=36.95, consensus_n_60m=None,
        market_stats={"liquidity": 22_040.91, "pair_age_s": 54_842.0, "price": 4.85e-05,
                      "pair_address": "EGmjfbdeV4Jw", "n_pairs": 5},
    )
    assert cls.facts["liquidity_pair_address"] == "EGmjfbdeV4Jw"
    assert cls.facts["liquidity_pair_count"] == 5
    # still descriptive: the deepest pair is what was measured, and it still reads deep
    assert cls.liquidity_class == "deep"


def test_route_provenance_is_absent_not_invented_when_the_batch_omits_it():
    import sqlite3

    from sniper.services import memecoin_market_state as ms

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    cls = ms.classify_funded_entry(
        conn, network_id="solana", mint="MINT", signal_source="follow_activity",
        decision_at="2026-08-12T15:04:37Z", effective_mcap=38_173.0, assumed_micro=False,
        base_clip_usd=36.95, consensus_n_60m=None, market_stats={},
    )
    assert cls.facts["liquidity_pair_address"] is None
    assert cls.facts["liquidity_pair_count"] is None


# --- revival-guard inputs -----------------------------------------------------------------------

def _classified(**stats):
    import sqlite3

    from sniper.services import memecoin_market_state as ms

    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return ms.classify_funded_entry(
        conn, network_id="solana", mint="MINT", signal_source="follow_activity",
        decision_at="2026-08-23T15:04:37Z", effective_mcap=38_173.0, assumed_micro=False,
        base_clip_usd=36.95, consensus_n_60m=None, market_stats=stats)


def test_the_classification_records_the_volume_the_revival_guard_judged():
    """`volume_alive_decision` read volume_m5 and volume_h1, refused under the turnover floors, and
    kept nothing. Only the refusal string held the number -- "m5 volume $902 under floor $24264" --
    and a refused coin has no outcome, so the filter could not be scored in either direction:
    refused entries had the volume and no outcome, filled entries the outcome and no volume."""
    cls = _classified(liquidity=22_040.91, price=4.85e-05, market_cap=2_426_400.0,
                      volume_m5=902.0, volume_h1=51_000.0)

    assert cls.facts["volume_m5"] == 902.0
    assert cls.facts["volume_h1"] == 51_000.0
    assert cls.facts["volume_mcap"] == 2_426_400.0


def test_the_recorded_mcap_is_the_one_the_gate_divides_by():
    """The gate divides by stats["market_cap"]; `effective_mcap` is resolved on a different path and
    can differ. A turnover recomputed from the wrong denominator would not be the gate's input."""
    cls = _classified(market_cap=2_426_400.0, volume_m5=902.0)

    assert cls.facts["effective_mcap"] == 38_173.0      # the resolved band mcap
    assert cls.facts["volume_mcap"] == 2_426_400.0      # ...not what the guard measured against


def test_the_recorded_facts_reproduce_the_refusal_exactly():
    from sniper.services.memecoin_real_runner import volume_alive_decision

    stats = {"market_cap": 2_426_400.0, "volume_m5": 902.0, "volume_h1": 51_000.0}
    live = volume_alive_decision(stats["volume_h1"], stats["market_cap"], abs_floor=0.0,
                                 turnover_floor=0.0, vol_m5=stats["volume_m5"], m5_floor=0.01)
    f = _classified(**stats).facts
    replayed = volume_alive_decision(f["volume_h1"], f["volume_mcap"], abs_floor=0.0,
                                     turnover_floor=0.0, vol_m5=f["volume_m5"], m5_floor=0.01)

    assert live == (False, 24_264.0, "m5_turnover_under_floor")
    assert replayed == live         # the row alone now answers "why", months later


def test_a_missing_volume_is_absent_not_invented():
    f = _classified(liquidity=22_040.91).facts

    assert f["volume_m5"] is None and f["volume_h1"] is None and f["volume_mcap"] is None
