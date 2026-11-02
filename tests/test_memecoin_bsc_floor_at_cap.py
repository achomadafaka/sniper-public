"""The BSC fast lane signs at the worst price the slow lanes accept, privately (2026-10-15, Alpha (B);
MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS).

All 8 lane reverts since 10-12 were min-out reverts on the lane's 3% slippage floor (the exact output
at the source's post state), which was stricter than the copy-price cap every time; on 6 the slow
j7/on-chain lane filled the same coin 1-2 s later inside the cap. A floor at the cap alone would
accept prices the slow lanes refuse (their on-chain premium gate caps at 1.2x the post-trade price,
the lane cap at 1.3x), and on a public send it is a sandwich invitation. So under the knob the floor
is max(lane cap, slow-lane post-trade premium, copy) and the bytes go to the private builders only.

Pins (Alpha): a price the lane signs at its floor is one the slow lanes' premium gate accepts (same
source facts); no public member is ever in the send set while the knob lists 56, read from the
built set; every send records its members and the private-only flag; the 3% floor stays in the
evidence; knob off is today's floor and send set.

D1 (2026-10-16, Alpha): the slow lane that buys after a fast-lane miss on 56 is J7, not the onchain
lane (which hands the event over). 眉眉 0xa099…7777 reverted on the onchain lane's post x 1.2 term at
+22% over post; J7 bought it 4 blocks later at +25.8% over the source average. The slow term is now
J7's chase bound (follow_lane_chase_cap); the cross-lane pin runs J7's real premium decision and
the executor's chase confirmation on the same facts."""
import json
import sqlite3
import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from sniper.services import memecoin_rh_fast_lane as rh  # noqa: E402
from sniper.services.memecoin_market_state import execution_premium_decision  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_memecoin_bsc_fast_lane_live import _payload, _signal, live56  # noqa: E402,F401

WBNB = bsc.WBNB


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS", "56")
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E11:56")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM", 1.5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE", 1.2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", 3.0)


COPY_PCT = 35            # MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT live (the copy floor _buy_tx adds)


