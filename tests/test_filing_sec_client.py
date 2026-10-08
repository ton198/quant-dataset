"""Offline tests for the bounded SEC HTTP client."""

from __future__ import annotations

import hashlib
import http.client
import io
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from filings.sec_client import (
    DEFAULT_MAX_RESPONSE_BYTES,
    FetchResponse,
    SecClient,
    SecClientError,
    SecForbiddenError,
    SecHTTPStatusError,
    SecIncompleteResourceError,
    SecInvalidContentLengthError,
    SecNotFoundError,
    SecRedirectError,
    SecResponseTooLargeError,
    SecRetryDeferredError,
    SecTimeoutError,
    TaxonomyClient,
    UnsafeSecURLError,
    _retry_after_seconds,
    validate_sec_url,
    validate_sec_user_agent,
)

VALID_AGENT = "Quant Research analyst@acme-financials.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"response",
        url: str | None = None,
        read_chunk_size: int | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self._body = body
        self._position = 0
        self.url = url
        self.read_chunk_size = read_chunk_size
        self.read_sizes: list[int] = []
        self.closed = False

    def geturl(self) -> str | None:
        return self.url

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.read_chunk_size is not None:
            size = min(size, self.read_chunk_size)
        if size < 0:
            size = len(self._body) - self._position
        start = self._position
        end = min(len(self._body), start + size)
        self._position = end
        return self._body[start:end]

    def close(self) -> None:
        self.closed = True


class IncompleteReadResponse(FakeResponse):
    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        raise http.client.IncompleteRead(b"abc", 2)


class MemorySocket:
    def __init__(self, payload: bytes) -> None:
        self._file = io.BytesIO(payload)

    def makefile(self, mode: str) -> io.BytesIO:
        return self._file


def make_stdlib_response(status: int, headers: bytes, body: bytes) -> http.client.HTTPResponse:
    reason = b"OK" if status == 200 else b"Partial"
    raw = b"HTTP/1.1 " + str(status).encode("ascii") + b" " + reason + b"\r\n"
    raw += headers + b"\r\n" + body
    response = http.client.HTTPResponse(MemorySocket(raw))
    response.begin()
    response.url = None
    return response


class QueueTransport:
    def __init__(self, *results: Any) -> None:
        self.results = deque(results)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> FakeResponse:
        self.calls.append((url, dict(headers), timeout))
        if not self.results:
            raise AssertionError("unexpected extra transport request")
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        if callable(result) and not isinstance(result, FakeResponse):
            result = result(url, headers, timeout)
        if result.url is None:
            result.url = url
        return result


