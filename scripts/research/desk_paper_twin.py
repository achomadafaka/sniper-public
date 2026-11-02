#!/usr/bin/env python3
"""Paper-arithmetic twin read on the real desk trades (pre-registration docs/audits/2026-10-26-desk-paper-twin-preregistration.md).
Uses Beta's replay entry points (curve_state / buy_fill / sell_proceeds, beta/desk-paper 03ea377f5, extracted verbatim from git into a temp module).
usage: PAPER_MODULE.py   (path of the extracted memecoin_desk_paper.py)"""
import importlib.util, json, sys, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("lessons_script", ROOT / "scripts" / "memecoin_desk_lessons.py")
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
L = S.L


def load_paper(path):
    sp = importlib.util.spec_from_file_location("desk_paper_mod", path)
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    return m


def clustered(vals, mints, draws=2000):
    u, inv = np.unique(mints, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    rng = np.random.default_rng(0)
    r = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(u), len(u)) for _ in range(draws))]
    return float(np.percentile(r, 5)), float(np.percentile(r, 95))


def main(paper_path):
    P = load_paper(paper_path)
    from sniper.util import clock as clockmod
    from sniper.util.state_paths import state_path
    clock = S.Clock(clockmod.offset_s())
    ledger, desk_db, desk_dir = str(state_path("sniper_tracker.db")), str(state_path("data", "memecoin_desk.db")), Path(state_path("data", "desk"))
    units, _ = S.discover(ledger, desk_db, time.time(), clock, 0, set())
    rows, skip = [], {}
    for u in units:
        if u["kind"] != "realized" or not u.get("entry_sig") or u.get("multi_entry") or u.get("entry_venue") == "pumpswap" or not u.get("t_verdict"):
            skip["not_eligible"] = skip.get("not_eligible", 0) + 1; continue
        es, status = S.entry_swap(desk_dir, u["mint"], u["entry_sig"], u.get("entry_qty"), u["t_ref"])
        if es is None:
            skip["entry_swap_" + status] = skip.get("entry_swap_" + status, 0) + 1; continue
        parts = L._curve_fee_parts(es)
        if parts is None:
            skip["entry_fee_unknown"] = skip.get("entry_fee_unknown", 0) + 1; continue
        spend = L.curve_spend_for_net(int(es["quote_atomic"]), parts)
        t_dec = u["t_verdict"] + P.LAND_DELAY_S
        st, why = P.curve_state(desk_dir, u["mint"], t_dec)
        if st is None:
            skip["paper_" + why] = skip.get("paper_" + why, 0) + 1; continue
        tok = P.buy_fill(st, int(spend))
        if not tok:
            skip["paper_no_fill"] = skip.get("paper_no_fill", 0) + 1; continue
        real_tok = int(es["token_atomic"])
        p_dec = st["virtual_sol_reserves"] / st["virtual_token_reserves"]
        st2, _ = P.curve_state(desk_dir, u["mint"], es["received_at"] - 1e-6)
        p_fill = (st2["virtual_sol_reserves"] / st2["virtual_token_reserves"]) if st2 else None
        row = {"pos": u["position_id"], "mint": u["mint"], "entry_gap": real_tok / tok - 1, "delay": es["received_at"] - u["t_verdict"],
               "landing_move": (p_fill / p_dec - 1) if p_fill else None, "spend_sol": spend / 1e9}
        # exit valuation twin: the first disposal leg that has a curve sell row on the tape
        legs = (u.get("exit_info") or {}).get("legs") or []
        for leg in legs:
            if leg.get("kind") == "entry":
                continue
            rr = None
            for day_off in (0, -86400, 86400):
                d = L.utc_day(u["t_ref"] + day_off + 3600)
            # find the tape row by signature + mint (sell)
            for day in {L.utc_day(u["t_ref"]), L.utc_day(u["t_ref"] + 86400), L.utc_day(u["t_ref"] - 86400)}:
                path = Path(desk_dir) / "swaps" / ("%s.db" % day)
                if not path.exists():
                    continue
                r = L.q(path, "SELECT received_at, token_atomic, quote_atomic, fee_bps, creator_fee_bps FROM swaps WHERE signature=? AND mint=? AND is_buy=0", (leg["sig"], u["mint"]))
                if len(r) == 1:
                    rr = r[0]; break
            if rr is None:
                continue
            gross = int(rr["quote_atomic"]); fees = sum(-(-gross * b // 10000) for b in (rr["fee_bps"] or 0, rr["creator_fee_bps"] or 0))
            real_net = gross - fees
            qty = int(rr["token_atomic"])
            v0, ven0 = P.sell_proceeds(desk_dir, u["mint"], qty, rr["received_at"] - 1e-6)
            v15, ven15 = P.sell_proceeds(desk_dir, u["mint"], qty, rr["received_at"] - P.LAND_DELAY_S)
            if v0:
                row["exit_gap_same_state"] = real_net / v0 - 1
            if v15:
                row["exit_gap_minus_1p5s"] = real_net / v15 - 1
            row["exit_venue"] = ven0
            break
        rows.append(row)
    print("eligible real desk entries:", len(rows), "skipped:", skip)
    e = np.array([r["entry_gap"] for r in rows]); m = np.array([r["mint"] for r in rows])
    lo, hi = clustered(e, m)
    print("ENTRY gap (real tokens / paper tokens - 1), n %d: median %+.4f mean %+.4f | coin-clustered 90%% [%+.4f, %+.4f] | |gap|<=1%%: %d of %d" % (len(e), np.median(e), e.mean(), lo, hi, int((abs(e) <= 0.01).sum()), len(e)))
    d = np.array([r["delay"] for r in rows]); print("real fill delay after the verdict (s): median %.2f p90 %.2f" % (np.median(d), np.percentile(d, 90)))
    lm = np.array([r["landing_move"] for r in rows if r["landing_move"] is not None]); print("tape price move between verdict+1.5 s and the real fill (n %d): median %+.4f mean %+.4f" % (len(lm), np.median(lm), lm.mean()))
    for key, lab in (("exit_gap_same_state", "EXIT gap vs the exact sell on the state just before the real swap (arithmetic)"), ("exit_gap_minus_1p5s", "EXIT gap vs the exact sell 1.5 s earlier (decision-lag proxy)")):
        x = [(r[key], r["mint"]) for r in rows if key in r and r.get("exit_venue") == "curve"]
        if x:
            v = np.array([a for a, _ in x]); mm = np.array([b for _, b in x]); lo, hi = clustered(v, mm)
            print("%s, n %d (curve exits): median %+.4f mean %+.4f | 90%% [%+.4f, %+.4f] | |gap|<=1%%: %d" % (lab, len(v), np.median(v), v.mean(), lo, hi, int((abs(v) <= 0.01).sum())))
    json.dump(rows, open("/tmp/claude-1000/-home-bigpapi-Desktop-Sniper/6a7c6910-619d-453f-8ce0-d59c296abe08/scratchpad/paper_twin.json", "w"))


if __name__ == "__main__":
    main(sys.argv[1])
