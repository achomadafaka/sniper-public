"""The flap.sh entry safety wrapper through build_real_evm_context (memecoin_evm_leg.py) and the
REAL evm_real_entry -> execute_entry_evm path, chain 56, MEMECOIN_EVM_FLAP_CURVE_ENABLED on.
Mirrors tests/test_memecoin_evm_launchpad_curve_bound_safety.py, plus one step further Alpha asked
for explicitly (2026-09-22, "the last gate before I put the flag to the operator"): not just that
the gate refuses/passes, but that a PASSING gate lets the REAL executor reach sign_fn with the
entry's native transaction.value intact, using the real dex_flap_curve_quote/wrap_token_safety_fn,
not a restated version of their logic.

(a)/(b) build `token_safety_fn` via a REAL build_real_evm_context("56", ...) call, not a
separately-constructed wrap_token_safety_fn -- Alpha's exact ask, twice: proving the function
behaves correctly in isolation is not the same claim as proving build_real_evm_context WIRES it
correctly, and a bound argument (rpc, taker, safety_deadline_scope) mismatch between the two would
pass the former and fail the latter silently.

(a) An override exit-sim that reverts refuses the entry before approve/sign/send.
(b) An override exit-sim that succeeds proceeds to sign_fn with transaction.value == the entry
    quote's own native amount, unchanged.
(c) With the flag OFF, evm_flap_curve.wrap_token_safety_fn is never called by
    build_real_evm_context, and chain 4663's own launchpad_curve wiring is unaffected by the
    flap flag's value either way.
"""
import json
import sqlite3

import pytest

from sniper.execution import evm_flap_curve as curve_mod
from sniper.execution import evm_wallet
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg

PORTAL = "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0"
YEE = "0x8d411a22bf0194b2f5ef665f8a2c7a9430ee7777"
NATIVE = "0x" + "00" * 20
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
TRADE_SIZE_USD = 50.0
NATIVE_USD_PRICE = 500.0
# Must equal what wrap_token_safety_fn's own usd-probe derives (amount_in = usd/px*1e18) --
# the parallel safety path checks exit-viability for THIS amount before the real amount is known,
# and the entry quote built directly below must be for the SAME amount to be a fair test of
# "did a PASSING check let a matching entry through", not an artefact of two different sizes.
AMOUNT_IN = int(TRADE_SIZE_USD / NATIVE_USD_PRICE * 10 ** 18)


class _Ev:
    def __init__(self, mint=YEE):
        self.mint = mint
        self.source_user_id = "u1"
        self.network_id = "56"


def _c(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, str(tmp_path / "kill")


def _state(**overrides):
    reserve = 304288643025442896
    r = 6140000000000000000
    h = 107036752000000000000000000
    k = 6797205657280000000000000000
    state = {
        "status": 1, "reserve": reserve, "circulating_supply": 52272443043027083493254115,
        "price": 6109695396, "token_version": 5, "r": r, "h": h, "k": k,
        "dex_supply_thresh": 800000000000000000000000000, "quote_token": NATIVE,
        "native_to_quote_swap_enabled": 0, "extension_id": "0" * 64, "creator_fee_bps": 100,
        "portal": PORTAL, "mint": YEE,
    }
    state.update(overrides)
    state["coin_reserve"] = (state["k"] * 10 ** 18) // (state["reserve"] + state["r"]) - state["h"]
    state["fee_bps"] = curve_mod._PLATFORM_FEE_BPS + state["creator_fee_bps"]
    return state


EXACT = object()   # the override sim pays exactly what the curve pays for the amount sold


def _stub_curve(monkeypatch, state, *, sell_via_override):
    """`sell_via_override`: the override-simulated sell payout, None to make it revert, or EXACT to
    pay the curve's own payout for whatever amount is sold (the safety check runs at the approved
    clip since 1b46ef794, so a payout pinned to one amount would not be exact for another).
    Patches the REAL module functions wrap_token_safety_fn and dex_flap_curve_quote both use --
    the same seam, not a duplicated one per caller."""
    monkeypatch.setattr(
        curve_mod, "curve_state",
        lambda chain, mint, rpc_url, http=None: state if str(mint).lower() == YEE else None)

    def simulate_fn(portal, input_token, output_token, amount_atomic, taker, rpc_url, *,
                    http=None, overrides=None):
        if str(input_token).lower() == NATIVE:
            return curve_mod.curve_buy_out(state, amount_atomic)
        if overrides is None:
            return None
        return curve_mod.curve_sell_out(state, amount_atomic) if sell_via_override is EXACT else sell_via_override
    monkeypatch.setattr(curve_mod, "simulate_swap_exact_input", simulate_fn)
    monkeypatch.setattr(
        curve_mod, "discover_sell_overrides",
        lambda mint, spender, amount, *, taker, rpc_url, http=None: {"stand-in": True})


def _seq(vals):
    it = {"i": 0}

    def f(*_a, **_k):
        v = vals[min(it["i"], len(vals) - 1)]
        it["i"] += 1
        return v
    return f


def _identity_http(chain_int):
    def http(_method, _url, **kwargs):
        request = json.loads(kwargs["body"])
        if request["method"] == "eth_chainId":
            return 200, {"result": hex(chain_int)}, None
        return 200, {"result": "0x"}, None
    return http


def _keypair(tmp_path, name):
    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / name)
    generate_keypair_file(kpf)
    return kpf


