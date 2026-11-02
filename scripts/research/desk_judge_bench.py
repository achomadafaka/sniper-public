"""Desk JUDGE bench: Haiku (subscription CLI) vs a local Ollama model on the desk playbook prompt.

    python3 scripts/research/desk_judge_bench.py judge haiku|ollama:<model> [--playbook PATH] [--out DIR]

Research only: no ledger, no funded call. Every model call appends one line to
data/research/desk-judge-latency.jsonl (UTC ts, model, prompt_version, wall/API ms, tokens) so the
subscription's time-of-day latency curve accumulates across runs (Alpha 2026-10-21)."""
import asyncio, csv, json, os, re, sys, time, urllib.request
import argparse
from datetime import datetime, timezone
_ap = argparse.ArgumentParser()
_ap.add_argument("cmd"); _ap.add_argument("who")
_ap.add_argument("--playbook", default="docs/superpowers/specs/2026-10-21-desk-judge-playbook.md")
_ap.add_argument("--csv", default="data/research/2026-10-21-desk-judge-labeled-set.csv")
_ap.add_argument("--out", default="data/research/desk_judge_bench")
_ap.add_argument("--latency-log", default="data/research/desk-judge-latency.jsonl")
_ap.add_argument("--set", default="tuning", choices=["tuning", "heldout"],
                 help="tuning = the v6 blind 40 (worked + top/bottom 20 of all rows); heldout = playbook v7 rule")
ARGS = _ap.parse_args() if __name__ == "__main__" else _ap.parse_args(["judge", "haiku"])
_PB_TEXT = open(ARGS.playbook).read()
SYS = _PB_TEXT.split("\n---\n", 1)[1]
PROMPT_VERSION = (re.search(r"prompt_version: `([^`]+)`", _PB_TEXT) or [None, None])[1]
CSV = ARGS.csv
OUT = ARGS.out
os.makedirs(OUT, exist_ok=True)


def log_latency(model, wall_s, meta):
    os.makedirs(os.path.dirname(ARGS.latency_log) or ".", exist_ok=True)
    with open(ARGS.latency_log, "a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "model": model,
                             "prompt_version": PROMPT_VERSION, "wall_ms": round(wall_s * 1000, 1),
                             "api_ms": meta.get("api_ms"), "out_tok": meta.get("out_tok"),
                             "tok_s": meta.get("tok_s"), "cache_read_tok": meta.get("cache_read_tok"),
                             "cache_write_tok": meta.get("cache_write_tok"), "in_tok": meta.get("in_tok"),
                             # SDK list-price estimate on a flat subscription: NOT a billed amount
                             "notional_api_equivalent_usd": meta.get("notional_usd"),
                             "billing_basis": meta.get("billing_basis")}, sort_keys=True) + "\n")
REASONS = range(1, 17)
RANGES = {"bu": (50, 5000), "tr": (1, 50)}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["k", "v", "e", "r", "c"], "properties": {
  "k": {"type": "string"}, "v": {"type": "string", "enum": ["E", "W", "S"]},
  "e": {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 5, "maxItems": 5}]},
  "r": {"anyOf": [{"type": "null"}, {"type": "array", "minItems": 3, "maxItems": 3}]},
  "c": {"type": "integer", "minimum": 1, "maximum": 16}}}
def f(x):
    try: return float(x)
    except Exception: return None
def flow(x):
    if not x: return None
    for s in (x, x.replace("'", '"')):
        try:
            d = json.loads(s); break
        except Exception: d = None
    if not isinstance(d, dict): return None
    return {"buy_n": d.get("buy_n"), "sell_n": d.get("sell_n"), "buy_usd": d.get("buy_usd"), "sell_usd": d.get("sell_usd"),
            "makers": d.get("unique_makers"), "largest_buy": d.get("largest_buy_usd")}
