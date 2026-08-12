from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge_agent.http_transport import (
    GitHubHttpTransport,
    RemoteRequestError,
    SlackHttpTransport,
)


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
    transport = GitHubHttpTransport(
        "ghp_secret_token",
        connection_factory=factory,  # type: ignore[arg-type]
        sleep=sleeps.append,
    )

    assert transport.get_json("https://api.github.com/repos/example?value=1") == {"sha": "ok"}
    assert sleeps == [2.0, 0.5]
    assert all(connection.closed for connection in connections)
    assert connections[-1].sock.timeouts == [30.0]
    assert connections[-1].requests[0][1] == "/repos/example?value=1"
    # The private repository is only readable with the token on every attempt.
    assert all(
        connection.requests[0][2]["Authorization"] == "Bearer ghp_secret_token"
        for connection in connections
    )


def test_retries_timeout_three_times_with_sanitized_error() -> None:
    factory, connections = _factory(
        [TimeoutError("secret-one"), TimeoutError("secret-two"), TimeoutError("secret-three")]
    )
    transport = GitHubHttpTransport(
        "ghp_secret_token",
        connection_factory=factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )

    with pytest.raises(RemoteRequestError, match="timed out") as captured:
        transport.get_text("https://api.github.com/private-owner")

    assert len(connections) == 3
    assert "secret" not in str(captured.value)


def test_does_not_retry_non_retryable_status_or_network_error() -> None:
    status_factory, status_connections = _factory([FakeResponse(404, b"secret response")])
    status_transport = GitHubHttpTransport(
        "ghp_secret_token",
        connection_factory=status_factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    with pytest.raises(RemoteRequestError, match="HTTP 404") as captured:
        status_transport.get_json("https://api.github.com/private-owner")
    assert len(status_connections) == 1
    assert "private-owner" not in str(captured.value)
    assert "secret response" not in str(captured.value)
    assert "ghp_secret_token" not in str(captured.value)

    network_factory, network_connections = _factory([OSError("sensitive-host")])
    network_transport = GitHubHttpTransport(
        "ghp_secret_token",
        connection_factory=network_factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )
    with pytest.raises(RemoteRequestError, match="request failed"):
        network_transport.get_text("https://api.github.com/private-owner")
    assert len(network_connections) == 1


class FakeSlackConnection(FakeConnection):
    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        *,
        headers: dict[str, str],
    ) -> None:
        self.requests.append((method, url, headers))
        self.body = body


def _slack_factory(
    results: list[FakeResponse | Exception],
) -> tuple[object, list[FakeSlackConnection]]:
    connections: list[FakeSlackConnection] = []

    def create(host: str, port: int | None, timeout: float) -> FakeSlackConnection:
        assert host == "slack.com"
        assert port == 443
        assert timeout == 5.0
        connection = FakeSlackConnection(results[len(connections)])
        connections.append(connection)
        return connection

    return create, connections


def test_slack_posts_json_with_bearer_token_and_retries_rate_limits() -> None:
    factory, connections = _slack_factory(
        [FakeResponse(429, retry_after="60"), FakeResponse(200, b'{"ok":true,"ts":"1.2"}')]
    )
    sleeps: list[float] = []
    transport = SlackHttpTransport(
        "xoxb-secret-token",
        connection_factory=factory,  # type: ignore[arg-type]
        sleep=sleeps.append,
    )

    assert transport.call("chat.postMessage", {"channel": "D1"}) == {"ok": True, "ts": "1.2"}
    assert sleeps == [2.0]
    method, path, headers = connections[-1].requests[0]
    assert (method, path) == ("POST", "/api/chat.postMessage")
    assert headers["Authorization"] == "Bearer xoxb-secret-token"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert connections[-1].body == b'{"channel": "D1"}'
    assert all(connection.closed for connection in connections)


def test_slack_rejects_bad_method_and_keeps_token_out_of_failures() -> None:
    factory, connections = _slack_factory([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    transport = SlackHttpTransport(
        "xoxb-secret-token",
        connection_factory=factory,  # type: ignore[arg-type]
        sleep=lambda _: None,
    )

    with pytest.raises(RemoteRequestError, match="method is invalid"):
        transport.call("/api/chat.postMessage", {})
    assert connections == []

    with pytest.raises(RemoteRequestError, match="HTTP 500") as captured:
        transport.call("chat.postMessage", {})
    assert len(connections) == 3
    assert "xoxb" not in str(captured.value)
