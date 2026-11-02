"""Bounded, nonwaiting local measurement reads; funded decisions only peek futures."""
from concurrent.futures import Future
import queue
import math
import threading
import time


class ShadowReads:
    def __init__(self, *, workers=2, maxsize=32, max_age_s=2.0):
        if workers < 1 or maxsize < 1 or not math.isfinite(max_age_s) or max_age_s <= 0:
            raise ValueError("shadow read capacity and age must be positive")
        self.queue = queue.Queue(maxsize=maxsize)
        self.max_age_s = max_age_s
        for _ in range(workers):
            threading.Thread(target=self._run, name="entry-shadow-read", daemon=True).start()

    def submit(self, fn, *args):
        future = Future()
        try:
            self.queue.put_nowait((future, fn, args, time.monotonic()))
        except queue.Full as exc:
            raise queue.Full("shadow_queue_full") from exc
        return future

    def _run(self):
        while True:
            future, fn, args, queued = self.queue.get()
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                if time.monotonic() - queued > self.max_age_s:
                    raise TimeoutError("shadow_queue_expired")
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)
            finally:
                self.queue.task_done()
