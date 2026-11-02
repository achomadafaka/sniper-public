"""The paper arms must never spend the funded Jupiter key.

THE INCIDENT, 2026-08-29. The promotion study's 24 paper positions priced on
`jupiter-executable-route-v2` from INSIDE the runner, so they used the funded key and the funded
IP. The runner ran 79 rate-limited of 199 quotes — 40% — and funded position 45003's exact-quote
previews failed 48 times in 62 seconds. The fail-safe held and no bad exit was taken, but the
funded exit brain was blind, and the study's own kill rule (>10% rate_limited invalidates it) was
firing at four times its threshold.

`declare_measurement_process` cannot fix this: it is process-level and these calls run inside the
funded runner, so declaring would hand the exit brain the paper key. The role is per-call.
"""
import contextvars
import threading

import pytest

from sniper.execution import solana_real_seams as S


def test_the_default_is_funded_so_a_missed_scope_shows_up_here():
    """Fails SAFE in the same direction as the process declaration: an unscoped paper call spends
    the funded key and trips a guard, rather than a funded call silently losing its key."""
    assert S.active_key_var() == S.JUP_API_KEY_VAR
    assert S.active_role() == S.ROLE_FUNDED


def test_a_paper_scope_selects_the_paper_key_and_restores_afterwards():
    with S.jupiter_role(S.ROLE_PAPER):
        assert S.active_key_var() == S.PAPER_JUP_API_KEY_VAR
        assert S.active_role() == S.ROLE_PAPER
    assert S.active_key_var() == S.JUP_API_KEY_VAR


def test_the_funded_key_is_unreachable_from_inside_a_paper_scope(monkeypatch):
    """The property that matters, asserted on the VALUE the seam would send."""
    monkeypatch.setenv(S.JUP_API_KEY_VAR, "funded-secret")
    monkeypatch.setenv(S.PAPER_JUP_API_KEY_VAR, "paper-secret")
    with S.jupiter_role(S.ROLE_PAPER):
        assert S._jup_api_key() == "paper-secret"
    assert S._jup_api_key() == "funded-secret"


def test_the_scope_does_not_leak_across_threads():
    """A ContextVar is per-context; a module global would have leaked the paper role into every
    funded quote issued by the exit sweep while a paper arm was mid-quote."""
    seen = {}

    def funded_worker():
        seen["thread"] = S.active_key_var()

    with S.jupiter_role(S.ROLE_PAPER):
        t = threading.Thread(target=funded_worker)
        t.start()
        t.join()
        assert S.active_key_var() == S.PAPER_JUP_API_KEY_VAR
    assert seen["thread"] == S.JUP_API_KEY_VAR, "a sibling thread must stay funded"


def test_an_unknown_role_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        with S.jupiter_role("reserach"):      # a typo must not silently spend the funded key
            pass


# ---------------------------------------------------------------- the paper call sites

