"""Per-wallet copy-premium cap (operator 2026-10-08: "raise 397397's chase cap to 50%").

MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET moves ONE wallet's hard cap over the source's average
and lifts its post-trade band to the same value; every other wallet keeps the global caps on every
lane. Gamma's stop rule (brain 663c9882) grades the entries the override admits from the
`copy_cap_override` substage."""
import re
from pathlib import Path

import pytest

from sniper.settings import memecoin as cfg

HIM = "61902b09-5394-5cf3-83ed-ea5fa93e963a"
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET",
                        cfg.parse_copy_premium_by_wallet("%s=50" % HIM), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {}, raising=False)


def test_parse_keeps_percent_entries_and_names_every_refused_one():
    said = []
    got = cfg.parse_copy_premium_by_wallet(
        "%s=50, 56:ABC=40,bad,x=0,y=80,z=nan,w=abc," % HIM.upper(), log=said.append)
    # y=80 is over the 75 ceiling: CLAMPED, not dropped (2026-10-09), so a wrong edit cannot cut a cap.
    assert got == {HIM: 50.0, "56:abc": 40.0, "y": 75.0}
    assert len(said) == 5 and sum("keeps the global cap" in s for s in said) == 4
    assert sum("clamped to 75" in s for s in said) == 1
    assert cfg.parse_copy_premium_by_wallet("u=75") == {"u": 75.0}           # the ceiling itself


def test_him_at_50_everyone_else_at_the_global_on_every_chain(caps, monkeypatch):
    for chain in ("solana", "1", "56", "8453", "5042", "4663"):
        assert cfg.copy_premium_cap(HIM, chain) == (50.0, "wallet")
        assert cfg.copy_premium_cap("someone-else", chain) == (35.0, "global")
        assert cfg.copy_premium_cap(None, chain) == (35.0, "global")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET",
                        {HIM: 50.0, "56:" + HIM: 45.0}, raising=False)
    assert cfg.copy_premium_cap(HIM, "56") == (45.0, "wallet")               # the chain entry wins
    assert cfg.copy_premium_cap(HIM, "8453") == (50.0, "wallet")


def test_a_global_cap_that_is_off_stays_off_for_him_too(caps, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0, raising=False)
    assert cfg.copy_premium_cap(HIM, "solana") == (0.0, "global_off")


def test_the_band_is_lifted_for_him_only_and_never_turned_on(caps):
    assert cfg.wallet_band_cap_pct(HIM, "56", 30.0) == 50.0
    assert cfg.wallet_band_cap_pct(HIM, "56", 60.0) == 60.0                  # a lift, never lower
    assert cfg.wallet_band_cap_pct(HIM, "56", 0.0) == 0.0                    # off stays off
    assert cfg.wallet_band_cap_pct("someone-else", "56", 30.0) == 30.0
    # The Solana slot builders' and on-chain executor's cap: min(band, hard), per wallet.
    assert cfg.solana_wallet_chase_cap_pct(HIM, 30.0) == 50.0
    assert cfg.solana_wallet_chase_cap_pct("someone-else", 30.0) == 30.0


def test_an_existing_exec_premium_lift_stays_clamped_at_the_global_hard_cap(caps, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {"solana:degen": 1.5},
                        raising=False)
    assert cfg.solana_wallet_chase_cap_pct("degen", 30.0) == 35.0


def test_the_slot_floor_admits_his_band_and_refuses_it_for_everyone_else(caps):
    from sniper.execution.pump_curve_buy import capped_min_out
    # $100 for 100 tokens at a 1.0 source average: the floor at 35% is 74.07 tokens, at 50% 66.67.
    kw = dict(source_price_usd=1.0, source_cost_usdc_atomic=100_000_000, source_price_cap_pct=50.0,
              base_decimals=6, source_average_price_usd=1.0)
    _m35, _o, floor35 = capped_min_out(100_000_000, 50.0, **kw)
    _m50, _o, floor50 = capped_min_out(100_000_000, 50.0, copy_cap_pct=50.0, **kw)
    assert floor35 == pytest.approx(100 / 1.35 * 1e6, rel=1e-6)
    assert floor50 == pytest.approx(100 / 1.50 * 1e6, rel=1e-6)


