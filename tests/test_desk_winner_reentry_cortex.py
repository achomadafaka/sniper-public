"""CORTEX (position 74139) replay: exit +$2.56 at 12:45:44; 771 retained curve trades (Codex fixture, recorded
JUDGE SOL/USD 104.329875 as a fixed FX: sensitivity, not reconstructed fresh FX)."""
import json
from datetime import datetime
from pathlib import Path

from sniper.services import memecoin_desk as service
from sniper.services import memecoin_desk_universe as U

FIX = json.loads((Path(__file__).parent / "fixtures" / "desk_cortex_74139_reentry.json").read_text())


def replay(*, above_exit=True, watch=False):
    pos, fx = FIX["position"], FIX["comparison_fx"]["sol_usd"]
    outcome = service._close_outcome(pos, FIX["fills"])
    assert outcome["exit_price_usd"] == FIX["fills"][1]["price"]          # the bound chain full-exit fill
    arm = U.WinnerReentry(window_s=1800, watch=watch)
    book = U.CurveBook(band=(30_000.0, 100_000.0), cooldown_s=300.0, sol_max_age_s=120.0)
    close = json.dumps({**outcome, "position_id": pos["id"], "closed_at": pos["closed_at"]})
    claims = []
    for s in sorted(FIX["swaps"], key=lambda r: (r["slot"], r["tx_index"], r["ordinal"])):
        now = float(s["received_at"])
        bt, mint = s["block_time"], s["mint"]
        arm.load([(mint, close)], now)
        armed = arm.pending(mint, bt) is not None
        em = book.on_trade(mint=mint, curve=s["curve"], virtual_quote=s["virtual_quote"],
                           virtual_token=s["virtual_token"], slot=s["slot"], signature=s["signature"],
                           ordinal=s["ordinal"], now=now, sol_usd=fx, sol_usd_at=now, skip_cooldown=armed)
        claim = None
        if em is not None and armed:
            claim = arm.take(mint, bt)
        elif em is None and armed and above_exit and book.state(mint) == "inside":
            price = (s["virtual_quote"] / 1e9) / (s["virtual_token"] / 1e6)
            claim = arm.check(mint, price * fx, bt)
        if claim:
            claims.append((s["slot"], s["received_at"], claim["trigger"]))
    return claims


def test_both_triggers_fire_once_at_the_first_in_band_print_above_exit():
    claims = replay()
    assert len(claims) == 1                                   # one chance per close
    assert claims[0][2] == "above_exit"
    assert claims[0][0] == FIX["expected_first_above_exit_slot_at_this_fx"]


def test_recross_alone_fires_at_the_observed_band_recross():
    claims = replay(above_exit=False)
    assert len(claims) == 1 and claims[0][2] == "recross"
    assert abs(claims[0][1] - datetime.fromisoformat(FIX["observed_band_recross_received_at"]).timestamp()) < 1e-5


def test_v23_watch_fires_once_at_the_first_in_band_post_close_print():
    """v23: re-entry as a WATCH emits on the first in-band trade after the close; JUDGE sets the trigger."""
    claims, above = replay(watch=True), replay()
    assert len(claims) == 1 and claims[0][2] == "watch"
    assert claims[0][0] <= above[0][0]                         # never later than the old above-exit trigger
