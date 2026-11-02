"""A source buy followed by its pool hook's own swaps in the same transaction is ONE exact source.

PAXOS 0xe3a2f45e on 4663 (badabeepp, pos 72077, 2026-10-08): pool 0xb24121ae (USDG / PAXOS, hook
0x4e3468951d). log 13 is the source's buy through the router 0x6aa80dbb; the hook then takes 130 bps
of the output to its fee processor 0x6f02324d, which sells, buys and sells on the same pool (logs 16,
21, 26). The fast decide called it source_swap_ambiguous. The rows below are the tape's own rows of
that transaction."""
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_post_trade as PT
from sniper.services import memecoin_rh_fast_decide as fast

HOOK = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
PAXOS = "0xe3a2f45e5109934339f9224afb1d1abf8ab91e18"
POOL = "0xb24121ae521f3654aa2e645d06616c3d98bb15215c2db14ca8ccf6ad2b4d95f8"
TX = "0x59015c424270920fc9f7d96a1c4179810f872138283da97163dc068ccd820911"
BLOCK = 83448010
ROUTER, PROCESSOR = "0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e", "0x6f02324d20cc679d0e585290caa6b16bacbc0f77"
LIQ = 4536438198464282845
META = {POOL: dict(protocol="v4", mint=PAXOS, token0=USDG, token1=PAXOS, fee=8388608, tick_spacing=8,
                   hooks=HOOK)}
SIG = dict(tx_hash=TX, seq=BLOCK, mint=PAXOS, payment_token=USDG, paid=599355368,
           wallet="0x6feae02e6fd4f43387b2ef2b772a04e2039b0a0a", signal=True)

PAXOS_ROWS = [  # (log, maker, amount0, amount1, sqrt): the tape's rows, pool perspective
    (13, ROUTER, 599355368, -3508034799998618716357940, 6036863717584037739962473545780995856),
    (16, PROCESSOR, -5728127, 33326330599986877815398, 6037444592502558256948546830576213613),
    (21, PROCESSOR, 14135, -81912640401506508521, 6037443161911486190754476557360762833),
    (26, PROCESSOR, -1283387, 7467646179310592372724, 6037573322306820827394973938271919429),
]
LAST_SQRT = PAXOS_ROWS[-1][4]


def _tape(rows, meta=META):
    tape = fast.TapeState(meta.get)
    tape.ingest([dict(pool=POOL, block_no=BLOCK, log_index=log, tx_sig=TX, amount0=a0, amount1=a1,
                      liquidity=LIQ, sqrt=sqrt, observed=1000.0, maker=maker)
                 for log, maker, a0, a1, sqrt in rows])
    return tape


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_4663_SELF_SWAP_HOOKS", frozenset({HOOK}), raising=False)


def test_paxos_the_source_is_the_first_row_and_the_post_state_is_the_last():
    source = _tape(PAXOS_ROWS).source_swap(SIG)
    assert source.get("reason") is None and source["pool"] == POOL
    assert source["src_out"] == 3508034799998618716357940         # the source's own row, never a sum
    expect = fast.coin_price(LAST_SQRT, True) / fast.Fraction(599355368, 3508034799998618716357940)
    assert source["post_ratio"] == pytest.approx(float(expect), rel=1e-12)


def test_paxos_state_keeps_the_source_amounts_at_the_post_transaction_price():
    kind, row = _tape(PAXOS_ROWS).state(POOL, BLOCK, TX)
    assert kind == "tape_post"
    assert (row["amount0"], row["amount1"]) == (599355368, -3508034799998618716357940)
    assert row["sqrt"] == LAST_SQRT and row["liquidity"] == LIQ


def test_a_processor_that_only_sells_is_the_same_shape():
    rows = [PAXOS_ROWS[0], PAXOS_ROWS[1], PAXOS_ROWS[3]]          # 0x9982538f's shape: two sells
    assert _tape(rows).source_swap(SIG).get("reason") is None


def test_a_single_row_source_is_unchanged():
    source = _tape(PAXOS_ROWS[:1]).source_swap(SIG)
    assert source["src_out"] == 3508034799998618716357940
    assert _tape(PAXOS_ROWS[:1]).state(POOL, BLOCK, TX)[1]["sqrt"] == PAXOS_ROWS[0][4]


@pytest.mark.parametrize("case", ["same_router_again", "router_alone_again", "two_later_makers",
                                  "first_is_a_sell", "maker_unknown", "makers_all_unknown",
                                  "hook_not_listed", "knob_empty"])
