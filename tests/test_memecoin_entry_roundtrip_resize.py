"""Round-trip resize: shrink the clip instead of refusing when OUR SIZE causes the cost.

A round trip over the cap mixes a fixed cost (route fees, transfer tax) with our own impact. A $1
probe prices the fixed part (2026-09-28, operator "go on the round-trip rebuild"; SHORK 7fa44410:
probe 7.6%, clip 30.6%). The fixture is a constant-product pool with a fee per leg, so every quote
depends on the clip: a size-blind fixture would make any clip look resizable.
"""
import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m
from test_memecoin_entry_roundtrip_cap import _c

TOKENS = 1_000_000_000_000      # pool token reserve, atomic


class _Pool:
    def __init__(self, usd_reserve, fee, probe_raises=False):
        self.x, self.fee, self.probe_raises = float(usd_reserve), float(fee), probe_raises
        self.calls, self.quoted_usd, self.spent = [], [], None

    def _buy_out(self, usd):
        net = usd * (1 - self.fee)
        return int(TOKENS - self.x * TOKENS / (self.x + net))

    def quote_fn(self, mint, usd, **kw):
        self.calls.append("quote"); self.quoted_usd.append(usd)
        if self.probe_raises and usd < 1:
            raise RuntimeError("probe provider down")
        out = self._buy_out(usd)
        return {"outAmount": str(out), "otherAmountThreshold": str(int(out * 0.9)),
                "priceImpactPct": "0.01", "inAmount": str(int(usd * 1e6))}

    def reverse_quote_fn(self, mint, atomic):
        self.calls.append("roundtrip")
        net = int(atomic) * (1 - self.fee)          # independent quote: original reserves
        usd = self.x - self.x * TOKENS / (TOKENS + net)
        return {"inAmount": str(atomic), "outAmount": str(int(usd * 1e6)),
                "otherAmountThreshold": "1"}

    def usdc_fn(self):
        return 1000.0 - (self.spent or 0.0)

    def token_fn(self, mint):
        return float(self._buy_out(self.spent)) if self.spent else 0.0   # _Deps convention: outAmount units

    def sign_fn(self, quote):
        self.calls.append("sign")
        self.spent = int(quote["inAmount"]) / 1e6
        return (b"signedtx", "SIG123")

    def send_fn(self, signed):
        self.calls.append("send"); return True

    def confirm_fn(self, sig):
        return "confirmed"


def _run(c, pid, p, usd=10.0):          # _c() reserves $10
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=usd, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-09-28T11:00:00Z", quote_fn=p.quote_fn, reverse_quote_fn=p.reverse_quote_fn,
        sign_fn=p.sign_fn, send_fn=p.send_fn, confirm_fn=p.confirm_fn, usdc_fn=p.usdc_fn,
        token_fn=p.token_fn, impact_cap_pct=5.0)     # 10% round-trip cap, target 8%


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    s = "sniper.settings.memecoin."
    monkeypatch.setattr(s + "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE", True)
    monkeypatch.setattr(s + "MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE", False)
    monkeypatch.setattr(s + "MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD", 0.1)
    monkeypatch.setattr(s + "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION", 0.8)
    monkeypatch.setattr(s + "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION", 0.25)
    monkeypatch.setattr(s + "MEMECOIN_MIN_TRADE_USD", 1.0)


def test_size_driven_cost_fills_at_a_smaller_clip_inside_the_cap():
    c, pid = _c(); p = _Pool(usd_reserve=200, fee=0.01)
    r = _run(c, pid, p)
    assert r["status"] == "filled", r
    clip, probe, resized = p.quoted_usd
    assert (clip, probe) == (10.0, 0.1) and 2.5 <= resized < 10.0
    assert p.spent == resized
    assert r["roundtrip_cost_pct"] <= 10.0


def test_a_fixed_cost_over_the_target_refuses_at_any_size():
    c, pid = _c(); p = _Pool(usd_reserve=200, fee=0.05)     # probe ~10%: fees, not size
    r = _run(c, pid, p)
    assert r["status"] == "aborted" and r["reason"].startswith("round trip ")
    assert p.quoted_usd == [10.0, 0.1] and "sign" not in p.calls
    assert m.pending_totals(c) == (0, 0.0)


def test_a_resize_below_the_floor_refuses():
    c, pid = _c(); p = _Pool(usd_reserve=30, fee=0.01)      # needs ~1/10 of the clip
    r = _run(c, pid, p)
    assert r["status"] == "aborted" and "sign" not in p.calls
    assert p.quoted_usd == [10.0, 0.1]


def test_a_probe_that_fails_leaves_the_refusal_standing():
    c, pid = _c(); p = _Pool(usd_reserve=200, fee=0.01, probe_raises=True)
    r = _run(c, pid, p)
    assert r["status"] == "aborted" and "sign" not in p.calls
    assert m.pending_totals(c) == (0, 0.0)


def test_one_resize_only_even_when_the_smaller_clip_is_still_over(monkeypatch):
    # target 1.0 x cap: the linear estimate lands exactly on the cap and curvature pushes it over
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION", 1.0)
    c, pid = _c(); p = _Pool(usd_reserve=90, fee=0.01)
    r = _run(c, pid, p)
    assert len(p.quoted_usd) <= 3, "clip, probe, one resized run; never a second resize"
    if r["status"] != "filled":
        assert r["reason"].startswith("round trip ") and m.pending_totals(c) == (0, 0.0)


def test_with_the_requote_on_the_whole_chain_is_bounded(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE", True)
    c, pid = _c(); p = _Pool(usd_reserve=30, fee=0.01)
    r = _run(c, pid, p)
    assert r["status"] == "aborted"
    assert p.quoted_usd == [10.0, 10.0, 0.1], "first, fresh re-quote, probe; nothing more"


def test_the_knob_off_refuses_on_the_first_reading(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE", False)
    c, pid = _c(); p = _Pool(usd_reserve=200, fee=0.01)
    r = _run(c, pid, p)
    assert r["status"] == "aborted" and p.quoted_usd == [10.0]


def test_a_normal_pool_never_probes():
    c, pid = _c(); p = _Pool(usd_reserve=20000, fee=0.01)
    r = _run(c, pid, p)
    assert r["status"] == "filled" and p.quoted_usd == [10.0]
