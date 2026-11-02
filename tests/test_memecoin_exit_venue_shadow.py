"""The EVM exit-venue shadow, slice 1 (spec 2026-09-30-memecoin-evm-exit-venue-selection-design).

It runs in the capture process. It reads the exit-decision quotes the runner already stores
(`memecoin_route_series`), asks the position's ENTRY venue on its own read host, and writes one
row to the captures store. It changes no exit, waits on none, and cannot sign: it is given an
address, never an account.
"""
import json
import re
import sqlite3
from pathlib import Path

import pytest
from eth_abi import decode
from eth_utils import keccak

from sniper.execution import evm_v4_quote as Q
from sniper.persistence import db_memecoin as m
from sniper.persistence import db_memecoin_captures as store
from sniper.services import memecoin_exit_venue_shadow as S
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
CHAIN, OWNER = "4663", "0x" + "a1" * 20
MINT, WETH = "0x" + "5e" * 20, "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE = "0x" + "00" * 20
HOOKLESS = {"currency0": NATIVE, "currency1": MINT, "fee": 10000, "tick_spacing": 200,
            "hooks": NATIVE}
HOOKED = dict(HOOKLESS, fee=0, hooks="0x" + "b8" * 20)
BAG, QUOTED_AT = 5 * 10 ** 21, "2026-09-30T16:00:00.684000+00:00"
SECOND = 1790784000                   # 2026-09-30T16:00:00Z, the quote's own second
SECOND_BLOCK = 5_000_000              # the first block of that second on the fake chain
EARLY, PIN = SECOND_BLOCK - 4, SECOND_BLOCK + 3      # 1.0 s and 0.3 s before the quote, 10 blocks/s
URL = "https://shadow-host/v1/path-with-the-key"


def _pool_id(key):
    return "0x" + keccak(bytes.fromhex(Q._encode_pool_key(key))).hex()


# ---- the Permit2 allowance override: no signature, a storage slot ----

def test_the_permit2_allowance_slot_matches_mainnet_state():
    """Verified on Ethereum mainnet on 2026-09-30 (block 26,091,279): eth_getStorageAt at this
    slot equals allowance(owner, token, spender) packed as amount | expiration << 160 |
    nonce << 208, for three Permit events. Permit2 is the same bytecode on every chain."""
    slot = S.permit2_allowance_slot(
        "0x039689047e03bc17ea97410ee7928658de0be794",
        "0x75e2fc69ff2ac12af65ba7d321bbf4f878c535d2",
        "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85")
    assert slot == "0xfbaeea9aec6ce5529d37ff87babbdba5f322a32a91c5685d372db55dee3a41b3"
    stored = int("0x00000000000100006ae4bb82ffffffffffffffffffffffffffffffffffffffff", 16)
    assert stored == S.packed_allowance((1 << 160) - 1, 0x6ae4bb82, 1)


@pytest.mark.parametrize("key, actions", [
    (HOOKLESS, bytes([Q._ACT_SWAP_IN_SINGLE, Q._ACT_SETTLE_ALL, Q._ACT_TAKE])),
    (HOOKED, bytes([Q._ACT_SWAP_IN, Q._ACT_SETTLE, Q._ACT_TAKE])),
], ids=["hookless", "hooked"])
def test_the_simulated_sell_carries_no_permit_and_needs_no_signature(key, actions):
    call, overrides = S.sell_simulation(CHAIN, key, MINT, BAG, 7 * 10 ** 15, OWNER)
    assert call["from"] == OWNER and call["to"] == Q.V4[CHAIN]["universal_router"]
    commands, inputs, _deadline = decode(["bytes", "bytes[]", "uint256"],
                                         bytes.fromhex(call["data"][10:]))
    assert Q._CMD_PERMIT2_PERMIT not in commands, "a permit is a custody signature"
    assert commands[0] == Q._CMD_V4_SWAP
    got_actions, _params = decode(["bytes", "bytes[]"], inputs[0])
    assert got_actions == actions
    # The ONLY override is the Permit2 allowance of owner / coin / router, set to the maximum.
    permit2 = Q.V4[CHAIN]["permit2"]
    assert list(overrides) == [permit2]
    slot = S.permit2_allowance_slot(OWNER, MINT, Q.V4[CHAIN]["universal_router"])
    assert list(overrides[permit2]["stateDiff"]) == [slot]
    value = int(overrides[permit2]["stateDiff"][slot], 16)
    assert value & ((1 << 160) - 1) == (1 << 160) - 1 and (value >> 160) & ((1 << 48) - 1) > 2 ** 40


def test_a_pool_that_pays_the_quote_token_sends_it_to_the_owner_and_a_native_one_wraps():
    """Gamma: forcing `to_sender` False survived. A WETH-currency pool pays WETH straight to the
    owner (one command); a native pool pays ETH, which the router wraps to the owner (two)."""
    weth_pool = {"currency0": MINT, "currency1": WETH, "fee": 3000, "tick_spacing": 60,
                 "hooks": NATIVE}
    for key, commands_expected in ((weth_pool, bytes([Q._CMD_V4_SWAP])),
                                   (HOOKLESS, bytes([Q._CMD_V4_SWAP, Q._CMD_WRAP_ETH]))):
        call, _ = S.sell_simulation(CHAIN, key, MINT, BAG, 1, OWNER)
        commands, _inputs, _d = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(call["data"][10:]))
        assert commands == commands_expected, key


@pytest.mark.parametrize("currency, mint", [
    (NATIVE, "0xffbb" + "22" * 18), (WETH, "0x00aa" + "11" * 18), (WETH, "0xffbb" + "22" * 18)],
    ids=["native", "weth-coin-first", "weth-coin-second"])
