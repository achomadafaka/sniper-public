"""Narrative guesser matcher (spec 2026-10-27-desk-narrative-guesser-design.md section 3-5). Pure functions, no I/O."""
from sniper.services import memecoin_desk_narrative_match as M

G = lambda **k: dict(id="g1", theme_id="animals", known_at=100.0, expires_at=100.0 + 7200, tickers=["bonkers"], names=["Bonk Brothers"], **k)  # noqa: E731
L = lambda **k: {**dict(mint="m1", network_id="solana", name="Bonkers", symbol="BONKERS", first_seen=200.0, theme_id=None), **k}  # noqa: E731


def test_normalise_ignores_case_punctuation_and_filler_suffixes():
    assert M.normalise("$Bonk-ers Coin") == "bonkers"
    assert M.normalise("PEPE  INU") == "pepe"
    assert M.normalise("AI") == "ai"                       # a bare filler word is not stripped to nothing
    assert M.normalise(None) == "" and M.normalise("   ") == ""


def test_exact_beats_fuzzy_and_names_match_too():
    r = M.match(L(), [G()], guesser_state="ok")
    assert (r["match_kind"], r["guess_id"]) == ("exact", "g1")
    r = M.match(L(name="Bonk Brothers", symbol="XYZQ"), [G()], guesser_state="ok")
    assert r["match_kind"] == "exact"
    r = M.match(L(name="Bonkerz", symbol="BONKERZ"), [G()], guesser_state="ok")
    assert r["match_kind"] == "fuzzy"


def test_short_keys_never_fuzzy_match():
    g = G()
    g["tickers"] = ["pepe"]
    assert M.match(L(name="Pepa", symbol="PEPA"), [g], guesser_state="ok")["match_kind"] == "none"          # len 4: edit-1 is too loose for 4 chars
    assert M.match(L(name="Pepe", symbol="PEPE"), [g], guesser_state="ok")["match_kind"] == "exact"


def test_a_guess_written_after_the_launch_or_expired_never_matches():
    late = G()
    late["known_at"] = 200.0                               # same instant is not before
    assert M.match(L(), [late], guesser_state="ok")["match_kind"] == "none"
    old = G()
    old["expires_at"] = 150.0
    assert M.match(L(), [old], guesser_state="ok")["match_kind"] == "none"


def test_theme_only_is_recorded_and_stale_guesser_is_unknown_not_none():
    r = M.match(L(name="Zzzz", symbol="ZZZZ", theme_id="animals"), [G()], guesser_state="ok")
    assert r["match_kind"] == "theme_only"
    r = M.match(L(name="Zzzz", symbol="ZZZZ"), [G()], guesser_state="stale")
    assert r["match_kind"] is None and r["guess_id"] is None
    r = M.match(L(), [G()], guesser_state="stale")
    assert r["match_kind"] is None                          # even an exact name: a stale guesser means unknown, never a control or a treated row


def test_best_guess_wins_and_ties_go_to_the_earliest_guess():
    a, b = G(), G()
    b["id"], b["known_at"] = "g0", 90.0
    assert M.match(L(), [a, b], guesser_state="ok")["guess_id"] == "g0"


def test_matcher_version_hash_changes_with_the_filler_list(monkeypatch):
    h = M.VERSION_HASH
    monkeypatch.setattr(M, "FILLER", M.FILLER + ("zzz",))
    assert M.version_hash() != h


def test_preload_cap_counts_only_exact_and_fuzzy():
    rows = [dict(match_kind=k, at=1000.0 + i) for i, k in enumerate(["exact", "theme_only", "fuzzy", "none", None])]
    assert M.preload_budget(rows, now=1100.0, cap_per_h=10) == 8
    assert M.preload_budget(rows, now=1100.0, cap_per_h=2) == 0
