"""A native pump exit records its curve leg (Alpha GO 2026-10-07, native-exit stranding map, item 2).

pump_native_exit.build_native_exit sizes the quote -> USDC tail to the curve leg's MINIMUM, so a
fill above it strands quote asset (Zphinx 71502: 20.0% of the exit value). Option B (a tighter
curve-leg slippage) needs the curve drift, and the expected / minimum curve out was never stored
on Solana exits. The leg now rides in the exit's existing exit_timing_json write, after booking:
no new write, no I/O, never before the sign. Actual curve out = min_out + the exit's residue row
(memecoin_quote_residue kind='exit'). De-risk (partial) exits have no timing write, so none.
"""
import json

from sniper.execution.exit_timing import ExitTiming
from sniper.persistence import db_memecoin as m
from test_memecoin_exit_overlap_reads import Deps, QUOTE, _c, _run

LEG = {"quote_mint": "QuoteMint1111", "expected_out": 1_000_000, "min_out": 800_000, "curve": "Curve111"}
NATIVE = dict(QUOTE, _venue="pump_native", slippageBps=2000, _curve_leg=LEG,
              outAmount="171530000", otherAmountThreshold="137224000")


class NativeDeps(Deps):
    def quote_fn(self, mint, tokens, slip, max_accounts=None, atomic=None):
        super().quote_fn(mint, tokens, slip, max_accounts=max_accounts, atomic=atomic)
        return json.loads(json.dumps(NATIVE))


def test_a_native_exit_books_with_its_curve_leg_in_the_timing(monkeypatch):
    c, pos = _c()
    result = _run(c, pos, NativeDeps(), monkeypatch=monkeypatch)
    assert result["status"] == "closed"
    timing = json.loads(m.get_position(c, pos["id"])["exit_timing_json"])
    assert timing["native_leg"] == {
        "quote_mint": "QuoteMint1111", "curve": "Curve111", "expected_out_atomic": "1000000",
        "min_out_atomic": "800000", "slippage_bps": 2000, "tail_out_atomic": "171530000",
        "tail_min_atomic": "137224000"}


def test_any_other_venue_records_no_leg(monkeypatch):
    c, pos = _c()
    _run(c, pos, Deps(), monkeypatch=monkeypatch)
    assert "native_leg" not in json.loads(m.get_position(c, pos["id"])["exit_timing_json"])


def test_a_malformed_leg_records_nothing_and_never_raises():
    t = ExitTiming(clock=iter(range(100)).__next__)
    t.note_native_leg(dict(NATIVE, _curve_leg={"expected_out": "x"}))
    t.note_native_leg(None)
    t.stage("quote")
    assert "native_leg" not in json.loads(t.to_json())