def test_the_simulated_sell_is_the_executors_sell_without_the_permit(monkeypatch, currency, mint):
    """Gamma LOW 1: compared with the executor's own build (`dex_v4_quote`, the plain V4 exit),
    command for command and swap argument for swap argument, minus the PERMIT2_PERMIT."""
    seen = []
    monkeypatch.setattr(Q, "_ur_execute_data",
                        lambda commands, inputs, deadline: seen.append(("build", bytes(commands))) or "0xdata")
    monkeypatch.setattr(Q, "_permit2_input", lambda *a, **k: "00" * 64)
    monkeypatch.setattr(Q, "_v4_swap_input", lambda key, zf1, amount, minimum, take_all_to_sender=False: (
        seen.append(("swap", zf1, amount, take_all_to_sender)) or "00" * 32))
    low, high = sorted((currency, mint))
    key = {"currency0": low, "currency1": high, "fee": 3000, "tick_spacing": 60, "hooks": NATIVE}
    monkeypatch.setattr(Q, "v4_quote_amount_out", lambda *a, **k: {
        "amount_out": 1000, "pool_id": "p", "pool_key": key, "zero_for_one": low == mint,
        "pool_fee": 3000, "quote_currency": currency})
    Q.dex_v4_quote(CHAIN, mint, WETH, BAG, "0xtaker", rpc_url="http://rpc", acct=object(),
                   now_fn=lambda: 0)
    executor, seen[:] = list(seen), []
    S.sell_simulation(CHAIN, key, mint, BAG, 1, OWNER)
    executor_commands = executor[-1][1].replace(bytes([Q._CMD_PERMIT2_PERMIT]), b"")
    assert seen[-1] == ("build", executor_commands)
    assert [x for x in seen if x[0] == "swap"] == [x for x in executor if x[0] == "swap"]


def test_the_sell_direction_follows_which_side_of_the_pool_the_coin_is():
    """Native sorts first, so the coin is usually currency1 and a sell is one-for-zero. A
    WETH-currency pool can hold the coin as currency0. A wrong direction still returns a number."""
    coin_first = {"currency0": MINT, "currency1": "0x" + "ee" * 20, "fee": 3000, "tick_spacing": 60,
                  "hooks": NATIVE}
    for key, zero_for_one in ((HOOKLESS, 0), (coin_first, 1)):
        words = bytes.fromhex(S.quote_call(CHAIN, key, MINT, BAG)["data"][10:])
        assert int.from_bytes(words[32 * 6:32 * 7], "big") == zero_for_one      # after the 5-word key
        assert int.from_bytes(words[32 * 7:32 * 8], "big") == BAG
        call, _ = S.sell_simulation(CHAIN, key, MINT, BAG, 1, OWNER)
        _c, inputs, _d = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(call["data"][10:]))
        _a, params = decode(["bytes", "bytes[]"], inputs[0])
        assert int.from_bytes(params[0][32 * 6:32 * 7], "big") == zero_for_one
        assert int.from_bytes(params[0][32 * 7:32 * 8], "big") == BAG
    call, _ = S.sell_simulation(CHAIN, HOOKED, MINT, BAG, 1, OWNER)
    _c, inputs, _d = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(call["data"][10:]))
    _a, params = decode(["bytes", "bytes[]"], inputs[0])
    assert "0x" + params[0][32 + 12:64].hex() == MINT             # the path form's currencyIn
    assert decode(["address", "uint256", "bool"], params[1]) == (MINT, 0, True)   # we pay


def test_the_module_cannot_sign_and_the_funded_runner_does_not_load_it():
    source = (ROOT / "sniper/services/memecoin_exit_venue_shadow.py").read_text()
    code = "\n".join(line.split("#")[0] for line in source.splitlines())
    for forbidden in ("load_keypair", "sign_typed_data", "sign_transaction", "eth_sendRawTransaction",
                      "acct", "private_key", "eth_account"):
        assert forbidden not in code, forbidden
    for runner_file in ("scripts/memecoin_runner_loop.py", "sniper/execution/evm_real_seams.py",
                        "sniper/execution/evm_exit_executor.py", "sniper/services/memecoin_evm_leg.py",
                        "sniper/services/memecoin_real_runner.py"):
        assert "memecoin_exit_venue_shadow" not in (ROOT / runner_file).read_text(), runner_file


# ---- one observation ----

class Node:
    """The shadow's read host: a chain of 10 blocks a second whose timestamps are whole seconds.
    Answers by method and BLOCK, records every call and the URL it was sent to."""

    def __init__(self, *, quote=8 * 10 ** 15, allowance=(1 << 255), sim="ok", status=200,
                 head=SECOND_BLOCK + 45, stamp=None, error=None):
        self.quote, self.allowance, self.sim, self.status = quote, allowance, sim, status
        self.head, self.stamp, self.error = head, stamp or self._stamp, error
        self.calls, self.urls = [], []

    @staticmethod
    def _stamp(number):
        return SECOND + (number - SECOND_BLOCK) // 10

    def tags(self, method="eth_call"):
        return [params[1] for name, params in self.calls if name == method]

    def __call__(self, url, body, timeout):
        self.calls.append((body["method"], body["params"]))
        self.urls.append(url)
        if self.status != 200:
            return self.status, {}
        method, params = body["method"], body["params"]
        if method == "eth_getBlockByNumber":
            number = self.head if params[0] == "latest" else int(params[0], 16)
            if number > self.head:
                return 200, {"result": None}
            return 200, {"result": {"number": hex(number), "timestamp": hex(self.stamp(number))}}
        assert params[1] != "latest", "every read of a tick is pinned to a block"
        block, to = int(params[1], 16), params[0]["to"].lower()
        approval = params[0]["data"].startswith("0xdd62ed3e")       # allowance(owner, spender)
        if self.error and self.error[0] == ("quote" if to == Q.V4[CHAIN]["quoter"] else
                                            "allowance" if approval else "sim"):
            return 200, {"error": {"code": -32000, "message": self.error[1]}}
        if to == Q.V4[CHAIN]["quoter"]:
            quote = self.quote(block) if callable(self.quote) else self.quote
            return 200, ({"result": "0x" + format(quote, "064x") + "0" * 64} if quote
                         else {"error": {"code": 3, "message": "execution reverted", "data": "0x"}})
        if approval:
            return 200, {"result": "0x" + format(self.allowance, "064x")}
        assert len(params) == 3, "the simulation must carry the state override"
        sim = self.sim(block, _minimum_out(params[0])) if callable(self.sim) else self.sim
        if sim == "ok":
            return 200, {"result": "0x"}
        return 200, {"error": {"code": 3, "message": "execution reverted", "data": sim}}


def _minimum_out(call):
    """amountOutMinimum of a hookless single-pool sell (the word after the key, side and amount)."""
    _c, inputs, _d = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(call["data"][10:]))
    _a, params = decode(["bytes", "bytes[]"], inputs[0])
    return int.from_bytes(params[0][32 * 8:32 * 9], "big")


