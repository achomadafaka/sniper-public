"""The fast lanes' swap deadline stays far inside the never-admitted dead proof (Alpha 2026-10-09).

The BSC lane also sends its buy to 48 Club Puissant privately, where custody cannot see it, and the
dead proof releases an unmined entry after MEMECOIN_EVM_DROPPED_ENTRY_MIN_AGE_S when custody shows
nothing pending. A held copy that lands after that release must not buy the coin: every lane buy
signs LANE_SWAP_DEADLINE_S, past which PancakeSwap V2 (and the 4663 routers) revert. Host clock
against chain time is the margin.
"""
import re
from pathlib import Path

from sniper.settings import memecoin as cfg

SKEW_MARGIN_S = 60
ROOT = Path(__file__).resolve().parent.parent
LANES = ("sniper/services/memecoin_rh_fast_lane.py", "sniper/services/memecoin_bsc_fast_lane.py",
         "sniper/services/memecoin_rh_fast_decide.py")


def test_the_lane_deadline_plus_skew_is_inside_the_dead_proof():
    assert cfg.LANE_SWAP_DEADLINE_S + SKEW_MARGIN_S < cfg.MEMECOIN_EVM_DROPPED_ENTRY_MIN_AGE_S


def test_every_lane_buy_signs_the_named_deadline_not_a_literal():
    for rel in LANES:
        src = (ROOT / rel).read_text()
        assert not re.search(r"(host_now|host_clock\(\)|\bnow)\)\s*\+\s*\d+", src), rel
        assert "LANE_SWAP_DEADLINE_S" in src or "_lane_deadline_s()" in src, rel
