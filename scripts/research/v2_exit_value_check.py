#!/usr/bin/env python3
"""Post-fix check for 882e5276: are V2-quoted EVM exit decisions valued on the whole bag now?

Before the fix, dex_v2_exit_quote valued a V2 exit bag at getAmountsOut(0.85 x bag), so every
percent rail priced BSC V2 bags ~15% low. 35 of 36 chain-56 exits 09-13..09-27 filled above their
decision quote, median about +13%, and b_stop fired at a quoted -35% on true losses of -23..-32%.
The fix values the decision on the whole bag less MEMECOIN_EVM_EXIT_VALUE_TAX_PCT (4) and records
`value_basis` in memecoin_route_series.quote_evidence_json for V2 exits:
  whole_bag_less_allowance   the fixed decision value
  max_tax_cut                the old 15% cut, now only the fallback when the value read fails

One command, ledger read-only. For every funded EVM position open or closed since --since: the
exit_decision rows by value_basis, and for a closed one the final exit fill against the last
exit_decision quote at or before it (fill / quote). EXPECTED after the fix, stated before the first
post-fix row exists: a whole_bag_less_allowance quote sits within about -3..+4% of its fill (real
sell taxes read 1.0-5.0% against the 4% allowance). A ratio outside [0.95, 1.05] is FLAGGED, and
so is any max_tax_cut row after the cut.

    python3 scripts/research/v2_exit_value_check.py [--since 2026-09-27T21:19:00]
    python3 scripts/research/v2_exit_value_check.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CUT = "2026-09-27T21:19:00"          # first runner carrying 882e5276 armed 21:19:09Z (Alpha)
EVM_CHAINS = ("56", "4663", "8453", "5042")
BAND = (0.95, 1.05)


def basis(evidence_json):
    """value_basis from a quote evidence blob, or 'none' (not a V2 exit / pre-fix row)."""
    try:
        return (json.loads(evidence_json or "{}") or {}).get("value_basis") or "none"
    except (TypeError, ValueError):
        return "unreadable"


def protocol(evidence_json):
    """The quote's route protocol ('uniswap_v2', 'pancake_v3', 'kyberswap', ...) or None."""
    try:
        return ((json.loads(evidence_json or "{}") or {}).get("route") or {}).get("protocol")
    except (TypeError, ValueError, AttributeError):
        return None


def verdict(ratio, quote_basis, quote_protocol=None):
    """FLAG when a fixed-basis quote misses its fill beyond BAND, when the old cut still decides,
    or when a V2-routed quote carries no value_basis at all (the fix is not running: pre-fix
    rows read exactly like that, which is why this check is only meaningful after the cut)."""
    if quote_basis == "none" and str(quote_protocol or "").endswith("_v2"):
        return "FLAG V2 quote without value_basis (fix not applied?)"
    if quote_basis == "max_tax_cut":
        return "FLAG old 15% cut decided this exit"
    if ratio is None:
        return "no fill/quote pair"
    if quote_basis == "whole_bag_less_allowance" and not (BAND[0] <= ratio <= BAND[1]):
        return "FLAG fill/quote %.4f outside [%.2f, %.2f]" % (ratio, BAND[0], BAND[1])
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=CUT)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        pos = c.execute(
            "SELECT id, network_id, source_handle, is_open, exit_reason, closed_at, "
            "round(realized_pnl, 2) FROM memecoin_positions WHERE execution_mode='real' "
            "AND network_id IN (%s) AND (is_open=1 OR closed_at >= ?) ORDER BY id"
            % ",".join("?" * len(EVM_CHAINS)), (*EVM_CHAINS, a.since)).fetchall()
        print("funded EVM positions open or closed since %s: %d" % (a.since, len(pos)))
        flags = 0
        for pid, chain, handle, is_open, reason, closed_at, pnl in pos:
            rows = c.execute(
                "SELECT id, observed_at, expected_usdc, quote_evidence_json FROM memecoin_route_series "
                "WHERE position_id=? AND seam='exit_decision' AND observed_at >= ? ORDER BY id",
                (pid, a.since)).fetchall()
            counts = {}
            for r in rows:
                b = basis(r[3])
                counts[b] = counts.get(b, 0) + 1
            line = "%6d %-5s %-16s %s decisions %s" % (
                pid, chain, handle, "OPEN  " if is_open else "closed", json.dumps(counts))
            if not is_open:
                fill = c.execute(
                    "SELECT notional, created_at FROM memecoin_fills WHERE position_id=? AND kind IN "
                    "('bracket_exit','mirror_exit','manual_close') ORDER BY id DESC LIMIT 1",
                    (pid,)).fetchone()
                last = [r for r in rows if fill and r[1] <= fill[1]]
                q = max(last, key=lambda r: r[0]) if last else None
                ratio = (fill[0] / q[2]) if fill and q and q[2] else None
                v = verdict(ratio, basis(q[3]) if q else "none", protocol(q[3]) if q else None)
                flags += v.startswith("FLAG")
                line += " | %s pnl %s fill/quote %s basis %s -> %s" % (
                    reason, pnl, "%.4f" % ratio if ratio else "-", basis(q[3]) if q else "-", v)
            elif counts.get("max_tax_cut"):
                flags += 1
                line += " -> FLAG max_tax_cut decisions on an open position"
            print(line)
        print("flags: %d" % flags)


def selftest():
    assert basis('{"value_basis": "whole_bag_less_allowance"}') == "whole_bag_less_allowance"
    assert basis('{"route": {}}') == "none" and basis(None) == "none" and basis("{x") == "unreadable"
    assert verdict(1.02, "whole_bag_less_allowance") == "ok"
    assert verdict(1.13, "whole_bag_less_allowance").startswith("FLAG")      # the pre-fix gap
    assert verdict(0.94, "whole_bag_less_allowance").startswith("FLAG")
    assert verdict(1.00, "max_tax_cut").startswith("FLAG")
    assert verdict(1.13, "none") == "ok"          # a non-V2 route: this check has no claim on it
    assert verdict(1.13, "none", "uniswap_v2").startswith("FLAG")        # V2 with no basis
    assert verdict(1.13, "none", "pancake_v3") == "ok"
    assert protocol('{"route": {"protocol": "uniswap_v2"}}') == "uniswap_v2" and protocol("{x") is None
    assert verdict(None, "whole_bag_less_allowance") == "no fill/quote pair"
    print("selftest ok")


if __name__ == "__main__":
    main()
