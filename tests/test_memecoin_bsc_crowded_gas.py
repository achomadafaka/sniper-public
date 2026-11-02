"""56 fast-lane crowded gas bid (operator YES 2026-10-16 via Alpha; memecoin_bsc_crowded_gas).

Gamma's read of 18 lane sends: 11 of 13 reverts were fillable at the front of S+1, where the copiers that
crossed our floor paid 6-21 gwei against our 0.05. An early private-only send signs gasPrice =
max(static, min(25, 30) gwei); a quarter of early sends are held out at the static price; the 0.05 BNB
daily cap and every other not-eligible reason are decided before the arm draw; nothing else in the
signed bytes moves; the fill ledger carries each EVM lane entry's receipt fee (network_fee_wei)."""
import importlib
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import rlp

sys.path.insert(0, str(Path(__file__).parent))
from sniper.persistence import db_memecoin as db  # noqa: E402
from sniper.services import memecoin_bsc_crowded_gas as cg  # noqa: E402
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from sniper.services.memecoin_crowded_fee import holdout  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from test_memecoin_bsc_fast_lane_live import ACCT, Chain, _ledger, _live, _payload  # noqa: E402

GWEI = 10 ** 9
GAS = bsc.V2_BUY_GAS
TREATED = next("a%d" % i for i in range(100) if not holdout("a%d" % i))
HELD = next("a%d" % i for i in range(100) if holdout("a%d" % i))
BLOCK_MS = 1_792_000_000_000


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI", 25 * GWEI)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_WEI", 30 * GWEI)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI", 5 * 10 ** 16)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS", 600)


def _gas(alerts=None, day="2026-10-16T12:00:00+00:00"):
    sink = [] if alerts is None else alerts
    return cg.BscCrowdedGas(now_iso_fn=lambda: day, alert_fn=lambda t, x: sink.append(x))


def _arm(g, attempt=TREATED, *, private=True, block_ms=BLOCK_MS, head_at=(BLOCK_MS + 80) / 1000,
         now_ms=BLOCK_MS + 300, static=5 * 10 ** 7, float_ok=True):
    return g.arm(attempt, private_only=private, source_block_ms=block_ms, source_head_at=head_at,
                 now_ms=now_ms, static_wei=static, gas_limit=GAS, float_ok_fn=lambda price: float_ok)


# --- the arm ---------------------------------------------------------------------------------------

def test_an_early_private_send_bids_the_target():
    out = _arm(_gas())
    assert (out["arm"], out["reason"], out["gas_price_wei"]) == ("treated", "eligible", 25 * GWEI)
    assert out["fee_bound_wei"] == GAS * 25 * GWEI and out["early_ms"] == 300.0
    assert out["rule_version"] == "bsc-crowded-early-sha256-q1-v1"


def test_the_holdout_bids_static():
    g = _gas()
    out = _arm(g, HELD)
    assert (out["arm"], out["gas_price_wei"]) == ("holdout", 5 * 10 ** 7)
    assert g.status["holdout"] == 1 and g.status["treated"] == 0


def test_the_ceiling_clamps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI", 40 * GWEI)
    assert _arm(_gas())["gas_price_wei"] == 30 * GWEI


@pytest.mark.parametrize("kw, reason", [
    (dict(private=False), "not_private"),
    (dict(attempt=None), "no_attempt_id"),
    (dict(block_ms=None), "head_unknown"),
    (dict(head_at=None), "head_unknown"),
    (dict(head_at=(BLOCK_MS - 1) / 1000), "head_unknown"),            # head before its own block: skew
    (dict(head_at=(BLOCK_MS + 2001) / 1000), "head_unknown"),         # stale socket
    (dict(now_ms=BLOCK_MS - 1), "head_unknown"),                      # host clock behind the chain
    (dict(now_ms=BLOCK_MS + 601), "late"),
    (dict(static=26 * GWEI), "target_below_static"),
    (dict(float_ok=False), "float_for_bid"),
])
def test_every_other_send_is_not_eligible_and_bids_static(kw, reason):
    g = _gas()
    attempt = kw.pop("attempt", TREATED)
    static = kw.get("static", 5 * 10 ** 7)
    out = _arm(g, attempt, **kw)
    assert (out["arm"], out["reason"], out["gas_price_wei"]) == (None, reason, static)
    assert g.status["not_eligible"] == {reason: 1} and g.status["treated"] == 0


