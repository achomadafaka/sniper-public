"""Item 7 — single-instance lock: a second runner must refuse to start. With real orders a
double-runner double-submits swaps (double position, double loss); the exclusive flock is the
guarantor (not a best-effort PID file)."""
import fcntl
from sniper.services.memecoin_runner import acquire_run_lock


def test_single_instance_lock_is_exclusive(tmp_path):
    p = str(tmp_path / "run.lock")
    h1 = acquire_run_lock(p)
    assert h1 is not None
    assert acquire_run_lock(p) is None          # a second instance cannot acquire it
    fcntl.flock(h1, fcntl.LOCK_UN); h1.close()   # first releases
    h3 = acquire_run_lock(p)
    assert h3 is not None                        # now it's free again
    h3.close()