def test_every_slot_builder_passes_the_wallets_hard_cap():
    """A builder that calls capped_min_out without copy_cap_pct would sign HIS floor at 35%."""
    for path in sorted((REPO / "sniper" / "execution").glob("*.py")):
        src = path.read_text()
        for match in re.finditer(r"capped_min_out\(", src):
            if src[max(0, match.start() - 4):match.start()] == "def ":
                continue
            call = src[match.start():src.index(")\n", match.start())]
            assert "copy_cap_pct" in call, "%s:%d" % (path.name, src[:match.start()].count("\n") + 1)


def test_the_global_value_is_read_nowhere_but_on_off_checks():
    """The hard cap's VALUE comes from copy_premium_cap; the global is read only to ask whether
    the cap is on (a wallet value never turns it on) and for the status line."""
    allowed = re.compile(r"MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT\s*(>|<=)\s*0|status\[|^\s*#|\"\"\"|`")
    offenders = []
    for path in sorted((REPO / "sniper").rglob("*.py")):
        if path.parts[-2] == "settings":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT" in line and "_BY_WALLET" not in line \
                    and not allowed.search(line) and "(MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT)" not in line \
                    and "from MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT" not in line \
                    and "which MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT caps" not in line:
                offenders.append("%s:%d %s" % (path.name, n, line.strip()))
    if "hard = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT if copy_cap_pct is None" in "\n".join(offenders):
        offenders = [o for o in offenders if "if copy_cap_pct is None" not in o]
    assert offenders == []


def test_the_onchain_lane_admits_his_band_and_refuses_it_for_everyone_else(caps):
    """Source average 1.0, post-trade 1.3, chase 30%; $10 for 7 tokens = +42.9% over the average:
    inside his 50% hard cap, over everyone else's 35%."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps
    out = {}
    for uid in (HIM, "someone-else"):
        c, pid = _c(source_price=1.0)
        d, signed = _Deps(), []
        out[uid] = ex.execute_entry(
            c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id=uid,
            now_iso="2026-10-08T10:00:00+00:00",
            quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": "7000000",
                                 "otherAmountThreshold": "7000000", "slippageBps": 1500,
                                 "priceImpactPct": "0.001"},
            reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                                "otherAmountThreshold": "9900000"},
            usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=lambda q: signed.append(q),
            send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=30.0,
            chase_reference_price=1.3)
        out[uid] = (out[uid], bool(signed))
        c.close()
    assert "copy price minimum" in out["someone-else"][0]["reason"] and not out["someone-else"][1]
    assert "copy price" not in str(out[HIM][0].get("reason")) and out[HIM][1]


def test_the_evm_copy_ceiling_admits_his_band_and_refuses_it_for_everyone_else(caps):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams
    reasons = {}
    for uid in (HIM, "someone-else"):
        c = _c()
        pid = m.reserve_pending(c, mint="0xtok", source_user_id=uid, strategy="alpha_b",
                                notional=10., reserved_at="2026-10-08T10:00:00Z", network_id="56",
                                source_price=1.)
        q = {**_QUOTE, "buyAmount": "7000000", "minBuyAmount": "7000000"}       # +42.9%
        seams, _calls = _seams(quote=q, token=((0., 6), (7., 6)))
        result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="56", usd_amount=10.,
                                   strategy="alpha_b", source_user_id=uid, now_iso="t", **seams)
        reasons[uid] = str(result.get("reason"))
        c.close()
    assert "copy price" in reasons["someone-else"]
    assert "copy price" not in reasons[HIM]


def test_the_override_substage_marks_entries_admitted_only_by_it(caps):
    f = cfg.copy_cap_override_fields(HIM, "solana", lane="slot", price=1.42, average=1.0, post=1.1,
                                     band_pct=30.0)
    assert f["override_applied"] is True and f["cap_used"] == 50.0 and f["cap_without_override"] == 35.0
    assert f["premium_pct"] == pytest.approx(42.0) and f["band_used"] == 50.0
    assert cfg.copy_cap_override_fields(HIM, "solana", lane="slot", price=1.2, average=1.0, post=1.1,
                                        band_pct=30.0)["override_applied"] is False
    assert cfg.copy_cap_override_fields(HIM, "solana", lane="slot", price=None, average=1.0,
                                        post=1.1, band_pct=30.0)["override_applied"] is None
    assert cfg.copy_cap_override_fields("someone-else", "solana", lane="slot", price=1.42,
                                        average=1.0) is None


class _Trace:
    """Records substages; the runner leaves the band cap WITHOUT the override on it."""

    def __init__(self, band_without):
        self.copy_cap_band_without_override = band_without
        self.subs = []

    def mark_substage(self, name, **kw):
        self.subs.append((name, kw))

    def __getattr__(self, name):                    # any other trace call is a no-op here
        return lambda *a, **k: None

    def override(self):
        return [kw for name, kw in self.subs if name == "copy_cap_override"]


def test_onchain_a_buy_admitted_only_by_the_band_lift_reads_applied(caps):
    """Gamma MEDIUM on 08daac507: average 1.0, post-trade 1.0, $10 for 7.5758 tokens = 1.32: over
    post x 1.30 (the band without the override), under average x 1.35. The runner hands the executor
    the LIFTED band (50) and leaves 30 on the trace."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps
    c, pid = _c(source_price=1.0)
    d, trace = _Deps(), _Trace(30.0)
    ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id=HIM,
        now_iso="2026-10-08T10:00:00+00:00",
        quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": "7575758",
                             "otherAmountThreshold": "7575758", "slippageBps": 1500,
                             "priceImpactPct": "0.001"},
        reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                            "otherAmountThreshold": "9900000"},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=lambda q: None,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=50.0,
        chase_reference_price=1.0, trace=trace)
    c.close()
    (f,) = trace.override()
    assert f["band_without_override"] == 30.0 and f["band_used"] == 50.0
    assert f["override_applied"] is True and f["premium_pct"] == pytest.approx(32.0, abs=0.01)


