"""B1 (spec 2026-10-01-memecoin-slot-lane-routed-venues-design.md, Alpha GO): a ROUTED pump buy whose
PumpSwap pool the cache does not know is learned and converted again, instead of handed off at once.

MEMECOIN_SLOT_LANE_ROUTED_POOL_LEARN (default 0). With it on, the MissLearner reads the pool and,
inside its budget, runs pump_slot_buy on the routed buy AGAIN with every check (the coin's own pool,
a SOL quote, migrated). A buy that then qualifies goes to the lane; anything else writes its routed
row as before. ONE SOURCE BUY, ONE FUNDED PATH in every outcome.
"""
import pytest

from sniper.services import memecoin_slot_lane as lane

from test_slot_lane_routed_pump import TXS, Handoff, Pools, _facts, _routed  # noqa: E402


class LearnPools(Pools):
    """The PumpSwap pool cache: `learn(pool)` installs what the chain read would return."""

    def __init__(self, learned):
        super().__init__({})
        self.learned = learned

    def learn(self, pool):
        if pool not in self.learned:
            return False
        self.facts[pool] = self.learned[pool]
        return True


class NoTimer:
    """The budget timer, held: a test fires `_expire` itself."""

    def __init__(self, *_a, **_k):
        self.daemon = True

    def start(self):
        pass

    def cancel(self):
        pass


class LedgerRoutedHandoff:
    """The REAL RoutedHandoff on a temporary ledger: `written` lists the signatures whose routed
    detection row (basis slot_lane_routed) is in the ledger, so a deleted write cannot hide."""

    def __init__(self, tmp_path):
        import sqlite3
        import threading
        from sniper.persistence import db_memecoin as m
        self.path = str(tmp_path / "ledger.db")
        conn = sqlite3.connect(self.path)
        m.ensure_memecoin_tables(conn)
        conn.commit()
        conn.close()
        self.awaiting = []
        self.real = lane.RoutedHandoff(conn_factory=lambda: sqlite3.connect(self.path),
                                       wake=threading.Event())

    def write(self, buy):
        result = self.real.write(buy)
        assert result == "awaiting_signer_proof"
        self.awaiting.append(buy.signature)
        return result

    @property
    def written(self):
        import json
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute("SELECT signature, source_valuation_json FROM "
                                "onchain_wallet_detections ORDER BY id").fetchall()
        finally:
            conn.close()
        return [sig for sig, raw in rows if json.loads(raw or "{}").get("basis") == lane.ROUTED_BASIS]


def _setup(tx, learned, tmp_path):
    pools = LearnPools(learned)
    puts, lines = [], []
    routed_handoff = LedgerRoutedHandoff(tmp_path)
    reasons = []
    # start_slot_lane's own hand-off, through the module function it uses (Gamma MEDIUM)
    production = lane.make_learner_handoff(live=True, routed_handoff=routed_handoff, handoff=None,
                                           sol_usd_fn=lambda: 150.0)

    def handoff_fn(buy, reason):
        reasons.append(reason)
        production(buy, reason)
    learner = lane.MissLearner(pools={"pump_amm": pools}, put=puts.append, handoff_fn=handoff_fn,
                               log=lines.append, timer=NoTimer)
    return pools, puts, lines, reasons, routed_handoff, learner


def _route(tx, pools, routed_handoff, learner, *, pool_learn=True, puts=None, lines=None):
    puts = [] if puts is None else puts
    lines = [] if lines is None else lines
    lane.route_routed_buy(_routed(tx), handoff=routed_handoff, log=lines.append, put=puts.append,
                          amm_pools=pools, cpmm_on=False, pump_on=True, lane_live=True,
                          learner=learner, pool_learn=pool_learn)
    return puts, lines


def test_b1_a_learned_coin_pool_sends_the_routed_buy_to_the_lane_and_writes_no_routed_row(
        tmp_path):
    tx = TXS[0]
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, {tx["pool"]: _facts(tx)}, tmp_path)
    route_puts, route_lines = _route(tx, pools, routed_handoff, learner)
    assert route_puts == [] and routed_handoff.written == []        # the learner owns it now
    assert any("pool_unknown_learning" in line for line in route_lines)
    learner.learn_once(tx["pool"])
    (buy,) = puts
    from sniper.services.memecoin_slot_signal import SlotBuy
    assert isinstance(buy, SlotBuy) and not isinstance(buy, lane.RoutedPoolMiss), \
        "the lane gets pump_slot_buy's result, never the learner's placeholder (Gamma LOW 1)"
    assert buy == lane.pump_slot_buy(_routed(tx), pools)[0] or (
        buy.pool == tx["pool"] and buy.mint == tx["coin"]["mint"] and buy.trade_event_log)
    assert buy.venue == "pump_amm" and buy.signature == tx["signature"] and buy.routed is not None
    assert routed_handoff.written == [] and reasons == []
    assert learner.is_retry(tx["signature"]), "the lane hands off any non-fill of this retry"
    assert lines and lines[-1].endswith("-> retry routed=1")


