"""Loop-local log containment for grpcio1.71's audited shared-CQ wake-read race.

This does not retry a callback or establish stream delivery, ACK, head or proof readiness.
Unknown builds and every other failure retain the loop's original error reporting.
"""
import asyncio
import errno
import functools

import grpc
from grpc._cython import cygrpc


_CQ_SOURCE = 'src/python/grpcio/grpc/_cython/_cygrpc/aio/completion_queue.pyx.pxi'
_CQ_METHOD = cygrpc.PollerCompletionQueue._handle_events
_MAX_SUPPRESSED = (1 << 31) - 1


class _WakeupHandler:
    def __init__(self, loop, prior):
        self._loop, self._prior = loop, prior
        self._suppressed_count = 0
        code = getattr(_CQ_METHOD, '__code__', None)
        self._audited_build = (getattr(grpc, '__version__', None) == '1.71.0'
            and getattr(_CQ_METHOD, '__module__', None) == 'grpc._cython.cygrpc'
            and getattr(_CQ_METHOD, '__qualname__', None) == 'PollerCompletionQueue._handle_events'
            and getattr(code, 'co_filename', None) == _CQ_SOURCE
            and getattr(code, 'co_firstlineno', None) == 143)

    @property
    def suppressed_count(self):
        return self._suppressed_count

    def _is_wakeup(self, loop, context):
        if not self._audited_build or loop is not self._loop:
            return False
        exc = context.get('exception')
        if type(exc) is not BlockingIOError or exc.errno != errno.EAGAIN:
            return False
        callback = getattr(context.get('handle'), '_callback', None)
        if (type(callback) is not functools.partial or len(callback.args) != 1
                or callback.args[0] is not loop or callback.keywords):
            return False
        method = callback.func
        if (getattr(method, '__func__', None) is not _CQ_METHOD
                or type(getattr(method, '__self__', None)) is not cygrpc.PollerCompletionQueue):
            return False
        leaf = exc.__traceback__
        if leaf is None:
            return False
        while leaf.tb_next is not None:
            leaf = leaf.tb_next
        code = leaf.tb_frame.f_code
        # The Cython traceback frame differs from its exported Python method code object.
        return (code.co_filename == _CQ_SOURCE
            and code.co_name == 'grpc._cython.cygrpc.PollerCompletionQueue._handle_events'
            and leaf.tb_lineno == 147)

    def __call__(self, loop, context):
        try:
            wakeup = self._is_wakeup(loop, context)
        except Exception:
            wakeup = False  # Unknown callback/context shapes retain original error reporting.
        if wakeup:
            self._suppressed_count = min(_MAX_SUPPRESSED, self._suppressed_count + 1)
        elif self._prior is not None:
            self._prior(loop, context)
        else:
            loop.default_exception_handler(context)


def install_grpc_wakeup_handler():
    """Install once on the running channel-owning loop, before channel construction."""
    loop = asyncio.get_running_loop()
    prior = loop.get_exception_handler()
    if isinstance(prior, _WakeupHandler) and prior._loop is loop:
        return prior
    handler = _WakeupHandler(loop, prior)
    loop.set_exception_handler(handler)
    return handler