SAFETY_NULL = {k: None for k in ("authority_mint", "authority_freeze", "cluster_hit", "lp_locked_or_burned", "top10_pct", "creator_pct", "sell_probe", "sells_exist_60s")}
def sheet(r, suspicious=False):
    closes = int(f(r["f_wallet_prior_funded_closes"]) or 0); pnl = f(r["f_wallet_prior_funded_pnl_usd"])
    cls = "new" if closes == 0 else ("losing" if (pnl or 0) < -25 else ("proven" if closes >= 5 and (pnl or 0) > 0 else "some"))
    cr = f(r["f_creator_prior_launches"])
    return {"opportunity_id": "op:h:solana:%s:%s" % (r["mint"], r["position_id"]), "trigger": "roster_buy", "mcap_usd": f(r["f_entry_mcap_usd"]), "age_s": f(r["f_token_age_s"]),
      "liq_usd": f(r["f_entry_liquidity_usd"]), "curve_pct": None, "creator_prior_launches": int(cr) if cr is not None else None,
      "flow_60s": flow(r["f_flow_60s"]), "price_vs_60s_low_pct": None, "price_vs_60s_high_pct": None, "price_vs_1h_high_pct": None,
      "roster": {"class": cls, "our_closes": closes, "our_pnl_usd": pnl, "buy_usd": f(r["f_source_buy_usd"]),
        "buy_vs_median": f(r["f_src_size_over_median"]), "lag_s": f(r["f_source_buy_age_at_decision_s"]),
        "lag_trusted": r["f_source_ts_trust"] != "claimed_untrusted"},
      "since_roster": {"s": f(r["f_source_buy_age_at_decision_s"]), "r_pct": f(r["f_quote_vs_source_pct"])},
      "swaps_since_source_5s": None, "swaps_since_source_2s": None,
      "quote_vs_roster_pct": f(r["f_quote_vs_source_pct"]), "safety": SAFETY_NULL, "crawler_intent": "unknown",
      "crawler_flags": {"copycat_ticker": None, "bought_hype_pattern": None, "suspicious_text": True if suspicious else None, "rug_history_claim": None}}
def validate(text, key):
    """Code-side contract: returns (verdict, cause). Any failure = S with a cause; never E."""
    try: v = json.loads((text or "").strip())
    except Exception: return "S", "malformed"
    if not isinstance(v, dict) or set(v) != {"k", "v", "e", "r", "c"}: return "S", "bad_keys"
    if v["k"] != key: return "S", "key_mismatch"
    if v["v"] not in ("E", "W", "S") or v["c"] not in REASONS: return "S", "out_of_enum"
    if v["v"] == "S": return "S", "model_skip"
    r = v["r"]
    if not (isinstance(r, list) and len(r) == 3 and 15 <= (r[0] or 0) <= 150 and 10 <= (r[1] or 0) <= 40
            and (r[2] is None or 8 <= r[2] <= 30)): return "S", "rails_out_of_bounds"
    if v["v"] == "E": return ("E", None) if v["e"] is None else ("S", "entry_on_enter")
    e = v["e"]
    if not (isinstance(e, list) and len(e) == 5 and e[0] in RANGES and e[1] == ">="
            and RANGES[e[0]][0] <= (e[2] or 0) <= RANGES[e[0]][1] and 1 <= (e[3] or 0) <= 5 and 10 <= (e[4] or 0) <= 30):
        return "S", "cond_out_of_range"
    return "W", None
def tuning_ids(rows):
    srt = sorted(rows, key=lambda r: f(r["y_return_pct"]) or 0)
    return {"70402", "60840", "73184"} | {r["position_id"] for r in srt[-20:] + srt[:20]}


def rows_sets(which="tuning"):
    """tuning: the v6 blind set. heldout (playbook v7): drop tuning ids and manual exits, then the
    20 highest and 20 lowest y_return_pct, ties by position_id ascending. No random draw."""
    rows = list(csv.DictReader(open(CSV)))
    by = {r["position_id"]: r for r in rows}
    real = by["73904"]                                    # STARMAN, fixed real sheet
    if which == "tuning":
        srt = sorted(rows, key=lambda r: f(r["y_return_pct"]) or 0)
        return real, [("worked", [by[i] for i in ("70402", "60840", "73184")]), ("top20", srt[-20:][::-1]), ("bottom20", srt[:20])]
    skip = tuning_ids(rows)
    pool = [r for r in rows if r["position_id"] not in skip and r["y_exit_reason"] != "manual"]
    srt = sorted(pool, key=lambda r: (f(r["y_return_pct"]) or 0, int(r["position_id"])))
    return real, [("top20", srt[-20:][::-1]), ("bottom20", srt[:20])]


