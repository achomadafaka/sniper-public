"""The honest cause of a Solana impact_cap refusal (operator 2026-10-02, option (a), via Alpha).

The gate is unchanged: Jupiter's priceImpactPct against the same mcap tiers. What changes is the
words. PACKZ (starcatcher444, 12:49:49Z) read "price impact 23.37% over 12.0% cap" on a $20 buy
whose own size moved its DAMM v2 pool about 0.2%: the 23% was the source's own push priced against a
lagging reference. The phone now says how far our quote sits above the source's fill, with
Jupiter's number beside it (no DexScreener depth: operator rule). The MACHINE reason stays byte-stable: the refusal
capture, the refusal shadow, the family reads and the impact distribution parse it."""
import sqlite3
import threading

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_telegram import render_entry_notification

PAIR = "EsrqqdUca8GzxqX2FRwsdUXFk9icAJdsbm8F1u9efLEN"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
COIN = "B3A4k63nD5SD4w9S718ffTgx3xcQ3WUHN6y3GyQG5kBA"
SOURCE_FILL = 5.2431874719039734e-05          # starcatcher444's average fill, USD per token
OUT_ATOMIC = 299750304109                      # 6 decimals: 299,750.3 tokens for $20


class Trace:
    def __init__(self, pick_pair=PAIR, quote_side=9643.54, fallback_on=False):
        pick = {"pair": pick_pair, "quote_side_usd": quote_side}
        self.events = [{"name": "dex_pair_selection", "fallback_on": fallback_on,
                        "liquidity_pick": pick, "activity_pick": dict(pick, quote_side_usd=1.0)}]

    def mark_substage(self, name, **kw):
        self.events.append(dict(kw, name=name))

    def shadow_context(self):
        return {"gate_events": [dict(e) for e in self.events]}

    def last(self, name):
        return next(e for e in reversed(self.events) if e["name"] == name)


class Snapshot:
    def __init__(self, decimals):
        self.atomic, self.decimals, self.human = 0, decimals, 0.0


def _run(trace, *, decimals=6, source_price=SOURCE_FILL):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=COIN, source_user_id="u1", strategy="alpha_b", notional=20.0,
                            reserved_at="t", source_price=source_price)
    token_read = threading.Event()

    def token_fn(mint):
        token_read.set()
        return Snapshot(decimals)

    def quote_fn(mint, usd):
        token_read.wait(2.0)                     # the overlapped read lands before the gate, as live
        return {"inAmount": "20000000", "outAmount": str(OUT_ATOMIC), "otherAmountThreshold": str(OUT_ATOMIC),
                "priceImpactPct": "0.2337388655686826",
                "routePlan": [{"percent": 100, "swapInfo": {
                    "ammKey": PAIR, "label": "Meteora DAMM v2", "inputMint": USDC, "outputMint": COIN,
                    "inAmount": "20000000", "outAmount": str(OUT_ATOMIC)}}]}
    never = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refused before this"))
    return ex.execute_entry(
        c, pending_id=pid, mint=COIN, usd_amount=20.0, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-10-02T12:49:49Z", quote_fn=quote_fn, reverse_quote_fn=never, sign_fn=never,
        send_fn=never, confirm_fn=never, usdc_fn=lambda: 100.0, token_fn=token_fn,
        impact_cap_pct=12.0, trace=trace)


def test_packz_keeps_its_machine_reason_and_records_the_honest_cause():
    trace = Trace()
    res = _run(trace)
    assert res["reason"] == "price impact 23.37% over 12.0% cap"           # byte-stable
    row = trace.last("impact_cap_route")
    ours = 20.0 / (OUT_ATOMIC / 10 ** 6)
    assert abs(row["ran_pct"] - (ours / SOURCE_FILL - 1) * 100) < 1e-9 and round(row["ran_pct"]) == 27
    assert "size_impact_pct" not in row and "quote_side_usd" not in row    # no DexScreener depth
    assert row["source_price"] == SOURCE_FILL and row["buy_decimals"] == 6


def test_the_phone_reads_the_honest_cause():
    trace = Trace()
    res = _run(trace)
    view = {"status": "aborted", "reason": res["reason"], "network_id": "solana", "ticker": "PACKZ",
            "source_handle": "starcatcher444", "source_size_usd": 431.0, "origin_event_id": "onchain:x",
            "requested_usd": 20.0,
            "gate_evidence": [{k: v for k, v in e.items() if not isinstance(v, (list, dict))}
                              for e in trace.events]}
    why = next(l for l in render_entry_notification(view).splitlines() if l.startswith("Why: "))
    assert why == ("Why: price already ran +27.3% above starcatcher444's fill "
                   "(Jupiter vs pre-buy price 23.4%, cap 12%)")


