#!/usr/bin/env python3
"""Test the hypothesis: do our LOSING mispriced-side entries coincide with BTC momentum AGAINST the side?

User's observation: we enter a cheap 'Up', then BTC plummets and we lose. If a short-horizon trend signal
separates winners from losers, a momentum-AGREEMENT filter on entries would help. This reconstructs the
engine's entries on the captured resolved windows and buckets them by whether BTC momentum at entry agreed
with the side taken. Read-only on the capture DB. n is small (~30 windows) — directional, not conclusive.
"""
from __future__ import annotations
import sqlite3, json, sys, statistics as st
sys.path.insert(0, ".")
from sniper.algos.polymarket_crypto5m.live_desk import fair_value_up, realized_vol_per_s, VOL_FLOOR_PER_S

CAP = "data/crypto5m_capture.sqlite"
ENTRY_EDGE = 0.04
FEE = 0.07
VOL_LOOKBACK = 180


def _fee(p):
    return FEE * p * (1 - p)


def load(c, cid, ws, up_tok, down_tok):
    bz = c.execute("SELECT source_event_ts t, price v FROM crypto5m_binance_observations "
                   "WHERE source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL ORDER BY t",
                   ((ws - VOL_LOOKBACK) * 1000, (ws + 300) * 1000)).fetchall()
    binance = {int(r["t"]) // 1000: float(r["v"]) for r in bz}
    asks = {}
    for tok, key in ((up_tok, "up"), (down_tok, "down")):
        rows = c.execute("SELECT exchange_ts t, raw_payload p FROM crypto5m_book_events "
                         "WHERE condition_id=? AND token_id=? AND event_type='best_bid_ask' "
                         "AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts",
                         (cid, tok, ws * 1000, (ws + 300) * 1000)).fetchall()
        for r in rows:
            try:
                d = json.loads(r["p"]); a = float(d["best_ask"])
                if 0 < a < 1:
                    asks.setdefault(int(r["t"]) // 1000, {})[key] = a
            except Exception:
                pass
    return binance, asks


def _ff(d, t, key=None):
    best = None
    for k in d:
        if k <= t and (best is None or k > best):
            best = k
    if best is None:
        return None
    return d[best] if key is None else d[best].get(key)


def run():
    c = sqlite3.connect(f"file:{CAP}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
    wins = c.execute("SELECT s.condition_id cid, m.window_start_ts ws, m.up_token_id up, m.down_token_id dn, "
                     "s.winning_outcome wout FROM crypto5m_settlements s "
                     "JOIN crypto5m_markets m ON m.condition_id=s.condition_id "
                     "WHERE s.winning_outcome IS NOT NULL").fetchall()
    all_entries, first_entries = [], []
    for w in wins:
        ws = w["ws"]; up_won = (w["wout"] or "").strip().lower().startswith("up")
        binance, asks = load(c, w["cid"], ws, str(w["up"]), str(w["dn"]))
        if len(binance) < 60 or not asks:
            continue
        start = _ff(binance, ws) or binance[min(binance)]
        first_taken = False
        for t in range(ws + 20, ws + 286, 5):
            if t not in binance or (t - 30) not in binance:
                continue
            t_left = 300 - (t - ws)
            if t_left < 8:
                continue
            recent = [binance[s] for s in range(t - VOL_LOOKBACK, t + 1) if s in binance]
            rv = realized_vol_per_s(recent)
            vol = max(rv, VOL_FLOOR_PER_S) if rv else None
            fair_up = fair_value_up(binance[t], start, vol, t_left)
            if fair_up is None:
                continue
            up_ask = _ff(asks, t, "up"); down_ask = _ff(asks, t, "down")
            cands = []
            if up_ask:
                cands.append((fair_up - up_ask - _fee(up_ask), "Up"))
            if down_ask:
                cands.append(((1 - fair_up) - down_ask - _fee(down_ask), "Down"))
            if not cands:
                continue
            edge, side = max(cands)
            if edge <= ENTRY_EDGE:
                continue
            mom30 = (binance[t] - binance[t - 30]) / binance[t - 30]
            mom60 = ((binance[t] - binance[t - 60]) / binance[t - 60]) if (t - 60) in binance else None
            won = (side == "Up") == up_won
            # agreement: momentum points the SAME way as the side we're buying
            agree30 = (mom30 > 0) if side == "Up" else (mom30 < 0)
            rec = {"side": side, "edge": edge, "mom30": mom30, "mom60": mom60, "won": won, "agree30": agree30}
            all_entries.append(rec)
            if not first_taken:
                first_entries.append(rec); first_taken = True
    c.close()

    def report(name, rows):
        if not rows:
            print(f"\n{name}: no entries"); return
        n = len(rows); wr = sum(r["won"] for r in rows) / n
        ag = [r for r in rows if r["agree30"]]; dis = [r for r in rows if not r["agree30"]]
        print(f"\n=== {name} (n={n}, overall WR {wr:.0%}) ===")
        for label, g in (("momentum AGREES with side", ag), ("momentum DISAGREES (knife-catch)", dis)):
            if g:
                print(f"   {label:34} n={len(g):>3}  WR {sum(r['won'] for r in g)/len(g):>4.0%}")
        # the user's specific case: Up entries split by BTC dropping vs rising
        ups = [r for r in rows if r["side"] == "Up"]
        if ups:
            drop = [r for r in ups if r["mom30"] < 0]; rise = [r for r in ups if r["mom30"] >= 0]
            print(f"   Up entries: BTC dropping(30s) n={len(drop)} WR {(sum(r['won'] for r in drop)/len(drop)) if drop else 0:.0%}"
                  f"  |  BTC rising n={len(rise)} WR {(sum(r['won'] for r in rise)/len(rise)) if rise else 0:.0%}")
        # winners vs losers: mean momentum-in-side-direction
        def mside(r):
            return r["mom30"] if r["side"] == "Up" else -r["mom30"]
        w_in = [mside(r) for r in rows if r["won"]]; l_in = [mside(r) for r in rows if not r["won"]]
        if w_in and l_in:
            print(f"   mean momentum-toward-side:  winners {1e4*st.mean(w_in):+.1f}bps   losers {1e4*st.mean(l_in):+.1f}bps")

    report("FIRST entry per window", first_entries)
    report("ALL decision-point entries", all_entries)
    print("\nread: if AGREE WR >> DISAGREE WR and losers have negative momentum-toward-side, a trend-"
          "agreement filter would help. If similar, momentum doesn't separate them (5m settles on END price).")


if __name__ == "__main__":
    run()
