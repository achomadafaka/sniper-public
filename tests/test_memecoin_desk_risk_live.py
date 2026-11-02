"""Desk RISK-live pure core: facts for every position, one validated verdict per key, never widen."""
import json

import pytest

from sniper.services import memecoin_desk_risk_live as L

OPENED = "2026-10-22T09:00:00+00:00"
T0 = 1792659600.0                      # OPENED as epoch
POLICY = {"stop_pct": 20.0, "trail_pct": 15.0, "tp_pct": 60.0, "catastrophe_pct": 50.0, "max_hold_s": 0}
BOUNDS = {"tp_pct": (15, 150), "sl_pct": (10, 40), "trail_pct": (8, 30)}


def bars(n, start=T0, price=1.0, step=0.0):
    return [{"t": int(start + i), "o": price + step * i, "h": price + step * i + 0.01, "l": price + step * i - 0.01,
             "c": price + step * i, "buy_usd": 10.0, "sell_usd": 5.0, "n_makers": 3} for i in range(n)]


def pos(i, entry=25.0, fill_price=1.0):
    return {"id": i, "opened_at": OPENED, "entry_notional": entry, "entry_price": fill_price}


def block(i, pnl=None):
    return {"k": "pos:%d" % i, "pnl_pct_exact": pnl}


def test_compress_keeps_the_last_minute_at_1s_and_merges_the_rest_to_5s():
    out = L.compress(bars(300))
    recent = [b for b in out if b["t"] > T0 + 299 - 60]
    assert len(recent) == 60 and all(b2["t"] - b1["t"] == 1 for b1, b2 in zip(recent, recent[1:]))
    old = out[:-60]
    assert old and all(b["t"] % 5 == 0 for b in old)
    assert sum(b["buy_usd"] for b in old) == 10.0 * (300 - 60)       # nothing dropped, flows summed
    assert L.compress([]) == []


def test_position_facts_pnl_is_null_without_a_fresh_exact_mark_and_tape_state_is_explicit():
    now = T0 + 100
    f = L.position_facts(pos(1), POLICY, mark=None, bars=bars(100), now=now, sol_usd=1.0)
    assert f["pnl_pct_exact"] is None and f["mark_age_s"] is None and f["tape"] == "ok"
    fresh = {"sweep_ts": "2026-10-22T09:01:35+00:00", "executable_exit_value": 30.0}
    assert L.position_facts(pos(1), POLICY, mark=fresh, bars=bars(100), now=now)["pnl_pct_exact"] == 20.0
    gone = L.position_facts(pos(2), POLICY, mark=None, bars=[], now=now)       # graduated: no curve tape
    assert gone["tape"] == "none" and gone["rows"] == [] and gone["dd_bp"] is None and gone["flow_10s"] is None


def test_facts_carry_numbers_and_enums_only():
    f = L.position_facts(pos(1), POLICY, mark=None, bars=bars(10), now=T0 + 10, sol_usd=1.0)
    flat = json.dumps(f)
    assert set(f) == {"k", "held_s", "to_5min_s", "rails", "pnl_pct_exact", "mark_age_s", "tape", "high_bp", "last_bp",
                      "dd_bp", "since_high_s", "flow_10s", "flow_30s", "sb", "rows", "st", "attn"}
    assert f["st"] == {"tape": "unknown", "reason": "not_provided"}                  # no adapter: explicit unknown
    assert "mint" not in flat and "symbol" not in flat


def run(text, ids=(1, 2), pnl=None):
    blocks = [block(i, pnl) for i in ids]
    return L.validate(text, blocks=blocks, policies={b["k"]: POLICY for b in blocks}, bounds=BOUNDS)


def entry(i, a, r=None, c=1):
    return {"k": "pos:%d" % i, "a": a, "r": r, "c": c}


def test_every_key_gets_a_verdict_and_an_omitted_one_is_a_hold():
    out = run(json.dumps([entry(1, "E")]))
    assert out["pos:1"][0] == "EXIT" and out["pos:2"] == ("HOLD", None, "omitted", None)


