"""Backfill runner (spec 2026-10-27-helius-history-backfill-design.md sections 3, 4, 6). Offline: injected fake transport and decoder, no provider, no network."""
import json
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_backfill_ledger as L
from sniper.services import memecoin_desk_backfill_runner as R
from sniper.services import memecoin_desk_backfill_store as S

NOW = datetime(2026, 10, 28, 3, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-27"
BT = int(datetime(2026, 10, 27, 12, tzinfo=timezone.utc).timestamp())
COSTS = {R.METHOD: 50}


def cycle(tmp_path, **k):
    c = dict(attested_at="2026-10-27T12:00:00Z", attested_by="op", epoch_id="e1", epoch_start="2026-10-27T00:00:00Z", epoch_end="2026-11-28T00:00:00Z", plan_credits=10_000_000,
             used_at_activation=0, month_cap=5_000_000, funded_reserve=2_000_000)
    c.update(k)
    p = tmp_path / "cycle.json"
    p.write_text(json.dumps(c))
    return L.load_cycle(p, now=NOW)


def swap(i, pool="P1", pre=None, post=None, **k):
    r = dict(network_id="solana", venue="pumpswap", pool=pool, mint="M", signature="s%s%d" % (pool, i), instruction_index=0, inner_index=-1, event_ordinal=0, slot=100 + i, tx_index=1,
             block_time=BT + i, is_buy=1, actor="a", quote_mint="So11", quote_atomic=1, token_atomic=1, pre_base=pre[0] if pre else i, pre_quote=pre[1] if pre else i,
             post_base=post[0] if post else i + 1, post_quote=post[1] if post else i + 1, fee_lp=0, fee_protocol=0, fee_creator=0, fee_bps=25, fetched_at=1.0)
    r.update(k)
    return r


class Fake:
    """Transport + decoder pair. pages: list per pool of (rows, next_token); script: optional list of (status, body) overrides consumed first."""

    def __init__(self, pages, script=None):
        self.pages, self.script, self.calls = pages, list(script or []), []
        self.pos = {}

    def transport(self, method, params):
        self.calls.append(params["address"])
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return 200, json.dumps({"jsonrpc": "2.0", "result": {"pool": params["address"]}}).encode()

    def decoder(self, result):
        pool = result["pool"]
        i = self.pos.get(pool, 0)
        self.pos[pool] = i + 1
        rows, token = self.pages[pool][i]
        return rows, token, 0


def build(tmp_path, fake, *, cap=None, targets=("P1",), costs=COSTS):
    c = cycle(tmp_path)
    if cap is not None:
        c["month_cap"] = cap
    ledger = L.Ledger(tmp_path / "credits.db", c, clock=lambda: NOW, costs=costs)
    store = S.Store(tmp_path / "hist")
    con = R.connect(tmp_path / "runner.db")
    R.write_targets(con, DAY, [dict(kind="ps_pool", network_id="solana", pool=p, mint="M", priority=i) for i, p in enumerate(targets)], now=NOW)
    return R.Runner(con, ledger, store, fake.transport, fake.decoder, clock=lambda: NOW), con, ledger, store


def status(con, pool):
    return con.execute("select status, gap, rows, credits_used from pool_runs where pool=?", (pool,)).fetchone()


def chain(n):
    return [swap(i) for i in range(n)]


def test_targets_are_written_before_any_fetch_and_a_complete_pool_is_marked_complete(tmp_path):
    seen = {}

    def transport(method, params):
        seen["targets"] = build_con.execute("select count(*) from backfill_targets").fetchone()[0]
        return 200, json.dumps({"result": {"pool": "P1"}}).encode()
    fake = Fake({"P1": [(chain(2), "t1"), (chain(4)[2:], None)]})
    runner, build_con, ledger, store = build(tmp_path, fake)
    runner.transport = lambda m, p: (transport(m, p), fake.transport(m, p))[1]
    assert runner.run(DAY) == {"P1": "complete"}
    assert seen["targets"] == 1 and status(build_con, "P1") == ("complete", None, 4, 100)
    assert ledger.counts() == {"committed": 2} and ledger.total() == 100


def test_a_quota_answer_stops_the_run_persists_the_stop_and_marks_every_remaining_target(tmp_path):
    body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32429, "message": "max usage reached"}}).encode()
    fake = Fake({"P1": [], "P2": [], "P3": []}, script=[(200, body)])
    runner, con, ledger, store = build(tmp_path, fake, targets=("P1", "P2", "P3"))
    assert runner.run(DAY) == {"P1": "max_usage", "P2": "max_usage", "P3": "max_usage"}
    assert len(fake.calls) == 1 and ledger.stopped() and ledger.total() == 50                     # charged, not retried, remaining targets never sent
    assert con.execute("select count(*) from backfill_targets where state='gap' and gap='max_usage'").fetchone()[0] == 3
    with pytest.raises(L.Refusal):
        ledger.reserve(R.METHOD)                                                                  # and the persisted stop holds for the UTC night


