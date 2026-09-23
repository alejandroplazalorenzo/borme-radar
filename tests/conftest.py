from __future__ import annotations

import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must run offline: any real connection attempt fails the test."""

    def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("network access attempted during tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
