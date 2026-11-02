"""Phased confirmed handover keeps nine steady funded streams within quota ten."""
import asyncio
import time

import pytest

from scripts.research import onchain_wallet_watch as watcher
from sniper.services import memecoin_shyft_confirmed as confirmed, memecoin_shyft_signal as signal
from sniper.services import memecoin_solana_provider as provider


@pytest.mark.parametrize('old_alive', [True, False])
def test_only_primary_opens_until_exact_old_identity_is_gone(monkeypatch, old_alive):
    monkeypatch.setattr(provider, 'infra_provider', lambda: 'shyft')
    monkeypatch.setattr(watcher, 'HTTP_URL', 'https://rpc.ams.shyft.to?api_key=offline')
    monkeypatch.setattr(signal, 'live_config', lambda: ('offline', (
        'https://grpc.ams.shyft.to', 'https://grpc.fra.shyft.to')))
    opened, alive = [], [old_alive]
    async def feed(url, token, owners, worker, status, stop):
        opened.append(url)
        status.update(connected=True, requested_owners=('owner',), acknowledged_owners=('owner',),
                      head_slot=42, head_monotonic=time.monotonic())
        while not stop.is_set():
            await asyncio.sleep(.005)
    monkeypatch.setattr(confirmed, 'run_feed', feed)
    async def exercise():
        group = watcher.start_confirmed_feed(lambda: {'owner': {}}, object(), {},
            old_watchers=((123, '/old/watch.py', 'birth'),), old_alive_fn=lambda rows: alive[0])
        try:
            assert group['status']['http_role']['hostname'] == 'rpc.ams.shyft.to'
            assert len(group['status']['http_role']['url_fingerprint']) == 64
            await asyncio.sleep(.03)
            assert len(opened) == (1 if old_alive else 2)
            if old_alive:
                assert group['status']['handover']['phase'] == 'primary'
                alive[0] = False
                await asyncio.sleep(.3)
            assert len(opened) == 2
            assert group['status']['handover']['phase'] == 'full'
        finally:
            group['stop'].set()
            for task in group['tasks']: task.cancel()
            await asyncio.gather(*group['tasks'], return_exceptions=True)
            group['worker'].close()
    asyncio.run(exercise())


def test_primary_ready_line_is_distinct_and_full_replaces_it(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, 'READY', tmp_path / 'ready')
    assert watcher._say_ready(primary=True)
    assert watcher.READY.read_text().split()[1] == 'primary'
    assert watcher._say_ready()
    assert watcher.READY.read_text().split()[1] != 'primary'
