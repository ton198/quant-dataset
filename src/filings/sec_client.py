"""Bounded, rate-limited HTTP access to SEC-hosted resources.

This module deliberately does not persist responses. Callers own archive layout and
manifest semantics; this layer validates destinations, performs one bounded GET, and
returns the response bytes with request metadata.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import ipaddress
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

DEFAULT_MAX_RESPONSE_BYTES = 128 * 1024 * 1024
_MAX_CONFIGURED_RESPONSE_BYTES = 1024 * 1024 * 1024
_MAX_RETRIES = 10
_MAX_REDIRECTS = 5
_MAX_RETRY_AFTER_SECONDS = 60.0
_RETRY_BACKOFF_BASE_SECONDS = 0.5
_RETRY_BACKOFF_MAX_SECONDS = 8.0
_READ_CHUNK_BYTES = 64 * 1024
_ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_EMAIL_RE = re.compile(
    r"(?i)^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$"
)
_BAD_CONTACT_PARTS = ("placeholder", "yourname", "test", "contact")
_BAD_CONTACT_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class SecClientError(RuntimeError):
    """Base error for SEC client failures; messages never include the User-Agent."""

    code = "sec_client_error"


class SecIncompleteResourceError(SecClientError):
    """A successful-looking response did not contain a complete representation."""

    code = "incomplete_resource"

    def __init__(
        self,
        message: str = "SEC response did not contain a complete resource",
        *,
        status: int | None = None,
        expected_bytes: int | None = None,
        observed_bytes: int | None = None,
    ) -> None:
        self.status = status
        self.expected_bytes = expected_bytes
        self.observed_bytes = observed_bytes
        super().__init__(message)


class SecInvalidContentLengthError(SecClientError):
    """The response Content-Length header was not a valid nonnegative integer."""

    code = "invalid_content_length"

    def __init__(self) -> None:
        super().__init__("SEC response has an invalid Content-Length")


class UnsafeSecURLError(SecClientError, ValueError):
    """A URL is outside the SEC HTTPS allowlist or contains unsafe components."""


class SecTransportError(SecClientError):
    """The injected or standard-library transport failed for a non-timeout reason."""


class SecTimeoutError(SecClientError, TimeoutError):
    """A SEC request timed out after the configured retry budget."""

    def __init__(self, attempts: int) -> None:
        self.attempts = attempts
        super().__init__(f"SEC request timed out after {attempts} request attempt(s)")


class SecResponseTooLargeError(SecClientError):
    """A response exceeded the configured byte cap."""

    def __init__(self, limit_bytes: int, observed_bytes: int | None = None) -> None:
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
        detail = (
            "declared size exceeds limit" if observed_bytes is None else "stream exceeded limit"
        )
        super().__init__(f"SEC response {detail} of {limit_bytes} bytes")


class SecHTTPStatusError(SecClientError):
    """A terminal HTTP status, without retaining or displaying the response body."""

    code = "http_status_error"

    def __init__(self, status: int, attempts: int, message: str) -> None:
        self.status = status
        self.attempts = attempts
        super().__init__(message)


class SecRetryDeferredError(SecHTTPStatusError):
    """A server cooldown cannot be waited out within this call's wait budget."""

    code = "retry_after_exceeds_budget"

    def __init__(self, status: int, attempts: int, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            status,
            attempts,
            "SEC retry deferred because Retry-After exceeds the permitted wait budget",
        )


class SecForbiddenError(SecHTTPStatusError):
    """SEC rejected the request; check contact configuration and pacing."""

    def __init__(self, attempts: int) -> None:
        super().__init__(
            403,
            attempts,
            "SEC returned HTTP 403; verify the configured contact and request pacing",
        )


class SecNotFoundError(SecHTTPStatusError):
    """The requested SEC resource does not exist."""

    def __init__(self, attempts: int) -> None:
        super().__init__(404, attempts, "SEC resource was not found (HTTP 404)")


class SecRedirectError(SecClientError):
    """A redirect was missing, excessive, or handled outside this client's guard."""


class _ResponseLike(Protocol):
    status: int
    headers: object

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


Transport = Callable[[str, Mapping[str, str], float], _ResponseLike]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """An in-memory HTTP response returned by :meth:`SecClient.get`."""

    body: bytes
    request_url: str
    final_url: str
    content_type: str | None
    fetched_at_utc: datetime
    attempts: int
    status: int
    transport_url: str | None = None
    redirect_chain: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        """Return the SHA-256 digest of ``body`` without storing another copy."""
        return hashlib.sha256(self.body).hexdigest()


