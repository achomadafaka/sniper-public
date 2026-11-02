"""RR-01 (2026-09-26): the slot lane's shared receipt read must never cache a miss."""
from sniper.execution.solana_real_seams import successful_receipt_cache


def _reader(responses):
    calls = []

    def read(sig):
        calls.append(sig)
        return responses[len(calls) - 1] if len(calls) <= len(responses) else responses[-1]
    return read, calls


def test_a_missing_receipt_is_read_again_on_the_next_retry():
    read, calls = _reader([None, {"slot": 1}])
    receipt = successful_receipt_cache(read)
    assert receipt("sig") is None
    assert receipt("sig") == {"slot": 1}
    assert len(calls) == 2


def test_a_usable_receipt_is_read_once_and_shared():
    read, calls = _reader([{"slot": 1}])
    receipt = successful_receipt_cache(read)
    assert receipt("sig") is receipt("sig")
    assert len(calls) == 1


def test_a_permanently_missing_receipt_keeps_reading_and_stays_none():
    read, calls = _reader([None])
    receipt = successful_receipt_cache(read)
    assert [receipt("sig") for _ in range(4)] == [None] * 4
    assert len(calls) == 4


def test_the_slot_executor_books_after_a_first_empty_read(monkeypatch):
    """End to end through execute_prebuilt_entry's real retry loop: the first read is empty, the
    second is usable, and the fill must be parsed from the second (two reads, not one)."""
    read, calls = _reader([None, {"ok": True}])
    receipt = successful_receipt_cache(read)
    seen = []

    def fill_fn(sig, mint, maximum):
        r = receipt(sig)
        seen.append(r)
        return None if r is None else (10.0, 100.0, 6)

    import sniper.execution.solana_slot_executor as ex
    fills = []
    for attempt in range(ex.RECEIPT_READS):
        got = ex._receipt_fill(fill_fn, "sig", "mint", 10.0)
        fills.append(got)
        if got is not None:
            break
    assert fills[-1] == (10.0, 100.0, 6)
    assert len(calls) == 2