def test_every_other_multi_row_shape_stays_ambiguous(case, monkeypatch):
    rows, meta = list(PAXOS_ROWS), META
    if case == "same_router_again":                      # the router itself swapped again: a split
        rows[2] = (21, ROUTER) + rows[2][2:]
    elif case == "router_alone_again":                   # every later swap is the source's own router
        rows = [rows[0]] + [(log, ROUTER) + tuple(rest) for log, _m, *rest in rows[1:]]
    elif case == "two_later_makers":                     # a batched second user, through another router
        rows[2] = (21, "0x" + "77" * 20) + rows[2][2:]
    elif case == "first_is_a_sell":
        rows = rows[1:]
    elif case == "maker_unknown":                        # an older row without the column
        rows[1] = (16, None) + rows[1][2:]
    elif case == "makers_all_unknown":                   # an older tape: no later row has a sender
        rows = [rows[0]] + [(log, None) + tuple(rest) for log, _m, *rest in rows[1:]]
    elif case == "hook_not_listed":
        meta = {POOL: dict(META[POOL], hooks="0x" + "99" * 20)}
    elif case == "knob_empty":
        from sniper.settings import memecoin as cfg
        monkeypatch.setattr(cfg, "MEMECOIN_4663_SELF_SWAP_HOOKS", frozenset(), raising=False)
    got = _tape(rows, meta).source_swap(SIG)
    # ambiguous, and since Alpha P1 (2026-10-14) it names its tape-path cause
    assert got["reason"] == "source_swap_ambiguous"
    assert got["source_post_reason"] in ("tape_swap_shape", "tape_hit_conflict")


def test_a_first_row_that_pays_nothing_is_not_a_buy():
    first = dict(amount0=0, amount1=PAXOS_ROWS[0][3], sqrt=PAXOS_ROWS[0][4], maker=ROUTER, liquidity=LIQ)
    later = dict(amount0=1, amount1=-1, sqrt=LAST_SQRT, maker=PROCESSOR, liquidity=LIQ)
    assert fast.self_swap_source([first, later], META[POOL], PAXOS) is None
    assert fast.self_swap_source([dict(first, amount0=599355368), later], META[POOL], PAXOS)["sqrt"] == LAST_SQRT


def test_the_decide_no_longer_refuses_paxos_and_hands_the_shared_route_the_source():
    from test_memecoin_rh_fast_decide import CTX
    tape = _tape(PAXOS_ROWS)
    source = tape.source_swap(SIG)
    assert source["pool"] == POOL and source["route_kind"] == "v4_shared"
    assert source["src_out"] == 3508034799998618716357940
    # The shared route sends on its receipt only (2026-10-14): before it, a re-ask, never a refusal.
    out = fast.decide(SIG, tape=tape, **dict(CTX, allow_estimated_source=True))
    assert out["reason"] == "source_fill_pending" and out["needs_source_fill"] is True


def test_the_tape_tail_and_learn_read_the_maker():
    assert "maker" in fast._TAPE_COLS


def test_the_live_source_pool_route_prices_paxos_at_the_last_row(monkeypatch):
    # MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL=1 in production: a USDG pool is a bridge, native -> USDG (A)
    # -> PAXOS (B, the source's pool at its post-transaction state).
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_decide import CTX
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL", True)
    a_pool = "0x" + "a5" * 32
    meta = dict(META, **{a_pool: dict(protocol="v4", mint=USDG, token0=fast.v4._NATIVE, token1=USDG,
                                      fee=500, tick_spacing=10, hooks=fast.v4._NATIVE)})
    tape = _tape(PAXOS_ROWS, meta)
    tape.ingest([dict(pool=a_pool, block_no=BLOCK - 1, log_index=0, tx_sig="0xa", amount0=1, amount1=-1,
                      liquidity=10 ** 22, sqrt=3909341405412880000000000, observed=1000.0, maker=ROUTER)])
    out = fast.decide(SIG, tape=tape, **dict(CTX, allow_estimated_source=True, eth_usd=2435.0))
    assert out["reason"] != "source_swap_ambiguous" and out["route_kind"] == "v4_bridge"
    assert out["est_post_sqrt"] == LAST_SQRT and out["src_out"] == 3508034799998618716357940
    assert out["state_kind"] == "tape_post"