def validate_sec_user_agent(user_agent: str) -> str:
    """Validate a non-placeholder contact email without echoing identity on failure."""
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ValueError("SEC User-Agent must be a non-empty string with a contact email")

    for token in re.findall(r"[^\s<>(),;]+@[^\s<>(),;]+", user_agent):
        candidate = token.strip(".\"'")
        if not _EMAIL_RE.fullmatch(candidate):
            continue
        local, domain = candidate.rsplit("@", 1)
        folded = candidate.casefold()
        if domain.casefold() in _BAD_CONTACT_DOMAINS:
            continue
        if any(part in folded for part in _BAD_CONTACT_PARTS):
            continue
        if not local or not domain:
            continue
        return user_agent.strip()

    raise ValueError("SEC User-Agent must include a non-placeholder contact email")


def validate_sec_url(url: str) -> str:
    """Validate and return a safe absolute HTTPS URL on an allowed SEC host.

    Query strings and fragments are intentionally rejected: SEC filing and API
    resources used by this client are path-addressed, and this keeps secrets from
    accidentally being embedded in URLs.
    """
    if (
        not isinstance(url, str)
        or not url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        raise UnsafeSecURLError("SEC URL must be a non-empty absolute HTTPS URL")
    if "?" in url or "#" in url:
        raise UnsafeSecURLError("SEC URLs must not contain a query or fragment")
    if re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise UnsafeSecURLError("SEC URL contains malformed percent encoding")

    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise UnsafeSecURLError("SEC URL authority is invalid") from None

    if parsed.scheme.casefold() != "https":
        raise UnsafeSecURLError("SEC URLs must use HTTPS")
    if not parsed.netloc or not hostname or hostname.casefold() not in _ALLOWED_HOSTS:
        raise UnsafeSecURLError("SEC URL host is not allowlisted")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeSecURLError("SEC URL credentials are not allowed")
    if port not in (None, 443):
        raise UnsafeSecURLError("SEC URL must use the standard HTTPS port")
    if parsed.path and not parsed.path.startswith("/"):
        raise UnsafeSecURLError("SEC URL path must be absolute")

    decoded_path = parsed.path
    for _ in range(5):
        next_path = urllib.parse.unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        raise UnsafeSecURLError("SEC URL path has excessive nested encoding")

    if "\\" in decoded_path or "\x00" in decoded_path:
        raise UnsafeSecURLError("SEC URL path contains an unsafe separator")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded_path):
        raise UnsafeSecURLError("SEC URL path contains control characters")
    if decoded_path.startswith("//") or any(
        part in {".", ".."} for part in decoded_path.split("/")
    ):
        raise UnsafeSecURLError("SEC URL path traversal is not allowed")

    return url


def _canonical_allowed_hostname(hostname: str, *, allow_trailing_dot: bool = False) -> str:
    """Normalize one explicit DNS hostname; reject local and address-like names."""
    if not isinstance(hostname, str):
        raise ValueError("allowed hosts must be DNS hostnames")
    value = hostname.strip().lower()
    if allow_trailing_dot and value.endswith("."):
        value = value[:-1]
    elif value.endswith("."):
        raise ValueError("trailing-dot hosts are not allowed")
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("allowed hosts must be valid IDNA hostnames") from None
    labels = value.split(".")
    if (
        not value
        or len(value) > 253
        or len(labels) < 2
        or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels)
        or value == "localhost"
        or value.endswith((".localhost", ".local", ".localdomain"))
        or all(label.isdigit() for label in labels)
    ):
        raise ValueError("allowed hosts must be public-style exact DNS hostnames")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP address hosts are not allowed")
    return value


def _normalize_allowed_hosts(allowed_hosts: Collection[str]) -> frozenset[str]:
    if isinstance(allowed_hosts, (str, bytes)):
        raise ValueError("allowed_hosts must be a collection of exact hostnames")
    try:
        hosts = frozenset(
            _canonical_allowed_hostname(host, allow_trailing_dot=True) for host in allowed_hosts
        )
    except TypeError:
        raise ValueError("allowed_hosts must be a collection of exact hostnames") from None
    return hosts


