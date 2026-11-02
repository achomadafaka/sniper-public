"""The scalp probe's two pieces of non-obvious logic: the dense schedule and the decisive step.

Both are cheap to get subtly wrong and neither fails loudly. A sampling schedule that never
tightens still produces tapes; a step parser that returns None on every row still produces a
summary — it just silently reports a mixture of nothing.
"""
import importlib.util
import pathlib

PROBE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_scalp_probe.py"
_spec = importlib.util.spec_from_file_location("scalp_probe", PROBE)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


def _row(**kw):
    base = {"rail": "stop", "rail_step_pct": None, "path": ""}
    base.update(kw)
    return base


def test_the_sampler_tightens_only_inside_the_dense_window():
    """One second for the first fifteen, three after. The window is where the firings cluster."""
    schedule = [(held, sp.DENSE_S if held < sp.DENSE_UNTIL_S else sp.SAMPLE_S)
                for held in (0.0, 5.0, 14.9, 15.0, 30.0, 599.0)]
    assert [s for _h, s in schedule] == [1.0, 1.0, 1.0, 3.0, 3.0, 3.0]
    assert sp.DENSE_S < sp.SAMPLE_S, "the dense window must be denser than the tail"


def test_the_decisive_step_is_the_last_interval_not_the_whole_move():
    """The mixture question is how far the price moved in the ONE interval that crossed the rail.
    Measuring from entry instead would report the trade's size, which is a different quantity and
    would make every gapping tape look like a traverse on a long hold."""
    row = _row(path="1.0s:-3.6% 2.0s:-4.9% 3.0s:-46.3%")
    assert sp._decisive_step(row) == -41.4


def test_a_recorded_step_is_preferred_over_the_derived_one():
    row = _row(rail_step_pct=-12.5, path="1.0s:-1.0% 2.0s:-40.0%")
    assert sp._decisive_step(row) == -12.5


def test_a_single_observation_has_no_step_rather_than_a_fake_zero():
    """A rail that fired on its FIRST sample has no observed path at all. Zero would read as a
    perfect traverse and would pull the mixture the wrong way."""
    assert sp._decisive_step(_row(path="3.0s:+25.7%")) is None
    assert sp._decisive_step(_row(path="")) is None


def test_a_timecap_exit_is_not_a_rail_crossing():
    assert sp._decisive_step(_row(rail="timecap", path="1.0s:-1.0% 2.0s:-2.0%")) is None


def test_an_unparseable_path_returns_none_rather_than_a_wrong_number():
    assert sp._decisive_step(_row(path="garbage tokens here")) is None


def test_the_lifetime_cap_matches_what_was_approved():
    """20 for the first run plus 20 for the traverse-vs-gap question. Counted against the table,
    so a restart cannot spend an allowance twice."""
    assert sp.MAX_TRADES == 44


def test_the_two_samplers_are_distinguishable():
    """A 1s and a 3s sampler realize different effective stops, so their rows must never pool."""
    assert sp.SAMPLER_ID and "1s" in sp.SAMPLER_ID
