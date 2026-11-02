"""The add-on refusal (memecoin_addon_guard; operator 2026-10-17 'Fix it, it happened again',
'At least for him'). Fixtures are notdecu's (pump:4vw54Bm...) live rows of 2026-10-17: three ~$305
first buys we missed (slot send BuySlippageBelowMinTokensOut, backup refused the chase), each
followed 15-22 s later by a ~$25 add-on we filled and lost ~30% on (#73925, #73928, #73929); his
$203 first buy of 6XsC91 (#73924) and DexGemsReal's $647 buy (#73927) must pass."""
import pytest

from sniper.services import memecoin_addon_guard as G
from sniper.settings import memecoin as cfg

NOTDECU = "pump:4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
DEXGEMS = "0bbbf8fd-dea4-5561-bd2e-54f98a573ff8"
#            mint,                                                 first (tx, usd, price),                         add-on (tx, usd)
CASES = {
    73925: ("2yEmn7cDuKMRGgJYQRm4Nj8rxMZvGGtA8JgAD8Lqpump", ("mFXRoxtKfP4MtMQPphY35ShyQD3ajoZNrfJMHgZNGCUHvR1YLaKFEU21asj9FpZLoEDjnwtkbsZzaqngRXASYqQ", 304.6249594320502, 1.8289055171213623e-05),
            ("3D1kxaf9mMQxFvfVqHqirsfL3WgznKRsNwDFcCc2KqKtSYoGC6hTcMMPjMDzPRAo5f3VB9HLvXkWvnyzaotnGCBh", 25.7051091825)),
    73928: ("EBkmhwnFHxg6zWfghfXyHxxSnugPxTcDVJqYUXQvpump", ("5vSECFPus6", 303.87823543213506, 1.0542020629454294e-05),
            ("4uYEemc4Jq2Bqx9JFgaEoLmZk3weyXtkNZt6hM9mLwimqHAV7WnaVb7xCB3ZcQygGHdc5M5S97ow3ajGyQvMYXyh", 25.6503468375)),
    73929: ("HFsxFfXgiX2MS29wSuezZHRKDwStbTJEQdKqJMPWpump", ("567KW6V5p7", 304.15159276543733, 1.6481518939853322e-05),
            ("NKKyGQjXvPLDfNtSHoRJeygicrnjWLPHbUdckbZhSmYF3zDAzzLb38zMgXhxGHisY3rm5z5aqbjvGzTK47x9J7Q", 25.65664794)),
}


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_WINDOW_S", 600.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_MAX_SHARE_PCT", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", frozenset({NOTDECU}))


class Clock:
    def __init__(self, t=1_792_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _ring():
    clock = Clock()
    return G.AddonRing(clock=clock), clock


@pytest.mark.parametrize("pid", sorted(CASES))
def test_notdecus_three_add_ons_refuse_with_the_rule_and_its_numbers(on, pid):
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[pid]
    ring, clock = _ring()
    assert G.judge("solana", NOTDECU, mint, ftx, fusd, ring=ring) is None          # his first buy passes
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    clock.t += 18.0
    out = G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring)
    assert out is not None
    reason, detail = out
    assert reason.startswith("addon_after_missed_entry prior_net=$%.1f addon=$%.1f share=" % (fusd, ausd))
    assert reason.endswith("first=%.2e" % fprice)
    assert detail["first_buy_usd"] == round(fusd, 2) and detail["first_buy_price"] == fprice
    assert detail["addon_share_pct"] == round(100 * ausd / (fusd + ausd), 1)


def test_73924_his_first_buy_of_a_coin_passes(on):
    ring, _ = _ring()
    assert G.judge("solana", NOTDECU, "6XsC91TSAWe4ma57cRKkMe3ut5wPUWnd5dyV2wAUpump",
                   "4T52LVWkeXsLx", 202.93654180901422, ring=ring) is None


