"""Tiny allowlist HTTP/HTTPS-CONNECT egress proxy for ccchat session containers.

Sessions with restricted egress run on an `--internal` docker network (no direct internet) and
reach the outside world ONLY through this proxy via HTTPS_PROXY/HTTP_PROXY. The proxy permits a
request only when its target host matches the domain allowlist (CCCHAT_EGRESS_ALLOW, comma-
separated suffixes), so a session can reach e.g. api.anthropic.com / pypi.org / github.com but
nothing else. HTTPS is filtered by the CONNECT host (no MITM); plain HTTP by the Host/URI.
"""
import os
import select
import socket
import sys
import threading
from urllib.parse import urlsplit

ALLOW = [d.strip().lower().lstrip("*") for d in os.environ.get("CCCHAT_EGRESS_ALLOW", "").split(",") if d.strip()]
PORT = int(os.environ.get("PORT", "8888"))


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def allowed(host: str) -> bool:
    host = (host or "").lower().strip().rstrip(".")
    if ":" in host:
        host = host.split(":", 1)[0]
    for d in ALLOW:
        d = d.lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def handle(client):
    try:
        client.settimeout(30)
        req = b""
        while b"\r\n\r\n" not in req and len(req) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            req += chunk
        first = req.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = first.split(" ")
        method, target = (parts + ["", ""])[:2]

        if method.upper() == "CONNECT":
            host, _, port = target.partition(":")
            port = int(port or 443)
            if not allowed(host):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                log("DENY  CONNECT", host)
                client.close()
                return
            up = socket.create_connection((host, port), timeout=15)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            log("ALLOW CONNECT", f"{host}:{port}")
            _pipe(client, up)
            return

        # plain HTTP (absolute-form request-target, or Host header)
        host = ""
        if "://" in target:
            sp = urlsplit(target)
            host = sp.hostname or ""
            port = sp.port or 80
        else:
            for ln in req.split(b"\r\n"):
                if ln.lower().startswith(b"host:"):
                    hv = ln.split(b":", 1)[1].strip().decode("latin1")
                    host = hv.split(":")[0]
                    port = int(hv.split(":")[1]) if ":" in hv else 80
                    break
        if not host or not allowed(host):
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            log("DENY  HTTP", host or "?")
            client.close()
            return
        up = socket.create_connection((host, port), timeout=15)
        up.sendall(req)
        log("ALLOW HTTP", f"{host}:{port}")
        _pipe(client, up)
    except Exception as e:
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass
        log("ERR", repr(e))


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(128)
    log(f"egress-proxy listening on :{PORT}; allow={ALLOW}")
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
