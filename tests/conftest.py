import socket

import duckdb
import pytest

# The static half of the no-network promise lives in test_llm.py: an ast walk
# that fails if any core module so much as imports a network library. This is
# the behavioral half. An import is not a connection, and the claim a buyer
# cares about is not "we do not import socket" but "nothing leaves your
# machine" -- so the suite runs with the socket layer torn out beneath it, and
# any code path that tries to open a connection fails loudly instead of
# quietly succeeding on a machine that happens to be online.
#
# Blocking is the DEFAULT and applies to every test. The llm tests do not need
# an exemption because their provider is a test double that never reaches the
# socket -- which is itself worth asserting, and this is what asserts it. A
# test that genuinely needs the network must ask for the `allow_network`
# fixture by name, which makes the exception visible in the test's own
# signature rather than buried in configuration.


class NetworkAccessDenied(RuntimeError):
    """Raised when a test tries to open a socket."""


_REAL_SOCKET = socket.socket
_REAL_CREATE_CONNECTION = socket.create_connection


def _denied(*args, **kwargs):
    raise NetworkAccessDenied(
        "a test tried to open a network connection. dataassay is a local tool: "
        "the core must never reach the network, and the llm adapter must reach "
        "it only through a provider a test has replaced. If this is deliberate, "
        "request the `allow_network` fixture."
    )


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if "allow_network" in request.fixturenames:
        return
    monkeypatch.setattr(socket, "socket", _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)


@pytest.fixture
def allow_network(monkeypatch):
    """Opt back in. Requesting this by name is the whole point -- it puts the
    exception in the test signature where a reviewer will see it."""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)
    monkeypatch.setattr(socket, "create_connection", _REAL_CREATE_CONNECTION)


@pytest.fixture
def write_csv(tmp_path):
    def _write(name: str, text: str, encoding: str = "utf-8"):
        p = tmp_path / name
        p.write_bytes(text.encode(encoding))
        return p

    return _write


@pytest.fixture
def write_parquet(tmp_path):
    def _write(name: str, select: str):
        p = tmp_path / name
        con = duckdb.connect(":memory:")
        con.execute(f"COPY ({select}) TO '{p}' (FORMAT PARQUET)")
        con.close()
        return p

    return _write
