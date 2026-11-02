"""Atomic file writes.

The runner dies ~5x/day (SIGKILL / suspend). Any store rewritten in place with a plain
`open(path, "w")` can be caught mid-write and left truncated. For `.fomo_tokens.json` that means
a spent refresh token on next load -> silent auth death; for the heartbeat / clock offset it
means a torn read by the web. Write to a temp file in the SAME directory, fsync, then
`os.replace` (atomic within one filesystem): a crash leaves either the whole old file or the
whole new one, never a partial.
"""
from __future__ import annotations

import json
import os
import tempfile


def atomic_write_json(path, obj, *, mode=None):
    """Serialize obj to JSON and replace `path` atomically. Raises on serialize/IO error, leaving
    any existing file at `path` untouched (the temp file is cleaned up). `mode` (e.g. 0o600) is
    applied to the temp file BEFORE the replace, so the final file never briefly exists with
    default perms."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
