"""A stand-in Claude Code session for the notice outbox tests.

It owns `<directory>/<its pid>.sock`, appends every byte a peer writes there to
`<record>`, and — given a command — launches it with
`CLAUDE_CODE_MESSAGING_SOCKET` naming that socket, so the child's PARENT is the
socket owner exactly as under Claude Code. It outlives its child: killing the
child alone is the "stdio died, session lives" case. `<record>.ready` names the
socket once it is bound; `<record>.child` carries the child's pid.

Usage: python fake_session.py <directory> <record> [command ...]
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading


def _take(connection: socket.socket, record: str, lock: threading.Lock) -> None:
    received = b""
    with connection:
        while chunk := connection.recv(65536):
            received += chunk
    with lock, open(record, "ab") as out:
        out.write(received)


def main() -> None:
    directory, record, *command = sys.argv[1:]
    path = os.path.join(directory, f"{os.getpid()}.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(16)
    lock = threading.Lock()

    def serve() -> None:
        while True:
            connection, _ = server.accept()
            threading.Thread(
                target=_take, args=(connection, record, lock), daemon=True
            ).start()

    threading.Thread(target=serve, daemon=True).start()
    with open(record + ".ready", "w") as ready:
        ready.write(path)
    if command:
        child = subprocess.Popen(
            command, env={**os.environ, "CLAUDE_CODE_MESSAGING_SOCKET": path}
        )
        with open(record + ".child", "w") as out:
            out.write(str(child.pid))
        child.wait()
    threading.Event().wait()


if __name__ == "__main__":
    main()