def test_73927_an_unscoped_wallet_passes_and_with_star_its_first_buy_still_passes(on, monkeypatch):
    ring, _ = _ring()
    mint = "6tsykNmR3WDqouj9uVUFZRgR2Kpai4F1Qm1gAcirpump"
    ring.note_buy("solana", DEXGEMS, mint, "earlier", 900.0, 1e-4)
    assert G.judge("solana", DEXGEMS, mint, "61hc1VpcmS6f", 646.94, held_fn=lambda: False, ring=ring) is None
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", frozenset({"*"}))
    fresh, _ = _ring()
    assert G.judge("solana", DEXGEMS, mint, "61hc1VpcmS6f", 646.94, ring=fresh) is None


def test_a_larger_buy_than_his_net_passes_and_the_share_knob_moves_the_line(on, monkeypatch):
    mint, (ftx, fusd, fprice), _ = CASES[73925]
    ring, _ = _ring()
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    assert G.judge("solana", NOTDECU, mint, "bigger", 400.0, held_fn=lambda: False, ring=ring) is None
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_MAX_SHARE_PCT", 5.0)            # only under 5% of his net
    assert G.judge("solana", NOTDECU, mint, "add", 25.7, held_fn=lambda: False, ring=ring) is None
    assert G.judge("solana", NOTDECU, mint, "add", 10.0, held_fn=lambda: False, ring=ring) is not None


@pytest.mark.parametrize("ids,knob", [(frozenset(), True), (frozenset({NOTDECU}), False)])
def test_no_scope_or_the_knob_off_is_today(on, monkeypatch, ids, knob):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", ids)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE", knob)
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[73925]
    ring, _ = _ring()
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is None


def test_holding_the_coin_passes_and_a_failed_holding_read_passes(on):
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[73925]
    ring, _ = _ring()
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: True, ring=ring) is None
    def broken():
        raise RuntimeError("database is locked")
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=broken, ring=ring) is None
    assert ring.stats["passed_held"] == 1 and ring.stats["held_read_errors"] == 1


def test_the_same_trade_from_another_lane_is_not_his_prior_net(on):
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[73925]
    ring, _ = _ring()
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)                     # slot lane
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)                     # on-chain backup, same tx
    assert ring.prior("solana", NOTDECU, mint, atx)[0] == fusd
    ring.note_buy("solana", NOTDECU, mint, atx, ausd, 2.36e-05)                   # the add-on itself
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is not None


def test_absent_is_not_zero_unknown_size_window_expiry_and_a_sell_all_pass(on):
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[73925]
    ring, clock = _ring()
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is None   # nothing known
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    assert G.judge("solana", NOTDECU, mint, atx, None, held_fn=lambda: False, ring=ring) is None   # size unknown
    ring.note_buy("solana", NOTDECU, mint, None, 999.0, fprice)                   # no tx: never noted
    assert ring.prior("solana", NOTDECU, mint, atx)[0] == fusd
    clock.t += 601.0
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is None   # window
    ring.note_buy("solana", NOTDECU, mint, ftx + "2", fusd, fprice)
    ring.note_sell("solana", NOTDECU, mint)
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is None   # he sold


def test_the_ring_is_bounded(on, monkeypatch):
    monkeypatch.setattr(G, "MAX_KEYS", 3)
    ring, _ = _ring()
    for i in range(5):
        ring.note_buy("solana", NOTDECU, "M%d" % i, "tx%d" % i, 10.0, 1.0)
    assert len(ring._keys) == 3 and ring.stats["evicted"] == 2


def test_the_runner_hook_refuses_before_any_quote_with_an_attempt_row(on, monkeypatch):
    """The hook leaves a memecoin_entry_attempts row refused with the rule and its numbers (Gamma's
    forward read), and never reaches execution."""
    import test_memecoin_source_buy_floor as F
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", frozenset({"u1"}))
    monkeypatch.setattr(G, "RING", G.AddonRing())
    conn = F._conn()
    G.RING.note_buy("solana", "u1", "So1Mint", "4" * 88, 308.0, 0.2)
    ev = F._event(25.7)
    ev.fomo_event_id = "onchain:" + "5" * 88
    res, reached = F._run(conn, ev)
    assert res["status"] == "rejected" and res["reason"].startswith("addon_after_missed_entry prior_net=$308.0")
    assert reached == []
    row = conn.execute("SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts").fetchone()
    assert row["terminal_status"] == "refused" and row["terminal_reason"].startswith("addon_after_missed_entry")


