"""Exit tape stage 3a: B1's pool-state model reproduces the program's own output on real funded exits.

Frames: tests/fixtures/solana/b1_exit_frames.json (a Raydium CP exit with a Token-2022 transfer fee, a
PumpSwap exit and that pool's virtual quote V). The CP case pins the fee rule: `in_amount` is already net
of the transfer fee, and subtracting it again ran 1% low on 131 exits.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "research"))
import exit_tape_b1_validation as V          # noqa: E402

FIX = json.loads((REPO / "tests/fixtures/solana/b1_exit_frames.json").read_text())


def test_raydium_cp_effective_reserves_give_the_programs_output_to_the_atom():
    f = FIX["raydium_cp"]
    ev = V.cp_event(f["tx"])
    assert ev["in_mint"] == f["mint"] and ev["in_transfer_fee"] > 0
    p = V.predict(f["tx"], f["mint"], {})
    assert p["venue"] == "raydium_cp" and p["pred"] == p["actual"] == ev["out_amount"]
    # The raw vault overstates the reserve by the fees accrued in it; it never prices exactly here.
    assert p["accrued_quote"] > 0 and p["pred_raw"] != p["actual"]


def test_pumpswap_vaults_plus_virtual_quote_give_the_programs_gross_to_the_atom():
    f = FIX["pumpswap"]
    v = {f["pool"]: f["virtual_quote"], "quote_mint:" + f["pool"]: f["quote_mint"]}
    p = V.predict(f["tx"], f["mint"], v)
    assert p["venue"] == "pumpswap" and p["pred_gross"] == p["actual_gross"]
    assert abs(p["pred"] / p["actual"] - 1) < 1e-6
    # Without V the same vaults price the bag wrong: the virtual quote is not optional.
    p0 = V.predict(f["tx"], f["mint"], {f["pool"]: 0})
    assert p0["pred_gross"] < p["actual_gross"] * 0.99
