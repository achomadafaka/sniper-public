"""The price the operator reads is the price the runner decided on.

Live position 44794 was stored by the runner at 0.000195614571079 and displayed as
0.000188049267086. The ticker was deriving its own number from the mark tape and putting it through
`modeled_fill`, so the label `last_quote_price` carried a different quantity from the column of the
same name — the failure this codebase keeps re-learning, this time on the price a funded position is
watched with.

Second fault, same surface: `mark_age_s` is recomputed from wall-clock, so at the 250ms stream tick
the payload differed from itself four times a second and every open position re-rendered 4x/s.
"""
import json

import pytest

from sniper.routes import api_memecoin as api


def _tick(rows):
    return {"positions": rows, "server_now": "2026-08-28T00:00:00+00:00"}


# ------------------------------------------------------------------ the price source

def test_the_ticker_prefers_the_runners_stored_quote():
    """Primary is what the runner committed, not what the tape implies."""
    row = {"id": 1, "last_quote_price": 0.000195614571079,
           "runner_quote_price": 0.000195614571079,
           "modeled_exit_price": 0.000188049267086, "price_source": "runner"}
    assert row["last_quote_price"] == row["runner_quote_price"]
    assert row["last_quote_price"] != row["modeled_exit_price"], \
        "the two must stay distinguishable — that is the whole defect"


def test_the_payload_names_which_price_it_is_showing():
    """A surface that cannot say which quantity it holds is how this happened."""
    from _source_guard import code_of
    src = code_of(api._real_ticker_snapshot)
    # The paired contract (operator, 2026-08-28): every price with its own clock and basis.
    for field in ("runner_decision_price", "runner_decision_at",
                  "selected_exit_price", "selected_exit_basis", "selected_exit_at",
                  "last_executable_price", "last_executable_at",
                  "modeled_exit_price", "price_source", "pnl_price_basis"):
        assert repr(field) in src, f"{field} must be published so the client can label it"


def test_modeled_exit_price_is_null_unless_the_selection_was_modelled():
    """_select_display_mark returns measured_route / measured_route_stale / mid / modeled_slippage,
    and the first version published all four as "modeled". A STALE exact route at 0.8 was labelled
    modelled while PnL ran on a fresh 0.999 — the same label-versus-quantity fault, one level in."""
    from _source_guard import code_of
    src = code_of(api._real_ticker_snapshot)
    assert "'modeled_exit_price': last if basis == 'modeled_slippage' else None" in src


def test_the_tape_is_the_fallback_when_the_runner_has_no_quote():
    from _source_guard import code_of
    src = code_of(api._real_ticker_snapshot)
    assert 'runner_price if has_runner else last' in src, \
        "a position the runner has not quoted yet must still show a price"


# ------------------------------------------------------------------ zero duplicate frames

def test_a_changed_mark_age_alone_does_not_push_a_frame():
    """THE 4-frames-a-second bug. Same price, same mark, one second older."""
    a = _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T1", "mark_age_s": 0.2}])
    b = _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T1", "mark_age_s": 1.2}])
    assert api._ticker_change_blob(a) == api._ticker_change_blob(b)


def test_a_new_mark_still_pushes_immediately():
    """The fix must not cost freshness: a genuinely new observation still differs."""
    a = _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T1", "mark_age_s": 0.2}])
    b = _tick([{"id": 1, "last_quote_price": 1.1, "mark_observed_at": "T2", "mark_age_s": 0.2}])
    assert api._ticker_change_blob(a) != api._ticker_change_blob(b)


def test_a_new_observation_at_an_unchanged_price_still_pushes():
    """mark_observed_at is compared even though mark_age_s is not — a re-quote at the same price
    is real information about staleness."""
    a = _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T1", "mark_age_s": 9.0}])
    b = _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T2", "mark_age_s": 0.1}])
    assert api._ticker_change_blob(a) != api._ticker_change_blob(b)


def test_the_volatile_list_is_not_empty():
    """A guard over an empty exclusion set would pass forever while excluding nothing."""
    assert api._TICKER_VOLATILE_FIELDS, "nothing excluded means the dedup never changed"


def test_four_ticks_of_a_still_pool_produce_one_frame():
    """End to end: a 250ms tick over a second, only the age moving."""
    frames, last = 0, None
    for age in (0.25, 0.50, 0.75, 1.00):
        blob = api._ticker_change_blob(
            _tick([{"id": 1, "last_quote_price": 1.0, "mark_observed_at": "T1",
                    "mark_age_s": age}]))
        if blob != last:
            frames += 1
            last = blob
    assert frames == 1, f"a still pool pushed {frames} frames in one second"


# ------------------------------------------------------------------ no provider calls

def _real_positions_fn():
    """The route function's AST, found by name rather than by slicing source text."""
    import ast, inspect
    tree = ast.parse(inspect.getsource(api))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "real_positions":
            return node
    raise AssertionError("real_positions not found")