def _tick(key=HOOKLESS, **kw):
    route = {"protocol": "uniswap_v4", "pool_id": _pool_id(key)}
    base = {"series_id": 7, "position_id": 66000, "network_id": CHAIN, "mint": MINT,
            "quoted_at": QUOTED_AT, "winner_venue": "uniswap_v2", "winner_pool_id": None,
            "winner_route_target": "0xrouter",
            "input_atomic": str(BAG), "winner_output_atomic": str(6 * 10 ** 15),
            "winner_min_output_atomic": str(54 * 10 ** 14), "winner_slippage_bps": 1000,
            "quote_asset_usd": 2683.26, "entry_route": route, "pool_key": key}
    return dict(base, **kw)


def _shadow(node, **kw):
    clock = kw.pop("clock", [1000.0])
    now = kw.pop("now", "2026-09-30T16:00:02.184000+00:00")
    return S.Shadow(CHAIN, URL, owner=OWNER, post_fn=node,
                    clock=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                    now_iso_fn=lambda: now, **kw)


def test_a_tick_asks_the_entry_pool_and_simulates_the_whole_bag_sell():
    node = Node()
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "ok" and row["series_id"] == 7 and row["position_id"] == 66000
    assert row["entry_protocol"] == "uniswap_v4" and row["entry_pool_id"] == _pool_id(HOOKLESS)
    assert row["entry_quote_atomic"] == row["entry_quote_early_atomic"] == str(8 * 10 ** 15)
    assert row["winner_venue"] == "uniswap_v2" and row["winner_output_atomic"] == str(6 * 10 ** 15)
    assert row["winner_slippage_bps"] == 1000 and row["lag_s"] == pytest.approx(1.5)
    assert row["allowance_standing"] == 1 and row["sim_ok"] == 1 and row["sim99_ok"] == 1
    # The executor's own minimum: the quote less the slippage the runner stored with its quote.
    assert row["sim_min_atomic"] == str(8 * 10 ** 15 * 9_000 // 10_000)
    sims = [params for name, params in node.calls if name == "eth_call" and len(params) == 3]
    assert [_minimum_out(sim[0]) for sim in sims] == [72 * 10 ** 14, 792 * 10 ** 13]   # 90%, then 99%
    assert sims[0][0]["from"] == OWNER and Q.V4[CHAIN]["permit2"] in sims[0][2]
    assert row["calls"] == len(node.calls) == 11        # head, block time, 4 timestamps, 5 reads


def test_every_read_of_a_tick_is_pinned_before_the_runners_quote():
    """The pins come from the CHAIN's clock: the first block of the quote's second is found by
    its timestamp, and the two blocks are 1.0 s and 0.3 s before the quote."""
    node = Node()
    row = _shadow(node).observe(_tick())
    assert row["head_block"] == SECOND_BLOCK + 45 and row["head_time"] == SECOND + 4
    assert row["block_time_s"] == pytest.approx(0.1) and row["second_block"] == SECOND_BLOCK
    assert (row["early_block"], row["pin_block"]) == (EARLY, PIN)
    # late quote, early quote, the allowance, the simulation and the simulation at 99%
    assert node.tags() == [hex(PIN), hex(EARLY), hex(PIN), hex(PIN), hex(PIN)]
    assert PIN < SECOND_BLOCK + 6                       # the block current at the quote itself


# The block where the entry pool's quote equals the stored output of a runner quote whose winner
# WAS the entry pool (4663, positions 66853, 66678, 66668; research host, 2026-09-30):
# (first block of the quote's second, the quote's offset into it, first and last matching block).
RUNNER_SAW = [
    (75553347, 0.679, 75553349, 75553349), (75553367, 0.684, 75553368, 75553373),
    (75137844, 0.395, 75137840, 75137841), (75137854, 0.186, 75137850, 75137851),
    (75137864, 0.408, 75137860, 75137861), (75137874, 0.189, 75137871, 75137871),
    (75137884, 0.569, 75137881, 75137881), (75127278, 0.421, 75127275, 75127275),
    (75127288, 0.246, 75127285, 75127285), (75127298, 0.472, 75127293, 75127295),
    (75127308, 0.240, 75127305, 75127305), (75127318, 0.479, 75127316, 75127316),
]


@pytest.mark.parametrize("second_block, offset, first, last", RUNNER_SAW)
def test_the_two_pins_bracket_the_block_the_runners_quote_read(second_block, offset, first, last):
    early, late = S.pin_blocks(second_block, SECOND, SECOND + offset, 0.1)
    assert early <= first <= late, (early, late)       # the first matching block is in the window
    assert late < second_block + int(offset * 10)      # never the block current at the quote


def test_the_fire_tick_is_read_before_the_runners_own_sell():
    """The tick that fires a rail: by the time the shadow reads it the sell has landed. At the
    head the wallet has no bag and the entry pool is the pool after our own sell."""
    sold_at = SECOND_BLOCK + 12                         # 0.5 s after the quote
    node = Node(quote=lambda block: 8 * 10 ** 15 if block < sold_at else 5 * 10 ** 15,
                sim=lambda block, _minimum: "ok" if block < sold_at else "0x08c379a0")
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "ok" and row["head_block"] > sold_at > row["pin_block"]
    assert row["entry_quote_atomic"] == str(8 * 10 ** 15) and row["sim_ok"] == 1


def test_a_pool_that_moved_inside_the_window_is_a_row_with_both_quotes():
    node = Node(quote=lambda block: 9 * 10 ** 15 if block < SECOND_BLOCK else 6 * 10 ** 15)
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "ok"
    assert row["entry_quote_early_atomic"] == str(9 * 10 ** 15)
    assert row["entry_quote_atomic"] == str(6 * 10 ** 15)
    assert row["sim_min_atomic"] == str(54 * 10 ** 14)  # the simulation follows pin_block


def test_a_transfer_tax_passes_the_executors_minimum_and_fails_at_ninety_nine_percent():
    """A 3% tax: the sell clears the executor's 90% and would fail a 99% bar. Both are facts."""
    too_little = "0x8b063d73" + format(792 * 10 ** 13, "064x") + format(776 * 10 ** 13, "064x")
    node = Node(sim=lambda _block, minimum: "ok" if minimum <= 776 * 10 ** 13 else too_little)
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "ok" and row["sim_ok"] == 1 and row["sim_revert"] is None
    assert row["sim99_ok"] == 0 and row["sim99_revert"] == too_little[:138]


def test_a_reverting_simulation_is_a_fact_with_its_revert_data():
    too_little = "0x8b063d73" + format(8 * 10 ** 15, "064x") + format(3, "064x")
    node = Node(sim=too_little)
    row = _shadow(node).observe(_tick(key=HOOKED))
    assert row["outcome"] == "ok" and row["sim_ok"] == 0 and row["entry_hooked"] == 1
    assert row["sim_revert"] == too_little[:138]
    assert row["sim99_ok"] is None and len(node.tags()) == 4     # no second simulation after a revert


def test_without_the_runners_slippage_the_executors_minimum_is_not_guessed():
    node = Node()
    row = _shadow(node).observe(_tick(winner_slippage_bps=None))
    assert row["outcome"] == "no_slippage" and row["entry_quote_atomic"] == str(8 * 10 ** 15)
    assert row["sim_min_atomic"] is None and row["sim_ok"] is None


def test_an_approval_smaller_than_the_bag_is_not_standing():
    node = Node(allowance=BAG - 1)
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "no_token_approval" and row["allowance_standing"] == 0


def test_a_quote_answer_that_is_not_a_quote_is_an_error_not_a_revert():
    """Gamma: a malformed result read as entry_quote_reverted would count the pool as dead."""
    class Short(Node):
        def __call__(self, url, body, timeout):
            if body["method"] == "eth_call" and body["params"][0]["to"].lower() == Q.V4[CHAIN]["quoter"]:
                self.calls.append((body["method"], body["params"]))
                return 200, {"result": "0x1234"}
            return super().__call__(url, body, timeout)
    row = _shadow(Short()).observe(_tick())
    assert row["outcome"] == "error:rpc_malformed_quote" and row["entry_quote_atomic"] is None


def test_without_a_standing_token_approval_the_sell_is_not_simulated():
    """The Permit2 override covers Permit2's own allowance. The coin's approval to Permit2 is
    real state; without it the simulation would revert for a reason that says nothing."""
    node = Node(allowance=0)
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "no_token_approval" and row["allowance_standing"] == 0
    assert row["entry_quote_atomic"] == str(8 * 10 ** 15) and row["sim_ok"] is None
    assert node.tags() == [hex(PIN), hex(EARLY), hex(PIN)]


@pytest.mark.parametrize("tick, outcome", [
    (dict(entry_route={"protocol": "relay_same_chain"}), "unpinnable:relay_same_chain"),
    (dict(entry_route={"protocol": "uniswap_v4"}), "unpinnable:uniswap_v4"),          # no pool id
    (dict(entry_route=None), "unpinnable:none"),
    (dict(pool_key=None), "no_pool_key"),
])
def test_a_position_with_no_pinnable_entry_venue_costs_no_call(tick, outcome):
    node = Node()
    row = _shadow(node).observe(_tick(**tick))
    assert row["outcome"] == outcome and node.calls == [] and row["calls"] == 0
    assert row["entry_quote_atomic"] is None and row["sim_ok"] is None


def test_a_quote_that_reverts_is_recorded_and_the_simulation_is_skipped():
    node = Node(quote=0)
    row = _shadow(node).observe(_tick())
    assert row["outcome"] == "entry_quote_reverted" and row["sim_ok"] is None
    assert node.tags() == [hex(PIN)]


@pytest.mark.parametrize("where, outcome, quote", [
    ("quote", "error:rpc_missing trie node 5e5e (path ) state is not available", None),
    ("allowance", "error:rpc_header not found", str(8 * 10 ** 15)),
    ("sim", "error:rpc_missing trie node", str(8 * 10 ** 15)),
])
def test_a_node_that_does_not_hold_the_state_is_never_a_revert(where, outcome, quote):
    """A host that pruned the pinned block answers with an error that is not a revert. Reading
    it as "the pool reverted" or "the sell fails" would be a confident wrong row."""
    row = _shadow(Node(error=(where, outcome[len("error:rpc_"):]))).observe(_tick())
    assert row["outcome"] == outcome and row["entry_quote_atomic"] == quote
    assert row["sim_ok"] is None and row["sim_revert"] is None


def test_a_chain_whose_blocks_are_not_where_its_block_time_says_gives_no_pin():
    behind = Node(head=SECOND_BLOCK - 5)                # the host's head is before the quote's second
    row = _shadow(behind).observe(_tick())
    assert row["outcome"] == "error:rpc_head_behind" and row["pin_block"] is None
    assert behind.tags() == []
    halted = Node(stamp=lambda number: SECOND + 4 if number >= SECOND_BLOCK + 45 else SECOND - 9)
    row = _shadow(halted).observe(_tick())             # the second starts outside the search window
    assert row["outcome"] == "error:rpc_block_time" and row["pin_block"] is None
    assert halted.tags() == []


def test_a_quote_time_that_cannot_be_read_costs_no_call():
    node = Node()
    row = _shadow(node).observe(_tick(quoted_at=None))
    assert row["outcome"] == "error:ValueError" and node.calls == [] and row["lag_s"] is None


def test_the_shadows_own_stamps_are_on_the_runners_clock():
    from sniper.util.clock import real_utc_now_iso
    assert S.Shadow(CHAIN, URL, owner=OWNER).now_iso_fn is real_utc_now_iso


# ---- its own host, the ceiling and the 429 pause ----

def test_every_request_goes_to_the_url_as_given(monkeypatch):
    node = Node()
    _shadow(node).observe(_tick())
    assert set(node.urls) == {URL}
    seen = []

    class Reply:
        status = 200

        def read(self):
            return b'{"result": "0x1"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen.append((request.full_url, json.loads(request.data), timeout))
        return Reply()
    monkeypatch.setattr(S.urllib.request, "urlopen", urlopen)
    assert S._post(URL, {"method": "eth_blockNumber"}, 3.0) == (200, {"result": "0x1"})
    assert seen == [(URL, {"method": "eth_blockNumber"}, 3.0)]


def test_the_calls_of_a_tick_are_spaced_not_a_burst():
    """About 10 calls inside 1.5 s drew one 429 from the research host. The shadow's own thread
    waits between two calls; across ticks too."""
    clock, at = [1000.0], []

    class Timed(Node):
        def __call__(self, url, body, timeout):
            at.append(clock[0])
            clock[0] += 0.05                            # the round trip
            return super().__call__(url, body, timeout)
    shadow = _shadow(Timed(), clock=clock)
    assert shadow.observe(_tick())["outcome"] == "ok"
    assert shadow.observe(_tick(series_id=8))["outcome"] == "ok"
    gaps = [later - earlier for earlier, later in zip(at, at[1:])]
    assert len(at) == 21 and min(gaps) == pytest.approx(S.CALL_GAP_S) == max(gaps)
    assert S.CALL_GAP_S == 0.4
    clock[0] += 61.0                                    # a call long after the last one does not wait
    before = clock[0]
    shadow.observe(_tick(series_id=9))
    assert at[21] == before


def test_the_first_429_pauses_the_shadow_for_sixty_seconds():
    clock = [1000.0]
    shadow = _shadow(Node(status=429), clock=clock)
    row = shadow.observe(_tick())
    assert row["outcome"] == "held_429" and shadow.held()
    node = Node()
    shadow.post_fn = node
    assert shadow.observe(_tick(series_id=8))["outcome"] == "held" and node.calls == []
    clock[0] += 60.1
    assert shadow.observe(_tick(series_id=9))["outcome"] == "ok"
    stats = shadow.stats()
    assert stats["http_429"] == 1 and stats["held_ticks"] == 1 and stats["rows"] == 3


def test_the_shadow_never_makes_more_calls_per_minute_than_its_ceiling():
    clock, node = [1000.0], Node()
    shadow = _shadow(node, clock=clock)                 # the real ceiling: 30 a minute
    rows = [shadow.observe(_tick(series_id=i)) for i in range(6)]
    assert [row["outcome"] for row in rows] == ["ok"] * 2 + ["over_ceiling"] * 4
    assert len(node.calls) <= 30 and [row["calls"] for row in rows[2:]] == [0] * 4   # never started
    clock[0] += 60.0
    assert shadow.observe(_tick(series_id=99))["outcome"] == "ok"
    assert shadow.stats()["over_ceiling"] == 4


@pytest.mark.parametrize("age_s", [1.5, 60, 600, 6000])
def test_a_tick_is_admitted_on_its_worst_case_which_grows_with_its_age(age_s):
    """An older tick needs a wider timestamp search. A tick admitted with exactly the calls its
    worst case names must finish: it never stops half way for the ceiling."""
    from datetime import datetime, timedelta
    now = (datetime.fromisoformat(QUOTED_AT) + timedelta(seconds=age_s)).isoformat()
    probe = _shadow(Node(), now=now)
    need = probe.need(_tick())
    node = Node(head=SECOND_BLOCK + 6 + int(age_s * 10))
    row = _shadow(node, now=now, max_calls_per_min=need).observe(_tick())
    assert row["outcome"] == "ok" and row["calls"] <= need
    assert (row["early_block"], row["pin_block"]) == (EARLY, PIN)
    refused = _shadow(Node(), now=now, max_calls_per_min=need - 1).observe(_tick())
    assert refused["outcome"] == "over_ceiling" and refused["calls"] == 0
    assert need >= _shadow(Node()).need(_tick())        # never below a fresh tick's


def test_the_ceiling_is_enforced_on_every_call_not_only_before_a_tick():
    """The host's head is 100 minutes past a quote the shadow believes fresh: the search is
    wider than the tick was admitted on, and the ceiling still holds."""
    shadow = _shadow(Node())
    need = shadow.need(_tick())
    node = Node(head=SECOND_BLOCK + 60_000)
    row = _shadow(node, max_calls_per_min=need).observe(_tick())
    assert row["outcome"] == "over_ceiling" and len(node.calls) == need == row["calls"]


# ---- reading the runner's stored quotes ----

def _ledger(tmp_path, routes):
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    for position_id, (network, route, mode) in routes.items():
        conn.execute(
            "INSERT INTO memecoin_positions (id, mint, network_id, source_user_id, execution_mode, "
            "is_open, entry_price, entry_notional, opened_at, evm_route_json) "
            "VALUES (?, ?, ?, 'u', ?, 1, 1.0, 100.0, ?, ?)",
            (position_id, MINT if position_id == 1 else "0x%040x" % position_id, network, mode,
             QUOTED_AT, json.dumps(route) if route else None))
    m.put_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=_pool_id(HOOKLESS),
                          pool_manager=Q.V4[CHAIN]["pool_manager"], key=HOOKLESS)
    conn.commit()
    return conn


