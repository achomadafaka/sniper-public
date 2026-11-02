from sniper.services.memecoin_fill_timing import execution_stage_spans, execution_stage_summary


def test_pretrace_wall_and_thread_cpu_reach_execution_summary_without_inference():
    attempt = {'trace_json': {'substage_events': [
        {'name': 'slot_receipt', 'pretrace_total_ms': 11.0, 'pretrace_cpu_ms': 0.3}]}}
    spans = execution_stage_spans(attempt)
    assert spans.get('pretrace_total_ms') == 11.0
    assert spans.get('pretrace_cpu_ms') == 0.3
    summary = execution_stage_summary([attempt, {}])
    assert summary['pretrace_total_ms'] == dict(n=1, missing=1, p50_ms=11.0, p90_ms=11.0)
    assert summary['pretrace_cpu_ms'] == dict(n=1, missing=1, p50_ms=0.3, p90_ms=0.3)
