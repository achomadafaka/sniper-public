"""Fakes for the process-wide `time` module that answer ONE thread (2026-09-29).

`some_module.time` in a test IS the process-wide `time` module. A fake patched there is read by every
thread in the xdist worker: pool workers, import-time sockets, and warmers leaked by earlier tests. A
recording or no-op sleep also catches their sleeps. A finite fake clock ran out that way under the
full deploy run and refused the deploy of 0aaab9a5
(test_onchain_evm_wallet_watch_write_scope, StopIteration).

`patch_local` answers the fake to the calling thread only; every other thread gets the real function.
Use it wherever a fake has state (an iterator, a counter, a list of sleeps). A constant clock or a
no-op sleep cannot run out, so those sites are left as they are.
"""
import threading
import time as _time


def patch_local(monkeypatch, name, fake, *, module=_time):
    """Patch `module.<name>` (default: the time module) with `fake` for the calling thread only."""
    me = threading.current_thread()
    real = getattr(module, name)

    def local(*args, **kwargs):
        return fake(*args, **kwargs) if threading.current_thread() is me else real(*args, **kwargs)
    monkeypatch.setattr(module, name, local)
    return local