async def haiku(prompt):
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
    o = ClaudeAgentOptions(model="claude-haiku-5-5", tools=[], setting_sources=[], mcp_servers={}, strict_mcp_config=True,
                           max_turns=1, effort="low", system_prompt=SYS,
                           extra_args={"no-session-persistence": None, "print": None})
    t = time.monotonic(); res = None
    async for m in query(prompt=prompt, options=o):
        if isinstance(m, ResultMessage): res = m
    u = (res.usage or {}) if res else {}
    return (res.result if res else ""), time.monotonic() - t, {
        "out_tok": u.get("output_tokens"), "in_tok": u.get("input_tokens"),
        "cache_read_tok": u.get("cache_read_input_tokens"), "cache_write_tok": u.get("cache_creation_input_tokens"),
        "api_ms": res.duration_api_ms if res else None,
        "notional_usd": getattr(res, "total_cost_usd", None) if res else None, "billing_basis": "subscription_flat"}
def ollama(model, prompt):
    body = json.dumps({"model": model, "stream": False, "think": False, "format": SCHEMA,
        "options": {"num_ctx": 4096, "temperature": 0}, "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": prompt}]}).encode()
    t = time.monotonic()
    d = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/chat", data=body,
        headers={"Content-Type": "application/json"}), timeout=60))
    w = time.monotonic() - t
    tps = d.get("eval_count", 0) / (d.get("eval_duration", 1) / 1e9) if d.get("eval_duration") else None
    return d["message"]["content"], w, {"out_tok": d.get("eval_count"), "tok_s": round(tps, 1) if tps else None,
        "in_tok": d.get("prompt_eval_count"), "billing_basis": "local",
        "prompt_ms": round(d.get("prompt_eval_duration", 0) / 1e6), "load_ms": round(d.get("load_duration", 0) / 1e6)}
async def call(who, sheet_obj):
    p = json.dumps(sheet_obj, separators=(",", ":"))
    if who == "haiku":
        out = await haiku(p)
    else:
        out = await asyncio.to_thread(ollama, who.split(":", 1)[1], p)
    log_latency("claude-haiku-5-5" if who == "haiku" else who.split(":", 1)[1], out[1], out[2])
    return out
def pct(xs, p):
    xs = sorted(x for x in xs if x is not None); return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None
async def judge(who):
    real, sets = rows_sets(ARGS.set); out = {"who": who, "set": ARGS.set, "prompt_version": PROMPT_VERSION, "repeat": {}, "blind": []}
    for name, tk in (("real", False), ("flag_suspicious", True)):
        recs = []
        for _ in range(20):
            s = sheet(real, tk); text, w, meta = await call(who, s)
            recs.append({"wall": w, "text": text, "verdict": validate(text, s["opportunity_id"]), **meta})
        out["repeat"][name] = recs
        print(who, name, "wall p50/p90", pct([r["wall"] for r in recs], .5), pct([r["wall"] for r in recs], .9),
              "verdicts", {v: sum(1 for r in recs if r["verdict"][0] == v) for v in "EWS"},
              "causes", {c: sum(1 for r in recs if r["verdict"][1] == c) for c in {r["verdict"][1] for r in recs}}, flush=True)
    for name, rs in sets:
        for r in rs:
            s = sheet(r); text, w, meta = await call(who, s)
            out["blind"].append({"set": name, "pid": r["position_id"], "ret": f(r["y_return_pct"]), "wall": w, "text": text,
                                 "verdict": validate(text, s["opportunity_id"]), **meta})
    json.dump(out, open(os.path.join(OUT, f"judge_{who.replace(':', '_')}_{ARGS.set}_{PROMPT_VERSION}.json"), "w"), indent=1)
    for name in [n for n, _ in sets]:
        b = [x for x in out["blind"] if x["set"] == name]
        print(who, name, {v: sum(1 for x in b if x["verdict"][0] == v) for v in "EWS"}, [(x["pid"], x["verdict"]) for x in b] if name == "worked" else "")
if __name__ == "__main__":
    asyncio.run(judge(ARGS.who))