@pytest.fixture
def j7_knobs(monkeypatch):
    """J7's live chase caps (30/30), no wallet override unless a test sets one."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "copy_premium_cap", lambda uid, chain: (COPY_PCT, "global"))


@pytest.mark.parametrize("raw, on", [("56", True), ("4663,56", True), ("", False), ("4663", False),
                                     ("56,x", False)])
def test_the_knob_is_read_at_call_time_and_malformed_is_off(monkeypatch, raw, on):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS", raw)
    assert bsc.floor_at_cap_enabled() is on


# --- the floor ---------------------------------------------------------------------------------

def _facts(r_quote=10 ** 20, r_coin=10 ** 26, src_in=10 ** 18, src_out=11 * 10 ** 23, tax=(0, 1),
           chase=30, premium=1.5):
    cap = bsc.bsc_price_cap(r_quote, r_coin, src_in, src_out, chase_pct=chase, premium=premium)
    return {"r_quote": r_quote, "r_coin": r_coin, "src_in": src_in, "src_out": src_out,
            "tax_num": tax[0], "tax_den": tax[1], "cap_num": cap.numerator, "cap_den": cap.denominator,
            "quote": WBNB, "hop": None, "coin1": True}


def test_knob_off_is_todays_floor_exactly():
    p = _payload()
    clip = 10 ** 17
    cap = Fraction(int(p["cap_num"]), int(p["cap_den"]))
    keep = 1 - Fraction(int(p["tax_num"]), int(p["tax_den"]))
    expected = bsc.expected_out_for(clip, p)
    today = max(int(Fraction(clip) / cap * keep), int(Fraction(expected) * Fraction(97, 100)))
    assert bsc.min_out_for(clip, p) == bsc.min_out_for(clip, p, floor_at_cap=False) == today


def test_under_the_knob_the_slippage_rule_is_not_a_floor(j7_knobs):
    """The reverts' shape: the 3% rule at the source's post state was the binding floor."""
    p = _facts()
    clip = 10 ** 16
    strict = bsc.min_out_for(clip, p)
    at_cap = bsc.min_out_for(clip, p, floor_at_cap=True)
    follow = bsc.follow_lane_chase_cap(p)
    assert at_cap == max(bsc.cap_min_out(clip, p), bsc.cap_min_out(clip, p, cap=follow)) < strict


def test_the_decide_and_the_cap_rule_share_one_cap():
    receipt, mint, post = __import__("test_memecoin_bsc_fast_lane", fromlist=["x"])._synthetic()
    out = __import__("test_memecoin_bsc_fast_lane", fromlist=["x"])._decide(receipt, mint, post)
    assert out["cap"] == bsc.bsc_price_cap(out["r_quote"], out["r_coin"], out["src_in"], out["src_out"],
                                           chase_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                                           premium=cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM)


def _copy_min_out(clip, p):
    """The copy-premium floor _buy_tx adds on top, in the same units (source average x 1.35)."""
    avg = Fraction(int(p["src_in"]), int(p["src_out"]))
    return bsc.cap_min_out(clip, p, cap=avg * (1 + Fraction(COPY_PCT, 100)))


def _j7_accepts(worst, avg, uid=None):
    """J7's real decision on the same facts: its premium gate (no post-trade price, so the source
    reference) and the executor's chase confirmation (quote_fallback basis) at the wallet's cap."""
    from sniper.execution.entry_attribution import buy_drift
    from sniper.execution.evm_swap_executor import _chase_gap
    premium = execution_premium_decision({}, exact_quote_price=float(worst), source_price=float(avg),
                                         onchain=True)
    gap, basis, _ = _chase_gap(premium, float(avg), buy_drift(float(avg), float(worst), None))
    cap = cfg.wallet_band_cap_pct(uid, "56", min(cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                                                 cfg.MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT))
    return premium.allow and basis == "quote_fallback" and gap <= float(cap) + 1e-9, (gap, cap)


def _signed_worst(clip, p):
    """The worst gross price the lane can fill at: clip over the signed minimum, tax added back."""
    keep = 1 - Fraction(p["tax_num"], p["tax_den"])
    min_out = max(bsc.min_out_for(clip, p, floor_at_cap=True), _copy_min_out(clip, p))
    return bsc._clip_in_quote(clip, p) / (Fraction(min_out) / keep)


@pytest.mark.parametrize("facts", [
    {},                                                         # chase binds the lane cap
    {"src_out": 2 * 10 ** 24},                                  # a cheap source fill: premium binds
    {"src_out": 10 ** 26},                                      # source average at the post price
    {"tax": (5, 100)},                                          # a 5% buy tax
    {"r_quote": 3 * 10 ** 20, "src_in": 2 * 10 ** 18},          # a deeper pool, a bigger source
    {"chase": 0},                                               # no chase term
])
@pytest.mark.parametrize("clip", [10 ** 16, 2 * 10 ** 17])
@pytest.mark.parametrize("override", [None, 50.0])
def test_any_price_the_lane_signs_j7_accepts_on_the_same_facts(monkeypatch, j7_knobs, facts, clip,
                                                                override):
    """Alpha's cross-lane pin, against the lane that actually buys after a fast-lane miss on 56."""
    if override:
        monkeypatch.setattr(cfg, "copy_premium_cap", lambda uid, chain: (override, "wallet"))
    p = dict(_facts(**facts), source_user_id="u1")
    avg = Fraction(p["src_in"], p["src_out"])
    ok, why = _j7_accepts(_signed_worst(clip, p), avg, "u1")
    assert ok, why