def test_a_valid_tighten_passes_and_a_widen_or_out_of_bounds_one_is_a_hold():
    ok = run(json.dumps([entry(1, "T", {"sl_pct": 12})]))
    assert ok["pos:1"][:2] == ("TIGHTEN", {"sl_pct": 12.0})
    for bad, why in (({"sl_pct": 30}, "tighten_loosens:sl_pct"), ({"trail_pct": 20}, "tighten_loosens:trail_pct"),
                     ({"tp_pct": 80}, "tighten_loosens:tp_pct"), ({"sl_pct": 5}, "out_of_bounds:sl_pct"),
                     ({"sl_pct": 0}, "tighten_loosens:sl_pct"), ({"sl_pct": True}, "tighten_loosens:sl_pct")):
        out = run(json.dumps([entry(1, "T", bad)]))
        assert out["pos:1"][0] == "HOLD" and out["pos:1"][2] == why, bad


def test_a_tighten_that_already_fires_must_be_an_exit():
    out = run(json.dumps([entry(1, "T", {"sl_pct": 12})]), pnl=-13.0)       # -13% is past a 12% stop
    assert out["pos:1"] == ("HOLD", None, "tighten_triggers:sl_pct", 1)
    out = run(json.dumps([entry(1, "T", {"tp_pct": 40})]), pnl=45.0)
    assert out["pos:1"][2] == "tighten_triggers:tp_pct"
    assert run(json.dumps([entry(1, "T", {"sl_pct": 12})]), pnl=None)["pos:1"][0] == "TIGHTEN"   # unknown pnl: engine rechecks


def test_malformed_wrong_shape_duplicates_and_invented_keys():
    assert all(v[2] == "malformed_json" for v in run("EXIT now").values())
    assert all(v[2] == "not_an_array" for v in run(json.dumps(entry(1, "E"))).values())
    dup = run(json.dumps([entry(1, "E"), entry(1, "H")]))
    assert dup["pos:1"][:3] == ("HOLD", None, "dup_key")
    out = run(json.dumps([entry(9, "E"), "junk", entry(2, "E")]))
    assert set(out) == {"pos:1", "pos:2"} and out["pos:2"][0] == "EXIT" and out["pos:1"][2] == "omitted"


def test_hash_is_stable_and_order_sensitive_to_content_only():
    a, b = [block(1), block(2)], [block(1), block(2)]
    assert L.inputs_hash(a) == L.inputs_hash(b) and L.inputs_hash(a) != L.inputs_hash([block(1, 5.0), block(2)])


def test_rows_are_int_bp_of_the_fill_and_relative_seconds_and_the_prompt_is_small():
    now = T0 + 99
    f = L.position_facts(pos(1, fill_price=2.0), POLICY, mark=None, bars=bars(100, price=2.0, step=0.002), now=now,
                         sol_usd=1.0)
    first, last = f["rows"][0], f["rows"][-1]
    assert all(type(v) is int for row in f["rows"] for v in row)
    assert last[0] == 0 and first[0] < 0 and abs(first[4] - 10000) < 100   # relative seconds; ~the fill bar = 10000 bp
    assert f["to_5min_s"] == pytest.approx(300 - f["held_s"]) and f["high_bp"] >= f["last_bp"] and f["dd_bp"] >= 0
    two = L.build_user([f, dict(f, k="pos:2")], BOUNDS)
    assert len(two) < 6000                                               # the first smoke: 16,334 chars for 2 x 120 s
    assert json.loads(two)["cols"] == L.COLS and set(json.loads(two)["bounds"]) == set(BOUNDS)


def test_the_prompt_defines_every_reason_code_the_validator_accepts():
    for c in range(1, 17):
        assert ("\n%d " % c) in L.SYSTEM_PROMPT and c in L.REASON_CODES
    assert "300" in L.SYSTEM_PROMPT and "total PnL" in L.SYSTEM_PROMPT


def test_a_single_markdown_fence_around_the_array_is_tolerated_the_body_stays_strict():
    body = json.dumps([entry(1, "E")])
    for wrapped in ("```json\n%s\n```" % body, "```\n%s\n```" % body, "  ```json\n%s```  " % body):
        assert run(wrapped)["pos:1"][0] == "EXIT", wrapped
    assert run("```json\n[{bad}]\n```")["pos:1"][2] == "malformed_json"
    assert run("Here you go: ```json\n%s\n```" % body)["pos:1"][2] == "malformed_json"      # prose is not tolerated