def _series(conn, position_id, network, at, venue="uniswap_v2", seam="exit_decision"):
    mint = MINT if position_id == 1 else "0x%040x" % position_id
    evidence = {"expected_output_atomic": str(6 * 10 ** 15), "input_atomic": str(BAG),
                "minimum_output_atomic": str(54 * 10 ** 14), "quote_asset_usd": 2683.26,
                "slippage_bps": 1000, "route": {"protocol": venue, "pool_id": "0xwinner"},
                "route_target": "0xrouter", "venue": venue}
    cur = conn.execute(
        "INSERT INTO memecoin_route_series (position_id, mint, network_id, seam, observed_at, "
        "quoted_at, expected_usdc, open_qty, unit_price, quote_evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?, 16.0, 5000.0, 0.0032, ?)",
        (position_id, mint, network, seam, at, at, json.dumps(evidence)))
    conn.commit()
    return cur.lastrowid


V4_ROUTE = {"protocol": "uniswap_v4", "pool_id": _pool_id(HOOKLESS)}


def test_new_ticks_are_the_newest_stored_quote_of_each_real_position_on_the_chain(tmp_path):
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real"), 2: (CHAIN, V4_ROUTE, "paper"),
                              3: ("56", V4_ROUTE, "real"), 4: (CHAIN, None, "real")})
    start = S.newest_series_id(conn)
    assert start == 0
    _series(conn, 1, CHAIN, "2026-09-30T16:00:00+00:00")
    newest = _series(conn, 1, CHAIN, "2026-09-30T16:00:01+00:00")
    _series(conn, 1, CHAIN, "2026-09-30T16:00:02+00:00", seam="entry")     # not an exit decision
    _series(conn, 2, CHAIN, "2026-09-30T16:00:01+00:00")                   # a paper position
    _series(conn, 3, "56", "2026-09-30T16:00:01+00:00")                    # another chain
    routeless = _series(conn, 4, CHAIN, "2026-09-30T16:00:01+00:00")
    ticks, cursor = S.new_ticks(conn, CHAIN, after_id=start)
    assert [t["series_id"] for t in ticks] == [newest, routeless]          # one per position
    assert cursor == S.newest_series_id(conn)
    first = ticks[0]
    assert first["entry_route"] == V4_ROUTE and first["pool_key"] == HOOKLESS
    assert first["winner_venue"] == "uniswap_v2" and first["input_atomic"] == str(BAG)
    assert first["winner_pool_id"] == "0xwinner" and first["winner_slippage_bps"] == 1000
    assert first["winner_output_atomic"] == str(6 * 10 ** 15) and first["quote_asset_usd"] == 2683.26
    assert ticks[1]["entry_route"] is None and ticks[1]["pool_key"] is None
    assert S.new_ticks(conn, CHAIN, after_id=cursor) == ([], cursor)       # nothing new


