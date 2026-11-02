"""Source-entry extractor: unit selection, same-slot exclusion, control guard, labels (synthetic U tape)."""
import importlib.util
import math
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("srcx", ROOT / "scripts/research/source_entry_extract.py")
X = importlib.util.module_from_spec(spec)
spec.loader.exec_module(X)

SRC, OTHER, REG = "SrcWallet", "Rando", "LaterRegistry"
VT = 1_000_000_000_000_000           # price = vq / vt * 1e-3


def tape(tmp_path, rows, launches=(("M", 100),)):
    d = tmp_path / "data/desk"
    (d / "swaps").mkdir(parents=True); (d / "launches").mkdir()
    sw = sqlite3.connect(d / "swaps/2026-10-22.db")
    sw.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, "
               "is_buy INTEGER, quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, "
               "virtual_token INTEGER, block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, "
               "creator_fee_bps INTEGER)")
    for i, (slot, actor, is_buy, vq, *rest) in enumerate(rows):
        real_token = rest[0] if rest else 1
        sw.execute("INSERT INTO swaps VALUES(?,?,0,'M','c',?,?,?,?,?,?,NULL,0,?,95,30)",
                   (slot, "sig%d" % i, is_buy, vq // 10**6, VT // 10**6, actor, vq, VT, real_token))  # fill = marginal
    sw.commit(); sw.close()
    la = sqlite3.connect(d / "launches/2026-10-22.db")
    la.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, slot INTEGER, "
               "signature TEXT, block_time INTEGER, received_at REAL)")
    for m, s in launches:
        la.execute("INSERT INTO launches VALUES(?,?,?,?,?,NULL,0)", (m, "c", "x", s, "l"))
    la.commit(); la.close()
    return tmp_path


def run(root, outcomes=False, k=1):
    return X.extract(root, "2026-10-22", "2026-10-22", roster={SRC}, registry={SRC, REG}, our_sigs=set(),
                     sol_usd={"2026-10-22": 200.0}, k=k, outcomes=outcomes)


def test_the_frozen_roster_refuses_a_changed_file(tmp_path):
    p = tmp_path / "r.tsv"
    p.write_text("a\tb\n")
    with pytest.raises(SystemExit):
        X.load_roster(p)
    assert len(X.load_roster()) == 105


def test_first_buy_only_and_same_slot_swaps_never_feed_features(tmp_path):
    vq = 30 * 10**15
    root = tape(tmp_path, [(1000, OTHER, 1, vq), (1010, OTHER, 1, vq * 2),
                           (1010, SRC, 1, vq * 3),             # source; a same-slot swap precedes it
                           (1020, SRC, 1, vq * 3)])            # a re-buy is not a unit
    units, stats = run(root, k=10**9)
    src = [u for u in units if u["kind"] == "SOURCE"]
    assert len(src) == 1 and src[0]["first_source"] and src[0]["slot"] == 1010
    f = src[0]["features"]
    assert f["n_hist"] == 1                                   # only slot 1000, never the same-slot 1010 swap
    assert f["age_s"] == pytest.approx((1010 - 100) * 0.4)
    assert src[0]["launch_on_tape"]


def test_control_is_guarded_after_a_registry_buy_and_strict_marks_registry_mints(tmp_path):
    vq = 30 * 10**15
    root = tape(tmp_path, [(1000, OTHER, 1, vq), (2000, REG, 1, vq), (2100, OTHER, 1, vq),
                           (2000 + 1501, OTHER, 1, vq)])
    units, _ = run(root, k=1)
    ctl = sorted(u["slot"] for u in units if u["kind"] == "CONTROL")
    assert ctl == [1000, 3501]                                # 2100 is inside the 600 s guard
    assert all(u["strict_ok"] is False for u in units if u["kind"] == "CONTROL")   # REG bought this mint


def test_up15_first_touch_and_graduation_censoring(tmp_path):
    vq = 30 * 10**15
    up = [(1000, SRC, 1, vq), (1100, OTHER, 1, int(vq * 0.8)), (1200, OTHER, 1, int(vq * 1.6)),
          (12000, OTHER, 1, vq)]
    units, _ = run(tape(tmp_path / "a", up), outcomes=True, k=10**9)
    o = [u for u in units if u["kind"] == "SOURCE"][0]["outcome"]
    assert o["up15"] == 1 and o["max_15"] == pytest.approx(math.log(1.6), rel=1e-6)
    assert o["dd_5"] == pytest.approx(math.log(0.8), rel=1e-6)
    grad = [(1000, SRC, 1, vq), (1100, OTHER, 1, vq, 0), (12000, OTHER, 1, vq)]   # real_token 0 = graduated, price flat
    units, _ = run(tape(tmp_path / "b", grad), outcomes=True, k=10**9)
    o = [u for u in units if u["kind"] == "SOURCE"][0]["outcome"]
    assert o["up15"] is None and o["graduated_slot"] == 1100   # undecided at graduation: censored, not a loss


def test_a_window_past_the_tape_end_is_censored(tmp_path):
    vq = 30 * 10**15
    units, _ = run(tape(tmp_path, [(1000, SRC, 1, vq), (1500, OTHER, 1, vq)]), outcomes=True, k=10**9)
    o = units[0]["outcome"]
    assert o["up15"] is None and o["max_5"] is None


def test_clock_is_exact_at_block_times_and_anchored_between():
    c = X.Clock({100: 1000, 110: 1003})
    assert c(100) == 1000 and c.exact(100) and not c(105) is None
    assert c(105) == pytest.approx(1002.0)                   # nearest EARLIER anchor + 0.4 s/slot
    assert c(90) == pytest.approx(996.0)                     # before the first anchor: first - 0.4 s/slot
    assert X.Clock({})(1000) == pytest.approx(400.0)         # no anchor: slot x 0.4
    assert not c.exact(105)


def test_a_gap_row_without_a_slot_still_censors_by_its_time(tmp_path):
    vq = 30 * 10**15
    root = tape(tmp_path, [(1000, SRC, 1, vq), (12000, OTHER, 1, vq)])
    (root / "data/desk/gaps").mkdir()
    g = sqlite3.connect(root / "data/desk/gaps/2026-10-22.db")
    g.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    g.execute("INSERT INTO gaps VALUES(?, 'worker_start', NULL)", (1000 * 0.4 + 100,))   # 100 s after t0
    g.commit(); g.close()
    o = run(root, outcomes=True, k=10**9)[0][0]["outcome"]
    assert o["up15"] is None and o["max_5"] is None and o["max_60"] is None
