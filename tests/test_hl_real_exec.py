"""Pure decision core for the HL real copy leg (no I/O)."""
from sniper.hyperlive import real_exec as RE


# ── cloid + eligibility (plan Task 3) ────────────────────────────────────────

def test_cloid_deterministic_128bit():
    a = RE.derive_cloid("dep1", 42, "open")
    assert a == RE.derive_cloid("dep1", 42, "open")
    assert a != RE.derive_cloid("dep1", 42, "close")
    assert a != RE.derive_cloid("dep2", 42, "open")
    assert a.startswith("0x") and len(a) == 34        # 0x + 32 hex = 128 bit
    int(a, 16)                                        # valid hex


def _row(**kw):
    base = dict(target="0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8",
                copy_tier="proven", is_cold_start=0, detect_ts_ms=2000)
    base.update(kw)
    return base


ALLOW = ["0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"]


def test_eligibility_gates():
    assert RE.eligibility(_row(), allow_targets=ALLOW, epoch_ms=1500) == (True, "ok")
    assert RE.eligibility(_row(target="0xdead"), allow_targets=ALLOW, epoch_ms=0)[1] == "target_not_allowed"
    assert RE.eligibility(_row(copy_tier="watch_fallback"), allow_targets=ALLOW, epoch_ms=0)[1] == "tier_not_proven"
    assert RE.eligibility(_row(is_cold_start=1), allow_targets=ALLOW, epoch_ms=0)[1] == "cold_start"
    assert RE.eligibility(_row(detect_ts_ms=100), allow_targets=ALLOW, epoch_ms=1500)[1] == "pre_epoch"


def test_eligibility_case_insensitive_target():
    up = _row(target="0xC9C78160120CD6ECF3DEA892E9AE5AF4115BF5F8")
    assert RE.eligibility(up, allow_targets=ALLOW, epoch_ms=0) == (True, "ok")


# ── sizing + entry rails (plan Task 4) ───────────────────────────────────────

def test_size_coin_floors_and_min_notional():
    assert RE.size_coin(15.0, 100.0, 2) == 0.15
    assert RE.size_coin(15.0, 7.0, 1) == 2.1          # floor(2.142.., 1dp)
    assert RE.size_coin(15.0, 200000.0, 4) is None    # floors to 0 → below the $10 HL min
    assert RE.size_coin(15.0, 200000.0, 5) == 7e-05   # $14 notional — still valid
    assert RE.size_coin(15.0, 0.0, 2) is None
    assert RE.size_coin(15.0, -1.0, 2) is None


def test_chase_bps_signed_by_side():
    assert round(RE.chase_bps("long", 100.0, 101.0)) == 100    # paid up = positive chase
    assert round(RE.chase_bps("short", 100.0, 99.0)) == 100
    assert RE.chase_bps("long", 100.0, 99.0) < 0               # better than paper = negative


class _Cfg:
    HL_REAL_PER_POS_USD = 15.0
    HL_REAL_MAX_CONCURRENT = 2
    HL_REAL_MAX_TOTAL_NOTIONAL = 30.0
    HL_REAL_MAX_CHASE_BPS = 100.0


def _dec(**kw):
    base = dict(enabled=True, breaker_latched=False, open_n=0, total_notional=0.0,
                mark=100.0, sz_decimals=2, cfg=_Cfg)
    base.update(kw)
    return RE.entry_decision({"side": "long", "entry_mark": 100.0}, **base)


def test_entry_rails_order_and_reasons():
    ok = _dec()
    assert ok.action == "submit" and ok.sz == 0.15 and ok.reason == "ok"
    assert _dec(enabled=False).reason == "disabled"
    assert _dec(breaker_latched=True).reason == "breaker_latched"
    assert _dec(open_n=2).reason == "max_concurrent"
    assert _dec(total_notional=20.0).reason == "max_total_notional"   # 20+15 > 30
    assert _dec(mark=101.5).reason == "chase_guard"                   # 150bps > 100
    # mark stays at paper (no chase) but flooring to 0 decimals lands under $10
    assert _dec(sz_decimals=0).reason == "below_min_notional"
    for d in (_dec(enabled=False), _dec(open_n=2)):
        assert d.action == "skip" and d.sz is None