@pytest.mark.parametrize("chase, fresh, want", [(30, 40, 30), (40, 30, 30), (30, 0, 30), (0, 0, None)])
def test_the_j7_term_takes_the_stricter_of_its_two_chase_caps(monkeypatch, j7_knobs, chase, fresh, want):
    """J7 uses the fresh-follow cap or the ordinary one depending on the event's lag; the lane does
    not know which, so it signs inside both. A cap of 0 is off; both off = no J7 term."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", float(chase))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT", float(fresh))
    p = _facts()
    got = bsc.follow_lane_chase_cap(p)
    avg = Fraction(p["src_in"], p["src_out"])
    assert got == (None if want is None else avg * (1 + Fraction(want, 100)))


def test_a_lifted_wallet_gets_j7s_lifted_bound(monkeypatch, j7_knobs):
    """Gamma F1: a wallet with a copy-premium lift has J7's chase cap lifted to it
    (wallet_band_cap_pct), so the follow term must lift too, or it reverts where J7 buys."""
    monkeypatch.setattr(cfg, "copy_premium_cap", lambda uid, chain: (50.0, "wallet"))
    p = dict(_facts(), source_user_id="lifted")
    avg = Fraction(p["src_in"], p["src_out"])
    assert bsc.follow_lane_chase_cap(p) == avg * Fraction(150, 100)
    assert _j7_accepts(avg * Fraction(150, 100), avg, "lifted")[1][1] == 50.0


def test_the_lane_cap_alone_would_accept_what_j7_refuses(j7_knobs):
    """Why the J7 term is in the floor: post x 1.3 sits 43% over this source's average."""
    p = _facts()
    keep = 1 - Fraction(p["tax_num"], p["tax_den"])
    clip = 10 ** 16
    worst = bsc._clip_in_quote(clip, p) / (Fraction(bsc.cap_min_out(clip, p)) / keep)
    assert not _j7_accepts(worst, Fraction(p["src_in"], p["src_out"]))[0]


def test_the_post_trade_term_is_a_diagnostic_not_a_floor(live56, monkeypatch):
    rec, ev, _puissant, _public = _run(live56, monkeypatch, "56")
    floors = ev["signed_floors"]
    assert "slow_premium" in floors and "slow_premium" not in floors["binding"]
    assert int(floors["signed_minimum"]) == max(int(floors[k]) for k in ("cap", "copy", "follow_chase")
                                                if k in floors)


# --- 眉眉 0xa099…7777, 2026-10-16 09:01:42Z, replayed from the chain ------------------------------

MEIMEI = {"r_quote": 264110172854297520464, "r_coin": 258395563221557623699027993,   # post-source
          "src_in": 7305930894285751748, "src_out": 7332825048346572231327183}       # source swap
MEIMEI_AT_OUR_INDEX = (291203289163334477536, 234410290454065795791339666)          # block 868 idx 4
MEIMEI_CLIP = 1499300000000000000            # $50 in the quote token (~$33.35 each), hop left out


def test_meimei_now_fills_where_the_post_trade_term_reverted(j7_knobs):
    p = dict(_facts(r_quote=MEIMEI["r_quote"], r_coin=MEIMEI["r_coin"], src_in=MEIMEI["src_in"],
                    src_out=MEIMEI["src_out"]), source_user_id="b81c99b9-4ec3-546b-b0d4-3c899ec4e652")
    got = bsc.v2_amount_out(MEIMEI_CLIP, *MEIMEI_AT_OUR_INDEX)
    new = max(bsc.min_out_for(MEIMEI_CLIP, p, floor_at_cap=True), _copy_min_out(MEIMEI_CLIP, p))
    old = max(bsc.cap_min_out(MEIMEI_CLIP, p), _copy_min_out(MEIMEI_CLIP, p),
              bsc.cap_min_out(MEIMEI_CLIP, p, cap=bsc.slow_lane_premium_cap(p)))
    assert old > got >= new                                       # reverted then, fills now
    fill = Fraction(MEIMEI_CLIP, got) / Fraction(*MEIMEI_AT_OUR_INDEX)
    assert 1.0 < float(fill) < 1.01                               # ~1.24228e-06 x 1.005
    assert _j7_accepts(Fraction(MEIMEI_CLIP, got), Fraction(MEIMEI["src_in"], MEIMEI["src_out"]))[0]


# --- the send set --------------------------------------------------------------------------------

class _Member:
    def __init__(self, host, reply=("rejected", "invalid sender")):
        self.host, self.reply, self.raws = host, reply, []

    def send(self, raw):
        self.raws.append(raw)
        return self.reply

    def warm(self):
        return True