def _validate_resource_url_for_scheme(
    url: str,
    allowed_hosts: frozenset[str],
    scheme: str,
) -> str:
    """Validate one exact-host URL with standard ports and traversal protection."""
    if (
        not isinstance(url, str)
        or not url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        raise UnsafeSecURLError("SEC URL must be a non-empty absolute resource URL")
    if "?" in url or "#" in url:
        raise UnsafeSecURLError("SEC resource URLs must not contain a query or fragment")
    if re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise UnsafeSecURLError("SEC URL contains malformed percent encoding")
    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise UnsafeSecURLError("resource URL authority is invalid") from None

    if parsed.scheme.casefold() != scheme or not parsed.netloc or not hostname:
        raise UnsafeSecURLError("resource URL scheme or host is not allowed")
    if hostname != hostname.strip() or parsed.netloc.endswith(":"):
        raise UnsafeSecURLError("resource URL authority is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeSecURLError("SEC URL credentials are not allowed")
    try:
        normalized_host = _canonical_allowed_hostname(hostname)
    except ValueError:
        raise UnsafeSecURLError("SEC URL host is invalid") from None
    if normalized_host not in allowed_hosts:
        raise UnsafeSecURLError("SEC URL host is not allowlisted")

    expected_port = 443 if scheme == "https" else 80
    if port not in (None, expected_port):
        raise UnsafeSecURLError("SEC URL must use the standard port")

    decoded_path = parsed.path
    for _ in range(5):
        next_path = urllib.parse.unquote(decoded_path)
        if next_path == decoded_path:
            break
        decoded_path = next_path
    else:
        raise UnsafeSecURLError("SEC URL path has excessive nested encoding")
    if "\\" in decoded_path or "\x00" in decoded_path:
        raise UnsafeSecURLError("SEC URL path contains an unsafe separator")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded_path):
        raise UnsafeSecURLError("SEC URL path contains control characters")
    if decoded_path.startswith("//") or any(
        part in {".", ".."} for part in decoded_path.split("/")
    ):
        raise UnsafeSecURLError("SEC URL path traversal is not allowed")

    return urllib.parse.urlunsplit((scheme, normalized_host, parsed.path or "/", "", ""))


def _validate_resource_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Validate an HTTPS resource URL on an explicitly configured host."""
    return _validate_resource_url_for_scheme(url, allowed_hosts, "https")


def _upgrade_http_resource_alias(
    url: str,
    allowed_hosts: frozenset[str],
) -> str:
    """Upgrade an approved HTTP-origin URI to an HTTPS transport URL."""
    validated_http = _validate_resource_url_for_scheme(url, allowed_hosts, "http")
    parsed = urllib.parse.urlsplit(validated_http)
    https_url = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
    return _validate_resource_url(https_url, allowed_hosts)


@dataclass(slots=True)
class _RateState:
    clock_domain: object
    last_started: float | None = None
    last_interval: float = 0.0
    cooldown_until: float | None = None
    cooldown_status: int | None = None


_RATE_LOCK = threading.Lock()
_RATE_STATES: dict[int, _RateState] = {}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Leave redirects to SecClient so targets are checked before a follow-up GET."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _stdlib_transport(url: str, headers: Mapping[str, str], timeout: float) -> _ResponseLike:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        return opener.open(request, timeout=timeout)  # type: ignore[return-value]
    except urllib.error.HTTPError as response:
        # With _NoRedirectHandler, HTTPError is also the response object for 3xx/4xx/5xx.
        return response  # type: ignore[return-value]


def _header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is not None:
            return str(value).strip()
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in items():
            if str(key).casefold() == name.casefold():
                return str(value).strip()
    return None


def _response_status(response: _ResponseLike) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    if isinstance(status, bool) or not isinstance(status, int):
        raise SecTransportError("SEC transport returned a response without an HTTP status")
    return status


def _response_url(response: _ResponseLike) -> str | None:
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        value = geturl()
        return value if isinstance(value, str) else None
    value = getattr(response, "url", None)
    return value if isinstance(value, str) else None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(reason, (TimeoutError, socket.timeout)) or (
            isinstance(reason, OSError) and reason.errno == errno.ETIMEDOUT
        )
    return isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After without shortening a server's requested wait."""
    if not value:
        return None
    stripped = value.strip()
    if re.fullmatch(r"[+-]?[0-9]+", stripped):
        try:
            integer_delay = int(stripped)
            if integer_delay <= 0:
                return 0.0
            return float(integer_delay)
        except (ValueError, OverflowError):
            # A valid but unrepresentably large delay must defer, not fall back
            # to a short local retry.
            return math.inf
    try:
        delay = float(stripped)
        if math.isfinite(delay):
            return max(delay, 0.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(stripped)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        else:
            retry_at = retry_at.astimezone(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        delay = (retry_at - now_utc).total_seconds()
        return max(delay, 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def _backoff_seconds(retries_used: int) -> float:
    return min(
        _RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, retries_used - 1)), _RETRY_BACKOFF_MAX_SECONDS
    )


def _safe_close(response: _ResponseLike | None) -> None:
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


class SecClient:
    """Small stdlib SEC GET client with a shared process-level start-rate limiter.

    ``transport`` is an offline-test seam with signature
    ``transport(url, headers, timeout) -> response_like``. The returned object must
    provide ``status`` (or ``getcode()``), ``headers``, ``read(size)``, and ``close()``;
    optional ``geturl()``/``url`` metadata is checked against automatic redirects.
    Each transport invocation, including redirects and retries, passes through the
    same process-shared limiter for clients that share a monotonic clock.
    """

    def __init__(
        self,
        user_agent: str,
        interval: float = 0.2,
        timeout: float = 30.0,
        retries: int = 3,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        *,
        transport: Transport | None = None,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        allowed_hosts: Collection[str] | None = None,
    ) -> None:
        self.user_agent = validate_sec_user_agent(user_agent)
        self._uses_default_sec_url_policy = allowed_hosts is None
        self._allowed_hosts = (
            _ALLOWED_HOSTS if allowed_hosts is None else _normalize_allowed_hosts(allowed_hosts)
        )
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval < 0.2
        ):
            raise ValueError("SEC request interval must be a finite number of at least 0.2 seconds")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("SEC timeout must be a finite positive number")
        if (
            isinstance(retries, bool)
            or not isinstance(retries, int)
            or not 0 <= retries <= _MAX_RETRIES
        ):
            raise ValueError(f"SEC retries must be an integer from 0 to {_MAX_RETRIES}")
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= _MAX_CONFIGURED_RESPONSE_BYTES
        ):
            raise ValueError(
                f"max_response_bytes must be an integer from 1 to {_MAX_CONFIGURED_RESPONSE_BYTES}"
            )

        self.interval = float(interval)
        self.timeout = float(timeout)
        self.retries = retries
        self.max_response_bytes = max_response_bytes
        self._transport = transport or _stdlib_transport
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    def __repr__(self) -> str:
        return (
            "SecClient(user_agent=<redacted>, "
            f"interval={self.interval!r}, timeout={self.timeout!r}, retries={self.retries!r}, "
            f"max_response_bytes={self.max_response_bytes!r})"
        )

    def _validate_initial_url(self, url: str) -> str:
        if self._uses_default_sec_url_policy:
            return validate_sec_url(url)
        return _validate_resource_url(url, self._allowed_hosts)

    def _validate_redirect_url(self, url: str) -> str:
        if self._uses_default_sec_url_policy:
            return validate_sec_url(url)
        return _validate_resource_url(url, self._allowed_hosts)

    def _rate_state_locked(self) -> _RateState:
        # Bound-method clocks use their owner as the time domain, so separately
        # retrieved ``fake_clock.monotonic`` methods still share limiter state.
        # Retaining that owner (or the clock callable itself) prevents id reuse.
        clock_owner = getattr(self._clock, "__self__", None)
        clock_domain = clock_owner if clock_owner is not None else self._clock
        key = id(clock_domain)
        state = _RATE_STATES.get(key)
        if state is None or state.clock_domain is not clock_domain:
            state = _RateState(clock_domain=clock_domain)
            _RATE_STATES[key] = state
        return state

    def _record_server_cooldown(self, status: int, delay: float) -> None:
        with _RATE_LOCK:
            state = self._rate_state_locked()
            now = float(self._clock())
            if not math.isfinite(now):
                raise SecClientError("SEC monotonic clock returned a non-finite value")
            if state.cooldown_until is not None and state.cooldown_until <= now:
                state.cooldown_until = None
                state.cooldown_status = None
            deadline = now + delay
            if not math.isfinite(deadline):
                deadline = math.inf
            if state.cooldown_until is None or deadline > state.cooldown_until:
                state.cooldown_until = deadline
                state.cooldown_status = status

    def _paced_transport(self, url: str, attempts: int) -> _ResponseLike:
        with _RATE_LOCK:
            state = self._rate_state_locked()
            now = float(self._clock())
            if not math.isfinite(now):
                raise SecClientError("SEC monotonic clock returned a non-finite value")

            if state.cooldown_until is not None and state.cooldown_until <= now:
                state.cooldown_until = None
                state.cooldown_status = None
            if state.cooldown_until is not None:
                remaining = state.cooldown_until - now
                if remaining > _MAX_RETRY_AFTER_SECONDS:
                    raise SecRetryDeferredError(
                        state.cooldown_status or 429,
                        attempts,
                        remaining,
                    )

            due = now
            if state.last_started is not None:
                due = max(due, state.last_started + max(state.last_interval, self.interval))
            if state.cooldown_until is not None:
                due = max(due, state.cooldown_until)
            while now < due:
                before = now
                self._sleep(due - now)
                now = float(self._clock())
                if not math.isfinite(now) or now <= before:
                    raise SecClientError("SEC injected sleeper did not advance the monotonic clock")

            state.last_started = now
            state.last_interval = self.interval
            headers = {
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "User-Agent": self.user_agent,
            }
            # Keep the lock through transport initiation: reserving a timestamp and
            # releasing it before the call would let a preempted thread start late.
            return self._transport(url, headers, self.timeout)

    def get(self, url: str) -> FetchResponse:
        """Fetch one complete HTTP 200 resource, following validated SEC redirects."""
        request_url = url
        transport_url = self._validate_initial_url(url)
        retries_used = 0
        attempts = 0

        while True:
            current_url = transport_url
            redirects = 0
            redirect_chain: list[str] = []
            retry_delay: float | None = None
            wait_for_server_cooldown = False

            while True:
                response: _ResponseLike | None = None
                declared_bytes: int | None = None
                status: int | None = None
                try:
                    attempts += 1
                    response = self._paced_transport(current_url, attempts - 1)
                    status = _response_status(response)
                    observed_url = _response_url(response)
                    if observed_url is not None and observed_url != current_url:
                        # A transport that followed redirects itself bypassed the
                        # pre-follow validation boundary; refuse its returned body.
                        raise SecRedirectError(
                            "SEC transport followed a redirect outside the client guard"
                        )

                    if status in _REDIRECT_STATUSES:
                        location = _header(getattr(response, "headers", None), "Location")
                        if not location:
                            raise SecRedirectError("SEC redirect did not include a Location target")
                        if redirects >= _MAX_REDIRECTS:
                            raise SecRedirectError("SEC redirect limit exceeded")
                        target = urllib.parse.urljoin(current_url, location)
                        current_url = self._validate_redirect_url(target)
                        redirect_chain.append(current_url)
                        redirects += 1
                        _safe_close(response)
                        response = None
                        continue

                    if status == 429 or 500 <= status <= 599:
                        retry_delay = _retry_after_seconds(
                            _header(getattr(response, "headers", None), "Retry-After")
                        )
                        if retry_delay is None:
                            retry_delay = _backoff_seconds(retries_used + 1)
                        if status in (429, 503):
                            self._record_server_cooldown(status, retry_delay)
                            wait_for_server_cooldown = True
                        if retry_delay > _MAX_RETRY_AFTER_SECONDS:
                            raise SecRetryDeferredError(status, attempts, retry_delay)
                        if retries_used >= self.retries:
                            raise SecHTTPStatusError(
                                status,
                                attempts,
                                f"SEC returned retryable HTTP {status} after {attempts} "
                                "request attempt(s)",
                            )
                        retries_used += 1
                        break

                    if status == 403:
                        raise SecForbiddenError(attempts)
                    if status == 404:
                        raise SecNotFoundError(attempts)
                    if 200 <= status <= 299 and status != 200:
                        raise SecIncompleteResourceError(
                            f"SEC returned HTTP {status}; a complete GET requires HTTP 200",
                            status=status,
                        )
                    if status != 200:
                        raise SecHTTPStatusError(
                            status,
                            attempts,
                            f"SEC returned terminal HTTP {status} after {attempts} "
                            "request attempt(s)",
                        )

                    headers = getattr(response, "headers", None)
                    declared_length = _header(headers, "Content-Length")
                    if declared_length is not None:
                        if not re.fullmatch(r"[0-9]+", declared_length):
                            raise SecInvalidContentLengthError()
                        normalized_length = declared_length.lstrip("0") or "0"
                        maximum_length = str(self.max_response_bytes)
                        if len(normalized_length) > len(maximum_length) or (
                            len(normalized_length) == len(maximum_length)
                            and normalized_length > maximum_length
                        ):
                            raise SecResponseTooLargeError(self.max_response_bytes)
                        declared_bytes = int(normalized_length)

                    body = self._read_bounded(response)
                    if declared_bytes is not None and len(body) != declared_bytes:
                        raise SecIncompleteResourceError(
                            "SEC response body length did not match Content-Length",
                            status=status,
                            expected_bytes=declared_bytes,
                            observed_bytes=len(body),
                        )
                    content_type = _header(headers, "Content-Type")
                    return FetchResponse(
                        body=body,
                        request_url=request_url,
                        final_url=current_url,
                        content_type=content_type,
                        fetched_at_utc=datetime.now(timezone.utc),
                        attempts=attempts,
                        status=status,
                        transport_url=transport_url,
                        redirect_chain=tuple(redirect_chain),
                    )
                except SecClientError:
                    raise
                except Exception as exc:
                    incomplete_read = exc if isinstance(exc, http.client.IncompleteRead) else None
                    if incomplete_read is None and isinstance(exc, urllib.error.URLError):
                        reason = exc.reason
                        if isinstance(reason, http.client.IncompleteRead):
                            incomplete_read = reason
                    if incomplete_read is not None:
                        partial = getattr(incomplete_read, "partial", b"")
                        observed_bytes = (
                            len(partial)
                            if isinstance(partial, (bytes, bytearray, memoryview))
                            else None
                        )
                        raise SecIncompleteResourceError(
                            "SEC response ended before the HTTP message was complete",
                            status=status,
                            expected_bytes=declared_bytes,
                            observed_bytes=observed_bytes,
                        ) from None
                    if not _is_timeout(exc):
                        raise SecTransportError("SEC transport failed") from None
                    if retries_used >= self.retries:
                        raise SecTimeoutError(attempts) from None
                    retries_used += 1
                    retry_delay = _backoff_seconds(retries_used)
                    break
                finally:
                    _safe_close(response)

            if retry_delay is not None and not wait_for_server_cooldown:
                self._sleep(retry_delay)

    def _read_bounded(self, response: _ResponseLike) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            read_size = min(_READ_CHUNK_BYTES, self.max_response_bytes - total + 1)
            chunk = response.read(read_size)
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise SecTransportError("SEC transport returned a non-byte response body")
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_response_bytes:
                raise SecResponseTooLargeError(self.max_response_bytes, total)
            chunks.append(bytes(chunk))
        return b"".join(chunks)