def test_a_position_is_sampled_at_most_once_per_interval(tmp_path):
    clock = [1000.0]
    shadow = _shadow(Node(), clock=clock, sample_every_s=5.0)
    assert shadow.due(66000) is True
    shadow.observe(_tick())
    assert shadow.due(66000) is False and shadow.due(66001) is True
    clock[0] += 5.0
    assert shadow.due(66000) is True


def _paused(shadow, clock):
    shadow._held_until = clock[0] + 60.0


def _full(shadow, clock):
    shadow._window.extend([clock[0]] * shadow.max_calls)


def _keyless(shadow, clock):
    pass


@pytest.mark.parametrize("state, outcome, pool_id", [
    (_paused, "held", _pool_id(HOOKLESS)), (_full, "over_ceiling", _pool_id(HOOKLESS)),
    (_keyless, "no_pool_key", "0x" + "77" * 32),
], ids=["held", "over_ceiling", "no_pool_key"])
def test_a_tick_that_is_not_read_is_sampled_like_the_rest(tmp_path, state, outcome, pool_id):
    """A pause, a full ceiling or a missing key: one row per interval, not one per 1 s pass."""
    conn = _ledger(tmp_path, {1: (CHAIN, {"protocol": "uniswap_v4", "pool_id": pool_id}, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock = [1000.0]
    shadow = _shadow(Node(), clock=clock, sample_every_s=5.0)
    state(shadow, clock)
    cursor = S.newest_series_id(conn)
    for second in range(7):                             # a new stored quote before every pass
        _series(conn, 1, CHAIN, "2026-09-30T16:00:0%d+00:00" % second)
        cursor = S.run_pass(shadow, conn, out, cursor)
        clock[0] += 1.0
    rows = out.execute("SELECT outcome FROM memecoin_exit_venue_shadow").fetchall()
    assert [r[0] for r in rows] == [outcome, outcome]   # at 0 s and at 5 s


# ---- the last quote of a closed position is always read ----

def _close(conn, position_id):
    conn.execute("UPDATE memecoin_positions SET is_open = 0 WHERE id = ?", (position_id,))
    conn.commit()


def _rows(out):
    return [tuple(r) for r in out.execute(
        "SELECT series_id, outcome FROM memecoin_exit_venue_shadow ORDER BY series_id")]


def test_the_last_quote_of_a_closed_position_is_read_whatever_the_sampling(tmp_path):
    """Sampling reads one tick per interval. The tick a rail fired on is the LAST one."""
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real"), 5: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock, node = [1000.0], Node()
    shadow = _shadow(node, clock=clock)
    cursor = S.newest_series_id(conn)
    first = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    other = _series(conn, 5, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    cursor = S.run_pass(shadow, conn, out, cursor)                      # both sampled
    clock[0] += 60.0
    shadow.sample_every_s = 1e9                                         # never due again
    _series(conn, 1, CHAIN, "2026-09-30T16:00:01.500000+00:00")
    _series(conn, 5, CHAIN, "2026-09-30T16:00:01.500000+00:00")
    cursor = S.run_pass(shadow, conn, out, cursor)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:02.500000+00:00")
    later = _series(conn, 5, CHAIN, "2026-09-30T16:00:02.500000+00:00")
    _close(conn, 1)
    cursor = S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(first, "ok"), (other, "ok"), (fired, "ok")]   # 5 is open: sampled only
    row = dict(out.execute("SELECT * FROM memecoin_exit_venue_shadow WHERE series_id = ?",
                           (fired,)).fetchone())
    assert row["pin_block"] == SECOND_BLOCK + 22                         # its own quote time
    calls = len(node.calls)
    assert S.run_pass(shadow, conn, out, cursor) == cursor               # read once
    assert len(node.calls) == calls and shadow.stats()["positions_tracked"] == 1
    _close(conn, 5)
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out)[-1] == (later, "ok") and shadow.stats()["positions_tracked"] == 0


def test_a_last_quote_the_sampling_already_read_is_not_read_again(tmp_path):
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    node = Node()
    shadow = _shadow(node)
    cursor = S.newest_series_id(conn)
    only = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    cursor = S.run_pass(shadow, conn, out, cursor)
    calls = len(node.calls)
    _close(conn, 1)
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(only, "ok")] and len(node.calls) == calls
    assert shadow.stats()["positions_tracked"] == 0


@pytest.mark.parametrize("state, waiting", [(_paused, "held"), (_full, "over_ceiling")],
                         ids=["held", "over_ceiling"])
def test_a_last_quote_waits_for_room_and_replaces_the_row_that_said_it_was_not_read(
        tmp_path, state, waiting):
    """A pinned read does not expire: the last quote is read when the pause or the minute ends."""
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock, node = [1000.0], Node()
    shadow = _shadow(node, clock=clock)
    state(shadow, clock)
    cursor = S.newest_series_id(conn)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    _close(conn, 1)
    cursor = S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, waiting)] and node.calls == []
    clock[0] += 30.0
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, waiting)] and shadow.stats()["positions_tracked"] == 1
    clock[0] += 31.0
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, "ok")] and shadow.stats()["positions_tracked"] == 0