def test_no_public_member_is_in_the_built_private_set():
    """Alpha's check, read from the production-built set, not a constant."""
    sender = bsc.BscSender(bsc.default_members(), bundle=lambda raw, max_block=None: ("ambiguous", "b"))
    public = {m.host for m in sender.members} - bsc.PRIVATE_HOSTS
    assert public                                               # the default set has public members
    _members, labels = sender.send_set(private_only=True)
    assert not public & set(labels) and set(labels) <= bsc.PRIVATE_HOSTS | {"blockrazor_bundle"}
    assert sender.send_set(private_only=False)[1][:len(sender.members)] == [
        m.host for m in sender.members]


def test_a_private_send_reaches_only_the_private_builders():
    puissant, public = _Member("puissant-builder.48.club"), _Member("bsc-dataseed.bnbchain.org")
    bundled = []
    sender = bsc.BscSender([puissant, public], bundle=lambda raw, max_block=None: bundled.append(
        raw) or ("ambiguous", "b"))
    sender.send("0xraw", private_only=True)
    assert puissant.raws == ["0xraw"] and public.raws == [] and bundled == ["0xraw"]
    sender.send("0xraw2")
    assert public.raws == ["0xraw2"]


def test_with_no_private_member_nothing_is_sent():
    public = _Member("bsc-dataseed.bnbchain.org")
    assert bsc.BscSender([public]).send("0xraw", private_only=True) == ("rejected", "no_private_member")
    assert public.raws == []


def test_a_cap_floor_send_is_never_fanned_out():
    live = bsc.BscLive.__new__(bsc.BscLive)
    assert live._fanout_allowed({"_private_only": True}) is False
    assert live._fanout_allowed({}) is True
    src = Path(rh.__file__).read_text()
    assert ('if kind == "ambiguous" and transport is None and self._fanout_allowed(payload):\n'
            '            try:\n                self.fanout_fn(raw)') in src


# --- the live lane, end to end ---------------------------------------------------------------------

def _run(live56, monkeypatch, knob):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS", knob)
    lane, ledger, _net, _sender, _prime = live56
    puissant, public = _Member("puissant-builder.48.club"), _Member("bsc-dataseed.bnbchain.org")
    lane["live"].sender = bsc.BscSender([puissant, public])
    rec = _signal(lane)
    lane_db = sqlite3.connect(lane["db_path"])
    (evj,) = lane_db.execute("SELECT execution_evidence_json FROM bsc_lane_live").fetchone()
    return rec, json.loads(evj), puissant, public


def test_knob_on_the_live_lane_sends_privately_and_records_it(live56, monkeypatch):
    rec, ev, puissant, public = _run(live56, monkeypatch, "56")
    assert puissant.raws and public.raws == []
    assert ev["private_only"] is True and ev["send_members"] == ["puissant-builder.48.club"]
    floors = ev["signed_floors"]
    assert floors["policy"] == "cap" and "slippage" in floors and "follow_chase" in floors
    assert set(floors["binding"]) <= {"cap", "follow_chase", "copy"}
    assert floors["slippage"] and int(floors["slippage"]) > 0       # the old 3% floor, kept as evidence


def test_knob_off_the_live_lane_is_todays_send_and_floor(live56, monkeypatch):
    rec, ev, puissant, public = _run(live56, monkeypatch, "")
    assert puissant.raws and public.raws                         # every member, as today
    assert ev["private_only"] is False and len(ev["send_members"]) == 2
    floors = ev["signed_floors"]
    assert floors["policy"] == "strict" and "slow_premium" not in floors
    assert "follow_chase" not in floors


def test_with_the_display_price_back_the_slow_term_steps_aside(monkeypatch):
    """E11 on (no E11:56 in MEMECOIN_DEXSCREENER_OFF): J7 prices against the chart, which the lane
    cannot see, so its floor is the lane cap alone."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "")
    p = _facts()
    assert bsc.slow_lane_premium_cap(p) is None and bsc.follow_lane_chase_cap(p) is None
    assert bsc.min_out_for(10 ** 16, p, floor_at_cap=True) == bsc.cap_min_out(10 ** 16, p)
