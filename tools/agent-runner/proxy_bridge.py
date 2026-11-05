#!/usr/bin/env python3
"""Narrow TCP-to-Unix bridge used only for the Codex control plane."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import socket
import threading
from typing import Callable
from urllib.parse import urlsplit


STOP = threading.Event()


def pump(source: socket.socket, destination: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            destination.sendall(data)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def relay(client: socket.socket, connect: Callable[[], socket.socket]) -> None:
    try:
        upstream = connect()
    except OSError:
        client.close()
        return
    with client, upstream:
        left = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        right = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        left.start()
        right.start()
        left.join()
        right.join()


def serve(server: socket.socket, connect: Callable[[], socket.socket]) -> int:
    server.settimeout(0.5)
    while not STOP.is_set():
        try:
            client, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=relay, args=(client, connect), daemon=True).start()
    return 0


def host(socket_path: Path, proxy: str) -> int:
    parsed = urlsplit(proxy)
    if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
        raise SystemExit("proxy must be an explicit http://host:port URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("proxy URL credentials, query and fragment are not supported")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(16)
    try:
        return serve(
            server,
            lambda: socket.create_connection((parsed.hostname, parsed.port), timeout=10),
        )
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def container(socket_path: Path, port: int) -> int:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(16)

    def connect() -> socket.socket:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(str(socket_path))
        return upstream

    try:
        return serve(server, connect)
    finally:
        server.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    host_parser = subparsers.add_parser("host")
    host_parser.add_argument("--socket", type=Path, required=True)
    host_parser.add_argument("--proxy", required=True)
    container_parser = subparsers.add_parser("container")
    container_parser.add_argument("--socket", type=Path, required=True)
    container_parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    if args.mode == "host":
        return host(args.socket, args.proxy)
    return container(args.socket, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