def test_unknown_source_fill_or_decimals_fall_back_to_the_pre_buy_wording():
    for kw in ({"source_price": None}, {"source_price": 0.0}, {"decimals": None}):
        trace = Trace()
        res = _run(trace, **kw)
        row = trace.last("impact_cap_route")
        assert row["ran_pct"] is None
        view = {"status": "aborted", "reason": res["reason"], "network_id": "solana",
                "origin_event_id": "onchain:x", "gate_evidence": [
                    {k: v for k, v in e.items() if not isinstance(v, (list, dict))} for e in trace.events]}
        why = next(l for l in render_entry_notification(view).splitlines() if l.startswith("Why: "))
        assert why == "Why: Jupiter vs pre-buy price 23.4% (cap 12%)"


def test_a_failing_cause_never_strands_the_reservation(monkeypatch):
    """Display code: if it raises, the refusal and the pending release still happen as before."""
    def boom(*a, **k):
        raise RuntimeError("display bug")
    monkeypatch.setattr(ex, "_impact_cause", boom)
    released, real = [], ex.m.release_pending
    monkeypatch.setattr(ex.m, "release_pending", lambda *a, **k: released.append(a[1:]) or real(*a, **k))
    trace = Trace()
    res = _run(trace)
    assert res["status"] == "aborted" and res["reason"] == "price impact 23.37% over 12.0% cap"
    assert len(released) == 1                                  # the reservation is released once
    assert "ran_pct" not in trace.last("impact_cap_route")


def test_a_small_run_prints_its_decimal_not_plus_zero():
    view = {"status": "aborted", "reason": "price impact 12.50% over 12.0% cap", "network_id": "solana",
            "source_handle": "w", "origin_event_id": "x",
            "gate_evidence": [{"name": "impact_cap_route", "ran_pct": 0.3}]}
    why = next(l for l in render_entry_notification(view).splitlines() if l.startswith("Why: "))
    assert why == "Why: price already ran +0.3% above w's fill (Jupiter vs pre-buy price 12.5%, cap 12%)"


def test_another_reason_renders_as_before():
    view = {"status": "refused", "reason": "not_approved", "network_id": "solana", "origin_event_id": "x"}
    assert "Why: wallet not armed on this chain" in render_entry_notification(view)


def test_the_entry_view_keeps_the_cause_fields_for_the_renderer():
    import json
    from sniper.services.memecoin_entry_view import _gate_evidence
    trace = Trace()
    _run(trace)
    rows = _gate_evidence({"trace_json": json.dumps({"substage_events": trace.events})})
    row = next(r for r in rows if r["name"] == "impact_cap_route")
    assert {"ran_pct", "impact_pct", "cap"} <= set(row)


# THE HONEST GATE (operator yes 2026-10-02 ~14:00Z, PACKZ ref 298875ad): when our price over the
# source's fill is known it decides the impact cap; it only lifts a Jupiter refusal, never adds one.
OURS = 20.0 / (OUT_ATOMIC / 10 ** 6)


def test_a_run_under_the_cap_is_admitted_although_jupiter_is_over_it():
    trace = Trace()
    res = _run(trace, source_price=OURS / 1.10)          # ran +10% vs cap 12%; Jupiter 23.37%
    row = trace.last("impact_cap_route")
    assert row["admitted_on_ran"] is True and row["gate_basis"] == "ran"
    assert not str(res.get("reason", "")).startswith("price impact ")   # went past the cap


def test_a_run_over_the_cap_is_still_refused_with_the_same_machine_reason():
    trace = Trace()
    res = _run(trace, source_price=OURS / 1.13)          # ran +13% vs cap 12%
    assert res["reason"] == "price impact 23.37% over 12.0% cap"
    assert trace.last("impact_cap_route")["admitted_on_ran"] is False


def test_an_unknown_run_keeps_jupiters_refusal():
    for kw in ({"source_price": None}, {"source_price": 0.0}, {"decimals": None}):
        trace = Trace()
        res = _run(trace, **kw)
        row = trace.last("impact_cap_route")
        assert row["admitted_on_ran"] is False and row["gate_basis"] == "jupiter"
        assert res["reason"] == "price impact 23.37% over 12.0% cap"