def test_the_slot_lane_judges_before_any_reservation_and_writes_the_attempt_row():
    """Source pin: the lane's refusal returns through _finish_traced before _reserve_and_execute."""
    import inspect
    from sniper.services import memecoin_slot_lane as L
    src = inspect.getsource(L.SlotLane._traced)
    assert src.index('facts.get("addon_refusal")') < src.index("self._reserve_and_execute(")
    handle = inspect.getsource(L.SlotLane._handle)
    assert handle.index("_addon_judge(") < handle.index("_addon_ring.note_buy(")


def test_the_backup_lane_judging_the_same_first_buy_never_sees_it_as_its_own_prior(on):
    """The slot lane notes his first buy; the on-chain backup then judges that same transaction
    (reported a cent smaller): it is his first buy, not an add-on to itself."""
    mint, (ftx, fusd, fprice), _ = CASES[73925]
    ring, _ = _ring()
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    assert G.judge("solana", NOTDECU, mint, ftx, fusd - 0.5, held_fn=lambda: False, ring=ring) is None


def test_a_source_tx_that_failed_or_never_landed_leaves_his_prior_net(on, monkeypatch):
    """Gamma LOW on 13da10592: LandingWatch's source_failed / source_not_landed verdict drops the tx,
    so his re-send after his own failed transaction is not judged an add-on to it."""
    from types import SimpleNamespace
    from sniper.services import memecoin_rabbit_signal as R
    mint, (ftx, fusd, fprice), (atx, ausd) = CASES[73925]
    ring, _ = _ring()
    monkeypatch.setattr(G, "RING", ring)
    ring.note_buy("solana", NOTDECU, mint, ftx, fusd, fprice)
    watch = R.LandingWatch(backstop=SimpleNamespace(verdict=lambda out: None), head_fn=lambda: (None, None), status={})
    watch.expect(signature=ftx, owner="O", source_user_id=NOTDECU, mint=mint, slot=100, frame_mono=1.0)
    watch._verdict(watch._watch[ftx], R.SOURCE_FAILED, "processed_feed")
    assert ring.prior("solana", NOTDECU, mint, atx) is None
    assert G.judge("solana", NOTDECU, mint, atx, ausd, held_fn=lambda: False, ring=ring) is None


def test_an_evm_runner_hook_add_on_refuses_with_an_attempt_row(on, monkeypatch):
    """4663: a mixed-case 0x mint, 0x transactions in the event ids; his first buy noted, then a
    smaller add-on refuses before execution with a refused row and the addon_refusal substage."""
    import json
    import test_memecoin_source_buy_floor as F
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ADDON_REFUSE_WALLET_IDS", frozenset({"u1"}))
    monkeypatch.setattr(G, "RING", G.AddonRing())
    mint = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"
    first_tx, add_tx = "0x" + "a1" * 32, "0x" + "b2" * 32
    G.RING.note_buy("4663", "u1", mint, first_tx, 300.0, 0.002)
    assert G.RING.prior("4663", "u1", mint.lower(), add_tx)[0] == 300.0          # case-insensitive coin key
    conn = F._conn()
    m_conn = conn
    from sniper.persistence import db_memecoin as m
    m.set_wallet_real_approved(m_conn, "u1", True, chains=["solana", "4663"], force=True)
    ev = F._event(25.0)
    ev.network_id, ev.mint = "4663", mint
    ev.fomo_event_id = "onchain:4663:%s:%s" % (add_tx.upper().replace("0X", "0x"), mint)
    reached = []
    res = F.runner.real_entry_hook(conn, ev, {"4663": F._context(reached)}, now_fn=lambda: F.NOW,
                                   trade_size=10.0, max_age_s=120)
    assert res["status"] == "rejected" and res["reason"].startswith("addon_after_missed_entry prior_net=$300.0"), res
    assert reached == []
    row = conn.execute("SELECT terminal_status, terminal_reason, trace_json FROM memecoin_entry_attempts").fetchone()
    assert row["terminal_status"] == "refused" and row["terminal_reason"].startswith("addon_after_missed_entry")
    names = [s.get("name") for s in json.loads(row["trace_json"])["substage_events"]]
    assert "addon_refusal" in names
