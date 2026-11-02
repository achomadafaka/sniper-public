"""Local CQ controls; these tests never contact a provider or grant trade authority."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import functools
import threading
from types import TracebackType

import grpc
import pytest


def _cq_context(loop):
    # Use the installed Cython callback and its genuine recv traceback, not a name mock.
    for key in loop._selector.get_map().values():
        for handle in key.data if isinstance(key.data, tuple) else ():
            callback = getattr(handle, '_callback', None)
            if (isinstance(callback, functools.partial)
                    and getattr(callback.func, '__qualname__', '') == 'PollerCompletionQueue._handle_events'):
                for _ in range(16):
                    try:
                        callback()
                    except BlockingIOError as exc:
                        return dict(message='Exception in CQ wake callback', exception=exc, handle=handle)
                pytest.fail('CQ did not expose the bounded empty-socket witness')
    pytest.fail('installed CQ reader was not registered')


def _with_cq(check):
    async def exercise():
        loop = asyncio.get_running_loop()
        prior = []
        loop.set_exception_handler(lambda got_loop, context: prior.append((got_loop, context)))
        channel = grpc.aio.insecure_channel('127.0.0.1:1')  # No RPC, hence no connection attempt.
        try:
            check(loop, _cq_context(loop), prior)
        finally:
            await channel.close()
    asyncio.run(exercise())


def test_exact_installed_wake_recv_is_counted_without_mutating_context():
    def check(loop, context, prior):
        from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
        handler = install_grpc_wakeup_handler()
        status = dict(acknowledged=False, head_slot=None, source_proof=None)
        context['authority_status'] = status
        original = context.copy()
        loop.call_exception_handler(context)
        assert handler.suppressed_count == 1
        assert prior == []
        assert context == original and status == dict(acknowledged=False, head_slot=None, source_proof=None)
    _with_cq(check)


@pytest.mark.parametrize('case', ['callback', 'errno', 'missing_trace', 'other_cq_site',
    'authority_error', 'missing_handle', 'malformed_handle', 'subclass', 'wrong_loop',
    'grpc_version', 'missing_code'])
def test_other_errors_delegate_the_original_context(case, monkeypatch):
    def check(loop, context, prior):
        from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
        if case == 'grpc_version':
            monkeypatch.setattr(grpc, '__version__', '1.72.0')
        elif case == 'missing_code':
            from types import SimpleNamespace
            from sniper.services import memecoin_grpc_wakeup
            monkeypatch.setattr(memecoin_grpc_wakeup, '_CQ_METHOD', SimpleNamespace(
                __module__='grpc._cython.cygrpc', __qualname__='PollerCompletionQueue._handle_events'))
        handler = install_grpc_wakeup_handler()
        exc = context['exception']
        if case == 'callback':
            context['handle'] = asyncio.Handle(lambda: None, (), loop)
        elif case == 'errno':
            exc.errno = errno.EBADF
        elif case == 'missing_trace':
            exc.__traceback__ = None
        elif case == 'other_cq_site':
            leaf = exc.__traceback__
            while leaf.tb_next:
                leaf = leaf.tb_next
            # Same actual callback/frame, but a different failure instruction/line.
            exc.__traceback__ = TracebackType(None, leaf.tb_frame, leaf.tb_lasti, leaf.tb_lineno + 1)
        elif case == 'authority_error':
            try:
                raise RuntimeError('source payment proof failed')
            except RuntimeError as authority_error:
                context['exception'] = authority_error
        elif case == 'missing_handle':
            del context['handle']
        elif case == 'malformed_handle':
            class UnknownHandle:
                @property
                def _callback(self):
                    raise ValueError('unknown handle layout')
            context['handle'] = UnknownHandle()
        elif case == 'subclass':
            class OtherBlockingError(BlockingIOError):
                pass
            context['exception'] = OtherBlockingError(errno.EAGAIN, 'other').with_traceback(exc.__traceback__)
        elif case == 'wrong_loop':
            callback = context['handle']._callback
            context['handle'] = asyncio.Handle(functools.partial(callback.func, object()), (), loop)
        loop.call_exception_handler(context)
        assert handler.suppressed_count == 0
        assert len(prior) == 1 and prior[0][0] is loop and prior[0][1] is context
    _with_cq(check)


def test_a_real_failing_proof_callback_keeps_its_error_visible():
    from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
    async def exercise():
        loop, delegated = asyncio.get_running_loop(), []
        loop.set_exception_handler(lambda got_loop, context: delegated.append(context))
        handler = install_grpc_wakeup_handler()
        error = RuntimeError('source payment proof failed')
        def proof_callback():
            raise error
        loop.call_soon(proof_callback)
        await asyncio.sleep(0)
        assert len(delegated) == 1 and delegated[0]['exception'] is error
        assert delegated[0]['handle']._callback is proof_callback
        assert handler.suppressed_count == 0
    asyncio.run(exercise())


def test_default_handler_receives_the_original_unrecognized_context(monkeypatch):
    def check(loop, context, prior):
        from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
        loop.set_exception_handler(None)
        delegated = []
        monkeypatch.setattr(loop, 'default_exception_handler', delegated.append)
        handler = install_grpc_wakeup_handler()
        context['exception'] = RuntimeError('channel callback failed')
        loop.call_exception_handler(context)
        assert delegated == [context] and delegated[0] is context
        assert handler.suppressed_count == 0
    _with_cq(check)


def test_installation_is_idempotent_per_loop_and_the_counter_saturates():
    handlers = []
    def check(loop, context, prior):
        from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
        handler = install_grpc_wakeup_handler()
        assert install_grpc_wakeup_handler() is handler
        assert loop.get_exception_handler() is handler
        handler._suppressed_count = (1 << 31) - 2
        for _ in range(3):
            loop.call_exception_handler(context)
        assert handler.suppressed_count == (1 << 31) - 1 and prior == []
        handlers.append(handler)
    _with_cq(check)
    _with_cq(check)
    assert handlers[0] is not handlers[1]


def test_two_loop_threads_deliver_all_32_identified_local_stream_replies():
    from sniper.services.memecoin_grpc_wakeup import install_grpc_wakeup_handler
    def reply(incoming, context):
        for request in incoming:
            yield b'ack:' + request
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler('local.Control', {
        'Replies': grpc.stream_stream_rpc_method_handler(reply)}),))
    port = server.add_insecure_port('127.0.0.1:0')
    assert port > 0
    server.start()
    barrier = threading.Barrier(2)
    results, errors = [None, None], [None, None]
    def client(index):
        async def exercise():
            loop, delegated = asyncio.get_running_loop(), []
            loop.set_exception_handler(lambda got_loop, context: delegated.append(context))
            handler = install_grpc_wakeup_handler()
            async with grpc.aio.insecure_channel(f'127.0.0.1:{port}') as channel:
                # Deterministically exercise the genuine empty wake read, separately from delivery.
                loop.call_exception_handler(_cq_context(loop))
                barrier.wait(timeout=3)
                requests = [f'{index}:{number}'.encode() for number in range(16)]
                async def incoming():
                    for request in requests:
                        yield request
                async def collect():
                    call = channel.stream_stream('/local.Control/Replies')
                    return [response async for response in call(incoming())]
                responses = await asyncio.wait_for(collect(), 5)
            results[index] = (responses, handler.suppressed_count, delegated)
        try:
            asyncio.run(exercise())
        except BaseException as exc:
            errors[index] = exc
    clients = [threading.Thread(target=client, args=(index,), daemon=True) for index in range(2)]
    try:
        for thread in clients:
            thread.start()
        for thread in clients:
            thread.join(8)
        assert not any(thread.is_alive() for thread in clients)
        assert errors == [None, None]
        for index, (responses, count, delegated) in enumerate(results):
            assert responses == [f'ack:{index}:{number}'.encode() for number in range(16)]
            assert count >= 1 and delegated == []
    finally:
        server.stop(0).wait(timeout=3)


@pytest.mark.parametrize('owner', ['signals', 'observer', 'price_accounts', 'source_accounts', 'rabbit_tape'])
def test_each_runner_loop_installs_before_its_first_grpc_consumer(owner, monkeypatch):
    from sniper.services import memecoin_grpc_wakeup as wake
    installed, consumers = [], []
    original = wake.install_grpc_wakeup_handler
    def install():
        installed.append(asyncio.get_running_loop())
        return original()
    monkeypatch.setattr(wake, 'install_grpc_wakeup_handler', install)
    stop = threading.Event()
    def reached():
        stop.set()
        assert installed == [asyncio.get_running_loop()]
        assert asyncio.get_running_loop().get_exception_handler() is not None
        consumers.append(owner)
    async def consume(*args, **kwargs):
        reached()
    if owner == 'signals':
        from sniper.services import memecoin_slot_signal as signal, memecoin_shyft_signal
        monkeypatch.setattr(memecoin_shyft_signal, 'run_feed', consume)
        asyncio.run(signal._run_signals(lambda buy: None, lambda: {}, '', {}, stop,
            None, None, None, None, ('local-token', ('https://grpc.ams.shyft.to',)),
            infra_provider='shyft'))
    elif owner == 'observer':
        from sniper.services import memecoin_execution_observer as observer
        from types import SimpleNamespace
        class Stream:
            def __aiter__(self):
                return self
            def cancel(self):
                pass
        class Channel:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream_stream(self, *args, **kwargs):
                return lambda *args, **kwargs: Stream()
        def channel(*args, **kwargs):
            reached()
            return Channel()
        monkeypatch.setattr(grpc.aio, 'secure_channel', channel)
        asyncio.run(observer._run_yellowstone_execution_observer(custody_pubkey='1' * 32,
            url='https://grpc.fra.shyft.to', token='local-token',
            journal=SimpleNamespace(), status={}, stop=stop))
    elif owner == 'rabbit_tape':
        from sniper.services import memecoin_rabbit_tape as tape_mod
        class TapeStream:
            def __aiter__(self):
                return self
            async def __anext__(self):
                raise StopAsyncIteration
            def cancel(self):
                pass
        class TapeChannel:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream_stream(self, *args, **kwargs):
                return lambda *args, **kwargs: TapeStream()
        def tape_channel(*args, **kwargs):
            reached()
            return TapeChannel()
        monkeypatch.setattr(grpc.aio, 'secure_channel', tape_channel)
        thread = tape_mod.start_tape_thread('https://grpc.ams.shyft.to', 'local-token', tape_mod.PoolTape(),
                                            lambda: (), {}, stop)
        thread.join(3)
        assert not thread.is_alive()
    elif owner == 'price_accounts':
        from sniper.services.memecoin_solana_swap_marks import AccountPriceStream
        stream = AccountPriceStream('https://grpc.ams.shyft.to', 'local-token', run_fn=consume)
        thread = stream.start()
        thread.join(2)
        stream.close()
        assert not thread.is_alive()
    else:
        from sniper.services.memecoin_source_sell_mirror import OwnerAccountWatch
        from sniper.services import memecoin_shyft_accounts
        monkeypatch.setattr(memecoin_shyft_accounts, 'run_accounts', consume)
        watch = OwnerAccountWatch(ws_url='', rpc_url='', sink=lambda *args: None,
            shyft_config=('local-token', ('https://grpc.ams.shyft.to',)))
        thread = watch.start()
        thread.join(2)
        watch._stop.set()
        assert not thread.is_alive()
    assert consumers == [owner]
