"""One write() per print() (2026-10-07, operator order).

CPython's print() writes the text and `end` in two calls. Under `python -u` (the runner) each call
is its own write(2), so a second thread appending to the same file between them joins two lines:
the runner log at 2026-10-07 15:56:33Z (`-> retry2026-...`) and 20:16:18Z
(`live_cap_per_hour=1000[real] ...`), 32 joins in one day. `line_print` builds the whole line
first and writes it once: the same bytes, one syscall instead of two, no lock and no added flush.

The entry scripts call `install()` before any other sniper import, because services bind
`log=print` as a default when imported, and only when run as a script (tests import them).
"""
import builtins
import sys


def line_print(*args, sep=" ", end="\n", file=None, flush=False):
    """builtins.print, writing `sep.join(args) + end` in ONE file.write call."""
    if sep is None:
        sep = " "
    if end is None:
        end = "\n"
    if not isinstance(sep, str):
        raise TypeError("sep must be None or a string, not %s" % type(sep).__name__)
    if not isinstance(end, str):
        raise TypeError("end must be None or a string, not %s" % type(end).__name__)
    if file is None:
        file = sys.stdout
        if file is None:                 # no console (pythonw): builtin print is a no-op too
            return
    file.write(sep.join(map(str, args)) + end)
    if flush:
        file.flush()


def install():
    builtins.print = line_print