def make_client(
    transport: QueueTransport,
    *,
    clock: FakeClock | None = None,
    interval: float = 0.2,
    timeout: float = 5.0,
    retries: int = 0,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> SecClient:
    active_clock = clock or FakeClock()
    # Keep this bound method object stable so separate clients share the same rate domain.
    monotonic = active_clock.monotonic
    sleeper = active_clock.sleep
    return SecClient(
        VALID_AGENT,
        interval=interval,
        timeout=timeout,
        retries=retries,
        max_response_bytes=max_response_bytes,
        transport=transport,
        clock=monotonic,
        sleep=sleeper,
    )


def make_taxonomy_client(
    transport: QueueTransport,
    *,
    allowed_hosts: set[str] | frozenset[str],
    clock: FakeClock | None = None,
    interval: float = 0.2,
    timeout: float = 5.0,
    retries: int = 0,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> TaxonomyClient:
    active_clock = clock or FakeClock()
    monotonic = active_clock.monotonic
    sleeper = active_clock.sleep
    return TaxonomyClient(
        VALID_AGENT,
        allowed_hosts=allowed_hosts,
        interval=interval,
        timeout=timeout,
        retries=retries,
        max_response_bytes=max_response_bytes,
        transport=transport,
        clock=monotonic,
        sleep=sleeper,
    )


def test_get_returns_bounded_response_metadata_and_hash() -> None:
    transport = QueueTransport(
        FakeResponse(
            status=200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=b'{"ok":true}',
        )
    )
    client = make_client(transport, timeout=3.5)
    url = "https://data.sec.gov/submissions/CIK0000000001.json"

    result = client.get(url)

    assert isinstance(result, FetchResponse)
    assert result.body == b'{"ok":true}'
    assert result.request_url == url
    assert result.final_url == url
    assert result.content_type == "application/json; charset=utf-8"
    assert result.fetched_at_utc.tzinfo is timezone.utc
    assert result.attempts == 1
    assert result.status == 200
    assert result.sha256 == hashlib.sha256(result.body).hexdigest()
    assert transport.calls[0][1]["User-Agent"] == VALID_AGENT
    assert transport.calls[0][1]["Accept"] == "*/*"
    assert transport.calls[0][2] == 3.5


def test_fetch_response_metadata_fields_keep_legacy_constructor_defaults() -> None:
    url = "https://www.sec.gov/resource"
    response = FetchResponse(b"body", url, url, None, datetime.now(timezone.utc), 1, 200)

    assert response.transport_url is None
    assert response.redirect_chain == ()


def test_default_sec_client_still_rejects_non_sec_hosts() -> None:
    transport = QueueTransport(FakeResponse())
    client = make_client(transport)

    with pytest.raises(UnsafeSecURLError):
        client.get("https://taxonomy.test/taxonomy.xsd")

    assert transport.calls == []


def test_sec_client_custom_allowlist_is_https_only() -> None:
    clock = FakeClock()
    transport = QueueTransport(FakeResponse(body=b"<schema/>"))
    client = SecClient(
        VALID_AGENT,
        allowed_hosts={"TAXONOMY.TEST."},
        transport=transport,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    result = client.get("https://taxonomy.test/schema.xsd")

    assert result.body == b"<schema/>"
    assert transport.calls[0][0] == "https://taxonomy.test/schema.xsd"
    with pytest.raises(UnsafeSecURLError):
        client.get("http://taxonomy.test/schema.xsd")
    assert len(transport.calls) == 1


def test_taxonomy_client_upgrades_http_origin_without_http_transport() -> None:
    original_url = "http://taxonomy.test:80/2003/xbrl-instance.xsd"
    transport_url = "https://taxonomy.test/2003/xbrl-instance.xsd"
    transport = QueueTransport(FakeResponse(body=b"<schema/>"))
    client = make_taxonomy_client(
        transport,
        allowed_hosts={"TAXONOMY.TEST."},
    )

    result = client.get(original_url)

    assert [call[0] for call in transport.calls] == [transport_url]
    assert all(call[0].startswith("https://") for call in transport.calls)
    assert result.request_url == original_url
    assert result.transport_url == transport_url
    assert result.final_url == transport_url
    assert result.redirect_chain == ()
    assert result.body == b"<schema/>"


def test_taxonomy_fetch_method_is_available_for_https_uris() -> None:
    url = "https://taxonomy.test/taxonomy.xsd"
    transport = QueueTransport(FakeResponse(body=b"<schema/>"))
    client = make_taxonomy_client(transport, allowed_hosts={"taxonomy.test"})

    result = client.fetch_taxonomy(url)

    assert result.request_url == url
    assert result.transport_url == url
    assert result.final_url == url


def test_taxonomy_redirect_chain_is_validated_and_recorded() -> None:
    transport = QueueTransport(
        FakeResponse(302, headers={"Location": "/schemas/core.xsd"}),
        FakeResponse(200, body=b"<schema/>"),
    )
    client = make_taxonomy_client(transport, allowed_hosts={"taxonomy.test"})

    result = client.get("https://taxonomy.test/start")

    assert [call[0] for call in transport.calls] == [
        "https://taxonomy.test/start",
        "https://taxonomy.test/schemas/core.xsd",
    ]
    assert result.transport_url == "https://taxonomy.test/start"
    assert result.final_url == "https://taxonomy.test/schemas/core.xsd"
    assert result.redirect_chain == ("https://taxonomy.test/schemas/core.xsd",)


@pytest.mark.parametrize(
    "location",
    [
        "https://outside.test/blocked.xsd",
        "http://taxonomy.test/downgrade.xsd",
        "https://taxonomy.test/%252e%252e/blocked.xsd",
    ],
)
def test_taxonomy_redirects_are_prevalidated_before_following(location: str) -> None:
    redirect = FakeResponse(302, headers={"Location": location})
    transport = QueueTransport(redirect)
    client = make_taxonomy_client(transport, allowed_hosts={"taxonomy.test"})

    with pytest.raises(UnsafeSecURLError):
        client.get("https://taxonomy.test/start")

    assert len(transport.calls) == 1
    assert redirect.read_sizes == []
    assert redirect.closed


@pytest.mark.parametrize(
    "url",
    [
        "http://outside.test/taxonomy.xsd",
        "https://outside.test/taxonomy.xsd",
        "http://taxonomy.test:8080/taxonomy.xsd",
        "https://taxonomy.test:8443/taxonomy.xsd",
        "http://analyst@taxonomy.test/taxonomy.xsd",
        "http://taxonomy.test/taxonomy.xsd?token=private",
        "http://taxonomy.test/taxonomy.xsd#fragment",
        "http://taxonomy.test/%2e%2e/taxonomy.xsd",
        "ftp://taxonomy.test/taxonomy.xsd",
        "file:///etc/passwd",
        "https://127.0.0.1/taxonomy.xsd",
        "https://localhost/taxonomy.xsd",
    ],
)
def test_taxonomy_client_rejects_unsafe_original_urls_before_transport(url: str) -> None:
    transport = QueueTransport(FakeResponse())
    client = make_taxonomy_client(transport, allowed_hosts={"taxonomy.test"})

    with pytest.raises(UnsafeSecURLError):
        client.get(url)

    assert transport.calls == []


@pytest.mark.parametrize(
    "allowed_host",
    ["*.xbrl.org", "127.0.0.1", "localhost", "service.localhost", "service.local"],
)
def test_taxonomy_client_requires_exact_nonlocal_dns_allowlist(allowed_host: str) -> None:
    with pytest.raises(ValueError):
        TaxonomyClient(VALID_AGENT, allowed_hosts={allowed_host}, transport=QueueTransport())


def test_sec_and_taxonomy_clients_share_the_same_request_pacer() -> None:
    clock = FakeClock()
    sec_transport = QueueTransport(FakeResponse(body=b"sec"))
    taxonomy_transport = QueueTransport(FakeResponse(body=b"taxonomy"))
    sec_client = make_client(sec_transport, clock=clock)
    taxonomy_client = make_taxonomy_client(
        taxonomy_transport,
        allowed_hosts={"taxonomy.test"},
        clock=clock,
    )

    sec_client.get("https://www.sec.gov/resource")
    taxonomy_client.get("https://taxonomy.test/schema.xsd")

    assert len(sec_transport.calls) == 1
    assert len(taxonomy_transport.calls) == 1
    assert clock.sleeps == [0.2]


def test_taxonomy_client_keeps_response_byte_cap() -> None:
    transport = QueueTransport(FakeResponse(body=b"12345"))
    client = make_taxonomy_client(
        transport,
        allowed_hosts={"taxonomy.test"},
        max_response_bytes=4,
    )

    with pytest.raises(SecResponseTooLargeError):
        client.get("https://taxonomy.test/schema.xsd")


def test_complete_get_requires_http_200() -> None:
    for status in (204, 206, 201):
        response = FakeResponse(status, headers={"Content-Length": "0"}, body=b"")
        client = make_client(QueueTransport(response))

        with pytest.raises(SecIncompleteResourceError) as exc_info:
            client.get("https://www.sec.gov/resource")

        assert exc_info.value.code == "incomplete_resource"
        assert exc_info.value.status == status
        assert response.read_sizes == []


def test_stdlib_response_with_short_declared_length_is_incomplete() -> None:
    response = make_stdlib_response(200, b"Content-Length: 100\r\n", b"abc")
    client = make_client(QueueTransport(response))

    with pytest.raises(SecIncompleteResourceError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.code == "incomplete_resource"
    assert exc_info.value.expected_bytes == 100
    assert exc_info.value.observed_bytes == 3


def test_incomplete_read_exception_is_classified_as_incomplete_resource() -> None:
    response = IncompleteReadResponse(headers={"Content-Length": "5"})
    client = make_client(QueueTransport(response))

    with pytest.raises(SecIncompleteResourceError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.code == "incomplete_resource"
    assert exc_info.value.expected_bytes == 5
    assert exc_info.value.observed_bytes == 3


def test_stdlib_response_with_exact_declared_length_succeeds() -> None:
    response = make_stdlib_response(200, b"Content-Length: 3\r\n", b"abc")
    client = make_client(QueueTransport(response))

    result = client.get("https://www.sec.gov/resource")

    assert result.body == b"abc"
    assert result.status == 200


@pytest.mark.parametrize("content_length", ["-1", "+3", "three", "3, 3"])
def test_invalid_content_length_is_rejected(content_length: str) -> None:
    response = FakeResponse(
        headers={"Content-Length": content_length},
        body=b"abc",
    )
    client = make_client(QueueTransport(response))

    with pytest.raises(SecInvalidContentLengthError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.code == "invalid_content_length"
    assert response.read_sizes == []


def test_user_agent_requires_a_non_placeholder_email_without_echoing_value() -> None:
    assert validate_sec_user_agent(f"  {VALID_AGENT}  ") == VALID_AGENT
    for invalid in (
        "",
        "No contact email",
        "Jane yourname@example.com",
        "Test User test@acme.org",
        "Contact contact@acme.org",
        "Jane placeholder@example.com",
        "Analyst analyst@example.com",
    ):
        with pytest.raises(ValueError) as exc_info:
            validate_sec_user_agent(invalid)
        if "@" in invalid:
            assert invalid not in str(exc_info.value)


def test_constructor_enforces_rate_retry_and_body_bounds() -> None:
    transport = QueueTransport(FakeResponse())
    with pytest.raises(ValueError, match="at least 0.2"):
        make_client(transport, interval=0.19)
    with pytest.raises(ValueError, match="retries"):
        SecClient(VALID_AGENT, retries=11, transport=transport)
    with pytest.raises(ValueError, match="max_response_bytes"):
        SecClient(VALID_AGENT, max_response_bytes=0, transport=transport)
    with pytest.raises(ValueError, match="timeout"):
        SecClient(VALID_AGENT, timeout=0, transport=transport)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/Archives/edgar/data/1/file.htm",
        "file:///etc/passwd",
        "https://sec.gov/Archives/edgar/data/1/file.htm",
        "https://www.sec.gov.evil.example/Archives/edgar/data/1/file.htm",
        "https://evil.example@www.sec.gov/Archives/edgar/data/1/file.htm",
        "https://www.sec.gov:8443/Archives/edgar/data/1/file.htm",
        "https://www.sec.gov/Archives/edgar/data/1/../secret.htm",
        "https://www.sec.gov/Archives/%2e%2e/secret.htm",
        "https://www.sec.gov/Archives/%252e%252e/secret.htm",
        "https://www.sec.gov/Archives/%2f..%2fsecret.htm",
        "https://www.sec.gov/Archives/%5c..%5csecret.htm",
        "https://www.sec.gov/Archives/edgar/data/1/file.htm?token=private",
        "https://www.sec.gov/Archives/edgar/data/1/file.htm#fragment",
        "https://www.sec.gov/Archives/%zz/file.htm",
    ],
)
def test_url_validation_rejects_non_sec_unsafe_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(UnsafeSecURLError):
        validate_sec_url(url)


def test_url_validation_allows_sec_hosts_and_encoded_unicode_path() -> None:
    url = "https://www.sec.gov/Archives/edgar/data/1/caf%C3%A9.htm"
    assert validate_sec_url(url) == url
    assert validate_sec_url("https://data.sec.gov/submissions/CIK0000000001.json").startswith(
        "https://data.sec.gov/"
    )


def test_unsafe_redirect_is_rejected_before_following_target() -> None:
    redirect = FakeResponse(
        302,
        headers={"Location": "https://evil.example/steal"},
        body=b"must not be read",
    )
    transport = QueueTransport(redirect)
    client = make_client(transport)

    with pytest.raises(UnsafeSecURLError):
        client.get("https://www.sec.gov/start")

    assert len(transport.calls) == 1
    assert redirect.closed
    assert redirect.read_sizes == []


def test_valid_relative_redirect_is_followed_and_counted_as_an_attempt() -> None:
    clock = FakeClock()
    first = FakeResponse(302, headers={"Location": "/Archives/edgar/data/1/filing.htm"})
    second = FakeResponse(200, body=b"filing")
    transport = QueueTransport(first, second)
    client = make_client(transport, clock=clock, retries=0)

    result = client.get("https://www.sec.gov/start")

    assert result.body == b"filing"
    assert result.request_url == "https://www.sec.gov/start"
    assert result.final_url == "https://www.sec.gov/Archives/edgar/data/1/filing.htm"
    assert result.attempts == 2
    assert [call[0] for call in transport.calls] == [
        "https://www.sec.gov/start",
        "https://www.sec.gov/Archives/edgar/data/1/filing.htm",
    ]
    assert clock.monotonic() >= 0.2
    assert first.closed and second.closed


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, SecForbiddenError), (404, SecNotFoundError)],
)
def test_forbidden_and_missing_are_terminal_without_retry(
    status: int, error_type: type[Exception]
) -> None:
    transport = QueueTransport(FakeResponse(status, body=b"sensitive response body"))
    client = make_client(transport, retries=3)

    with pytest.raises(error_type) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert len(transport.calls) == 1
    assert exc_info.value.status == status
    assert "sensitive response body" not in str(exc_info.value)


def test_retry_after_and_server_errors_retry_with_bounded_attempts() -> None:
    clock = FakeClock()
    transport = QueueTransport(
        FakeResponse(429, headers={"Retry-After": "1.25"}),
        FakeResponse(500),
        FakeResponse(200, body=b"done"),
    )
    client = make_client(transport, clock=clock, retries=2)

    result = client.get("https://www.sec.gov/resource")

    assert result.body == b"done"
    assert result.attempts == 3
    assert len(transport.calls) == 3
    assert clock.sleeps == [1.25, 1.0]
    assert clock.monotonic() >= 2.25


def test_retry_after_over_budget_defers_and_cannot_be_bypassed_by_another_client() -> None:
    clock = FakeClock()
    transport = QueueTransport(FakeResponse(429, headers={"Retry-After": "9999"}), FakeResponse())
    client = make_client(transport, clock=clock, retries=1)

    with pytest.raises(SecRetryDeferredError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.code == "retry_after_exceeds_budget"
    assert exc_info.value.status == 429
    assert exc_info.value.retry_after_seconds == 9999.0
    assert len(transport.calls) == 1
    assert clock.sleeps == []

    other_transport = QueueTransport(FakeResponse())
    other_client = make_client(other_transport, clock=clock, retries=1)
    with pytest.raises(SecRetryDeferredError) as other_exc:
        other_client.get("https://www.sec.gov/other-resource")

    assert other_exc.value.code == "retry_after_exceeds_budget"
    assert other_exc.value.status == 429
    assert other_transport.calls == []
    assert clock.sleeps == []


def test_short_server_cooldown_is_shared_and_waited_out_once() -> None:
    clock = FakeClock()
    first_transport = QueueTransport(FakeResponse(503, headers={"Retry-After": "1"}))
    first_client = make_client(first_transport, clock=clock, retries=0)

    with pytest.raises(SecHTTPStatusError):
        first_client.get("https://www.sec.gov/resource")

    second_transport = QueueTransport(FakeResponse(200, body=b"ready"))
    second_client = make_client(second_transport, clock=clock, retries=0)
    result = second_client.get("https://www.sec.gov/other-resource")

    assert result.body == b"ready"
    assert len(first_transport.calls) == 1
    assert len(second_transport.calls) == 1
    assert clock.sleeps == [1.0]


def test_retry_after_negative_and_http_date_values_use_utc() -> None:
    assert _retry_after_seconds("-5") == 0.0

    target = datetime.now(timezone.utc) + timedelta(seconds=15)
    delay = _retry_after_seconds(format_datetime(target, usegmt=True))

    assert delay is not None
    assert 10.0 < delay <= 15.0


def test_retry_exhaustion_reports_status_without_response_body() -> None:
    transport = QueueTransport(FakeResponse(503, body=b"private body"), FakeResponse(503))
    client = make_client(transport, retries=1)

    with pytest.raises(SecHTTPStatusError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.status == 503
    assert exc_info.value.attempts == 2
    assert len(transport.calls) == 2
    assert "private body" not in str(exc_info.value)


def test_timeout_is_retried_and_attempt_count_includes_failed_request() -> None:
    clock = FakeClock()
    transport = QueueTransport(TimeoutError("do not echo"), FakeResponse(200, body=b"ok"))
    client = make_client(transport, clock=clock, retries=1)

    result = client.get("https://www.sec.gov/resource")

    assert result.status == 200
    assert result.attempts == 2
    assert len(transport.calls) == 2
    assert clock.sleeps == [0.5]


def test_timeout_exhaustion_has_actionable_terminal_error() -> None:
    transport = QueueTransport(TimeoutError("sensitive transport detail"))
    client = make_client(transport, retries=0)

    with pytest.raises(SecTimeoutError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.attempts == 1
    assert "sensitive transport detail" not in str(exc_info.value)


def test_body_stream_is_capped_even_without_content_length() -> None:
    response = FakeResponse(200, body=b"12345", read_chunk_size=2)
    client = make_client(QueueTransport(response), max_response_bytes=4)

    with pytest.raises(SecResponseTooLargeError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert exc_info.value.limit_bytes == 4
    assert exc_info.value.observed_bytes == 5
    assert response.closed


def test_declared_oversize_is_rejected_before_body_read() -> None:
    response = FakeResponse(
        200,
        headers={"Content-Length": "5", "Content-Type": "application/octet-stream"},
        body=b"12345",
    )
    client = make_client(QueueTransport(response), max_response_bytes=4)

    with pytest.raises(SecResponseTooLargeError):
        client.get("https://www.sec.gov/resource")

    assert response.read_sizes == []
    assert response.closed


def test_transport_cannot_return_an_automatically_followed_redirect() -> None:
    followed = FakeResponse(
        200,
        body=b"outside",
        url="https://evil.example/final",
    )
    client = make_client(QueueTransport(followed))

    with pytest.raises(SecRedirectError):
        client.get("https://www.sec.gov/start")

    assert followed.read_sizes == []
    assert followed.closed


def test_clients_share_global_request_spacing_across_threads_retries_and_redirects() -> None:
    clock = FakeClock()
    starts: list[float] = []
    starts_lock = threading.Lock()

    redirect_transport = QueueTransport(
        FakeResponse(302, headers={"Location": "/target"}),
        FakeResponse(200, body=b"redirected"),
    )
    retry_transport = QueueTransport(
        FakeResponse(429, headers={"Retry-After": "0"}),
        FakeResponse(200, body=b"retried"),
    )

    # Replace callables after queue setup so both clients record transport start times.
    def instrument(queue: QueueTransport):
        def transport(url: str, headers: Any, timeout: float) -> FakeResponse:
            with starts_lock:
                starts.append(clock.monotonic())
            return queue(url, headers, timeout)

        return transport

    client_a = SecClient(
        VALID_AGENT,
        interval=0.2,
        retries=0,
        transport=instrument(redirect_transport),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    client_b = SecClient(
        VALID_AGENT,
        interval=0.2,
        retries=1,
        transport=instrument(retry_transport),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    results: list[FetchResponse] = []
    errors: list[BaseException] = []

    def fetch(client: SecClient) -> None:
        try:
            results.append(client.get("https://www.sec.gov/start"))
        except BaseException as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)

    threads = [threading.Thread(target=fetch, args=(client,)) for client in (client_a, client_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result.body for result in results) == [b"redirected", b"retried"]
    assert len(starts) == 4
    ordered = sorted(starts)
    assert all(
        later - earlier >= 0.2 - 1e-9 for earlier, later in zip(ordered, ordered[1:], strict=False)
    )


def test_user_agent_is_not_exposed_by_client_repr_or_error_messages() -> None:
    client = make_client(QueueTransport(FakeResponse()))
    assert VALID_AGENT not in repr(client)
    assert "redacted" in repr(client)
    with pytest.raises(UnsafeSecURLError) as exc_info:
        client.get("https://www.sec.gov/file?private=value")
    assert VALID_AGENT not in str(exc_info.value)


def test_non_timeout_transport_errors_are_not_retried_or_echoed() -> None:
    transport = QueueTransport(OSError("sensitive network detail"))
    client = make_client(transport, retries=4)

    with pytest.raises(SecClientError) as exc_info:
        client.get("https://www.sec.gov/resource")

    assert len(transport.calls) == 1
    assert "sensitive network detail" not in str(exc_info.value)