@pytest.mark.parametrize("failures, outcome, how", [
    (2, "ok", "reset"), (5, "error:URLError", "reset"), (2, "ok", "502"), (5, "error:http_502", "502")])
def test_a_last_quote_the_host_did_not_answer_is_read_again_a_bounded_number_of_times(
        tmp_path, failures, outcome, how):
    """Gamma LOW 3. A pinned read does not expire, so a transport failure (no answer, a 5xx, a
    timeout) on a closed position's last quote is retried; after LAST_TICK_RETRIES it is kept."""
    import urllib.error
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock, node, left = [1000.0], Node(), [failures]

    failed_at = []

    def flaky(url, body, timeout):
        if left[0] > 0 and body["method"] == "eth_call":
            left[0] -= 1
            failed_at.append(clock[0])
            if how == "502":
                return 502, {}
            raise urllib.error.URLError("connection reset")
        return node(url, body, timeout)
    shadow = _shadow(flaky, clock=clock)
    cursor = S.newest_series_id(conn)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    _close(conn, 1)
    assert S.LAST_TICK_RETRIES == 3 and S.LAST_TICK_RETRY_AFTER_S == (60.0, 300.0)
    for _ in range(40):                                 # passes every 10 s for 400 s
        cursor = S.run_pass(shadow, conn, out, cursor)
        clock[0] += 10.0
    assert _rows(out) == [(fired, outcome)] and shadow.stats()["positions_tracked"] == 0
    # Spaced, not pass after pass: a host outage of a minute still gets its fire tick read.
    gaps = [later - earlier for earlier, later in zip(failed_at, failed_at[1:])]
    assert len(failed_at) == min(failures, S.LAST_TICK_RETRIES)
    assert all(gap >= wait for gap, wait in zip(gaps, S.LAST_TICK_RETRY_AFTER_S)), gaps