@pytest.mark.parametrize("facts,reason", [
    ({"base_mint": "OtherCoin1111111111111111111111111111111111"}, "no_pump_event_into_coin"),
    ({"quote_mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}, "pump_quote_not_sol"),
    ({"pump_migrated": False}, "pump_pool_not_migrated"),
])
def test_b1_a_learned_pool_that_does_not_qualify_writes_the_routed_row_with_its_reason(
        facts, reason, tmp_path):
    tx = TXS[0]
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, {tx["pool"]: _facts(tx, **facts)}, tmp_path)
    _route(tx, pools, routed_handoff, learner)
    learner.learn_once(tx["pool"])
    assert puts == [] and reasons == [reason]
    assert routed_handoff.awaiting == [tx["signature"]] and routed_handoff.written == []


@pytest.mark.parametrize("outcome", ["expired", "unlearnable"])
def test_b1_a_miss_the_learner_cannot_settle_in_time_writes_the_routed_row(outcome, tmp_path):
    tx = TXS[0]
    learned = {} if outcome == "unlearnable" else {tx["pool"]: _facts(tx)}
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, learned, tmp_path)
    _route(tx, pools, routed_handoff, learner)
    if outcome == "expired":
        learner._expire(tx["signature"])
    else:
        learner.learn_once(tx["pool"])
    assert puts == [] and routed_handoff.awaiting == [tx["signature"]] and routed_handoff.written == []
    assert reasons == ["pool_unknown"]


def test_b1_knob_off_is_todays_immediate_hand_off(tmp_path):
    tx = TXS[0]
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, {tx["pool"]: _facts(tx)}, tmp_path)
    route_puts, route_lines = _route(tx, pools, routed_handoff, learner, pool_learn=False)
    assert route_puts == [] and routed_handoff.awaiting == [tx["signature"]] and routed_handoff.written == []
    assert not learner._waiting and not any("pool_unknown_learning" in l for l in route_lines)


def test_b1_a_known_non_sol_pool_is_not_learned_and_hands_off_at_once(tmp_path):
    """B1 is the pool miss only; a non-SOL quote waits for D1."""
    tx = TXS[0]
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, {}, tmp_path)
    pools.facts[tx["pool"]] = _facts(tx, quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    route_puts, _ = _route(tx, pools, routed_handoff, learner)
    assert route_puts == [] and routed_handoff.awaiting == [tx["signature"]] and routed_handoff.written == [] and not learner._waiting


def test_b1_a_full_learner_hands_off_at_once(tmp_path):
    tx = TXS[0]
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, {tx["pool"]: _facts(tx)}, tmp_path)
    learner.max_waiting = 0
    _route(tx, pools, routed_handoff, learner)
    assert routed_handoff.awaiting == [tx["signature"]] and routed_handoff.written == [] and puts == []


def test_b1_every_lane_send_records_its_venue_in_the_receipt_substage(tmp_path, monkeypatch):
    """Gamma's routed-venues pre-registration reads the venue of every lane send from the trace."""
    import contextlib
    import json
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from test_memecoin_slot_lane import _buy, _live_lane
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    with contextlib.closing(conn):
        ln, _sent = _live_lane(conn, tmp_path, monkeypatch)
        ln.handle(_buy())
        (raw,) = conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()
        receipt = next(e for e in json.loads(raw)["substage_events"] if e["name"] == "slot_receipt")
        assert receipt["venue"] == "pump_curve" and receipt["routed"] is False
        assert receipt["source_slot"] == 450_000_000



@pytest.mark.parametrize("outcome", ["expired", "unlearnable"])
def test_a_learner_settled_pool_miss_authorizes_the_mechanical_fallback_like_an_immediate_one(
        outcome, tmp_path, monkeypatch):
    """Codex N5 (night review 2026-10-06): the learner's budget expiry or failed read wrote the routed
    row but never published the routed_handoff marker, so fast-lane-only refused the confirmed
    watcher's fallback (slot_lane_verdict (None, None)); the same miss handed off at once got
    (None, 'routed_handoff'). Both now get the marker and the on-chain lane's wake."""
    import threading
    import time as _t
    monkeypatch.setattr(lane, "ROUTED_HANDOFFS", lane.PendingLegs())     # a fresh registry
    tx = TXS[0]
    learned = {} if outcome == "unlearnable" else {tx["pool"]: _facts(tx)}
    pools, puts, lines, reasons, routed_handoff, learner = _setup(tx, learned, tmp_path)
    routed_handoff.wake = threading.Event()
    _route(tx, pools, routed_handoff, learner)
    assert not lane.ROUTED_HANDOFFS.released(tx["signature"], now=_t.monotonic())
    if outcome == "expired":
        learner._expire(tx["signature"])
    else:
        learner.learn_once(tx["pool"])
    assert reasons == ["pool_unknown"]
    assert lane.ROUTED_HANDOFFS.released(tx["signature"], now=_t.monotonic())
    assert routed_handoff.wake.is_set()


def test_a_learned_pool_the_conversion_refuses_publishes_no_mechanical_marker(tmp_path, monkeypatch):
    tx = TXS[0]
    import time as _t
    monkeypatch.setattr(lane, "ROUTED_HANDOFFS", lane.PendingLegs())
    pools, puts, lines, reasons, routed_handoff, learner = _setup(
        tx, {tx["pool"]: _facts(tx, pump_migrated=False)}, tmp_path)
    _route(tx, pools, routed_handoff, learner)
    learner.learn_once(tx["pool"])
    assert reasons == ["pump_pool_not_migrated"]
    assert not lane.ROUTED_HANDOFFS.released(tx["signature"], now=_t.monotonic())
