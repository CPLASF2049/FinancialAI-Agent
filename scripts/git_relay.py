"""极简 HTTP CONNECT 中继：给 git 用的本地代理。

**为什么需要它**：国内网络下 `git push` 到 GitHub 常年 `Connection was reset` 或连不上，
而 Python 的 urllib（同一个网络）却能稳定 200。与其折腾代理软件，不如让 git 借 Python 的
连接走一趟：起一个本地 CONNECT 代理，git 的流量由它转交给 Python 的 socket。

用法（两个终端）::

    python scripts/git_relay.py 8899

    git -c http.proxy=http://127.0.0.1:8899 push origin main

它只监听 127.0.0.1，只允许 CONNECT 到 github.com:443，用完 Ctrl+C 关掉。
"""

from __future__ import annotations

import select
import socket
import sys
import threading

UPSTREAM = ("github.com", 443)
UPSTREAM_FALLBACKS = [("20.205.243.166", 443), ("140.82.121.3", 443)]


def connect_upstream() -> socket.socket:
    last: Exception | None = None
    for host, port in [UPSTREAM, *UPSTREAM_FALLBACKS]:
        try:
            sock = socket.create_connection((host, port), timeout=15)
            sock.settimeout(None)
            print(f"[relay] upstream {host}:{port} ok", flush=True)
            return sock
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"无法连接上游: {last}")


def pump(client: socket.socket) -> None:
    try:
        client.settimeout(20)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
            if len(head) > 65536:
                return

        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if not first.upper().startswith("CONNECT"):
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return

        upstream = connect_upstream()
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        client.settimeout(None)

        # 双向转发
        sockets = [client, upstream]
        while True:
            readable, _, errored = select.select(sockets, [], sockets, 60)
            if errored or not readable:
                break
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                target = upstream if source is client else client
                try:
                    target.sendall(data)
                except OSError:
                    return
    except Exception as exc:  # noqa: BLE001
        print(f"[relay] {exc}", flush=True)
    finally:
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(32)
    print(f"[relay] listening on 127.0.0.1:{port}", flush=True)
    while True:
        client, _addr = server.accept()
        threading.Thread(target=pump, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
