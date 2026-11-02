"""All tests are synthetic. Reject outbound connections and DNS resolution."""
import socket
import threading
import pytest

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args,**kwargs):raise AssertionError('Outbound network disabled in tests')
    # Windows implements socketpair with a temporary loopback connection. Allow
    # only that stdlib operation, not general loopback or external networking.
    pair=socket.socketpair;connect=socket.socket.connect;local=threading.local()
    def socketpair(*args,**kwargs):
        local.pair=True
        try:return pair(*args,**kwargs)
        finally:local.pair=False
    def guarded_connect(sock,address):
        if getattr(local,'pair',False) and address[0] in ('127.0.0.1','::1'):
            return connect(sock,address)
        return denied()
    monkeypatch.setattr(socket,'socketpair',socketpair)
    monkeypatch.setattr(socket.socket,'connect',guarded_connect)
    monkeypatch.setattr(socket.socket,'connect_ex',denied)
    monkeypatch.setattr(socket,'create_connection',denied)
    monkeypatch.setattr(socket,'getaddrinfo',denied)