def test_a_success_after_a_transport_failure_is_a_read(tmp_path):
    """Gamma LOW on 290c0c42e: the flag is reset for every observation. A last tick that fails
    on the transport once and then succeeds is recorded ok, marked read, and forgotten."""
    import urllib.error
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock, node, left = [1000.0], Node(), [1]

    def once(url, body, timeout):
        if left[0] and body["method"] == "eth_call":
            left[0] -= 1
            raise urllib.error.URLError("reset")
        return node(url, body, timeout)
    shadow = _shadow(once, clock=clock)
    cursor = S.newest_series_id(conn)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    _close(conn, 1)
    cursor = S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [] and shadow.transient and shadow.last_tick_failures[1] == 1
    clock[0] += 60.0
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, "ok")] and not shadow.transient
    assert shadow.stats()["positions_tracked"] == 0 and 1 not in shadow.last_tick_failures
    assert 1 not in shadow.last_tick_retry_at and shadow.read == {}


def test_a_last_quote_cut_by_a_429_is_read_after_the_pause(tmp_path):
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock = [1000.0]
    shadow = _shadow(Node(status=429), clock=clock)
    cursor = S.newest_series_id(conn)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    _close(conn, 1)
    cursor = S.run_pass(shadow, conn, out, cursor)
    assert shadow.held() and shadow.stats()["positions_tracked"] == 1
    assert _rows(out) == []                             # a read cut by the 429 is not a row
    shadow.post_fn = Node()
    clock[0] += 61.0
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, "ok")] and shadow.stats()["positions_tracked"] == 0


