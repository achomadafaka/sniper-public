"""log_sampled: the episode start always logs; repeats are counted, never silently dropped."""
from sniper.util import log_sample
from sniper.util.log_sample import log_sampled


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def setup_function(_fn):
    log_sample._last.clear()


def test_first_occurrence_always_logs():
    out = []
    assert log_sampled(lambda m, *a: out.append(m % a), "k", "boom %s", 1, clock=Clock()) is True
    assert out == ["boom 1"]


def test_repeats_inside_the_window_are_counted_not_logged():
    out, clock = [], Clock()
    for i in range(5):
        log_sampled(lambda m, *a: out.append(m % a), "k", "boom %s", i, clock=clock)
        clock.t += 1.0
    assert len(out) == 1, "one line for five occurrences inside the window"
    clock.t += 60.0
    log_sampled(lambda m, *a: out.append(m % a), "k", "boom %s", 9, clock=clock)
    assert len(out) == 2
    assert "+4 suppressed" in out[1], "the count must surface — nothing is silently dropped"


def test_distinct_keys_do_not_suppress_each_other():
    out, clock = [], Clock()
    log_sampled(lambda m, *a: out.append(m % a), ("k", 1), "a %s", 1, clock=clock)
    log_sampled(lambda m, *a: out.append(m % a), ("k", 2), "b %s", 2, clock=clock)
    assert len(out) == 2
