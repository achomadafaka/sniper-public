from sniper.web.ops_health import classify_load, usable_cpu_count


def test_load_five_is_healthy_on_32_cpu():
    threshold, issue = classify_load(5.0, 32)
    assert threshold == 28.8
    assert issue is None


def test_load_above_ninety_percent_warns():
    threshold, issue = classify_load(29.0, 32)
    assert threshold == 28.8
    assert issue == "load_high:29.0"


def test_unknown_cpu_capacity_preserves_legacy_fallback():
    threshold, issue = classify_load(3.1, None)
    assert threshold == 3.0
    assert issue == "load_high:3.1"


def test_usable_cpu_count_prefers_affinity(monkeypatch):
    monkeypatch.setattr(
        "sniper.web.ops_health.os.sched_getaffinity",
        lambda _pid: set(range(6)),
    )
    monkeypatch.setattr("sniper.web.ops_health.os.cpu_count", lambda: 32)
    assert usable_cpu_count() == 6


def test_one_minute_spike_alone_is_not_an_issue():
    # 2026-09-22: a runner redeploy plus one research query pushed 1-min to 17.94 on 32 CPUs
    # while vmstat showed 92-95% idle. The 5-minute average is what says "this persists".
    threshold, issue = classify_load(29.5, 32, load_5min=10.0)
    assert threshold == 28.8
    assert issue is None


def test_sustained_load_is_an_issue():
    threshold, issue = classify_load(29.5, 32, load_5min=29.1)
    assert threshold == 28.8
    assert issue == "load_high:29.5"