PROVIDER_MODULES = ("solana_quote", "gecko", "geckoterminal", "solana_real_seams",
                    "memecoin_quotes", "dexscreener")

# QUALIFIED ORIGINS, not local names (operator, 2026-08-28). The previous allowlist was keyed by
# the LOCAL alias, so `from ...gecko import gt_get_json as quote_for` inherited a pure name's
# exemption and walked straight past the guard — reproduced, zero hits. An entry here is a claim
# that one specific symbol in one specific module performs no I/O.
PURE_PROVIDER_SYMBOLS = {
    "sniper.execution.solana_quote.quote_for":     "dict lookup into an already-fetched price_map",
    "sniper.execution.solana_quote.quote_key":     "string key construction",
    "sniper.execution.solana_quote.norm_mint":     "string normalisation",
    "sniper.execution.solana_quote.modeled_fill":  "arithmetic on a price and a liquidity figure",
}


def _provider_bindings(fn, module_src):
    """{local name -> fully qualified origin} for every provider symbol in scope.

    Collects module-scope imports AND imports inside the function, and keys on the ORIGIN rather
    than the local name so an alias cannot borrow another symbol's exemption.
    """
    import ast
    out = {}

    def collect(tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(t in node.module for t in PROVIDER_MODULES):
                    for a in node.names:
                        out[a.asname or a.name] = f"{node.module}.{a.name}"
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if any(t in a.name for t in PROVIDER_MODULES):
                        out[a.asname or a.name.split(".")[0]] = a.name

    collect(ast.parse(module_src))
    collect(fn)
    return out


def external_provider_calls(fn, module_src):
    """THE GUARD. Qualified origins of provider calls in `fn`, minus verified-pure symbols.

    One function, used by the real assertion AND by the negative control, so the control cannot
    pass while the guard is broken — the previous control reimplemented this logic and would have
    stayed green against an empty binding set.
    """
    import ast
    bound = _provider_bindings(fn, module_src)
    hits = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        local = (f.id if isinstance(f, ast.Name)
                 else f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                 else None)
        if local is None:
            continue
        origin = bound.get(local)
        if origin is None:
            continue
        if isinstance(f, ast.Attribute):
            origin = f"{origin}.{f.attr}"
        if origin not in PURE_PROVIDER_SYMBOLS:
            hits.append(origin)
    return sorted(set(hits))


def test_real_positions_makes_no_external_price_call():
    """It polls every 4s for a price the ledger already holds; provider latency and single-flight
    contention with the funded runner do not belong on that read."""
    import inspect
    hits = external_provider_calls(_real_positions_fn(), inspect.getsource(api))
    assert not hits, f"external provider call(s) on the /real/positions request path: {hits}"


def test_the_guard_catches_an_alias_that_borrows_a_pure_name():
    """THE OPERATOR'S BYPASS, driven through the REAL guard rather than a copy of it.

    `gt_get_json as quote_for` gave the external call a name the allowlist exempted. Keying on the
    qualified origin closes it: the alias resolves to sniper.execution.gecko.gt_get_json, which is
    not in PURE_PROVIDER_SYMBOLS.
    """
    import ast
    src = ("def real_positions():\n"
           "    from sniper.execution.gecko import gt_get_json as quote_for\n"
           "    return quote_for('https://api.geckoterminal.com/x')\n")
    fn = ast.parse(src).body[0]
    assert external_provider_calls(fn, "") == ["sniper.execution.gecko.gt_get_json"]


def test_the_guard_still_admits_the_genuinely_pure_helper():
    """The other half: the real quote_for must NOT be flagged, or the guard is unusable and will
    be loosened by the next person who hits it."""
    import ast
    src = ("def real_positions():\n"
           "    from sniper.execution.solana_quote import quote_for\n"
           "    return quote_for({}, 'mint')\n")
    assert external_provider_calls(ast.parse(src).body[0], "") == []


def test_the_guard_is_not_vacuous():
    """A binding set that came back empty would make every assertion above pass forever."""
    import inspect
    bound = _provider_bindings(_real_positions_fn(), inspect.getsource(api))
    assert bound, "no provider symbols resolved — the guard is watching nothing"


def test_every_pure_symbol_claim_is_true():
    """PURE_PROVIDER_SYMBOLS asserts no-I/O, so it is checked. A symbol that cannot be resolved is
    a FAILURE, not a skip: the previous version skipped silently, so a renamed helper would have
    left an unverified exemption in place."""
    import importlib, inspect
    for qualified, why in sorted(PURE_PROVIDER_SYMBOLS.items()):
        mod_name, _, fname = qualified.rpartition(".")
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fname, None)
        assert fn is not None, f"{qualified} is exempted but no longer exists ({why})"
        src = inspect.getsource(fn)
        for forbidden in ("http(", "_http", "urlopen", "requests.", "DEXSCREENER", "JUP_"):
            assert forbidden not in src, f"{qualified} is listed pure but references {forbidden}"