@pytest.mark.parametrize("knob, value", [
    ("MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI", None), ("MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI", 0),
    ("MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_WEI", None), ("MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI", None),
    ("MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS", None)])
def test_knob_off_or_malformed_is_todays_price(monkeypatch, knob, value):
    monkeypatch.setattr(cfg, knob, value)
    out = _arm(_gas())
    assert (out["arm"], out["reason"], out["gas_price_wei"]) == (None, "off", 5 * 10 ** 7)


def test_the_cap_binds_both_arms_and_pages_once():
    alerts = []
    g = _gas(alerts)
    g.sent("0x1", 3 * 10 ** 16)
    assert alerts == []
    g.sent("0x2", 3 * 10 ** 16)                          # 0.06 >= 0.05 BNB
    g.sent("0x3", 10 ** 16)
    assert len(alerts) == 1 and "cap reached" in alerts[0] and g.status["capped"] is True
    for attempt in (TREATED, HELD):
        out = _arm(g, attempt)
        assert (out["arm"], out["reason"], out["gas_price_wei"]) == (None, "daily_cap", 5 * 10 ** 7)


def test_a_receipt_fee_replaces_the_bound_and_midnight_resets():
    day = ["2026-10-16T23:59:00+00:00"]
    g = cg.BscCrowdedGas(now_iso_fn=lambda: day[0], alert_fn=lambda *a: None)
    g.sent("0xAB", GAS * 25 * GWEI)
    g.paid("0xab", 150_000 * 25 * GWEI)
    assert g.spent == 150_000 * 25 * GWEI
    g.paid("0xab", 1)                                   # once only
    g.paid("0xcd", 10 ** 18)                            # a send this object never counted
    assert g.spent == 150_000 * 25 * GWEI
    day[0] = "2026-10-17T00:00:01+00:00"
    _arm(g)
    assert g.spent == 0 and g.status["day"] == "2026-10-17"


def _attempt(conn, aid, events, *, source="bsc_fast_lane", tx="0xt", at="2026-10-16T10:00:00+00:00"):
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, signal_source, network_id, mint, "
                 "decision_started_at, tx_sig, trace_json) VALUES (?,?,?,?,?,?,?)",
                 (aid, source, "56", "0xm", at, tx, json.dumps({"substage_events": events})))


def test_a_restart_seeds_todays_treated_spend_from_the_ledger():
    conn = _ledger()
    armed = {"name": cg.SUBSTAGE, "arm": "treated", "fee_bound_wei": 10 ** 16}
    _attempt(conn, "p", [armed, {"name": cg.SUBSTAGE_PAID, "fee_wei": 4 * 10 ** 15}])     # receipt fee
    _attempt(conn, "b", [armed])                                                        # bound only
    _attempt(conn, "h", [dict(armed, arm="holdout")])
    _attempt(conn, "y", [armed], at="2026-10-15T23:00:00+00:00")                       # yesterday
    _attempt(conn, "u", [armed], tx=None)                                               # never sent
    _attempt(conn, "j", [armed], source="follow_activity")
    g = _gas()
    assert g.seed(conn) == 4 * 10 ** 15 + 10 ** 16 and g.status["spent_wei"] == g.spent


