"""A funded position must not wait up to 30s for the hot lane to notice it.

The hot lane's position book is fixed for a 30-second epoch, so a position opening just after a
rebuild is invisible until the next one. Measured over 3 days, open to first price mark:

    solana  p50  2.8s  p90 10.7s   69% inside 5s
    4663    p50 11.4s  p90 28.0s   12% inside 5s      <- a uniform wait for a 30s cycle

For that whole window no rail can fire on the position, because there is no price.
"""
import inspect
import threading

import pytest

from sniper.services import memecoin_hot_supervisor as H


@pytest.fixture(autouse=True)
def _clean():
    H.rebuild_requested()
    yield
    H.rebuild_requested()


def test_a_request_is_visible_once_then_cleared():
    """One open must cause one early rebuild, not a permanent rebuild loop."""
    assert H.rebuild_requested() is False
    H.request_rebuild()
    assert H.rebuild_requested() is True
    assert H.rebuild_requested() is False, "the flag must be consumed, not latched"


def test_it_is_safe_from_another_thread():
    """It is called from the funded entry path, which is not the lane's loop thread."""
    t = threading.Thread(target=H.request_rebuild)
    t.start(); t.join()
    assert H.rebuild_requested() is True


def test_repeated_requests_collapse_to_one():
    for _ in range(5):
        H.request_rebuild()
    assert H.rebuild_requested() is True
    assert H.rebuild_requested() is False


def test_the_housekeeping_loop_consumes_it_and_ends_the_epoch():
    src = inspect.getsource(H.run_hot_exit_lane)
    assert "rebuild_requested()" in src, "nothing consumes the request"
    body = src[src.index("rebuild_requested()"):]
    body = body[:body.index("await asyncio.sleep")]
    assert "rebuild.set()" in body and "break" in body, (
        "the request must end the epoch the same way a socket gap does")


def test_the_request_reuses_the_EXISTING_rebuild_path():
    """Mutating a live book mid-epoch would race the lane's own reads. Ending the epoch is the
    routine, already-tested path -- the stream loop does it on any socket gap."""
    src = inspect.getsource(H.run_hot_exit_lane)
    assert src.count("rebuild.set()") >= 2, (
        "the early-rebuild path should set the same event the socket-gap path does")


@pytest.mark.parametrize("path,anchor", [
    ("sniper/execution/evm_swap_executor.py", "m.open_position("),
    ("sniper/execution/solana_swap_executor.py", "m.open_position("),
])
def test_both_funded_open_sites_request_a_rebuild(path, anchor):
    """One chain wired and the other not is how half of today's defects happened."""
    import pathlib
    src = pathlib.Path(path).read_text()
    assert anchor in src
    assert "request_rebuild()" in src, f"{path} opens a funded position without asking for a rebuild"


@pytest.mark.parametrize("path", [
    "sniper/execution/evm_swap_executor.py",
    "sniper/execution/solana_swap_executor.py",
])
def test_the_request_can_never_break_an_entry(path):
    """It runs immediately after a funded fill is booked. An exception here would surface as a
    failed entry on a position that already exists."""
    import pathlib
    src = pathlib.Path(path).read_text()
    # Anchor on the CALL, not the import that precedes it inside the same try -- and take a
    # window wide enough to contain the handler that follows.
    i = src.rindex("            request_rebuild()") if "            request_rebuild()" in src \
        else src.rindex("        request_rebuild()")
    window = src[max(0, i - 600):i + 400]
    assert "try:" in window and "except Exception:" in window, (
        f"{path}: the rebuild request is not guarded; an exception here would surface as a failed "
        "entry on a position that already exists")