def test_the_knob_is_off_by_default_and_a_malformed_list_is_off(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.undo()                                   # drop this file's autouse knob
    assert cfg.MEMECOIN_4663_SELF_SWAP_HOOKS == frozenset()
    assert cfg._address_set(" %s , " % HOOK.upper().replace("0X", "0x")) == frozenset({HOOK})
    assert cfg._address_set(HOOK + ",0x4e3468951d") == frozenset()       # one bad entry: all off
    assert cfg._address_set("") == frozenset()


def test_no_hook_level_take_for_a_hook_whose_take_is_per_token():
    # Receipts of 12 tokens' buys on pools of this hook (2026-10-09, research dRPC): the take to the
    # token's fee processor was 50, 75.8, 100, 105, 130 (PAXOS), 140 and 200 bps of the pool output.
    # A single hook-level value would overstate some buys' output and understate others'.
    from sniper.settings import memecoin as cfg
    assert HOOK not in cfg.MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS


# ---- the receipt path: memecoin_evm_post_trade._decode, shared by J7, on-chain and the fast lane ----

RECEIPTS = json.loads((Path(__file__).parent / "fixtures" / "rh_4663_hook_self_swap_receipts.json").read_text())
KEYS = {POOL: dict(currency0=USDG, currency1=PAXOS, fee=8388608, tick_spacing=8, hooks=HOOK),
        "0x8111082d181841cbd123e82bced032b1331140332b6ce3da37f53a6b24ed736c": dict(
            currency0="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            currency1="0x100765a16f42636bd84ee1e86bc5107b84bcdba3", fee=8388608, tick_spacing=200,
            hooks=HOOK)}


def _post(name, *, chain="4663", edit=None):
    case = json.loads(json.dumps(RECEIPTS[name]))
    if edit:
        edit(case["receipt"]["logs"])
    return PT.source_post_trade(chain, case["receipt"], case["mint"], owner=case["wallet"],
                                pool_key_fn=lambda _chain, pool: KEYS.get(pool))


@pytest.mark.parametrize("name,gross,last_sqrt", [
    ("paxos_a", 3508034799998618716357940, LAST_SQRT),
    ("paxos_b", 2651208958725891853251413, None)])
def test_the_receipt_takes_the_source_leg_and_the_last_rows_post_state(name, gross, last_sqrt):
    post = _post(name)
    assert post.get("reason") is None and post["pool"] == POOL and post["venue"] == "v4"
    assert int(post["pool_output_atomic"]) == gross
    swaps = [l for l in RECEIPTS[name]["receipt"]["logs"]
             if l["topics"] and l["topics"][0].startswith("0x40e9cecb") and l["topics"][1].lower() == POOL]
    final = int(swaps[-1]["data"][2 + 128: 2 + 192], 16)            # the LAST swap's sqrtPriceX96
    assert int(post["sqrt_price_x96"]) == final and (last_sqrt is None or final == last_sqrt)


def test_a_processor_that_only_sells_was_already_one_leg_and_is_unchanged(monkeypatch):
    from sniper.settings import memecoin as cfg
    with_rule = _post("sells_only")
    monkeypatch.setattr(cfg, "MEMECOIN_4663_SELF_SWAP_HOOKS", frozenset(), raising=False)
    assert with_rule == _post("sells_only") and with_rule.get("reason") is None


def _sender(log_index, sender):
    def edit(logs):
        for log in logs:
            if int(log["logIndex"], 16) == log_index:
                log["topics"][2] = "0x" + "00" * 12 + sender[2:]
    return edit


@pytest.mark.parametrize("why", ["knob_empty", "other_chain", "router_swaps_again", "router_alone_again",
                                 "two_other_senders"])
def test_every_other_shape_of_the_receipt_stays_a_split(why, monkeypatch):
    from sniper.settings import memecoin as cfg
    kwargs = {}
    if why == "knob_empty":
        monkeypatch.setattr(cfg, "MEMECOIN_4663_SELF_SWAP_HOOKS", frozenset(), raising=False)
    elif why == "other_chain":                           # the same logs as if on Base: not 4663's rule
        monkeypatch.setitem(PT.V4_POOL_MANAGERS, "8453", PT.V4_POOL_MANAGERS["4663"])
        kwargs["chain"] = "8453"
    elif why == "router_swaps_again":
        kwargs["edit"] = _sender(21, ROUTER)
    elif why == "router_alone_again":                    # every later swap is the source's own router
        kwargs["edit"] = lambda logs: [_sender(i, ROUTER)(logs) for i in (16, 21, 26)]
    elif why == "two_other_senders":
        kwargs["edit"] = _sender(21, "0x" + "77" * 20)
    assert _post("paxos_a", **kwargs).get("reason") == "split_route"


def test_the_receipt_rule_needs_the_first_swap_on_the_pool_to_be_a_buy():
    key, leg = KEYS[POOL], ("v4", POOL, 14135, 81912640401506508521, True)
    sold_first = {POOL: [(ROUTER, None, key), (PROCESSOR, leg, key)]}     # the pool's first swap sold
    assert PT._drop_hook_self_swaps([leg], sold_first) == [leg]
    bought_first = {POOL: [(ROUTER, ("v4", POOL, 1, 2, True), key), (PROCESSOR, leg, key)]}
    assert PT._drop_hook_self_swaps([leg], bought_first) == []
