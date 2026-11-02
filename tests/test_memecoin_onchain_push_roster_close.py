"""A retired real WebSocket must not hold the replacement roster behind its peer's close."""
import json
import socket
import threading
import time

import pytest
from websockets.client import ClientProtocol
from websockets.frames import Frame, Opcode
from websockets.sync.client import ClientConnection
from websockets.uri import parse_uri
from websockets.utils import accept_key

from sniper.services import memecoin_onchain_push as push


OLD = "0x" + "11" * 20
NEW = "0x" + "22" * 20


def _reply(request_id, subscription):
    return json.dumps({"id": request_id, "result": subscription})


def _event(subscription, payload):
    return json.dumps({"method": "eth_subscription", "params": {
        "subscription": subscription, "result": payload}})


@pytest.mark.parametrize("trigger", ["head", "transfer"])
def test_roster_rollover_does_not_wait_for_an_unresponsive_close_peer(trigger, record_property):
    """Removing transport retirement must delay the new roster's actual ACK and delivered head."""
    state = push.PushState()
    roster = [OLD]
    ready, stop = threading.Event(), threading.Event()
    clients, peers, sent, no_overlap, unpublished, errors = [], [], [], [], [], []
    changed_at, acknowledged_at, completed = [], [], []

    def connect(_url, **_kwargs):
        no_overlap.append(all(client.socket.fileno() == -1 for client in clients))
        client_socket, peer = socket.socketpair()
        protocol = ClientProtocol(parse_uri("ws://offline.invalid"))
        client = ClientConnection(client_socket, protocol, close_timeout=5.0)
        peer.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key(protocol.key)}\r\n\r\n"
        ).encode())
        client.handshake(timeout=1.0)
        clients.append(client)
        peers.append(peer)
        real_send = client.send

        def send(message):
            sent.append(json.loads(message))
            real_send(message)

        client.send = send
        script = [_reply(1, "heads"), _reply(2, "transfers"),
                  _event("heads", {"number": "0x1"})]
        if len(clients) == 1:
            if trigger == "transfer":
                script.append(_event("transfers", {"transactionHash": "0x" + "aa" * 32}))
            # Leave another real message queued when consumption stops for the roster change.
            script.append(_event("heads", {"number": "0x2"}))
        for message in script:
            peer.sendall(Frame(Opcode.TEXT, message.encode()).serialize(mask=False))
        # The peer deliberately never reads or answers a close frame. No network is opened.
        return client

    def head():
        if len(clients) == 1 and trigger == "head":
            changed_at.append(time.monotonic())
            roster.append(NEW)
        elif len(clients) == 2 and state.transfer_addresses == (OLD, NEW):
            acknowledged_at.append(time.monotonic())
            completed.append((dict(state.reconnect_causes), state.transfer_addresses))
            ready.set()

    def transfer(_log):
        if len(clients) == 1:
            changed_at.append(time.monotonic())
            roster.append(NEW)

    def run():
        try:
            push.run_push_stream(
                "ws://offline.invalid", on_transfer=transfer, on_initialize=lambda _log: None,
                addresses_fn=lambda: list(roster), pool_manager=None, init_topic="unused",
                connect_fn=connect, state=state, on_head=head, roster_check_s=1e-9,
                on_connect_attempt=lambda: unpublished.append(state.transfer_addresses),
                sleep_fn=lambda _seconds: None, should_stop=stop.is_set)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        replaced = ready.wait(1.0)
        observed_until = time.monotonic()
    finally:
        stop.set()
        # Bound a RED run and the second socket's ordinary shutdown without touching services.
        for client in clients:
            client.close_socket()
        for peer in peers:
            peer.close()
        worker.join(2.0)

    elapsed = (acknowledged_at[0] if acknowledged_at else observed_until) - changed_at[0]
    record_property("replacement_ack_observed", replaced)
    record_property("replacement_ack_observation_s", elapsed)
    assert not worker.is_alive() and not errors, errors
    assert all(not client.recv_events_thread.is_alive() for client in clients)
    assert replaced, f"roster rollover still waited for the retired peer after {elapsed:.3f}s"
    assert len(clients) == 2 and all(no_overlap)
    assert unpublished == [None, None], "the old ACK must clear before the replacement connects"
    # Teardown intentionally closes the replacement too; inspect causality before that closure.
    assert completed == [({"armed_set": 1}, (OLD, NEW))]
    transfers = [request for request in sent if request["params"][0] == "logs"]
    assert transfers[-1]["params"][1]["topics"][2] == [
        "0x" + "00" * 12 + "11" * 20, "0x" + "00" * 12 + "22" * 20]