def test_a_position_closed_in_the_pass_of_its_last_quote_is_read_once(tmp_path):
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real"), 4: (CHAIN, {"protocol": "kyberswap"}, "real")})
    out = store.connect(tmp_path / "captures.db")
    node = Node()
    shadow = _shadow(node)
    cursor = S.newest_series_id(conn)
    fired = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    routed = _series(conn, 4, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    cursor = S.run_pass(shadow, conn, out, cursor)      # 4 has no entry pool: recorded once
    _series(conn, 4, CHAIN, "2026-09-30T16:00:01.500000+00:00")
    _close(conn, 1)
    _close(conn, 4)
    before = len(node.calls)
    S.run_pass(shadow, conn, out, cursor)
    assert _rows(out) == [(fired, "ok"), (routed, "unpinnable:kyberswap")]
    assert len(node.calls) == before == 11              # one tick's reads, none for the close
    fresh = _ledger_position(conn, 9)
    last = _series(conn, 9, CHAIN, "2026-09-30T16:00:02.500000+00:00")
    _close(conn, 9)                                     # its first quote is its last, and it is due
    S.run_pass(shadow, conn, out, S.newest_series_id(conn) - 1)
    assert _rows(out)[-1] == (last, "ok") and len(node.calls) - before <= 10 and fresh == 9


def _ledger_position(conn, position_id):
    conn.execute(
        "INSERT INTO memecoin_positions (id, mint, network_id, source_user_id, execution_mode, "
        "is_open, entry_price, entry_notional, opened_at, evm_route_json) "
        "VALUES (?, ?, ?, 'u', 'real', 1, 1.0, 100.0, ?, ?)",
        (position_id, "0x%040x" % position_id, CHAIN, QUOTED_AT, json.dumps(V4_ROUTE)))
    conn.commit()
    return position_id


def test_the_worst_case_counts_the_block_time_read_while_it_is_due():
    shadow = _shadow(Node())
    before = shadow.need(_tick())
    shadow.observe(_tick())
    assert shadow.need(_tick()) == before - 1


def test_a_pass_writes_one_immutable_row_per_tick_and_never_the_same_series_twice(tmp_path):
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    out = store.connect(tmp_path / "captures.db")
    shadow = _shadow(Node())
    cursor = S.newest_series_id(conn)
    series_id = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
    cursor = S.run_pass(shadow, conn, out, cursor)
    assert S.run_pass(shadow, conn, out, cursor) == cursor
    rows = [dict(r) for r in out.execute("SELECT * FROM memecoin_exit_venue_shadow")]
    assert len(rows) == 1 and rows[0]["series_id"] == series_id and rows[0]["outcome"] == "ok"
    assert rows[0]["entry_quote_atomic"] == str(8 * 10 ** 15) and rows[0]["sim_ok"] == 1
    assert rows[0]["pin_block"] == SECOND_BLOCK + 2 and rows[0]["early_block"] == SECOND_BLOCK - 5
    assert rows[0]["winner_pool_id"] == "0xwinner" and rows[0]["sim99_ok"] == 1
    store.record_exit_venue_shadow(out, dict(rows[0], outcome="again"))     # first write wins
    assert out.execute("SELECT outcome FROM memecoin_exit_venue_shadow").fetchall()[0][0] == "ok"


def test_a_position_with_no_entry_pool_is_recorded_once_not_every_pass(tmp_path):
    conn = _ledger(tmp_path, {4: (CHAIN, {"protocol": "kyberswap"}, "real")})
    out = store.connect(tmp_path / "captures.db")
    clock = [1000.0]
    shadow = _shadow(Node(), clock=clock)
    cursor = S.newest_series_id(conn)
    for second in range(3):
        _series(conn, 4, CHAIN, "2026-09-30T16:00:0%d+00:00" % second)
        cursor = S.run_pass(shadow, conn, out, cursor)
        clock[0] += 30.0
    rows = out.execute("SELECT outcome FROM memecoin_exit_venue_shadow").fetchall()
    assert [r[0] for r in rows] == ["unpinnable:kyberswap"]


# ---- the knob ----

def test_the_shadow_is_off_until_a_chain_is_given_its_own_read_host(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663", raising=False)
    assert cfg.memecoin_exit_venue_shadow_hosts({}) == {}
    env = {"MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663": " https://host/x ",
           "MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_56": "",
           "MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_solana": "https://nope",
           "MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_8453": "ftp://nope"}
    assert cfg.memecoin_exit_venue_shadow_hosts(env) == {"4663": "https://host/x"}


def test_the_store_columns_and_the_table_agree(tmp_path):
    out = store.connect(tmp_path / "captures.db")
    have = [r[1] for r in out.execute("PRAGMA table_info(memecoin_exit_venue_shadow)")]
    assert have[1:] == list(store.EXIT_VENUE_SHADOW_COLUMNS)
    assert not re.search(r"sig|signature", " ".join(have))
    row = _shadow(Node()).observe(_tick())
    assert set(row) == set(store.EXIT_VENUE_SHADOW_COLUMNS)       # no column the shadow never fills


# ---- the worker in the capture process ----

OWNER_ENV = {"MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663": URL,
             "MEMECOIN_EXIT_VENUE_SHADOW_OWNER": OWNER}


def test_the_read_host_is_never_a_funded_rpc_host(tmp_path, monkeypatch):
    """Gamma LOW 4 / Alpha. Compared by host NAME: another key on a funded provider's host
    spends the same provider. The custody RPC, the pool and the archive pool all count."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {CHAIN: (
        "https://rpc.funded.example/", "https://robinhood-mainnet.g.alchemy.example/v2/KEY1")})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {CHAIN: ("https://archive.example/",)})
    for url in ("https://robinhood-mainnet.g.alchemy.example/v2/OTHERKEY", "https://archive.example/x",
                "https://custody.example/k"):
        env = dict(OWNER_ENV, MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663=url,
                   MEMECOIN_EVM_RPC_URL_4663="https://custody.example/k")
        worker, why = S.build_worker(env, cfg, ledger_path=tmp_path / "l.db", store_path=tmp_path / "c.db")
        assert worker is None and "is a funded RPC host" in why, url
    env = dict(OWNER_ENV, MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663="https://research.example/key",
               MEMECOIN_EVM_RPC_URL="https://research.example/")      # the generic fallback counts
    assert S.build_worker(env, cfg, ledger_path=tmp_path / "l.db", store_path=tmp_path / "c.db")[0] is None
    env = dict(OWNER_ENV, MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663="https://research.example/key",
               MEMECOIN_EVM_RPC_URL_4663="https://custody.example/k")
    worker, why = S.build_worker(env, cfg, ledger_path=tmp_path / "l.db", store_path=tmp_path / "c.db")
    assert why is None and worker.shadows[0].url == "https://research.example/key"


@pytest.mark.parametrize("env, reason", [
    ({}, "no MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_<chain> is set"),
    ({"MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_4663": "https://host/x"}, "MEMECOIN_EXIT_VENUE_SHADOW_OWNER"),
    (dict(OWNER_ENV, MEMECOIN_EXIT_VENUE_SHADOW_OWNER="0x1234"), "MEMECOIN_EXIT_VENUE_SHADOW_OWNER"),
    ({"MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_56": "https://host/x",
      "MEMECOIN_EXIT_VENUE_SHADOW_OWNER": OWNER}, "no shadowed chain has a Uniswap V4 deployment"),
    ({"MEMECOIN_EXIT_VENUE_SHADOW_READ_URL_5042": "https://host/x",     # Gamma: native IS the quote
      "MEMECOIN_EXIT_VENUE_SHADOW_OWNER": OWNER}, "a quote token apart from its native coin"),
])
def test_the_worker_is_not_built_without_a_host_and_the_wallets_address(tmp_path, env, reason):
    worker, why = S.build_worker(env, cfg, ledger_path=tmp_path / "l.db", store_path=tmp_path / "c.db")
    assert worker is None and reason in why


def test_the_worker_starts_at_the_newest_quote_and_writes_what_comes_after(tmp_path, monkeypatch):
    import time
    conn = _ledger(tmp_path, {1: (CHAIN, V4_ROUTE, "real")})
    _series(conn, 1, CHAIN, "2026-09-30T15:59:00+00:00")        # before the start: never read
    node = Node()
    monkeypatch.setattr(S, "_post", node)
    worker, why = S.build_worker(OWNER_ENV, cfg, ledger_path=tmp_path / "ledger.db",
                                 store_path=tmp_path / "captures.db")
    assert why is None and [shadow.chain for shadow in worker.shadows] == [CHAIN]
    assert worker.shadows[0].owner == OWNER and worker.shadows[0].url == URL
    worker.poll_s = 0.02
    worker.shadows[0].sleep = lambda seconds: None      # the spacing has its own test
    worker.start()
    try:
        end = time.monotonic() + 5.0
        while worker.last_pass is None and time.monotonic() < end:
            time.sleep(0.01)
        after = _series(conn, 1, CHAIN, "2026-09-30T16:00:00.500000+00:00")
        out = store.connect(tmp_path / "captures.db")
        rows = []
        while not rows and time.monotonic() < end:
            rows = out.execute("SELECT series_id, outcome FROM memecoin_exit_venue_shadow").fetchall()
            time.sleep(0.01)
    finally:
        worker.stop()
        worker.join(5)
    assert [tuple(r) for r in rows] == [(after, "ok")]
    stats = worker.stats()
    assert stats["errors"] == 0 and stats["chains"][0]["rows"] == 1
    assert stats["chains"][0]["calls"] == len(node.calls) and set(node.urls) == {URL}


def test_the_capture_loop_starts_it_and_reports_it_in_its_heartbeat():
    loop = (ROOT / "scripts/memecoin_refusal_capture_loop.py").read_text()
    assert "exit_venue_shadow.build_worker(" in loop
    assert '"exit_venue_shadow": exit_shadow.stats() if exit_shadow is not None else None' in loop
