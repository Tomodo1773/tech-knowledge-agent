from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge_agent.http_transport import GitHubHttpTransport, RemoteRequestError


@dataclass
class FakeSocket:
    timeouts: list[float] = field(default_factory=list)

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        *,
        retry_after: str | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self._retry_after = retry_after

    def getheader(self, name: str) -> str | None:
        return self._retry_after if name == "Retry-After" else None

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]


class FakeConnection:
    def __init__(self, result: FakeResponse | Exception) -> None:
        self.result = result
        self.sock = FakeSocket()
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def connect(self) -> None:
        if isinstance(self.result, TimeoutError):
            raise self.result

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, url, headers))

    def getresponse(self) -> FakeResponse:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


def _factory(results: list[FakeResponse | Exception]) -> tuple[object, list[FakeConnection]]:
    connections: list[FakeConnection] = []

    def create(host: str, port: int | None, timeout: float) -> FakeConnection:
        assert host == "api.github.com"
        assert port is None
        assert timeout == 5.0
        connection = FakeConnection(results[len(connections)])
        connections.append(connection)
        return connection

    return create, connections


def test_retries_retryable_status_and_caps_retry_after() -> None:
    factory, connections = _factory(
        [
            FakeResponse(429, retry_after="99"),
            FakeResponse(503),
            FakeResponse(200, b'{"sha":"ok"}'),
        ]
    )
    sleeps: list[float] = []
    transport = GitHubHttpTransport(connection_factory=factory, sleep=sleeps.append)  # type: ignore[arg-type]

    assert transport.get_json("https://api.github.com/repos/example?value=1") == {"sha": "ok"}
    assert sleeps == [2.0, 0.5]
    assert all(connection.closed for connection in connections)
    assert connections[-1].sock.timeouts == [30.0]
    assert connections[-1].requests[0][1] == "/repos/example?value=1"


def test_retries_timeout_three_times_with_sanitized_error() -> None:
    factory, connections = _factory(
        [TimeoutError("secret-one"), TimeoutError("secret-two"), TimeoutError("secret-three")]
    )
    transport = GitHubHttpTransport(connection_factory=factory, sleep=lambda _: None)  # type: ignore[arg-type]

    with pytest.raises(RemoteRequestError, match="timed out") as captured:
        transport.get_text("https://api.github.com/private-owner")

    assert len(connections) == 3
    assert "secret" not in str(captured.value)


def test_does_not_retry_non_retryable_status_or_network_error() -> None:
    status_factory, status_connections = _factory([FakeResponse(404, b"secret response")])
    status_transport = GitHubHttpTransport(
        connection_factory=status_factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    with pytest.raises(RemoteRequestError, match="HTTP 404") as captured:
        status_transport.get_json("https://api.github.com/private-owner")
    assert len(status_connections) == 1
    assert "private-owner" not in str(captured.value)
    assert "secret response" not in str(captured.value)

    network_factory, network_connections = _factory([OSError("sensitive-host")])
    network_transport = GitHubHttpTransport(
        connection_factory=network_factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    with pytest.raises(RemoteRequestError, match="request failed"):
        network_transport.get_text("https://api.github.com/private-owner")
    assert len(network_connections) == 1
