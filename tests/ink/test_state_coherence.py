"""Ink coherent safety state (plan Task 5a, Alpha GO 2026-09-29).

The REAL funded EVM entry (`evm_real_entry`), the REAL executor and the REAL bound token safety, over
the scripted chain. The head is read beside the executor's first quote; the exact-clip safety reads
(code, sell simulation, token pull) run pinned to that block. State that is not ready refuses before
any approval or signature.
"""
from types import SimpleNamespace

import pytest

from ink_constants import INK_TOKEN, INK_WETH
from sniper.execution.evm_token_safety import build_evm_token_safety
from sniper.services import memecoin_arming
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg

SLOT_PROOF = ("0x" + "11" * 32, "0x" + "22" * 32)       # a known layout: no slot discovery


def _enter(t, monkeypatch, *, source_block_time, head_fn="real"):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"57073"})
    monkeypatch.setattr(memecoin_arming, "in_quiet_hours", lambda: False)
    safety = build_evm_token_safety(
        chain_id="57073", rpc_url="https://ink.test", taker=t.acct.address,
        quote_fn=t.chain.venue_quote, http=t.chain.http, decimals_fn=lambda _m: 18,
        slot_proof_load=lambda _key: SLOT_PROOF)
    ev = SimpleNamespace(mint=INK_TOKEN, source_user_id="ink-wallet", network_id="57073",
                         signal_source="copy", source_block_time=source_block_time)
    return leg.evm_real_entry(
        t.conn, ev, route="evm:57073", day="2026-09-29", usdc_balance=300.0, native_balance=1.0,
        now_iso="2026-09-29T09:00:01Z", trade_size=10.0, token_safety_fn=safety,
        execute_fn=t.ex["execute_fn"], kill_path="/nonexistent/kill",
        head_fn=t.ex["head_fn"] if head_fn == "real" else head_fn)


def _head_time(t):
    return t.chain.ts0 + 2 * (t.chain.block - t.chain.block0)


def test_the_safety_reads_run_at_the_head_read_beside_the_quote(ink_real, monkeypatch):
    t = ink_real()
    head = t.chain.block
    r = _enter(t, monkeypatch, source_block_time=_head_time(t))
    assert r["status"] == "filled", r
    pinned = [tag for method, tag in t.chain.read_tags
              if method == "eth_getCode" or tag != "latest"]
    assert pinned and set(pinned) == {hex(head)}, t.chain.read_tags
    assert ("eth_getCode", hex(head)) in t.chain.read_tags, "the code read is pinned too"


@pytest.mark.parametrize("lagging", [{"eth_call", "eth_getCode"}, {"eth_call"}])
def test_incoherent_state_cannot_reach_signer(ink_real, monkeypatch, lagging):
    """The read endpoint cannot serve the head block (it lags): not a token verdict, not a
    signature. Lagging only on eth_call proves the simulation's own answer is typed, not only the
    code read's."""
    t = ink_real()
    t.chain.serve_upto = t.chain.block - 1
    t.chain.lagging = lagging
    r = _enter(t, monkeypatch, source_block_time=_head_time(t))
    assert r["status"] in ("unsafe", "route_failed") and r["reason"].startswith("state_not_ready"), r
    assert t.acct.sign_calls == 0 and len(t.chain.sent) == 0
    assert t.conn.execute("SELECT status FROM memecoin_pending_entries").fetchone()[0] != "reserved"


@pytest.mark.parametrize("why, source_time, head_fn", [
    ("the head is older than the source block", "after", "real"),
    ("the source block time is unknown", None, "real"),
    ("the head read failed", "at", lambda: (_ for _ in ()).throw(TimeoutError("head"))),
    ("no head reader at all", "at", None),
])
def test_state_not_ready_refuses_before_any_signature(ink_real, monkeypatch, why, source_time, head_fn):
    t = ink_real()
    source = {"after": _head_time(t) + 5, "at": _head_time(t), None: None}[source_time]
    r = _enter(t, monkeypatch, source_block_time=source, head_fn=head_fn)
    assert r["reason"].startswith("state_not_ready"), (why, r)
    assert t.acct.sign_calls == 0 and len(t.chain.sent) == 0, why


def test_other_chains_never_pin_or_read_a_head(monkeypatch):
    """Byte-identical elsewhere: no head reader is built off 57073, and the bound safety keeps
    every read at 'latest' when no block tag is passed."""
    from eth_account import Account
    from sniper.execution import evm_real_seams as S
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
                                   http=lambda *_a, **_k: (200, {"result": "0x0"}, None),
                                   usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    assert ex["head_fn"] is None


def test_without_a_block_tag_the_bound_safety_reads_latest(ink_real):
    t = ink_real()
    safety = build_evm_token_safety(
        chain_id="57073", rpc_url="https://ink.test", taker=t.acct.address,
        quote_fn=t.chain.venue_quote, http=t.chain.http, decimals_fn=lambda _m: 18,
        slot_proof_load=lambda _key: SLOT_PROOF)
    t.chain.venue_quote(INK_WETH, INK_TOKEN, 5 * 10 ** 15)
    safety(INK_TOKEN, usd=10.0, bought_amount_atomic=5 * 10 ** 23)
    assert t.chain.read_tags and {tag for _m, tag in t.chain.read_tags} == {"latest"}