def test_evm_a_buy_admitted_only_by_the_band_lift_reads_applied(caps):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams

    class PostTrade:                                 # the source's post-trade price is 1.0
        def resolve(self, average, pool):
            return 1.0, "post_trade", None

        def payload(self):
            return {}
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id=HIM, strategy="alpha_b", notional=10.,
                            reserved_at="2026-10-08T10:00:00Z", network_id="56", source_price=1.)
    q = {**_QUOTE, "buyAmount": "7575758", "minBuyAmount": "7575758"}       # 1.32
    seams, _calls = _seams(quote=q, token=((0., 6), (7.575758, 6)))
    trace = _Trace(30.0)
    execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="56", usd_amount=10.,
                      strategy="alpha_b", source_user_id=HIM, now_iso="t", chase_max_pct=50.0,
                      chase_reference=PostTrade(), trace=trace, **seams)
    c.close()
    (f,) = trace.override()[:1]
    assert f["band_without_override"] == 30.0 and f["override_applied"] is True


def _hook(monkeypatch, uid, network, *, mcap, display_price=1.0, waive=False):
    """real_entry_hook to the real_entry seam: the quote chase cap it hands over and the
    production trace it passes along."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_real_runner import _c, _Ev
    c, got = _c(), {}
    if waive:
        m.add_wallet(c, uid, "h")
        m.set_wallet_gate_exemption(c, uid, "chase_past_entry", active=True, actor="test",
                                    reason="test", now_iso="2026-10-08T09:59:00+00:00")
    for knob, value in {"MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT": 37.0,
                        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
                        "MEMECOIN_REAL_NANO_QUOTE_CHASE_MAX_PCT": 40.0,
                        "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT": 10.0,
                        "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT": 30.0}.items():
        monkeypatch.setattr(R.cfg, knob, value, raising=False)
    monkeypatch.setattr(R, "real_entry", lambda *_a, **kw: (got.update(kw), {
        "status": "rejected", "reason": "test seam"})[1])
    ev = _Ev("0xMint" if network != "solana" else "SolMint", uid=uid,
             created_at="2026-10-08T09:59:59+00:00")
    ev.network_id, ev.their_price, ev.signal_source, ev.lag_s = network, 1.0, "follow_activity", 1.0
    R.real_entry_hook(
        c, ev, {network: {} if network == "solana" else {"entry_quote_probe_fn": lambda *_: None}},
        now_fn=lambda: "2026-10-08T10:00:00+00:00", trade_size=37.72, max_age_s=120.0,
        resolved_identity=R.ResolvedEntryIdentity(network, "resolved", "test"),
        classification_context={"effective_mcap": mcap, "assumed_micro": False,
                                 "consensus_n_60m": 1,
                                 "market_stats": {"price": display_price, "market_cap": mcap}})
    assert got, "the hook never reached real_entry"
    assert got["trace"] is not None
    return got["chase_max_pct"], getattr(got["trace"], "copy_cap_band_without_override", None)


def test_the_hook_leaves_the_band_without_the_override_on_the_production_trace(caps, monkeypatch):
    """Gamma LOW on 8dabc5937: the writer was untested; both executor tests set it by hand."""
    assert _hook(monkeypatch, HIM, "solana", mcap=500_000.0) == (50.0, 37.0)
    assert _hook(monkeypatch, "someone-else", "solana", mcap=500_000.0) == (37.0, None)


@pytest.mark.parametrize("waive", [False, True])
def test_4663_above_nano_keeps_the_30_ceiling_for_him(caps, monkeypatch, waive):
    """Gamma MEDIUM on 8dabc5937: the lift ran after over_nano_chase_cap, so his band escaped the
    Robinhood above-nano ceiling (30 -> 50; waived 0 -> 30 -> 50). The lift comes first again."""
    assert _hook(monkeypatch, HIM, "4663", mcap=500_000.0, waive=waive) == (30.0, None)


def test_4663_nano_with_a_disagreeing_display_keeps_its_own_cap_for_him(caps, monkeypatch):
    """Display 0.80 vs source 1.0 (20% > 10%): MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT, not
    lifted, exactly as 08daac507 (Gamma money-logic PASS)."""
    assert _hook(monkeypatch, HIM, "4663", mcap=11_867.0, display_price=0.80) == (10.0, None)


class _BareTrace(_Trace):
    """A trace the runner left no band on: its lift did not change the cap (no override, or a 4663
    clamp brought it back to the cap without the override)."""

    def __init__(self):
        self.subs = []

    def __getattr__(self, name):
        if name == "copy_cap_band_without_override":
            raise AttributeError(name)
        return super().__getattr__(name)


def test_evm_band_used_is_the_band_enforced_after_a_4663_clamp(caps):
    """Gamma LOW on 6c0e57ae0: above nano on 4663 the runner clamps HIS lifted band 50 back to 30 and
    sets no attribute, so band_used read 50 and a refused 1.32 buy read override_applied=True."""
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams

    class PostTrade:
        def resolve(self, average, pool):
            return 1.0, "post_trade", None

        def payload(self):
            return {}
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id=HIM, strategy="alpha_b", notional=10.,
                            reserved_at="2026-10-08T10:00:00Z", network_id="4663", source_price=1.)
    q = {**_QUOTE, "buyAmount": "7575758", "minBuyAmount": "7575758"}       # 1.32
    seams, _calls = _seams(quote=q, token=((0., 6), (7.575758, 6)))
    trace = _BareTrace()
    execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.,
                      strategy="alpha_b", source_user_id=HIM, now_iso="t", chase_max_pct=30.0,
                      chase_reference=PostTrade(), trace=trace, **seams)
    c.close()
    (f,) = trace.override()[:1]
    assert f["band_without_override"] == 30.0 and f["band_used"] == 30.0
    assert f["override_applied"] is False                    # 1.32 is over post x 1.30: refused


def test_the_fields_take_the_band_the_caller_enforces(caps):
    f = cfg.copy_cap_override_fields(HIM, "solana", lane="onchain", price=1.42, average=1.0, post=1.1,
                                     band_pct=60.0, band_used_pct=50.0)
    assert f["band_without_override"] == 60.0 and f["band_used"] == 50.0
    # Without one the lift is recomputed, as the slot lane records it.
    assert cfg.copy_cap_override_fields(HIM, "solana", lane="slot", price=1.42, average=1.0, post=1.1,
                                        band_pct=30.0)["band_used"] == 50.0


def test_onchain_band_used_is_the_executors_chase_cap(caps, monkeypatch):
    """A roster band above HIS 50 is clamped to 50 by solana_wallet_chase_cap_pct; the recomputed lift
    read 60."""
    from sniper.execution import solana_swap_executor as ex
    from test_memecoin_entry_chase_quote_recheck import _c, _Deps
    c, pid = _c(source_price=1.0)
    d, trace = _Deps(), _BareTrace()
    ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id=HIM,
        now_iso="2026-10-08T10:00:00+00:00",
        quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": "7575758",
                             "otherAmountThreshold": "7575758", "slippageBps": 1500,
                             "priceImpactPct": "0.001"},
        reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                            "otherAmountThreshold": "9900000"},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=lambda q: None,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=60.0,
        chase_reference_price=1.0, trace=trace)
    c.close()
    (f,) = trace.override()
    assert f["band_without_override"] == 60.0 and f["band_used"] == 50.0
