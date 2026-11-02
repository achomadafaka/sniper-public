import json
from pathlib import Path
import threading

from sniper.office import wallet_watch as W
from sniper.office.wallet_network import WalletNetwork


def test_concurrent_watch_updates_export_the_committed_list(tmp_path, monkeypatch):
    (tmp_path / 'pumpswap').mkdir()
    path = tmp_path / 'graph.db'
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    replace = Path.replace
    def paused_replace(source, target):
        if threading.current_thread().name == 'first-watch' and Path(target) == W.watch_file(tmp_path):
            entered.set()
            assert release.wait(3)
        return replace(source, target)
    monkeypatch.setattr(Path, 'replace', paused_replace)
    def add(address, second=False):
        try:
            net = WalletNetwork(path, clock=lambda: 100.)
            W.add(net, tmp_path, address=address, operator_id=None, evidence='test', source='host', actor='test', now=100.)
        except Exception as exc:
            errors.append(exc)
        finally:
            if second: done.set()
    # Initialize schema before racing writers, so this checks export ordering rather than DDL.
    WalletNetwork(path)._conn().close()
    a = threading.Thread(target=add, args=('1' * 40,), name='first-watch')
    b = threading.Thread(target=add, args=('2' * 40, True), name='second-watch')
    a.start()
    try:
        assert entered.wait(3)
        b.start()
        assert not done.wait(.15), 'second writer bypassed the export transaction'
    finally:
        release.set()
        a.join(3)
        if b.ident is not None: b.join(3)
    assert not errors
    assert set(json.loads(W.watch_file(tmp_path).read_text())['wallets']) == {'1' * 40, '2' * 40}
