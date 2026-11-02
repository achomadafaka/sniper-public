"""A venue that refuses a buy on chain is excluded for that coin, and the buy is re-quoted around it.

BURNED (ref 8a545100, 2026-09-13): Jupiter routed the buy through Quantum, Quantum reverted it
(custom 0xe), no USDC moved, and the signal was lost because a non-slippage revert is one-shot.
"""
import pytest

from sniper.execution import solana_real_seams as S
from sniper.services.memecoin_real_runner import _is_transient_entry_miss

QUANTUM = "QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv"
BURNED_LOGS = [
    "Program ComputeBudget111111111111111111111111111111 invoke [1]",
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
    "Program log: Instruction: SharedAccountsRoute",
    "Program %s invoke [2]" % QUANTUM,
    "Program %s consumed 5550 of 68699 compute units" % QUANTUM,
    "Program %s failed: custom program error: 0xe" % QUANTUM,
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 consumed 11116 of 74265 compute units",
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 failed: custom program error: 0xe",
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    S._VENUE_EXCLUSIONS.clear()
    monkeypatch.setitem(S._JUP_LABELS, "map", {QUANTUM: "Quantum"})
    monkeypatch.setitem(S._JUP_LABELS, "at", S._monotonic())
    yield
    S._VENUE_EXCLUSIONS.clear()


def test_the_failing_venue_is_the_innermost_non_jupiter_program():
    assert S._failing_program(BURNED_LOGS) == QUANTUM
    assert S._failing_program(BURNED_LOGS[-1:]) is None          # Jupiter alone is plumbing


def test_the_failed_verdict_carries_the_program(monkeypatch):
    monkeypatch.setattr(S, "_rpc_call", lambda *a, **k: (
        200, {"result": {"meta": {"logMessages": BURNED_LOGS}}}))
    state = S._failed_confirm_state({"InstructionError": [3, {"Custom": 14}]}, "SIG", "rpc", None)
    assert state == "failed" and state.failed_program == QUANTUM


def test_an_excluded_venue_reaches_the_jupiter_quote_for_that_mint_only(monkeypatch):
    paths = []
    monkeypatch.setattr(S, "_jup_call", lambda method, path, **k: paths.append(path) or (200, {"outAmount": "1"}))
    assert S.exclude_failed_venue("MINT", QUANTUM) == "Quantum"
    S._jup_quote_status(S.USDC_MINT, "MINT", 175_000_000, 1000, None)
    S._jup_quote_status(S.USDC_MINT, "OTHER", 175_000_000, 1000, None)
    assert "&excludeDexes=Quantum" in paths[0] and "excludeDexes" not in paths[1]


def test_plumbing_and_unlabelled_programs_are_never_excluded():
    assert S.exclude_failed_venue("MINT", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4") is None
    assert S.exclude_failed_venue("MINT", "UnknownProgram11111111111111111111111111111") is None
    assert S.excluded_venues("MINT") == frozenset()


def test_the_exclusion_expires(monkeypatch):
    S.exclude_failed_venue("MINT", QUANTUM)
    clock = S._monotonic() + S.VENUE_EXCLUSION_TTL_S + 1
    monkeypatch.setattr(S, "_monotonic", lambda: clock)
    assert S.excluded_venues("MINT") == frozenset()


def test_the_retry_lane_re_quotes_only_an_excluded_venue_refusal():
    refused = {"status": "failed", "reason": "chain_error:custom program error: 0xe"}
    assert _is_transient_entry_miss(refused) is False               # one-shot, as before
    assert _is_transient_entry_miss({**refused, "venue_excluded": "Quantum"}) is True


DENALI = "DNL1tgEj3nJovHw9jtyCCQD3arssCJzkmpDizknwzey4"
DENALI_LOGS = [
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
    "Program %s invoke [2]" % DENALI,
    "Program log: AnchorError occurred. Error Code: VolumeCapExceeded. Error Number: 6012.",
    "Program %s failed: custom program error: 0x177c" % DENALI,
    "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 failed: custom program error: 0x177c",
]


def test_a_venue_wide_cap_refusal_excludes_the_venue_for_every_mint(monkeypatch):
    """Ethena 91679b13 then hyENA 69601c34 (2026-09-23): Denali's VolumeCapExceeded is a fact
    about Denali's pool, so the second coin must not be routed through it either."""
    monkeypatch.setitem(S._JUP_LABELS, "map", {QUANTUM: "Quantum", DENALI: "Denali"})
    monkeypatch.setattr(S, "_rpc_call", lambda *a, **k: (
        200, {"result": {"meta": {"logMessages": DENALI_LOGS}}}))
    state = S._failed_confirm_state({"InstructionError": [1, {"Custom": 6012}]}, "SIG", "rpc", None)
    assert state.program_error == "VolumeCapExceeded" and state.failed_program == DENALI
    assert S.exclude_failed_venue("ETHENA", state.failed_program,
                                  program_error=state.program_error) == "Denali"
    assert "Denali" in S.excluded_venues("HYENA")
    paths = []
    monkeypatch.setattr(S, "_jup_call", lambda method, path, **k: paths.append(path) or (200, {"outAmount": "1"}))
    S._jup_quote_status(S.USDC_MINT, "HYENA", 150_000_000, 1000, None)
    assert "excludeDexes=Denali" in paths[0]
    # A coin-level refusal stays per-mint, exactly as before.
    S._VENUE_EXCLUSIONS.clear()
    S.exclude_failed_venue("MINT", QUANTUM, program_error="ConstraintMintTokenProgram")
    assert S.excluded_venues("OTHER") == frozenset()
    assert S.excluded_venues("MINT") == frozenset({"Quantum"})


def test_the_venue_wide_exclusion_expires_with_the_per_mint_one(monkeypatch):
    monkeypatch.setitem(S._JUP_LABELS, "map", {DENALI: "Denali"})
    S.exclude_failed_venue("ETHENA", DENALI, program_error="VolumeCapExceeded")
    clock = S._monotonic() + S.VENUE_EXCLUSION_TTL_S + 1
    monkeypatch.setattr(S, "_monotonic", lambda: clock)
    assert S.excluded_venues("HYENA") == frozenset()
    assert S.excluded_venues("ETHENA") == frozenset()


def test_learned_wildcard_excludes_other_buys_but_not_unrelated_exits(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    monkeypatch.setitem(S._JUP_LABELS, "map", {DENALI: "Denali", QUANTUM: "Quantum"})
    S.exclude_failed_venue("BOUGHT", DENALI, program_error="VolumeCapExceeded")
    S.exclude_failed_venue("HELD", QUANTUM)
    paths = []
    monkeypatch.setattr(S, "_jup_call", lambda method, path, **k:
                        paths.append(path) or (200, {"outAmount": "100", "priceImpactPct": "0"}))
    S.jupiter_entry_quote("OTHER", 10)
    S.jupiter_exit_quote("UNRELATED", 100, 10)
    S.jupiter_exit_quote("HELD", 100, 10)
    S.jupiter_exit_quote("BOUGHT", 100, 10)
    exclusions = [parse_qs(urlsplit(path).query).get("excludeDexes", []) for path in paths]
    assert exclusions == [["Denali"], [], ["Quantum"], ["Denali"]]


@pytest.mark.parametrize("venue_wide", [False, True])
def test_learned_raydium_exclusion_is_honored_by_the_fallback(monkeypatch, venue_wide):
    monkeypatch.setattr(S, "_jupiter_venue_label", lambda *a, **k: "Raydium CLMM")
    S.exclude_failed_venue("MINT", "ray-program",
                           program_error="VolumeCapExceeded" if venue_wide else None)
    monkeypatch.setattr(S, "_jup_call", lambda *a, **k: (400, {"error": "no route"}))
    monkeypatch.setattr(S, "_ray_quote", lambda *a, **k: {"outAmount": "100", "_venue": "raydium"})
    assert S.jupiter_entry_quote("OTHER" if venue_wide else "MINT", 10) is None


def test_static_exclusions_come_from_the_knob_and_tolerate_an_empty_value(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_EXCLUDE_DEXES", "Denali, Quantum,,", raising=False)
    assert S.static_excluded_venues() == frozenset({"Denali", "Quantum"})
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_EXCLUDE_DEXES", "", raising=False)
    assert S.static_excluded_venues() == frozenset()
    monkeypatch.delenv("MEMECOIN_SOLANA_EXCLUDE_DEXES", raising=False)
    import importlib, os
    assert os.getenv("MEMECOIN_SOLANA_EXCLUDE_DEXES") is None
    assert "Denali" in (importlib.reload(cfg).MEMECOIN_SOLANA_EXCLUDE_DEXES or "")


def test_the_configured_static_exclusion_reaches_entry_quotes_and_not_exits(monkeypatch):
    """The configured MEMECOIN_SOLANA_EXCLUDE_DEXES (Denali) is ENTRY-ONLY, before and after the
    wildcard scope change: `static_excluded_venues` documents it, the entry closure passes it, and
    `jupiter_exit_quote` passes none. Only the LEARNED buy wildcard stopped applying to exits.
    """
    from urllib.parse import parse_qs, urlsplit
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_EXCLUDE_DEXES", "Denali")
    paths = []
    monkeypatch.setattr(S, "_jup_call", lambda method, path, **k:
                        paths.append(path) or (200, {"outAmount": "1"}))
    S.jupiter_entry_quote("ENTRYMINT", 10.0, exclude_dexes=S.static_excluded_venues())
    S.jupiter_exit_quote("EXITMINT", 1000, 5.0)
    entry, exit_ = (parse_qs(urlsplit(p).query).get("excludeDexes", [""])[0] for p in paths)
    assert "Denali" in entry.split(",")
    assert "Denali" not in exit_.split(",")


def test_a_configured_non_raydium_exclusion_no_longer_disables_the_raydium_fallback(monkeypatch):
    """Since 4bfea3d1 every funded Solana entry carries the static Denali exclusion, and the old
    gate (`not exclude_dexes`) therefore switched the Raydium fallback OFF for every funded entry --
    both the Jupiter-miss fallback and the thin-route second opinion. Denali is not a Raydium venue,
    so it cannot stop Raydium's own route; the fallback now runs unless a Raydium label is excluded.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True)
    monkeypatch.setattr(S, "_jup_call", lambda *a, **k: (400, {"error": "no route"}))
    called = []
    monkeypatch.setattr(S, "_ray_quote", lambda *a, **k: called.append(a) or {"outAmount": "100"})
    out = S.jupiter_entry_quote("ENTRYMINT", 10.0, exclude_dexes=frozenset({"Denali"}))
    assert called, "the Raydium fallback must run on an entry whose only exclusion is Denali"
    assert out["outAmount"] == "100"
