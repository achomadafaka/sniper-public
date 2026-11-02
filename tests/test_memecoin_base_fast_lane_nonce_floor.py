"""The Base 8453 live lane claims its nonce, and pre-approves its fill, through 8453's own nonce
floor (2026-10-14, found minutes after live): the real lane_claim_nonce_fn returned None unless the
approval PIPELINE was on (4663,56 only), so every live Base send would have aborted nonce_in_flight
after its reservation, and the ordinary lane would then have skipped the same buy as attempted.
8453's entry signer already takes its nonce from that floor (receipt-first with pre-approval)."""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from sniper.settings import memecoin as cfg

sys.path.insert(0, str(Path(__file__).parent))


def _ctx(monkeypatch, tmp_path, chain, *, pipelined, preapprove):
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda c=None: pipelined)
    monkeypatch.setattr(cfg, "memecoin_evm_preapprove_chains", lambda: frozenset(preapprove))
    return _production_context(monkeypatch, tmp_path, chain)


def test_the_live_base_lane_claims_the_snapshot_nonce_through_8453_s_floor(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path, "8453", pipelined=False, preapprove={"4663", "1", "8453"})
    claim, release = ctx["lane_claim_nonce_fn"], ctx["lane_release_nonce_fn"]
    assert claim(42) == 42                       # the production .env on 2026-10-14: no pipeline
    assert claim(42) is None                     # our 42 in flight: the floor (43) is above it
    assert release(42) is True and claim(42) == 42


@pytest.mark.parametrize("chain,pipelined,preapprove", [
    ("8453", False, {"4663", "1"}),              # 8453 without pre-approval has no floor
    ("4663", False, {"4663", "1", "8453"})])     # 4663 without its pipeline: unchanged, no floor
def test_a_chain_without_a_nonce_floor_never_hands_the_lane_a_nonce(monkeypatch, tmp_path, chain,
                                                                    pipelined, preapprove):
    ctx = _ctx(monkeypatch, tmp_path, chain, pipelined=pipelined, preapprove=preapprove)
    assert ctx["lane_claim_nonce_fn"](42) is None


@pytest.mark.parametrize("pipelined,chains,started", [
    (False, {"8453"}, True), (False, set(), False), (True, {"8453"}, True)])
def test_a_base_lane_fill_starts_the_entry_pre_approval_like_the_ordinary_8453_entry(
        monkeypatch, tmp_path, pipelined, chains, started):
    from sniper.services import memecoin_base_fast_lane as base
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda c=None: pipelined)
    monkeypatch.setattr(cfg, "memecoin_evm_preapprove_chains", lambda: frozenset(chains))
    calls, done = [], threading.Event()
    live = object.__new__(base.BaseLive)
    live.ctx = {"preapprove_position_fn": lambda db, pid, route_protocol=None: (
        calls.append((pid, route_protocol)), done.set())}
    conn = sqlite3.connect(tmp_path / "ledger.db")
    live._start_preapproval(conn, 7, {"route_kind": "v4_native"})
    assert done.wait(2.0) is started
    assert calls == ([(7, "uniswap_v4")] if started else [])
