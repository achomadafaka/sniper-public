"""risk-break-v2 (operator 2026-10-23, MEND pos 74155): a structure break is 5 s CLOSES measured against the coin's own noise and a
support that includes the pre-fill history, never a wick and never a few seconds of post-fill bars. Stop and catastrophe rails untouched."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_risk as R
from sniper.services import memecoin_desk_risk_live as L

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("risk_watcher", ROOT / "scripts/memecoin_desk_risk_watcher.py")
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

FILL = 1_000_000          # the fill second (chain UTC)
P = 1.0e-8


def bar(t, c, *, lo=None, hi=None, buy=10.0, sell=10.0):
    return {"t": t, "o": c, "h": hi if hi else c, "l": lo if lo else c, "c": c, "buy_usd": buy, "sell_usd": sell, "n_makers": 3}


def pre_history(seconds=400):
    """A coin with ordinary noise: a +-2% wobble every few seconds, lows near 0.98 P."""
    out = []
    for i in range(seconds):
        t = FILL - seconds + i
        c = P * (1.0 + 0.02 * (((i // 3) % 2) * 2 - 1))
        out.append(bar(t, c, lo=c * 0.995, hi=c * 1.005))
    return out


def path(closes, start=FILL, **kw):
    return [bar(start + i, c, **kw) for i, c in enumerate(closes)]


def sb(hist, k=2.0):
    return R.sb_closes(hist, k=k)


def test_the_mend_wick_is_not_a_break_but_the_v1_rule_read_it_as_one_from_post_fill_bars():
    # fill, 4 quiet seconds, then a 15% one-bar wick that recovers inside 12 s (MEND 74155)
    post = path([P] * 4 + [P * 0.85] * 5 + [P * 0.97] * 3 + [P] * 20, start=FILL - 1)
    v1 = [n for n in range(1, len(post) + 1) if R.structure_break(post[:n]) is not None]
    assert v1                                                          # v1: three 1 s closes under the tiny post-fill 'prior low' fired
    hist = pre_history() + post
    pre_n = len(pre_history())
    for k in (1.0, 2.0, 3.0):                                          # v2 never fires on ANY prefix of the same path
        assert all(sb(hist[:pre_n + n], k=k) is None for n in range(1, len(post) + 1)), k


def test_two_consecutive_5s_closes_far_under_support_are_a_break_and_the_key_is_stable():
    post = path([P] * 5 + [P * 0.70] * 14, start=FILL)               # buckets after the fill close 30% under, sells = buys
    key = sb(pre_history() + post)
    assert key == FILL + 5
    assert sb(pre_history() + post + path([P * 0.70] * 5, start=FILL + 19)) == key                 # the same streak, the same key
    assert sb(pre_history() + post + path([P * 0.70] * 30, start=FILL + 19)) == key               # a long streak keeps the key of its start


def test_one_close_under_support_needs_sellers_dominating_its_bucket():
    base = pre_history() + path([P] * 5, start=FILL)
    drop = lambda buy, sell: path([P * 0.70] * 5, start=FILL + 5, buy=buy, sell=sell)
    assert sb(base + drop(1.0, 30.0)) is not None                    # one close, sellers dominate
    assert sb(base + drop(30.0, 1.0)) is None                        # one close, buyers dominate: no break
    assert sb(base + drop(1.0, 30.0)[:4]) is None                    # the bucket is not complete: not counted


def test_a_close_under_support_but_inside_the_coins_own_noise_margin_is_not_a_break():
    base = pre_history() + path([P] * 5, start=FILL)
    shallow = path([P * 0.96] * 10, start=FILL + 5, sell=30.0, buy=1.0)       # under the 0.98 P support by 2%: inside k x noise
    assert sb(base + shallow) is None
    deep = path([P * 0.80] * 10, start=FILL + 5, sell=30.0, buy=1.0)
    assert sb(base + deep) is not None


def test_a_coin_that_has_not_moved_has_no_noise_estimate_and_is_never_a_break():
    frozen = path([P] * 400, start=FILL - 400)                           # zero range in every bucket: noise 0 = unknown, not margin 0
    assert sb(frozen + path([P * 0.5] * 20, start=FILL, sell=30.0, buy=1.0)) is None


def test_unknown_noise_unknown_support_and_a_gap_are_never_a_break():
    post = path([P * 0.5] * 20, start=FILL, sell=30.0, buy=1.0)
    assert sb(path([P] * 20, start=FILL - 40) + post) is None                       # under 12 noise buckets: unknown, not a break
    assert sb(pre_history(400)[-30:] + post) is None                                 # under 6 support buckets before the run
    gap = path([P * 0.5] * 5, start=FILL, sell=30.0, buy=1.0) + path([P * 0.5] * 5, start=FILL + 10, sell=30.0, buy=1.0)
    hist = pre_history() + path([P] * 0, start=FILL) + gap
    assert sb(hist) is None or sb(hist) == FILL + 10                                 # the missing bucket breaks 'consecutive'


def wobble(n, start):
    """n seconds of the same ordinary +-2% wobble as pre_history, starting at second `start`."""
    out = []
    for i in range(n):
        c = P * (1.0 + 0.02 * (((i // 3) % 2) * 2 - 1))
        out.append(bar(start + i, c, lo=c * 0.995, hi=c * 1.005))
    return out


def test_a_support_made_of_too_few_buckets_is_unknown_even_when_the_noise_is_known():
    old = pre_history(400)[100:300]                                      # 40 noisy buckets (inside the 300 s noise window), then ~17 silent ones
    sparse = wobble(15, FILL - 15)                                        # 3 data buckets inside the 60 s before the run
    post = path([P * 0.5] * 20, start=FILL, sell=30.0, buy=1.0)
    assert sb(old + sparse + post) is None
    full = wobble(60, FILL - 60)                                          # the same break with a proper 60 s of support
    assert sb(old + full + post) is not None


def test_quiet_buckets_count_as_zero_noise_so_a_mostly_silent_coin_has_unknown_noise():     # Gamma's range5 pin 2026-10-23
    old = pre_history(400)[250:300]                                      # 10 noisy buckets, then ~17 silent, then the break: median of the window is 0
    sparse = wobble(60, FILL - 60)
    post = path([P * 0.5] * 20, start=FILL, sell=30.0, buy=1.0)
    assert sb(old + sparse + post) is not None                            # 12 noisy support buckets keep the median above 0 here
    assert sb(pre_history(400)[290:300] + post) is None                  # too few buckets, mostly zeros: unknown, never a break


def test_the_break_wakes_and_feeds_the_prompt_through_the_watcher_helper(tmp_path, monkeypatch):
    desk = tmp_path / "desk"
    (desk / "swaps").mkdir(parents=True)
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(FILL, timezone.utc).strftime("%Y-%m-%d")
    s = sqlite3.connect(desk / "swaps" / ("%s.db" % day))
    s.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, is_buy INTEGER, quote_atomic INTEGER, actor TEXT, "
              "virtual_quote INTEGER, virtual_token INTEGER, received_at REAL)")
    for i, b in enumerate(pre_history()):
        s.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?)", (i, "s%d" % i, 0, "M", i % 2, 1_000_000_000, "a", int(b["c"] * 1e18), int(1e15), b["t"] + 0.5 + 100.0))
    s.commit(); s.close()
    pre = W.pre_fill_bars(desk, "M", float(FILL), 100.0, 104.3)                   # host clock = chain clock + 100 s offset
    assert pre and pre[-1]["t"] < FILL and pre[0]["t"] >= FILL - W.SB_PRE_S and all(b["buy_usd"] >= 0 for b in pre)
    pos = {"mint": "M", "opened_at": datetime.fromtimestamp(FILL, timezone.utc).isoformat()}
    post = path([P] * 5 + [P * 0.70] * 14, start=FILL)
    import json as _json
    host_now = float(FILL + 20) + 100.0
    beat = lambda **kw: (desk / "universe.heartbeat").write_text(_json.dumps({"ts": host_now, "connected": True, "sol_usd": 104.3,
                                                                              "curve_committed_through": host_now - 1.0, "curve_capture_from": host_now - 600, **kw}))
    beat()                                                                                                           # capture PROVEN alive: fresh beat + committed watermark, no gap
    assert W.structure_break_of(pos, post, desk, host_now, 100.0) is not None
    beat(curve_committed_through=host_now - 60.0)                                                                   # 'connected' but the commit watermark stalled: not proven
    assert W.structure_break_of(pos, post, desk, host_now, 100.0) is None
    beat(curve_committed_through=None)
    assert W.structure_break_of(pos, post, desk, host_now, 100.0) is None                                            # no watermark at all: unknown
    beat()
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": host_now - 60, "connected": True, "sol_usd": 104.3}))
    assert W.structure_break_of(pos, post, desk, host_now, 100.0) is None                                            # stale heartbeat: unknown, not the last break
    beat()
    (desk / "gaps").mkdir()
    g = sqlite3.connect(desk / "gaps" / ("%s.db" % day))
    g.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    g.execute("INSERT INTO gaps VALUES(?, 'ws', 0)", (host_now - 100.0,))
    g.commit(); g.close()
    assert W.structure_break_of(pos, post, desk, host_now, 100.0) is None                                            # a capture gap inside the noise window: unknown
    monkeypatch.setattr(W, "SB_CLOSES", False)                                       # the kill knob: v1 on the post-fill bars
    assert W.structure_break_of(pos, path([P] * 4 + [P * 0.85] * 3, start=FILL - 1), desk, float(FILL + 20), 0.0) is not None


def test_position_facts_carry_the_confirmed_signal_and_the_prompt_requires_it_for_code_10():
    pos = {"id": 1, "mint": "M", "opened_at": "2026-10-23T16:20:32+00:00", "entry_price": 1e-8, "entry_notional": 25.0, "executed_pool": None}
    assert L.RISK_LIVE_VERSION == "desk-risk-live-v12"
    p = " ".join(L.SYSTEM_PROMPT.split())
    assert "risk-break-v2" in p and "two consecutive 5 s bar CLOSES" in p and "code 10 EXITs need sb = 1" in p
    assert "a wick that closes back above" in p and "10: structure break: confirmed closes under the prior low (sb = 1)" in "\n".join(
        "%d: %s" % kv for kv in L.REASON_CODES.items())


def test_a_bucket_closes_by_as_of_with_the_candle_lag_never_by_a_trade_in_its_last_second():
    post = path([P] * 5 + [P * 0.7] * 10, start=FILL, buy=1, sell=1)                        # sellers do NOT dominate: two closes are needed
    hist = pre_history() + post
    assert R.sb_closes(hist, as_of=FILL + 14.0 + R.SB_LATE_S) is None                      # the second bucket (FILL+10..14) has not elapsed by the lag yet
    assert R.sb_closes(hist, as_of=FILL + 15.0 + R.SB_LATE_S) == FILL + 5                  # elapsed + lag: two closed buckets under support
    assert R.sb_closes(hist, as_of=FILL + 9.9 + R.SB_LATE_S) is None                       # only the pre-break bucket has elapsed
    sell = pre_history() + path([P] * 5 + [P * 0.7] * 10, start=FILL, buy=1, sell=30)
    assert R.sb_closes(sell, as_of=FILL + 10.0 + R.SB_LATE_S) == FILL + 5                  # ONE elapsed close under support with sellers dominating fires
    assert R.sb_closes(hist, after=FILL + 30.0, as_of=FILL + 100.0) is None                # nothing closed after the 'fill': any run is history


def test_noise_in_log_units_and_the_default_contract_when_no_as_of_is_given():
    base = [bar(FILL - 300 + i, 1.1, lo=1.0, hi=1.2) for i in range(300)]
    drop = [bar(FILL + i, 0.67, buy=1.0, sell=10.0) for i in range(10)]
    assert R.sb_closes(base + drop, k=2) is not None and R.sb_closes(base + drop[:5], k=2) is not None
    deep = [bar(FILL + i, 0.9, buy=1.0, sell=10.0) for i in range(10)]
    assert R.sb_closes(base + deep, k=2) is None                                            # a log drop of 0.2 is inside 2 x ln(1.2) = 0.36


def test_the_knobs_resolve_after_the_state_env_with_a_valid_k(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_RISK_SB_CLOSES", "0")
    monkeypatch.setenv("MEMECOIN_DESK_RISK_REGIME", "0")
    monkeypatch.setenv("MEMECOIN_DESK_RISK_SB_K", "3.5")
    W.refresh_knobs()
    assert (W.SB_CLOSES, W.REGIME, W.SB_K) == (False, False, 3.5)
    for bad in ("0", "-1", "nan", "inf", "x"):
        monkeypatch.setenv("MEMECOIN_DESK_RISK_SB_K", bad)
        W.refresh_knobs()
        assert W.SB_K == R.SB_K if bad != "inf" else W.SB_K == R.SB_K, bad                  # a bad k keeps the default (never 0, nan, negative, inf)
    monkeypatch.delenv("MEMECOIN_DESK_RISK_SB_CLOSES")
    monkeypatch.delenv("MEMECOIN_DESK_RISK_REGIME")
    monkeypatch.delenv("MEMECOIN_DESK_RISK_SB_K")
    W.refresh_knobs()
    assert (W.SB_CLOSES, W.REGIME, W.SB_K) == (True, True, R.SB_K)


def test_after_graduation_the_break_reads_the_pumpswap_bars_only_and_never_compares_across_venues(tmp_path, monkeypatch):
    import json as _json
    from datetime import datetime, timezone
    desk = tmp_path / "desk"
    desk.mkdir()
    host_now = float(FILL + 40) + 100.0
    pos = {"mint": "M", "opened_at": datetime.fromtimestamp(FILL, timezone.utc).isoformat()}
    monkeypatch.setattr(W, "pre_fill_bars", lambda *a: pre_history())
    # PS bars at 0.8x the curve price: a VENUE jump, not a break; they alone give no support/noise history
    ps = [dict(b, venue="ps") for b in path([P * 0.8] * 30, start=FILL)]
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": host_now, "connected": True, "ps_committed_through": host_now - 1, "curve_committed_through": host_now - 1, "curve_capture_from": host_now - 600}))
    assert W.structure_break_of(pos, ps, desk, host_now, 100.0) is None
    curve_then_ps = [dict(b, venue="curve") for b in path([P] * 5, start=FILL)] + ps
    assert W.structure_break_of(pos, curve_then_ps, desk, host_now, 100.0) is None                                  # still no cross-venue comparison
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": host_now, "connected": True, "curve_committed_through": host_now - 1, "curve_capture_from": host_now - 600}))
    noisy_ps = [dict(b, venue="ps") for b in pre_history(300)[:0] + path([P * (1 + 0.02 * ((i // 3) % 2 * 2 - 1)) for i in range(240)], start=FILL - 240)]
    assert W.capture_healthy(desk, host_now, ("ps",)) is False                                                      # PS evidence needs the PS committed watermark
    assert W.capture_healthy(desk, host_now, ("curve",)) is True


def test_capture_healthy_needs_a_fresh_beat_per_venue_watermarks_and_no_gap_or_span(tmp_path):
    import json as _json
    desk = tmp_path / "desk"
    desk.mkdir()
    now = 1_800_000_000.0
    assert W.capture_healthy(desk, now) is False                                                                    # no heartbeat
    import sqlite3
    from sniper.services import memecoin_desk_ps_tape as PT
    (desk / "pumpswap").mkdir()
    with sqlite3.connect(PT.tape_path(desk)) as c:
        c.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
        c.execute("CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL, cause TEXT, ended_by TEXT)")
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": now - 1, "connected": True, "curve_committed_through": now - 1.5, "ps_committed_through": now - 2, "curve_capture_from": now - 600, "ps_capture_from": now - 600, "pumpswap_capture": True}))
    assert W.capture_healthy(desk, now) is True and W.capture_healthy(desk, now, ("curve", "ps")) is True
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": now - 30, "connected": True, "curve_committed_through": now - 31}))
    assert W.capture_healthy(desk, now) is False                                                                    # a stale beat
    (desk / "universe.heartbeat").write_text(_json.dumps({"ts": now - 1, "connected": True, "curve_committed_through": now - 1.5, "ps_committed_through": now - 2, "curve_capture_from": now - 600, "ps_capture_from": now - 600, "pumpswap_capture": True}))
    from sniper.services import memecoin_desk_ps_tape as PT
    import sqlite3
    c = sqlite3.connect(PT.tape_path(desk))
    c.execute("INSERT INTO gaps_ps VALUES(?, 'ws_gap', 1)", (now - 100,))                                            # a PS capture gap inside the window (stop SPANS are read by PT.ps_gaps on the promise-feeds branch)
    c.commit(); c.close()
    assert W.capture_healthy(desk, now, ("ps",)) is False and W.capture_healthy(desk, now, ("curve",)) is True
