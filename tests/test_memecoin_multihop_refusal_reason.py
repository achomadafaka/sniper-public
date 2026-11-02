"""A multihop refusal names its gate (2026-09-05). Over three days 46 of 46 USDG-reachable
`no_route` refusals carried a v4_multihop rung with no reason, so nobody could say whether depth,
the bridge, a leg quote or the round-trip budget refused the coin."""
from sniper.execution import evm_v4_multihop as mh
from sniper.execution import evm_real_seams as seams

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MINT = "0xd63e51f09f8d14e0765c72d849ebc898f8cfd02a"
NATIVE = "0x" + "00" * 20


def _quote(monkeypatch, *, bridge, leg, out=None, back=None):
    monkeypatch.setattr(mh, "_pools_with_depth", lambda *a, **k: [])
    monkeypatch.setattr(mh, "_candidate_bridges", lambda *a, **k: iter([USDG]))
    monkeypatch.setattr(mh, "_decision_block", lambda *a, **k: "latest")
    monkeypatch.setattr(mh, "_bridge_options", lambda *a, **k: [bridge] if bridge else [])
    monkeypatch.setattr(mh, "_coin_leg_options", lambda *a, **k: [leg] if leg else [])
    calls = {"n": 0}

    def _path(*a, **k):
        calls["n"] += 1
        return out if calls["n"] == 1 else back
    monkeypatch.setattr(mh, "_quote_path", _path)
    return mh.multihop_quote("4663", NATIVE, MINT, 10 ** 16, rpc_url="http://rpc",
                             bridge_currencies=(USDG,), clip_usd=140.0, min_depth_clips=20.0)


def test_bridge_pool_refusal_is_named(monkeypatch):
    assert _quote(monkeypatch, bridge=None, leg=None) is None
    assert mh.last_multihop_refusal().startswith("bridge_pool_unusable:")


def test_coin_leg_refusal_is_named_with_the_floor(monkeypatch):
    key = {"currency0": NATIVE, "currency1": USDG, "fee": 460, "tick_spacing": 10, "hooks": NATIVE}
    assert _quote(monkeypatch, bridge=("bpid", key, 1000000.0), leg=None) is None
    assert mh.last_multihop_refusal() == "coin_leg_missing_or_under_floor:min_depth_usd=2800"


def test_reverse_path_refusal_is_named_and_a_route_clears_it(monkeypatch):
    key = {"currency0": NATIVE, "currency1": USDG, "fee": 460, "tick_spacing": 10, "hooks": NATIVE}
    ckey = {"currency0": USDG, "currency1": MINT, "fee": 0, "tick_spacing": 60, "hooks": NATIVE}
    assert _quote(monkeypatch, bridge=("bpid", key, 1e6), leg=("cpid", ckey, 5000.0),
                  out=10 ** 20, back=None) is None
    assert mh.last_multihop_refusal() == "reverse_path_no_quote"
    route = _quote(monkeypatch, bridge=("bpid", key, 1e6), leg=("cpid", ckey, 5000.0),
                   out=10 ** 20, back=9 * 10 ** 15)
    assert route is not None and mh.last_multihop_refusal() is None


def test_the_ladder_records_the_multihop_refusal_reason_on_its_rung(monkeypatch):
    """Through the real multihop rung: the quote refuses with a noted gate, the ladder writes it as
    `reason` on the v4_multihop rung and leaves `blind_reason` None (a reason must never re-enter
    the requote the way blindness does)."""
    from sniper.settings import memecoin as cfg
    from sniper.execution import geckoterminal_quote as gt
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    monkeypatch.setattr(gt, "cooldown_active", lambda *a, **k: False)

    def _refuse(*a, **k):
        mh._note("coin_leg_missing_or_under_floor:min_depth_usd=2800")
        return None
    monkeypatch.setattr(mh, "dex_v4_multihop_quote", _refuse)
    rungs = []
    seams.venue_quote_with_v2_fallback(
        lambda s, b, a, slippage_bps=None: None,
        chain="4663", quote_token="0x0bd7d308f8e1639fab988df18a8011f41eacad73", taker="0xtaker",
        v2_quote_fn=lambda *a, **k: None,
        v4_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=rungs.append)(
        "0x0bd7d308f8e1639fab988df18a8011f41eacad73", MINT, 10 ** 16)
    r = [x for x in rungs if x["rung"] == "v4_multihop"]
    assert r, rungs
    assert r[-1]["hit"] is False and r[-1]["blind_reason"] is None
    assert r[-1]["reason"] == "coin_leg_missing_or_under_floor:min_depth_usd=2800"
    assert all("reason" not in x for x in rungs if x["rung"] != "v4_multihop")


def test_the_exit_search_names_every_refusal_branch():
    """A silent None is not a diagnosis.

    AIR (0x31dc304f, chain 4663, 2026-09-06) refused with "configured sell venue returned no
    executable quote" and no cause: its only pool pairs it with another memecoin, the BUY
    direction quoted, the sell did not, and the reason channel still held a stale note from an
    earlier call — so finding the branch meant re-running the search by hand. The entry search has
    named its branches since 2026-09-05; the exit search named none of them.
    """
    import inspect
    from sniper.execution import evm_v4_multihop as mh

    src = inspect.getsource(mh.dex_v4_multihop_exit_quote)
    # every `return None` in the exit search is preceded by a named reason
    lines = [l.strip() for l in src.split("\n")]
    for i, line in enumerate(lines):
        if line == "return None":
            preceding = " ".join(lines[max(0, i - 3):i])
            assert "_note(" in preceding, (
                f"unnamed `return None` at exit-search line {i}: {preceding!r}")
    assert src.count('_note("exit_') >= 5


def test_the_exit_build_failure_is_not_returned_as_a_silent_none():
    """`as_zerox_exit_quote` returning None used to be returned unchecked, which is the branch AIR
    actually took. The quote succeeding and the transaction failing to build are different facts."""
    import inspect
    from sniper.execution import evm_v4_multihop as mh

    src = inspect.getsource(mh.dex_v4_multihop_exit_quote)
    assert "exit_build_failed" in src
    assert "if built is None:" in src


def test_a_marginal_over_budget_rung_gets_its_own_code():
    """The sentence is unchanged; only the code separates a re-askable marginal miss (2026-09-17)."""
    code, detail = seams.classify_quote_miss([
        {"rung": "v4_multihop", "hit": False, "skipped": False, "blind_reason": None,
         "reason": "round_trip_over_budget_marginal:-15.0927"},
    ])

    assert code == "route_over_budget_marginal"
    assert "declined it: round_trip_over_budget_marginal:-15.0927" in detail

    code, _detail = seams.classify_quote_miss([
        {"rung": "v4_multihop", "hit": False, "skipped": False, "blind_reason": None,
         "reason": "round_trip_over_budget:-18.5"},
    ])
    assert code == "route_over_budget"
