"""Injectable stdlib GitHub transport with bounded retries and sanitized failures."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

_MAX_ATTEMPTS = 3
_CONNECT_TIMEOUT_SECONDS = 5.0
_RESPONSE_TIMEOUT_SECONDS = 30.0
_MAX_RETRY_AFTER_SECONDS = 2.0
_MAX_RESPONSE_BYTES = 5_000_000
_ALLOWED_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})


class RemoteRequestError(RuntimeError):
    """Raised without request URLs or remote response bodies."""


class HttpResponse(Protocol):
    status: int

    def getheader(self, name: str) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...


class HttpsConnection(Protocol):
    sock: Any

    def connect(self) -> None: ...

    def request(self, method: str, url: str, *, headers: dict[str, str]) -> None: ...

    def getresponse(self) -> HttpResponse: ...

    def close(self) -> None: ...


ConnectionFactory = Callable[[str, int | None, float], HttpsConnection]


def _connection_factory(host: str, port: int | None, timeout: float) -> HttpsConnection:
    return http.client.HTTPSConnection(host, port=port, timeout=timeout)


class GitHubHttpTransport:
    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory = _connection_factory,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._connection_factory = connection_factory
        self._sleep = sleep

    @staticmethod
    def _retry_delay(response: HttpResponse | None, attempt: int) -> float:
        raw_retry_after = response.getheader("Retry-After") if response is not None else None
        try:
            retry_after = float(raw_retry_after) if raw_retry_after is not None else None
        except ValueError:
            retry_after = None
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, _MAX_RETRY_AFTER_SECONDS)
        return min(0.25 * (2 ** (attempt - 1)), _MAX_RETRY_AFTER_SECONDS)

    def _get(self, url: str, *, accept: str) -> bytes:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise RemoteRequestError("GitHub request URL is invalid") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_HOSTS
            or port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise RemoteRequestError("GitHub request URL is invalid")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            connection = self._connection_factory(
                parsed.hostname,
                port,
                _CONNECT_TIMEOUT_SECONDS,
            )
            response: HttpResponse | None = None
            try:
                connection.connect()
                if connection.sock is None:
                    raise RemoteRequestError("GitHub connection did not open")
                connection.sock.settimeout(_RESPONSE_TIMEOUT_SECONDS)
                connection.request(
                    "GET",
                    path,
                    headers={"Accept": accept, "User-Agent": "tech-knowledge-agent"},
                )
                response = connection.getresponse()
                retryable = response.status == 429 or 500 <= response.status < 600
                if retryable:
                    if attempt == _MAX_ATTEMPTS:
                        raise RemoteRequestError(
                            f"GitHub request failed with HTTP {response.status}"
                        )
                elif not 200 <= response.status < 300:
                    raise RemoteRequestError(
                        f"GitHub request failed with HTTP {response.status}"
                    )
                else:
                    content = response.read(_MAX_RESPONSE_BYTES + 1)
                    if len(content) > _MAX_RESPONSE_BYTES:
                        raise RemoteRequestError("GitHub response exceeded the size limit")
                    return content
            except TimeoutError:
                if attempt == _MAX_ATTEMPTS:
                    raise RemoteRequestError("GitHub request timed out") from None
            except OSError:
                raise RemoteRequestError("GitHub request failed") from None
            except http.client.HTTPException:
                raise RemoteRequestError("GitHub response failed") from None
            finally:
                connection.close()
            self._sleep(self._retry_delay(response, attempt))
        raise AssertionError("retry loop exhausted")

    def get_json(self, url: str) -> Any:
        content = self._get(url, accept="application/vnd.github+json")
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RemoteRequestError("GitHub response was not valid JSON") from None

    def get_text(self, url: str) -> str:
        content = self._get(url, accept="text/plain")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            raise RemoteRequestError("GitHub response was not UTF-8") from None
