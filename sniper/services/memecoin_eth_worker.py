"""Ethereum owns its L1 waits and ledger connection; other chains never join its queue."""
import collections
import copy
import logging
import queue
import threading
import time

logger = logging.getLogger("memecoin")


class EthereumWorker:
    def __init__(self, hook, *, connect, tick, maxsize=32):
        self.hook, self.connect, self.tick = hook, connect, tick
        self.queue = queue.Queue(maxsize=maxsize)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="memecoin-ethereum", daemon=True)
        self.raw = getattr(hook, "raw", hook)
        self.excluded_retry_networks = {"1"}
        self.dip_lock = threading.Lock()
        self.last_tick = None
        self.error = None
        self.processed = self.refused = 0
        self.waits = collections.deque(maxlen=256)

    def __call__(self, conn, ev):
        if str(getattr(ev, "network_id", "")) != "1":
            return self.hook(conn, ev)
        try:
            self.queue.put_nowait((copy.deepcopy(ev), time.monotonic()))
        except queue.Full:
            self.refused += 1
            logger.error("Ethereum worker queue full: origin=%s", getattr(ev, "fomo_event_id", None))
            return {"status": "rejected", "reason": "eth_worker_queue_full"}
        return {"status": "queued"}

    def attempt(self, conn, ev):
        if str(getattr(ev, "network_id", "")) == "1" and threading.current_thread() is not self.thread:
            return {"status": "duplicate_delivery", "reason": "ethereum_worker_owns_retry"}
        return getattr(self.hook, "attempt", self.hook)(conn, ev)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=3)

    def run(self):
        conn = None
        try:
            while not self.stopping.is_set():
                try:
                    if conn is None:
                        conn = self.connect()
                    try:
                        ev, enqueued = self.queue.get(timeout=0.1)
                    except queue.Empty:
                        ev = None
                    if ev is not None:
                        try:
                            self.waits.append((time.monotonic() - enqueued) * 1000)
                            self.hook(conn, ev)  # freshness and authority checked NOW, by the ordinary hook
                            self.processed += 1
                        finally:
                            self.queue.task_done()
                    if self.last_tick is None or time.monotonic() - self.last_tick >= 2:
                        self.tick(conn)
                        self.last_tick = time.monotonic()
                    conn.commit()
                    self.error = None
                except Exception as exc:
                    self.error = type(exc).__name__
                    logger.exception("Ethereum worker failed")
                    if conn is not None:
                        conn.rollback()
                        conn.close()
                        conn = None
                    self.stopping.wait(1)
        finally:
            if conn is not None:
                conn.close()

    def status(self):
        waits = sorted(self.waits)
        return {"alive": self.thread.is_alive(), "queued": self.queue.qsize(),
                "processed": self.processed, "queue_full": self.refused, "error": self.error,
                "last_sweep_age_s": None if self.last_tick is None else
                round(time.monotonic() - self.last_tick, 3),
                "queue_wait_ms_p50": waits[len(waits) // 2] if waits else None,
                "queue_wait_ms_p90": waits[min(len(waits) - 1, int(len(waits) * .9))] if waits else None}