def test_both_paper_fill_paths_are_scoped():
    """Enumerated from the source: a third paper quote added later must scope itself too."""
    import ast
    import pathlib
    from _source_guard import code_of

    path = pathlib.Path(
        __file__).resolve().parents[1] / "sniper/services/memecoin_copy_trader.py"
    tree = ast.parse(code_of(path))
    for fname in ("executable_entry_fill", "executable_exit_fill"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        src = ast.unparse(fn)
        assert "jupiter_role(ROLE_PAPER)" in src, f"{fname} quotes on the funded key"
        # and the scope must wrap the QUOTE, not merely appear somewhere in the function
        quote = "quote_entry(" if "entry" in fname else "quote_exit("
        i = src.index("jupiter_role(ROLE_PAPER)")
        assert quote in src[i:i + 400], f"{fname}'s scope does not cover its quote call"


def test_the_paper_path_never_names_the_funded_key_var():
    """Same shape as the existing measurement-module guard. Scans CODE only."""
    import pathlib
    import re
    from _source_guard import code_of

    for rel in ("sniper/services/memecoin_copy_trader.py",
                "sniper/services/memecoin_quotes.py"):
        path = pathlib.Path(__file__).resolve().parents[1] / rel
        if not path.exists():
            continue
        for i, line in enumerate(code_of(path).splitlines(), 1):
            assert not re.search(r"(?<![A-Z_])MEMECOIN_JUPITER_API_KEY", line), (
                f"{rel} code stmt {i} names the funded key")


# ---------------------------------------------------------------- funded first

def test_a_funded_call_never_waits_on_a_paper_call():
    """The asymmetry IS the design. Funded only increments a counter; if it could block on a lock
    a paper quote holds, the fix would have recreated the incident with an extra step."""
    with S._funded_inflight():
        assert S._FUNDED_INFLIGHT["n"] == 1
        # a second funded entry must be immediate, not queued
        with S._funded_inflight():
            assert S._FUNDED_INFLIGHT["n"] == 2
    assert S._FUNDED_INFLIGHT["n"] == 0


def test_a_paper_call_yields_while_funded_is_in_flight_but_not_forever():
    """Bounded, deliberately: an arm starved indefinitely stops producing evidence, and a silent
    arm reads as a result rather than as an outage."""
    import time
    with S._funded_inflight():
        t0 = time.monotonic()
        S._yield_to_funded(max_wait_s=0.15)
        waited = time.monotonic() - t0
    assert waited >= 0.1, "it must actually yield"
    assert waited < 1.0, "and it must give up rather than starve"


def test_a_paper_call_does_not_wait_when_the_funded_book_is_quiet():
    import time
    assert S._FUNDED_INFLIGHT["n"] == 0
    t0 = time.monotonic()
    S._yield_to_funded(max_wait_s=1.0)
    assert time.monotonic() - t0 < 0.05


# ---------------------------------------------------------------- accounting

def test_the_load_snapshot_splits_by_role():
    """So the next incident names its consumer in one read. Attributing 40% to the paper arms took
    a cross-reference between the runner log, the arms config and the study's position count."""
    snap = S.jupiter_load_snapshot()
    assert "by_role" in snap and isinstance(snap["by_role"], dict)


def test_the_role_counters_are_copied_not_aliased():
    """A shallow copy would hand the reader the live nested counters, and they would move
    underneath the read — the same defect `by_host` was fixed for."""
    S.JUPITER_LOAD["by_role"]["funded"] = {"quotes": 1, "rate_limited": 0}
    snap = S.jupiter_load_snapshot()
    S.JUPITER_LOAD["by_role"]["funded"]["quotes"] = 999
    assert snap["by_role"]["funded"]["quotes"] == 1


# ---------------------------------------------------------------- honest counters

def test_quotes_are_counted_where_QUOTES_happen_not_where_429s_happen():
    """REGRESSION. The first version incremented `by_role[...]["quotes"]` inside the 429 handler,
    so a field named `quotes` held rate-limits and a role that never 429'd read as a role that
    never quoted. `by_role {funded: 2}` meant two funded rate-limits while 50+ paper positions were
    open, and a resumption gate was waiting on it."""
    import ast
    import pathlib
    from _source_guard import code_of

    tree = ast.parse(code_of(pathlib.Path(S.__file__)))
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_note_host_429")
    body = ast.unparse(handler)
    assert "note_role_quote" not in body, "the 429 handler must not count a quote"
    assert '"quotes"' not in body, "the 429 handler must not touch the quotes field"


def test_the_paper_lane_can_reach_the_shared_counters():
    """The v6 paper path has its OWN host ladder and never reaches `_jup_call`, so without an
    exported recorder the gate waits on a number that path cannot produce."""
    import pathlib
    from _source_guard import code_of

    assert callable(S.note_role_quote) and callable(S.note_role_rate_limited)
    v6 = pathlib.Path(S.__file__).resolve().parents[1] / "services/memecoin_quotes.py"
    src = code_of(v6)
    assert "_note_role_quote(ROLE_PAPER)" in src, "the paper lane must count its own quotes"
    assert "_note_role_rate_limited(ROLE_PAPER)" in src


def test_the_paper_lane_passes_its_role_EXPLICITLY():
    """This lane is paper by construction. Reading the ambient context would make the count depend
    on whether a caller remembered a scope, which is how the last counter went wrong."""
    import pathlib
    from _source_guard import code_of

    v6 = pathlib.Path(S.__file__).resolve().parents[1] / "services/memecoin_quotes.py"
    assert "_note_role_quote(ROLE_PAPER)" in code_of(v6)


def test_both_roles_accrue_independently():
    S.JUPITER_LOAD["by_role"].clear()
    S.note_role_quote(S.ROLE_PAPER)
    S.note_role_quote(S.ROLE_PAPER)
    S.note_role_rate_limited(S.ROLE_PAPER)
    S.note_role_quote(S.ROLE_FUNDED)
    snap = S.jupiter_load_snapshot()["by_role"]
    assert snap["paper"] == {"quotes": 2, "rate_limited": 1}
    assert snap["funded"] == {"quotes": 1, "rate_limited": 0}


def test_by_role_PARTITIONS_the_totals():
    """A partition that does not sum to its total is worse than no partition.

    The heartbeat read `quotes: 1` against a `by_role` summing to 7, because the total was bumped
    inside the seam while the paper lane — which bypasses that seam — bumped only its role. Anyone
    computing "paper share of total" got 600%. One place counts now.
    """
    S.JUPITER_LOAD["quotes"] = 0
    S.JUPITER_LOAD["rate_limited"] = 0
    S.JUPITER_LOAD["by_role"].clear()
    S.note_role_quote(S.ROLE_PAPER)
    S.note_role_quote(S.ROLE_PAPER)
    S.note_role_quote(S.ROLE_FUNDED)
    assert S.JUPITER_LOAD["quotes"] == 3
    assert sum(v["quotes"] for v in S.JUPITER_LOAD["by_role"].values()) == 3
    assert S._totals_agree()


def test_the_seam_does_not_double_count_a_funded_quote():
    """The total used to be incremented in the seam AND in the role recorder. Moving it into one
    place must not leave the funded path counting itself twice."""
    import ast
    import pathlib
    from _source_guard import code_of

    src = code_of(pathlib.Path(S.__file__))
    tree = ast.parse(src)
    # ONLY the TOTAL, `JUPITER_LOAD["quotes"]`. The role's own counter is also subscripted
    # "quotes" but hangs off a setdefault call, not off the JUPITER_LOAD name — a guard that
    # matched on the key alone counted both and failed against correct code.
    bumps = [n for n in ast.walk(tree)
             if isinstance(n, ast.AugAssign)
             and isinstance(n.target, ast.Subscript)
             and isinstance(n.target.value, ast.Name)
             and n.target.value.id == "JUPITER_LOAD"
             and getattr(n.target.slice, "value", None) == "quotes"]
    assert len(bumps) == 1, (
        f"the quotes TOTAL must be incremented in exactly one place, found {len(bumps)} — "
        "two writers is how the partition stopped summing to the whole")