def test_the_month_cap_ends_the_run_with_credits_exhausted_gaps_for_the_rest(tmp_path):
    fake = Fake({"P1": [(chain(2), "t"), (chain(3)[2:], None)], "P2": [(chain(2), None)]})
    runner, con, ledger, store = build(tmp_path, fake, cap=50, targets=("P1", "P2"))
    out = runner.run(DAY)
    assert out == {"P1": "partial", "P2": "credits_exhausted"}
    assert status(con, "P1")[:2] == ("partial", "credits_exhausted") and status(con, "P2")[:2] == ("gap", "credits_exhausted")


def test_plain_rate_limits_are_bounded_charged_and_typed(tmp_path):
    fake = Fake({"P1": []}, script=[(429, b"Too Many Requests")] * 10)
    runner, con, ledger, store = build(tmp_path, fake)
    assert runner.run(DAY) == {"P1": "rate_limited"}
    assert len(fake.calls) == 4 and ledger.total() == 200 and not ledger.stopped()                # first attempt + 3 retries, each charged, no quota stop


def test_a_timeout_is_ambiguous_charged_in_full_and_three_in_a_row_stop_the_run(tmp_path):
    fake = Fake({"P1": [], "P2": [], "P3": [], "P4": []}, script=[TimeoutError("t")] * 9)
    runner, con, ledger, store = build(tmp_path, fake, targets=("P1", "P2", "P3", "P4"))
    out = runner.run(DAY)
    assert out["P1"] == out["P2"] == out["P3"] == "transport_error" and out["P4"] == "transport_error"
    assert len(fake.calls) == 3 and ledger.counts() == {"ambiguous": 3} and ledger.total() == 150     # the 4th target was never sent


def test_a_reserve_break_is_marked_never_repaired(tmp_path):
    rows = [swap(0), swap(1, pre=(999, 999))]                                                     # post of row 0 != pre of row 1
    fake = Fake({"P1": [(rows, None)]})
    runner, con, ledger, store = build(tmp_path, fake)
    assert runner.run(DAY) == {"P1": "reserve_break"}
    assert status(con, "P1")[0] == "reserve_break" and status(con, "P1")[2] == 2


def test_undecodable_rows_make_the_pool_partial_not_complete(tmp_path):
    fake = Fake({"P1": [(chain(2), None)]})
    fake.decoder = lambda result: (chain(2), None, 1)                                             # one tx the decoder could not read
    runner, con, ledger, store = build(tmp_path, fake)
    runner.decoder = fake.decoder
    assert runner.run(DAY) == {"P1": "partial"}
    assert status(con, "P1")[:2] == ("partial", "unreadable_tx")


def test_a_pending_store_claim_of_this_pool_blocks_complete(tmp_path, monkeypatch):
    fake = Fake({"P1": [(chain(2), None)]})
    runner, con, ledger, store = build(tmp_path, fake)
    monkeypatch.setattr(store, "_mark_done", lambda key: None)                                    # claims stay pending
    assert runner.run(DAY) == {"P1": "partial"}
    assert status(con, "P1")[:2] == ("partial", "pagination_incomplete")


def test_a_store_conflict_is_a_typed_gap(tmp_path):
    fake = Fake({"P1": [([swap(0)], None)], "P2": [([swap(0, pool="P1", quote_atomic=99)], None)]})
    runner, con, ledger, store = build(tmp_path, fake, targets=("P1", "P2"))
    out = runner.run(DAY)
    assert out["P1"] == "complete" and out["P2"] == "identity_conflict"                           # P2 re-delivers P1's event identity with a different payload: conflict, nothing written


def test_pagination_has_a_hard_page_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_PAGES", 3)
    fake = Fake({"P1": [([swap(i)], "more") for i in range(10)]})
    runner, con, ledger, store = build(tmp_path, fake)
    assert runner.run(DAY) == {"P1": "partial"}
    assert status(con, "P1")[:2] == ("partial", "pagination_incomplete") and len(fake.calls) == 3


def test_targets_run_in_priority_order_and_a_finished_target_is_not_reset_by_rewriting_the_list(tmp_path):
    fake = Fake({"A": [(chain(1), None)], "B": [(chain(1), None)]})
    runner, con, ledger, store = build(tmp_path, fake, targets=("B", "A"))                         # priorities: B=0, A=1
    runner.run(DAY)
    assert fake.calls == ["B", "A"]
    R.write_targets(con, DAY, [dict(kind="ps_pool", network_id="solana", pool="B", mint="M", priority=0)], now=NOW)
    assert con.execute("select state from backfill_targets where pool='B'").fetchone()[0] == "done"
    assert runner.run(DAY) == {}


def test_the_module_has_no_http_client_and_no_cli():
    import inspect
    src = inspect.getsource(R)
    assert not any(w in src for w in ("urllib", "requests", "http.client", "socket", "argparse", "__main__"))
