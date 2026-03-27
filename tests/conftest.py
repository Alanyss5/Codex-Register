import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fallback_socketpair_no_peer_check(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    if family == socket.AF_INET:
        host = "127.0.0.1"
    elif family == socket.AF_INET6:
        host = "::1"
    else:
        raise ValueError("Only AF_INET and AF_INET6 socket address families are supported")
    if type != socket.SOCK_STREAM:
        raise ValueError("Only SOCK_STREAM socket type is supported")
    if proto != 0:
        raise ValueError("Only protocol zero is supported")

    listener = socket.socket(family, type, proto)
    try:
        listener.bind((host, 0))
        listener.listen(1)
        address = listener.getsockname()[:2]
        client = socket.socket(family, type, proto)
        try:
            client.setblocking(False)
            try:
                client.connect(address)
            except (BlockingIOError, InterruptedError):
                pass
            client.setblocking(True)
            server, _ = listener.accept()
        except Exception:
            client.close()
            raise
    finally:
        listener.close()

    return server, client


if sys.platform.startswith("win"):
    socket.socketpair = _fallback_socketpair_no_peer_check
