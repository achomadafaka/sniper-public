"""Pre-registration amendment 11 item 2: every input of the trigger path has received_at STRICTLY < as_of. The legacy call keeps <=."""
import pytest

from sniper.services import memecoin_desk_asof as A
from sniper.services.memecoin_desk_asof import StaleSnapshot
from test_memecoin_desk_asof import SOL, VQ_E, VT_E, cand, tape  # noqa: F401

AGENT = next(iter(__import__("sniper.services.memecoin_desk_safety", fromlist=["x"]).MAYHEM_AGENT_IDS))
EPS = 0.001


@pytest.fixture
def cut_tape(tape):
    """The base tape plus three swaps around the as-of time: as_of - eps (regular), exactly as_of (agent), as_of + eps (regular)."""
    now = tape.now
    tape.swap("before" + "x" * 70, 0, 20, now - EPS, 44_500_000_000, 636_000_000_000_000, quote=1_000_000_000, actor="r1")
    tape.swap("exact" + "x" * 70, 0, 21, now, 60_000_000_000, 600_000_000_000_000, quote=3_000_000_000, actor=AGENT)
    tape.swap("after" + "x" * 70, 0, 22, now + EPS, 70_000_000_000, 590_000_000_000_000, quote=5_000_000_000, actor="r2")
    return tape


def test_the_trigger_path_excludes_a_row_stamped_exactly_at_as_of_from_tip_flow_and_run_the_legacy_call_includes_it(cut_tape):
    t = cut_tape
    legacy_c, legacy_meta = A.refresh(cand(), desk_dir=t.dir, now=t.now)
    assert legacy_meta["last_swap"]["signature"].startswith("exact")                       # legacy: <= as-of, the exact row is the tip
    assert legacy_meta["window_swaps"] == 4                                                 # the two base-tape swaps + the before row + the exact row
    c, meta = A.refresh(cand(), desk_dir=t.dir, now=t.now, rank_inputs=True)
    assert meta["last_swap"]["signature"].startswith("before")                              # trigger path: strictly <, the tip is the row before
    assert meta["window_swaps"] == legacy_meta["window_swaps"] - 1
    rank = meta["rank"]
    assert rank["agent_run_rows"] == 0 and rank["agent_run_lift"] is None                  # the exact (agent) row is not a run row
    legacy_rank = A.refresh(cand(), desk_dir=t.dir, now=t.now, rank_inputs=True)[1]["rank"]
    assert legacy_rank == rank                                                              # deterministic
    # the row stamped just AFTER as_of is excluded by both
    assert not meta["last_swap"]["signature"].startswith("after") and not legacy_meta["last_swap"]["signature"].startswith("after")


def test_flow_and_price_features_do_not_see_the_exact_row(cut_tape):
    t = cut_tape
    _, strict = A.refresh(cand(), desk_dir=t.dir, now=t.now, rank_inputs=True)
    _, loose = A.refresh(cand(), desk_dir=t.dir, now=t.now)
    assert strict["rank"]["features"]["n_swaps"] == loose["window_swaps"] - 1
    from sniper.services import memecoin_desk_ranker as R
    assert strict["rank"]["price_now"] == pytest.approx(R.price(44_500_000_000, 636_000_000_000_000))   # the row before, not the exact one
    assert loose["rank"]["price_now"] == pytest.approx(R.price(60_000_000_000, 600_000_000_000_000)) if "rank" in loose else True
    assert strict["last_swap"]["received_at"] < t.now


def test_a_gap_stamped_exactly_at_as_of_does_not_block_the_trigger_path_but_does_block_the_legacy_call(tape):
    tape.gap(tape.now)                                                                      # a gap row at exactly as_of
    with pytest.raises(StaleSnapshot) as exc:
        A.refresh(cand(), desk_dir=tape.dir, now=tape.now)
    assert exc.value.reason == "tape_gap_since_last_swap"                                   # legacy: at <= as_of
    c, meta = A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)          # strict: a gap AT as_of is not before it
    assert meta["flow_complete"] is True
    tape.gap(tape.now - EPS)                                                                # a gap strictly before as_of still counts
    with pytest.raises(StaleSnapshot):
        A.refresh(cand(), desk_dir=tape.dir, now=tape.now, rank_inputs=True)
