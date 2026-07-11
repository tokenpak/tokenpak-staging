import socket
import threading

from tokenpak.proxy.server import _ThreadedHTTPServer


class _Proxy:
    def __init__(self):
        self._admission = threading.BoundedSemaphore(1)
        self._admission_rejected = 0


def _server(proxy):
    server = _ThreadedHTTPServer.__new__(_ThreadedHTTPServer)
    server.proxy_server = proxy
    server.shutdown_request = lambda request: request.close()
    return server


def test_model_admission_rejects_before_worker_creation():
    proxy = _Proxy()
    assert proxy._admission.acquire(False)
    server = _server(proxy)
    client, request = socket.socketpair()
    try:
        server.process_request(request, ("local", 0))
        response = client.recv(512)
        assert b"503 Service Unavailable" in response
        assert b"managed_admission_capacity" in response
        assert proxy._admission_rejected == 1
    finally:
        client.close()
        proxy._admission.release()


def test_control_plane_is_not_admission_gated():
    proxy = _Proxy()
    assert proxy._admission.acquire(False)
    server = _server(proxy)
    client, request = socket.socketpair()
    try:
        # The worker will fail harmlessly because this is only an admission test;
        # importantly, it is not rejected with the overload response.
        client.sendall(b"GET /health HTTP/1.1\r\nHost: local\r\n\r\n")
        server.process_request(request, ("local", 0))
        client.settimeout(0.5)
        try:
            client.recv(512)
        except (ConnectionResetError, OSError):
            pass
        assert proxy._admission_rejected == 0
    finally:
        client.close()
        proxy._admission.release()