@pytest.mark.parametrize("env, want", [
    ({}, (25 * GWEI, 30 * GWEI, 5 * 10 ** 16, 600)),
    ({"MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI": "0"}, (0, 30 * GWEI, 5 * 10 ** 16, 600)),
    ({"MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI": ""}, (None, 30 * GWEI, 5 * 10 ** 16, 600)),
    ({"MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI": "x", "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_GWEI": "-1",
      "MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_BNB": "nan", "MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS": "inf"},
     (None, None, None, None)),
    ({"MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI": "6.5"}, (6_500_000_000, 30 * GWEI, 5 * 10 ** 16, 600)),
])
def test_the_knobs_parse_exactly_and_malformed_is_off_never_a_crash(monkeypatch, env, want):
    for name in ("MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_GWEI", "MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_GWEI",
                 "MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_BNB", "MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        importlib.reload(cfg)
        assert (cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI, cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_WEI,
                cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI, cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS) == want
    finally:
        monkeypatch.undo()
        importlib.reload(cfg)


# --- the paired head, watcher to lane --------------------------------------------------------------

def test_the_watcher_pairs_the_source_with_its_blocks_head(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watcher

    class State:
        def head_for(self, block):
            return {100: (1792000000.08, BLOCK_MS)}.get(block)
    seen = []
    monkeypatch.setattr(watcher, "_say", lambda line: None)
    monkeypatch.setattr(watcher, "PUSH_STATE", State())
    signal = lambda row, *a, **k: seen.append(row) or dict(reason="x", sent=False, decide_ms=0.1)  # noqa: E731
    watcher._signal_bsc_lane(signal, dict(tx_hash="0x1", buy={"mint": "0xm"}, log={"blockNumber": "0x64"}),
                             {"_receipt": {"blockNumber": "0x64"}}, 600, .1)
    watcher._signal_bsc_lane(signal, dict(tx_hash="0x2", buy={"mint": "0xm"}, log={"blockNumber": "0x65"}),
                             {"_receipt": {"blockNumber": "0x65"}}, 600, .1)
    assert seen[0]["source_head"] == (1792000000.08, BLOCK_MS) and "source_head" not in seen[1]
    monkeypatch.setattr(watcher, "PUSH_STATE", None)                 # poll mode: no pairing
    watcher._signal_bsc_lane(signal, dict(tx_hash="0x3", buy={"mint": "0xm"}, log={"blockNumber": "0x64"}),
                             {"_receipt": {"blockNumber": "0x64"}}, 600, .1)
    assert "source_head" not in seen[2]


@pytest.mark.parametrize("head, want", [
    ((1792000000.08, BLOCK_MS), {"source_block_ms": BLOCK_MS, "source_head_at": 1792000000.08}),
    ((1792000000.08, None), {}), (None, {}), ((True, BLOCK_MS), {}), ("x", {})])
def test_the_datagram_carries_the_paired_head_only_when_whole(head, want):
    assert bsc._source_head({"source_head": head}) == want


def test_the_lane_datagram_carries_the_head_the_watcher_paired():
    from test_memecoin_bsc_fast_lane import ROW, _decide, _synthetic
    receipt, mint, post = _synthetic()
    out = _decide(receipt, mint, post)
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    p = bsc.lane_payload(dict(row, source_head=(1792000000.08, BLOCK_MS)), out, decimals=18,
                         bnb_usd=600.0, bnb_usd_at=1.0, sent_epoch=2.0)
    assert (p["source_block_ms"], p["source_head_at"]) == (BLOCK_MS, 1792000000.08)
    p = bsc.lane_payload(row, out, decimals=18, bnb_usd=600.0, bnb_usd_at=1.0, sent_epoch=2.0)
    assert "source_block_ms" not in p and "source_head_at" not in p


# --- the live lane, end to end ---------------------------------------------------------------------

class FeeChain(Chain):
    """The receipt carries gasUsed and the signed gasPrice as effectiveGasPrice (BSC: no base fee)."""
    GAS_USED = 150_000

    def read(self, method, params):
        out = super().read(method, params)
        if method == "eth_getTransactionReceipt" and out is not None:
            price = int.from_bytes(rlp.decode(bytes.fromhex(self.raw[2:]))[1], "big")
            out = dict(out, gasUsed=hex(self.GAS_USED), effectiveGasPrice=hex(price))
        return out


def _lane_run(tmp_path, monkeypatch, *, arm_holdout, native=10 ** 18, crowded=True, now=None, reply=None):
    from sniper.persistence import db_memecoin as m
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE_FLOOR_AT_CAP_CHAINS", "56")
    monkeypatch.setattr(cg, "holdout", lambda attempt_id: arm_holdout)
    now = now or time.time()
    conn, p = _ledger(), _payload(source_block_ms=int(now * 1000) - 300, source_head_at=now - 0.22,
                                  bnb_usd_at=now, bnb_usd=600.0)
    chain = FeeChain(p)

    class Private:
        def send(self, raw, max_block=None, private_only=False):
            assert private_only is True
            from eth_utils import keccak
            if reply is not None:
                self.raw = raw
                return reply
            chain.raw = raw
            return ("accepted", "0x" + keccak(hexstr=raw).hex())
    live = _live(conn, chain, tmp_path, sender=Private(), host_clock=lambda: now)
    if crowded:
        live.crowded = cg.BscCrowdedGas(now_iso_fn=lambda: "2026-10-16T12:00:00+00:00",
                                        alert_fn=lambda *a: None)
    pending_id = m.reserve_pending(conn, mint=p["mint"], source_user_id="uid", strategy="alpha_b",
                                   notional=50.0, reserved_at="2026-10-02T00:00:00+00:00",
                                   network_id="56", signal_source="bsc_fast_lane")
    rec = {"_perf0": time.perf_counter(), "attempt_id": "att-1"}
    snap = dict(native_wei=native, weth_wei=0, nonce=7, gas_price_wei=10 ** 8, read_at=now - 1)
    result = live.execute_reserved(dict(p, snapshot=snap), rec, conn, pending_id, 50.0, decimals=18)
    fields = rlp.decode(bytes.fromhex(chain.raw[2:])) if chain.raw else None
    return result, rec, live, conn, fields


STATIC = 150_000_000                                    # snapshot 0.1 gwei x the 1.5 buffer


def _signed(fields):
    nonce, price, gas, to, value, data = (fields[0], fields[1], fields[2], fields[3], fields[4], fields[5])
    return dict(nonce=int.from_bytes(nonce, "big"), price=int.from_bytes(price, "big"),
                gas=int.from_bytes(gas, "big"), to="0x" + to.hex(), value=int.from_bytes(value, "big"),
                data="0x" + data.hex())


def test_a_treated_send_signs_the_bid_and_only_the_bid(tmp_path, monkeypatch):
    now = time.time()
    r1, rec1, live1, conn1, f1 = _lane_run(tmp_path / "t", monkeypatch, arm_holdout=False, now=now)
    r2, rec2, live2, conn2, f2 = _lane_run(tmp_path / "h", monkeypatch, arm_holdout=True, now=now)
    assert r1["status"] == r2["status"] == "filled", (r1, r2)
    t, h = _signed(f1), _signed(f2)
    assert (t.pop("price"), h.pop("price")) == (25 * GWEI, STATIC)
    assert t == h                          # nonce, gas limit, router, clip and min_out-bearing calldata
    assert t["gas"] == GAS and t["value"] == rec1["clip_wei"] == rec2["clip_wei"]
    assert rec1["min_out"] == rec2["min_out"]
    ev = rec1["execution_evidence"]["crowded_gas"]
    assert (ev["arm"], ev["gas_price_gwei"], ev["fee_bnb_est"]) == ("treated", 25.0, GAS * 25 / 1e9)
    assert rec2["execution_evidence"]["crowded_gas"]["arm"] == "holdout"


def test_the_receipt_fee_books_on_the_fill_and_trues_up_the_cap(tmp_path, monkeypatch):
    _r, rec, live, conn, _f = _lane_run(tmp_path, monkeypatch, arm_holdout=False)
    fee = FeeChain.GAS_USED * 25 * GWEI
    (got,) = conn.execute("SELECT network_fee_wei FROM memecoin_fills WHERE kind='entry'").fetchone()
    assert got == str(fee) and live.crowded.spent == fee
    assert rec["execution_evidence"]["crowded_gas"]["fee_bnb_actual"] == fee / 1e18


def test_a_holdout_fill_records_its_fee_and_spends_nothing(tmp_path, monkeypatch):
    _r, _rec, live, conn, f = _lane_run(tmp_path, monkeypatch, arm_holdout=True)
    (got,) = conn.execute("SELECT network_fee_wei FROM memecoin_fills WHERE kind='entry'").fetchone()
    assert got == str(FeeChain.GAS_USED * STATIC) and live.crowded.spent == 0


def test_a_send_every_member_rejected_spends_nothing_and_an_ambiguous_one_counts(tmp_path, monkeypatch):
    r, rec, live, _c, _f = _lane_run(tmp_path / "r", monkeypatch, arm_holdout=False,
                                     reply=("rejected", "invalid sender"))
    assert r["status"] == "aborted" and rec["gas_bid_wei"] == 25 * GWEI and live.crowded.spent == 0
    r, rec, live, _c, _f = _lane_run(tmp_path / "a", monkeypatch, arm_holdout=False,
                                     reply=("ambiguous", "timeout"))
    assert live.crowded.spent == GAS * 25 * GWEI and live.crowded.status["sent_treated"] == 1


def test_no_crowded_object_is_todays_send(tmp_path, monkeypatch):
    _r, rec, _live_, _conn, f = _lane_run(tmp_path, monkeypatch, arm_holdout=False, crowded=False)
    assert _signed(f)["price"] == STATIC and "gas_bid_wei" not in rec


def test_a_float_that_covers_the_clip_but_not_the_bid_bids_static(tmp_path, monkeypatch):
    clip = int(50.0 / 600.0 * 10 ** 18)
    native = clip + 10 ** 15                    # the static 3-swap reserve 1.2e14 fits, the bid's 1e16 does not
    r, rec, live, _c, f = _lane_run(tmp_path, monkeypatch, arm_holdout=False, native=native)
    assert r["status"] == "filled" and _signed(f)["price"] == STATIC
    assert rec["execution_evidence"]["crowded_gas"]["reason"] == "float_for_bid"
    assert live.crowded.spent == 0


def test_the_locked_float_check_holds_the_bid_once_and_the_reserve_at_the_snapshot_price():
    """float_covers keeps a 3-swap reserve; the bid pays on THIS swap only, so the lock checks
    native >= clip + gas x bid + 2 x gas x snapshot price + low, the arm's check exactly."""
    from sniper.services.memecoin_rh_fast_lane import bid_extra_wei, inputs_reason
    now, clip, snap_price, bid, low = time.time(), 10 ** 17, 10 ** 8, 25 * GWEI, 7
    need = clip + GAS * bid + 2 * GAS * snap_price + low
    kw = dict(generation=0.0, now_host=now, clip_wei=clip, gas_limit=GAS, low_wei=low,
              max_snapshot_age_s=30.0, eth_usd_max_age_s=60.0)
    for native, ok in ((need, True), (need - 1, False)):
        snap = dict(native_wei=native, gas_price_wei=snap_price, read_at=now - 1)
        payload = {"snapshot": snap, "eth_usd": 600.0, "eth_usd_at": now}
        extra = bid_extra_wei(snap, GAS, bid)
        assert (inputs_reason(payload, extra_gas_wei=extra, **kw) is None) is ok
        assert bsc.inputs_float_ok(snap, clip, GAS, bid, low) is ok
        assert inputs_reason(payload, **kw) is None                  # no bid: today's check
    assert bid_extra_wei({"gas_price_wei": None}, GAS, bid) == 0 and bid_extra_wei({}, GAS, bid) == 0
    assert bsc.inputs_float_ok({"native_wei": None, "gas_price_wei": 1}, clip, GAS, bid, low) is False


def test_the_lane_heartbeat_carries_the_test(tmp_path):
    class Live:
        crowded = _gas()
    _arm(Live.crowded, private=False)
    out = bsc.lane_counters(Live())
    assert out["crowded_gas"]["not_eligible"] == {"not_private": 1}
    assert out["crowded_gas"]["rule_version"] == cg.RULE_VERSION