def _build_ctx_56(monkeypatch, tmp_path, *, flap_enabled):
    """A REAL build_real_evm_context("56", ...) call -- the exact function the runner calls,
    not a stand-in. native_usd_price is patched at its own module (the deferred import inside
    wrap_token_safety_fn resolves it there) so the usd-probe derivation is deterministic without
    a real price-feed call over the identity-only http fake above."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", _keypair(tmp_path, "evm56.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", flap_enabled)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_56", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://bsc-dataseed.bnbchain.org")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    monkeypatch.setattr(evm_wallet, "native_usd_price", lambda chain, http=None: NATIVE_USD_PRICE)
    rc = leg.build_real_evm_context("56", http=_identity_http(56))
    assert rc is not None
    return rc


def _executor_seams(quote):
    """`quote`: a fixed quote, or a callable usd -> quote built for the clip the executor is asked."""
    calls = {"approve": [], "sign": [], "send": 0, "quotes": []}

    def quote_fn(_mint, usd):
        q = quote(usd) if callable(quote) else quote
        calls["quotes"].append((usd, q))
        return q

    token_reads = {"n": 0}

    def token_fn(*_a, **_k):
        # Before the buy: 0. After it: the quoted buy amount of the clip the executor bought.
        token_reads["n"] += 1
        if token_reads["n"] == 1 or not calls["quotes"]:
            return 0.0
        return int(calls["quotes"][-1][1]["buyAmount"])

    def approve_fn(spender, amount):
        calls["approve"].append((spender, amount))
        return True

    def sign_fn(tx):
        calls["sign"].append(tx)
        return "0xraw", "0xhash", 7

    def send_fn(raw):
        calls["send"] += 1
        return "0xsent"

    def execute_fn(conn, pending_id, mint, usd_amount, now_iso, safety_gate_fn=None):
        return execute_entry_evm(
            conn, pending_id=pending_id, mint=mint, network_id="56", usd_amount=usd_amount,
            strategy="alpha_b", source_user_id="t", now_iso=now_iso,
            quote_fn=quote_fn, approve_fn=approve_fn, sign_fn=sign_fn,
            send_fn=send_fn, confirm_fn=lambda h: "confirmed",
            usdc_fn=_seq([1.0, 0.99]), token_fn=token_fn,
            safety_gate_fn=safety_gate_fn,
        )
    return execute_fn, calls


@pytest.fixture(autouse=True)
def _bsc_allowlisted(monkeypatch):
    # evm_real_entry refuses a chain outside the allowlist (Gamma H3, 2026-09-30); production has a
    # context only for allowlisted chains, so the test allowlists the chain it drives.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "56"})


def test_entry_refuses_before_any_mutation_when_the_override_exit_sim_reverts(monkeypatch, tmp_path):
    # STUB BEFORE BUILDING THE CONTEXT: wrap_token_safety_fn resolves its state_fn default
    # (curve_state) EAGERLY at construction time (evm_flap_curve.py:807, matching
    # evm_launchpad_curve.py's own pattern), not per-call -- so build_real_evm_context must run
    # AFTER the module is patched, exactly like the reference bound-safety file always passes an
    # explicit state_fn instead of relying on a post-construction patch.
    state = _state()
    _stub_curve(monkeypatch, state, sell_via_override=None)   # override sell reverts
    rc = _build_ctx_56(monkeypatch, tmp_path, flap_enabled=True)
    c, kill = _c(monkeypatch, tmp_path)

    entry_quote = curve_mod.dex_flap_curve_quote(
        "56", NATIVE, YEE, AMOUNT_IN, rc["address"], rpc_url="https://rpc.example")
    assert entry_quote is not None
    execute_fn, calls = _executor_seams(entry_quote)

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:56", day="2026-09-22", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-09-22T00:00:00Z", trade_size=TRADE_SIZE_USD,
        token_safety_fn=rc["token_safety_fn"], execute_fn=execute_fn, kill_path=kill,
    )
    assert result["status"] == "unsafe", result
    assert calls["approve"] == [] and calls["sign"] == [] and calls["send"] == 0


def test_entry_proceeds_to_sign_fn_with_native_value_intact_when_the_override_exit_sim_is_exact(
        monkeypatch, tmp_path):
    state = _state()

    def quote_for(usd):
        # Built with EXPLICIT state_fn/simulate_fn (order-independent of the module patch below),
        # for the clip the executor is asked: the reservation's approved clip, which the safety
        # check also judges since 1b46ef794. One amount end to end, never two sizes.
        return curve_mod.dex_flap_curve_quote(
            "56", NATIVE, YEE, int(usd / NATIVE_USD_PRICE * 10 ** 18), "0x" + "ab" * 20,
            rpc_url="https://rpc.example",
            state_fn=lambda chain, mint, rpc_url, http=None: state,
            simulate_fn=lambda portal, i, o, amt, taker, rpc_url, *, http=None, overrides=None:
                curve_mod.curve_buy_out(state, amt))
    assert quote_for(TRADE_SIZE_USD) is not None
    # STUB BEFORE BUILDING THE CONTEXT -- see the sibling refusal test for why.
    _stub_curve(monkeypatch, state, sell_via_override=EXACT)
    rc = _build_ctx_56(monkeypatch, tmp_path, flap_enabled=True)
    c, kill = _c(monkeypatch, tmp_path)
    execute_fn, calls = _executor_seams(quote_for)

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:56", day="2026-09-22", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-09-22T00:00:00Z", trade_size=TRADE_SIZE_USD,
        token_safety_fn=rc["token_safety_fn"], execute_fn=execute_fn, kill_path=kill,
    )
    assert result["status"] == "filled", result
    approved = result["approved_clip_usd"]
    assert [usd for usd, _q in calls["quotes"]] == [approved]
    assert calls["approve"] == []                    # native-in entry needs no ERC-20 allowance
    assert len(calls["sign"]) == 1
    assert calls["sign"][0]["value"] == hex(int(approved / NATIVE_USD_PRICE * 10 ** 18))
    assert calls["sign"][0]["to"] == PORTAL


def test_flag_off_never_calls_flap_wrap_token_safety_fn(monkeypatch, tmp_path):
    def _raise(*a, **k):
        raise AssertionError("wrap_token_safety_fn must not be called with the flag off")
    monkeypatch.setattr(curve_mod, "wrap_token_safety_fn", _raise)

    rc = _build_ctx_56(monkeypatch, tmp_path, flap_enabled=False)
    assert callable(rc["token_safety_fn"])            # built fine, just not flap-wrapped


def test_flag_on_installs_the_flap_wrapper(monkeypatch, tmp_path):
    calls = []
    real_wrap = curve_mod.wrap_token_safety_fn

    def _recording(*a, **k):
        calls.append((a, k))
        return real_wrap(*a, **k)
    monkeypatch.setattr(curve_mod, "wrap_token_safety_fn", _recording)

    _build_ctx_56(monkeypatch, tmp_path, flap_enabled=True)
    assert len(calls) == 1
    assert calls[0][0][0] == "56"                     # chain positional arg


def test_flap_flag_does_not_touch_chain_4663s_launchpad_wiring(monkeypatch, tmp_path):
    """The flap flag is chain-56-gated in code (str(chain) == "56"); prove it at the wiring
    boundary, not just by reading the guard, so a future refactor that merges the two blocks
    cannot silently cross-wire them."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", _keypair(tmp_path, "evm4663.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", True)     # ON, wrong chain
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)

    def _raise(*a, **k):
        raise AssertionError("flap wrapper must never engage on chain 4663")
    monkeypatch.setattr(curve_mod, "wrap_token_safety_fn", _raise)

    rc = leg.build_real_evm_context("4663", http=_identity_http(4663))
    assert rc is not None
    assert callable(rc["token_safety_fn"])
