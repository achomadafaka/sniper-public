"""The one desk model-echo rule (Gamma, Alpha 2026-10-21): MODEL, or MODEL plus an 8-digit dated
snapshot; a missing echo is allowed."""
import pytest

from sniper.services import memecoin_desk_model_echo as E

M = "claude-haiku-5-5"


@pytest.mark.parametrize("echo", [M, M + "-20261015", None])
def test_the_model_its_dated_snapshot_and_a_missing_echo_match(echo):
    assert E.echo_ok(echo, M) is True


@pytest.mark.parametrize("echo", ["claude-sonnet-5-5", M + "-anything", M + "-2026101", M + "-202610155",
                                  M + "x", M[:-2], "", M + "-20261015\n", " " + M])
def test_anything_else_is_a_mismatch(echo):
    assert E.echo_ok(echo, M) is False


class _Named:
    def __str__(self):
        return M

    __repr__ = __str__


class _BadRepr:
    def __repr__(self):
        raise RuntimeError("no")


@pytest.mark.parametrize("echo", [{"model": M}, [M], (M,), b"claude-haiku-5-5", _Named(), 5])
def test_a_non_string_echo_is_a_mismatch_even_when_it_prints_as_the_model(echo):
    assert E.echo_ok(echo, M) is False


@pytest.mark.parametrize("echo, stored", [(None, None), (M, M), ("", ""),
                                          ({"model": M}, "nonstr:dict:{'model': 'claude-haiku-5-5'}"),
                                          ([M], "nonstr:list:['claude-haiku-5-5']"), (b"x", "nonstr:bytes:b'x'"),
                                          (5, "nonstr:int:5"), (_Named(), "nonstr:_Named:claude-haiku-5-5"),
                                          (_BadRepr(), "nonstr:_BadRepr:<repr failed: RuntimeError>")])
def test_echo_text_gives_the_adapter_str_or_none_and_a_non_string_stays_a_mismatch(echo, stored):
    import sqlite3
    text = E.echo_text(echo)
    assert text == stored
    if echo is not None and not isinstance(echo, str):
        assert E.echo_ok(text, M) is False      # never None: None is allowed, a bad echo must not pass
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(model_echo TEXT)")
    db.execute("INSERT INTO t VALUES(?)", (text,))   # a container bound raw raises ProgrammingError
    assert db.execute("SELECT model_echo FROM t").fetchone()[0] == stored


@pytest.mark.parametrize("digits", ["\u0662\u0660\u0662\u0666\u0661\u0660\u0661\u0665", "\uff12\uff10\uff12\uff16\uff11\uff10\uff11\uff15"])
def test_a_snapshot_suffix_is_ascii_digits_only(digits):
    assert E.echo_ok(M + "-" + digits, M) is False


def test_a_huge_container_echo_is_capped_and_still_a_mismatch():
    text = E.echo_text([M] * 10_000)
    assert text == "nonstr:list:" + repr([M] * 10_000)[:E.REPR_CAP] + "..."
    assert len(text) <= E.MARKER_MAX
    assert E.echo_ok(text, M) is False


def test_a_repr_exactly_at_the_cap_is_kept_whole():
    assert E.echo_text(_Exact()) == "nonstr:_Exact:" + "y" * E.REPR_CAP
    assert E.echo_text([1]) == "nonstr:list:[1]"
    assert E.echo_text(b"z" * (E.REPR_CAP - 2)) == "nonstr:bytes:b'" + "z" * (E.REPR_CAP - 2) + "..."


class _Exact:
    def __repr__(self):
        return "y" * E.REPR_CAP


def test_a_long_type_name_and_a_broken_repr_stay_bounded_mismatches():
    Long = type("L" * 5000, (), {"__repr__": lambda self: 1 / 0})
    text = E.echo_text(Long())
    assert text == "nonstr:%s:<repr failed: ZeroDivisionError>" % ("L" * E.TYPE_CAP)
    assert len(text) <= E.MARKER_MAX and E.echo_ok(text, M) is False
    assert E.echo_text(M + "-20261015") == M + "-20261015"     # a str echo is never touched


class _SurrogateRepr:
    def __repr__(self):
        return "\ud800"


_BAD = {"lone": lambda: "\ud800", "model+lone": lambda: M + "\udfff", "1MB": lambda: "x" * 1_000_000,
        "1M-surrogates": lambda: "\ud800" * 1_000_000, "repr-surrogate": _SurrogateRepr}   # type() refuses a surrogate name itself


# Values built inside the test: xdist cannot ship a surrogate parameter to its workers.
@pytest.mark.parametrize("case", sorted(_BAD))
def test_a_surrogate_or_huge_echo_becomes_a_bounded_bindable_mismatch(case):
    import sqlite3
    text = E.echo_text(_BAD[case]())
    assert len(text) <= E.MARKER_MAX and E.echo_ok(text, M) is False
    text.encode("utf-8")
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(model_echo TEXT)")
    db.execute("INSERT INTO t VALUES(?)", (text,))
    assert db.execute("SELECT model_echo FROM t").fetchone()[0] == text


def test_bad_strings_get_their_own_marker_and_valid_echoes_are_untouched():
    assert E.echo_text("\ud800") == "badstr:'\\ud800'"
    assert E.echo_text("x" * (E.REPR_CAP + 1)) == "badstr:" + ascii("x" * (E.REPR_CAP + 1))[:E.REPR_CAP] + "..."
    for good in (M, M + "-20261015", "", "x" * E.REPR_CAP, "modèle"):
        assert E.echo_text(good) == good