# ── order-result normalization + settlement + accounting (plan Task 5) ───────

def test_normalize_order_result_states():
    ok = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.15", "avgPx": "101.0"}}]}}}
    r = RE.normalize_order_result(ok, requested_sz=0.15)
    assert r == {"state": "filled", "fill_px": 101.0, "fill_sz": 0.15}
    part = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.10", "avgPx": "101.0"}}]}}}
    assert RE.normalize_order_result(part, requested_sz=0.15)["state"] == "partial"
    err = {"status": "ok", "response": {"data": {"statuses": [{"error": "slippage"}]}}}
    assert RE.normalize_order_result(err, requested_sz=0.15)["state"] == "rejected"
    assert RE.normalize_order_result(RuntimeError("timeout"), 0.15)["state"] == "ambiguous"
    assert RE.normalize_order_result({"status": "err"}, 0.15)["state"] == "ambiguous"


def test_settle_from_status():
    # Real HL shape: query_order_by_cloid -> {"status":"order","order": INNER}; the adapter
    # hands INNER = {"order": {...fields...}, "status": "filled", "statusTimestamp": ...}
    assert RE.settle_from_status({"order": {"oid": 1}, "status": "filled"}) == "filled"
    assert RE.settle_from_status({"order": {"oid": 1}, "status": "canceled"}) == "rejected"
    assert RE.settle_from_status({"order": {"oid": 1}, "status": "rejected"}) == "rejected"
    assert RE.settle_from_status({"order": {"oid": 1}, "status": "marginCanceled"}) == "rejected"
    assert RE.settle_from_status({"order": {"oid": 1}, "status": "open"}) is None
    assert RE.settle_from_status(None) is None


def test_close_accounting_signed_drift():
    out = RE.close_accounting({"fill_px": 100.0, "fill_sz": 0.15},
                              {"fill_px": 102.0, "fill_sz": 0.15},
                              fees_usd=0.03, paper_pnl_bps=200.0, side="long")
    assert abs(out["realized_usd"] - 0.27) < 1e-9
    assert abs(out["real_net_bps"] - 180.0) < 0.01
    assert abs(out["drift_bps"] - (-20.0)) < 0.01
    short = RE.close_accounting({"fill_px": 100.0, "fill_sz": 0.15},
                                {"fill_px": 98.0, "fill_sz": 0.15},
                                fees_usd=0.0, paper_pnl_bps=100.0, side="short")
    assert short["realized_usd"] > 0 and abs(short["real_net_bps"] - 200.0) < 0.01
    assert abs(short["drift_bps"] - 100.0) < 0.01


def test_close_accounting_partial_uses_min_sz():
    out = RE.close_accounting({"fill_px": 100.0, "fill_sz": 0.15},
                              {"fill_px": 101.0, "fill_sz": 0.10},
                              fees_usd=0.0, paper_pnl_bps=0.0, side="long")
    assert abs(out["realized_usd"] - 0.10) < 1e-9     # 1.0 * 0.10


# ── reconcile diff (plan Task 6) ─────────────────────────────────────────────

def _book(**kw):
    base = dict(copy_id=1, coin="ZEC", side="long", fill_px=100.0, fill_sz=0.15,
                cloid="0xabc")
    base.update(kw)
    return base


def test_reconcile_clean():
    assert RE.reconcile([_book()], {"ZEC": {"szi": 0.15}}) == []


def test_reconcile_missing_on_exchange():
    faults = RE.reconcile([_book()], {})
    assert faults == [{"kind": "missing_on_exchange", "coin": "ZEC", "copy_id": 1}]


def test_reconcile_unknown_on_exchange():
    faults = RE.reconcile([], {"SOL": {"szi": 1.0}})
    assert faults == [{"kind": "unknown_on_exchange", "coin": "SOL", "copy_id": None}]


def test_reconcile_size_and_sign_mismatch():
    assert RE.reconcile([_book()], {"ZEC": {"szi": 0.05}})[0]["kind"] == "size_mismatch"
    assert RE.reconcile([_book()], {"ZEC": {"szi": -0.15}})[0]["kind"] == "size_mismatch"
    # short book position must match negative szi
    assert RE.reconcile([_book(side="short")], {"ZEC": {"szi": -0.15}}) == []
