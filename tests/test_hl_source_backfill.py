from sniper.hyperlive import source_backfill as SB


def _copy(**overrides):
    base = {
        "id": 4,
        "target": "0xabc",
        "coin": "kPEPE",
        "side": "short",
        "detect_ts_ms": 1_200,
        "target_szi": -3.0,
        "is_cold_start": 0,
    }
    return {**base, **overrides}


def _fill(**overrides):
    base = {
        "tid": 7,
        "coin": "kPEPE",
        "side": "A",
        "dir": "Open Short",
        "time": 1_100,
        "sz": "1",
        "startPosition": "-2",
    }
    return {**base, **overrides}


def test_reconstructs_unique_exact_position_completion():
    result = SB.reconstruct_entry_event(_copy(), [_fill()], after_ts_ms=900)

    assert result == {
        "status": "matched",
        "reason": "unique_exact_position_completion",
        "source_event_ts_ms": 1_100,
        "tids": ["7"],
    }


def test_reconstruction_refuses_cold_start_wrong_case_and_missing_target_size():
    assert SB.reconstruct_entry_event(
        _copy(is_cold_start=1), [_fill()], after_ts_ms=900,
    )["status"] == "censored_cold_start"
    assert SB.reconstruct_entry_event(
        _copy(), [_fill(coin="KPEPE")], after_ts_ms=900,
    )["status"] == "unmatched"
    assert SB.reconstruct_entry_event(
        _copy(target_szi=None), [_fill()], after_ts_ms=900,
    )["status"] == "unavailable_target_size"


def test_reconstruction_refuses_ambiguous_completions_and_out_of_window_fills():
    duplicate_completion = _fill(tid=8, time=1_150)
    assert SB.reconstruct_entry_event(
        _copy(), [_fill(), duplicate_completion], after_ts_ms=900,
    )["status"] == "ambiguous"
    assert SB.reconstruct_entry_event(
        _copy(), [_fill(time=899)], after_ts_ms=900,
    )["status"] == "unmatched"


def test_reconstruction_uses_two_percent_position_tolerance_and_exact_side():
    assert SB.reconstruct_entry_event(
        _copy(target_szi=-3.0), [_fill(sz="0.99")], after_ts_ms=900,
    )["status"] == "matched"
    assert SB.reconstruct_entry_event(
        _copy(), [_fill(side="B", dir="Open Long")], after_ts_ms=900,
    )["status"] == "unmatched"


def test_fetch_history_pages_backwards_and_deduplicates():
    calls = []

    def fetch(wallet, start_ms, end_ms):
        calls.append((wallet, start_ms, end_ms))
        if len(calls) == 1:
            return [
                _fill(tid=index, time=2_000 + index)
                for index in range(SB.PROVIDER_PAGE_LIMIT)
            ]
        return [_fill(tid="old", time=1_500)]

    result = SB.fetch_wallet_history(
        "0xABC", start_ms=1_000, end_ms=9_000, fetch=fetch,
    )

    assert calls == [("0xabc", 1_000, 9_000), ("0xabc", 1_000, 1_999)]
    assert len(result["fills"]) == SB.PROVIDER_PAGE_LIMIT + 1
    assert result["ceiling_reached"] is False

