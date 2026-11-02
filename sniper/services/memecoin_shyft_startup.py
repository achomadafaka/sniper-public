"""Stage shared-quota streams without withholding funded exit/recovery ownership."""
from pathlib import Path
import logging
import threading


class ShyftStartupGate:
    def __init__(self, *, env=None, stop=None, ready_fn=None):
        from sniper.services.memecoin_solana_provider import infra_provider
        self.ready = threading.Event()
        self.stop = stop if stop is not None else threading.Event()
        self.env, self.ready_fn = env, ready_fn
        self.status = {'state': 'staged', 'errors': ()}
        if infra_provider(env) != 'shyft':
            self.ready.set()
            self.status['state'] = 'ready'
            return
        threading.Thread(target=self._wait, name='shyft-startup-readiness', daemon=True).start()

    def _wait(self):
        from scripts.memecoin_runtime_verify import verify_shyft_watcher
        from sniper.util.state_paths import state_root
        code_root = Path(__file__).resolve().parents[2]
        while not self.stop.is_set():
            try:
                errors = verify_shyft_watcher(code_root, state_root(self.env))
                if self.ready_fn is not None and not self.ready_fn():
                    errors = [*errors, 'custody_identity_unverified']
            except Exception as exc:
                errors = ['watcher_check:' + type(exc).__name__]
            self.status['errors'] = tuple(errors)
            if not errors:
                self.status['state'] = 'ready'
                self.ready.set()
                return
            self.stop.wait(1.0)

    def wait(self):
        while not self.stop.is_set():
            if self.ready.wait(.25):
                return not self.stop.is_set()
        return False

    def defer(self, start, *, name):
        if self.ready.is_set():
            return start()
        def wait_and_start():
            if self.wait():
                try:
                    start()
                except Exception:
                    logging.getLogger(__name__).exception('Shyft staged startup failed: %s', name)
        thread = threading.Thread(target=wait_and_start, name=name, daemon=True)
        thread.start()
        return thread