class TaxonomyClient(SecClient):
    """Bounded HTTPS taxonomy fetcher using an explicit exact-host allowlist.

    Explicit HTTP-origin taxonomy URIs are retained in ``request_url`` while the
    actual request is upgraded to HTTPS and exposed as ``transport_url``. Redirects
    remain HTTPS-only and must stay within the supplied allowlist.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        allowed_hosts: Collection[str],
        interval: float = 0.2,
        timeout: float = 30.0,
        retries: int = 3,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        if allowed_hosts is None:
            raise ValueError("TaxonomyClient requires an explicit allowed_hosts collection")
        super().__init__(
            user_agent,
            interval=interval,
            timeout=timeout,
            retries=retries,
            max_response_bytes=max_response_bytes,
            transport=transport,
            clock=clock,
            sleep=sleep,
            allowed_hosts=allowed_hosts,
        )

    def _validate_initial_url(self, url: str) -> str:
        if isinstance(url, str):
            try:
                scheme = urllib.parse.urlsplit(url).scheme.casefold()
            except ValueError:
                scheme = ""
            if scheme == "http":
                return _upgrade_http_resource_alias(url, self._allowed_hosts)
        return super()._validate_initial_url(url)

    def fetch_taxonomy(self, url: str) -> FetchResponse:
        """Fetch one explicit taxonomy URI without ever making an HTTP request."""
        return self.get(url)
