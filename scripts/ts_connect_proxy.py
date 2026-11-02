#!/usr/bin/env python3
"""Minimal HTTP CONNECT proxy on 127.0.0.1:3128 for tailscaled.

Why this exists (2026-07-10): this host runs NordVPN with a kill switch and fake-IP DNS. Any
NORMAL process resolves hostnames to Nord's synthetic IPs and its TLS flows fine through the
tunnel (SNI-routed). tailscaled, however, MARKS its own packets to bypass policy routing (so it
never loops through a foreign VPN) — and those marked packets are dropped by the kill switch, so
it can never reach the Tailscale control plane. Pointing tailscaled at this proxy
(HTTPS_PROXY=http://127.0.0.1:3128) fixes it: tailscaled CONNECTs to localhost (allowed), and THIS
process — an ordinary unmarked process — dials the hostname through the Nord tunnel like curl does.

Control plane + DERP-over-HTTPS both traverse it; WireGuard UDP stays dead on this network and
Tailscale falls back to DERP relays, which is fine for dashboard traffic.
"""
import socket
import socketserver
import threading

LISTEN = ("127.0.0.1", 3128)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                head += chunk
                if len(head) > 32768:
                    return
            line = head.split(b"\r\n", 1)[0].decode("latin1")
            method, target, _ = line.split(" ", 2)
            if method.upper() != "CONNECT":
                self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                return
            host, _, port = target.rpartition(":")
            upstream = socket.create_connection((host, int(port)), timeout=20)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        except Exception:
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
            return
        # bidirectional pump until either side closes
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
        t = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        t.start()
        pump(self.request, upstream)
        t.join(timeout=5)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    Server(LISTEN, Handler).serve_forever()
