"""Prepare explicitly authorized XBRL dependencies for offline Arelle parsing.

The module never performs HTTP requests.  Callers provide an auditable fetch
callback; every URI is checked against an exact host allowlist before that
callback is invoked.  Fetched and already-copied source bytes are mirrored into a
fresh private Arelle-compatible cache without rewriting source documents.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

_XML_DOCTYPE_OR_ENTITY = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_XSI_SCHEMA_LOCATION = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"
_XSI_NO_NAMESPACE_SCHEMA_LOCATION = (
    "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
)
_XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_MAX_XML_DEPTH = 40
_ARELLE_CACHE_LOCK = threading.Lock()


class DependencyPreparationError(ValueError):
    """Fail-closed dependency preparation error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.url = url


class FetchResponseLike(Protocol):
    """Minimum response shape accepted from a caller-owned fetch callback.

    The repository SEC response already supplies ``body``, ``request_url`` and
    ``final_url``. A ``redirect_chain`` tuple is consumed when available. The fetcher
    is responsible for checking each redirect destination *before* following it;
    post-response validation here cannot retroactively prevent an unsafe connection.
    """

    body: bytes
    final_url: str


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Small immutable fetch response for callers and offline fixture tests."""

    body: bytes
    final_url: str
    redirect_chain: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DependencyRecord:
    """Auditable source-to-cache provenance for one requested resource URL."""

    requested_url: str
    final_url: str
    local_path: Path
    sha256: str
    size: int
    redirect_chain: tuple[str, ...] = ()
    transport_url: str | None = None
    relative_base_url: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedDependencies:
    """Prepared offline dependency set and exact resource/cache mappings."""

    cache_dir: Path
    records: tuple[DependencyRecord, ...]
    url_map: Mapping[str, Path]
    origin_map: Mapping[Path, str]
    expected_hashes: Mapping[Path, str]

    def entrypoint_path(self, original_url: str) -> Path:
        """Return the private-cache entrypoint corresponding to an input source URL."""
        try:
            return self.url_map[original_url]
        except KeyError as exc:
            raise KeyError(
                f"entrypoint URL is not in this prepared dependency set: {original_url}"
            ) from exc


@dataclass(frozen=True, slots=True)
class _XmlDocument:
    requested_url: str
    effective_url: str
    source_path: Path
    body: bytes
    depth: int
    primary: bool = False
    relative_base_url: str | None = None


@dataclass(slots=True)
class _PreparationState:
    stage_dir: Path
    mapper: Any
    cache_paths: dict[str, Path] = field(default_factory=dict)
    path_contents: dict[Path, bytes] = field(default_factory=dict)
    path_origins: dict[Path, str] = field(default_factory=dict)
    records: list[DependencyRecord] = field(default_factory=list)
    total_cache_bytes: int = 0


def _host_allowlist(allowed_hosts: Collection[str]) -> frozenset[str]:
    hosts: set[str] = set()
    for item in allowed_hosts:
        value = item.strip().lower()
        if not value or "://" in value or "/" in value or "@" in value:
            raise DependencyPreparationError(
                "invalid_allowlist", f"allowed_hosts must contain bare exact hostnames: {item!r}"
            )
        if value.endswith("."):
            value = value[:-1]
        if not value or "*" in value:
            raise DependencyPreparationError(
                "invalid_allowlist", f"wildcards and empty hostnames are not allowed: {item!r}"
            )
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise DependencyPreparationError(
                "invalid_allowlist", f"invalid IDNA hostname: {item!r}"
            ) from exc
        hosts.add(value)
    return frozenset(hosts)


def _canonical_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Validate a resource URL, drop only its fragment, and normalize scheme/host."""
    if not isinstance(url, str) or not url or any(char in url for char in "\r\n\x00"):
        raise DependencyPreparationError(
            "invalid_url", f"invalid resource URL: {url!r}", url=str(url)
        )
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise DependencyPreparationError(
            "unsafe_url_scheme", f"only explicit HTTP(S) resources are supported: {url}", url=url
        )
    if parsed.username is not None or parsed.password is not None:
        raise DependencyPreparationError(
            "unsafe_url", "credentials in resource URLs are not allowed", url=url
        )
    if parsed.query or "?" in url.split("#", 1)[0]:
        raise DependencyPreparationError(
            "unsafe_url", "query-bearing resource URLs are not allowed", url=url
        )
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DependencyPreparationError(
            "invalid_url", f"invalid host or port in URL: {url}", url=url
        ) from exc
    if not host:
        raise DependencyPreparationError("invalid_url", f"resource URL has no host: {url}", url=url)
    if host.endswith("."):
        raise DependencyPreparationError(
            "unsafe_url", "trailing-dot hosts are not allowed", url=url
        )
    try:
        normalized_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise DependencyPreparationError(
            "invalid_url", f"invalid IDNA host in URL: {url}", url=url
        ) from exc
    if normalized_host not in allowed_hosts:
        raise DependencyPreparationError(
            "untrusted_host", f"host {normalized_host!r} is not in the explicit allowlist", url=url
        )
    expected_port = 80 if scheme == "http" else 443
    if port not in (None, expected_port):
        raise DependencyPreparationError(
            "unsafe_url", f"non-standard resource ports are not allowed: {url}", url=url
        )
    # WebCache maps URL path segments into cache path components. Reject encoded
    # traversal and separators rather than relying on path normalization behavior.
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or "\x00" in decoded_path:
        raise DependencyPreparationError("unsafe_url", "invalid URL path characters", url=url)
    if any(part in {".", ".."} for part in decoded_path.split("/")):
        raise DependencyPreparationError(
            "unsafe_url", "dot segments in resource URL are not allowed", url=url
        )

    netloc = normalized_host
    if ":" in normalized_host and not normalized_host.startswith("["):
        netloc = f"[{normalized_host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                raise DependencyPreparationError(
                    "unsafe_path", f"symlink path component is not allowed: {current}"
                )
        except OSError as exc:
            raise DependencyPreparationError(
                "unsafe_path", f"cannot inspect path component {current}: {exc}"
            ) from exc


def _safe_workspace_path(path: Path, workspace_root: Path) -> Path:
    root = workspace_root.resolve(strict=True)
    if not root.is_dir():
        raise DependencyPreparationError("unsafe_path", "workspace_root must be a directory")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    _assert_no_symlink_components(candidate.absolute())
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise DependencyPreparationError(
            "unsafe_path", f"input file is missing or outside workspace_root: {path}"
        ) from exc
    if not resolved.is_file():
        raise DependencyPreparationError("unsafe_path", f"input is not a regular file: {path}")
    return resolved


def _cache_root_path(cache_dir: Path, workspace_root: Path) -> tuple[Path, Path]:
    root = workspace_root.resolve(strict=True)
    requested_cache = Path(cache_dir).absolute()
    _assert_no_symlink_components(requested_cache)
    resolved_cache = requested_cache.resolve(strict=False)
    try:
        resolved_cache.relative_to(root)
    except ValueError:
        pass
    else:
        raise DependencyPreparationError(
            "unsafe_cache_path", "cache_dir must be outside workspace_root"
        )
    if resolved_cache.exists():
        raise DependencyPreparationError(
            "cache_not_empty_or_private",
            "cache_dir must be a fresh, non-existing private directory",
        )
    parent = resolved_cache.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DependencyPreparationError(
            "cache_unavailable", f"cannot create cache parent: {exc}"
        ) from exc
    _assert_no_symlink_components(parent)
    return resolved_cache, parent


def _doctype_has_internal_subset(raw: bytes, encoding: str | None) -> bool:
    try:
        text = raw.decode(encoding or "utf-8", errors="ignore")
    except (LookupError, UnicodeError):
        text = raw.decode("utf-8", errors="ignore")
    match = re.search(r"<!DOCTYPE\b", text, re.IGNORECASE)
    if match is None:
        return False
    quote: str | None = None
    for char in text[match.end() :]:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "[":
            return True
        elif char == ">":
            return False
    return False


def _parse_xml_resource(body: bytes, *, url: str, primary: bool = False) -> Any:
    try:
        from lxml import etree
    except ImportError as exc:
        raise DependencyPreparationError(
            "parser_dependency_missing",
            "install arelle-release==2.46.0 for lxml XML parsing",
            url=url,
        ) from exc
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        root = etree.fromstring(body, parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        if re.search(rb"<\s*(?:!doctype\s+html|html|body)\b", body[:8192], re.IGNORECASE):
            raise DependencyPreparationError(
                "unsupported_non_xml",
                f"legacy HTML is not an XML taxonomy dependency: {url}",
                url=url,
            ) from exc
        raise DependencyPreparationError(
            "invalid_xml", f"invalid XML resource {url}: {exc}", url=url
        ) from exc
    if root is None:
        raise DependencyPreparationError("invalid_xml", f"empty XML resource: {url}", url=url)
    tree = root.getroottree()
    docinfo = tree.docinfo
    if docinfo.doctype:
        doctype_name = re.fullmatch(r"<!DOCTYPE\s+html\s*>", docinfo.doctype, re.IGNORECASE)
        html_root = str(root.tag).rsplit("}", 1)[-1].lower() == "html"
        has_ixbrl = any(
            isinstance(node.tag, str)
            and node.tag.startswith(
                (
                    "{http://www.xbrl.org/2013/inlineXBRL}",
                    "{http://www.xbrl.org/2008/inlineXBRL}",
                )
            )
            for node in root.iter()
        )
        allowed_bare_html = (
            primary
            and bool(doctype_name)
            and html_root
            and has_ixbrl
            and not docinfo.system_url
            and not docinfo.public_id
            and not _doctype_has_internal_subset(body, docinfo.encoding)
            and not (docinfo.internalDTD and docinfo.internalDTD.entities())
        )
        if not allowed_bare_html:
            if html_root and not has_ixbrl:
                code = "unsupported_non_xml"
                message = f"legacy HTML is not an XBRL dependency: {url}"
            else:
                code = "xml_declaration_forbidden"
                message = f"DOCTYPE/entity declarations are forbidden: {url}"
            raise DependencyPreparationError(code, message, url=url)
    root_tag = str(root.tag).rsplit("}", 1)[-1].lower() if isinstance(root.tag, str) else ""
    if root_tag in {"html", "xhtml"}:
        has_ixbrl = any(
            isinstance(node.tag, str)
            and node.tag.startswith(
                (
                    "{http://www.xbrl.org/2013/inlineXBRL}",
                    "{http://www.xbrl.org/2008/inlineXBRL}",
                )
            )
            for node in root.iter()
        )
        if not has_ixbrl:
            raise DependencyPreparationError(
                "unsupported_non_xml", f"legacy HTML is not an XBRL dependency: {url}", url=url
            )
    return root


def _resource_references(root: Any, *, url: str) -> list[str]:
    refs: list[str] = []
    for node in root.iter():
        if _XML_BASE in node.attrib:
            raise DependencyPreparationError(
                "unsupported_xml_base", f"xml:base is unsupported and fails closed: {url}", url=url
            )
        for attribute, value in node.attrib.items():
            if attribute == _XLINK_HREF:
                # XLink references are resource edges. Ordinary HTML href attributes
                # are intentionally ignored; in-document fragments are filtered below.
                refs.append(value)
            elif attribute == _XSI_SCHEMA_LOCATION:
                locations = value.split()
                if len(locations) % 2:
                    raise DependencyPreparationError(
                        "invalid_schema_location",
                        f"xsi:schemaLocation must contain namespace/location pairs: {url}",
                        url=url,
                    )
                refs.extend(locations[1::2])
            elif attribute == _XSI_NO_NAMESPACE_SCHEMA_LOCATION:
                refs.append(value)
            elif (
                attribute.rsplit("}", 1)[-1] == "schemaLocation"
                and str(node.tag).startswith(f"{{{_XSD_NS}}}")
                and str(node.tag).rsplit("}", 1)[-1]
                in {"import", "include", "redefine", "override"}
            ):
                refs.append(value)
    return [ref.strip() for ref in refs if ref.strip() and not ref.strip().startswith("#")]


def _relative_resource_references(references: Collection[str]) -> tuple[str, ...]:
    return tuple(
        reference
        for reference in references
        if not urlsplit(reference).scheme
        and bool(urlsplit(reference).path or urlsplit(reference).netloc)
    )


def _scheme_relative_resource_references(references: Collection[str]) -> tuple[str, ...]:
    return tuple(
        reference
        for reference in references
        if not urlsplit(reference).scheme and bool(urlsplit(reference).netloc)
    )


def _relative_local_companion(href: str, source_path: Path, workspace_root: Path) -> Path | None:
    parsed = urlsplit(href.strip())
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.query:
        return None
    relative_path = unquote(parsed.path)
    candidate = source_path.parent / relative_path
    if not candidate.exists() and not candidate.is_symlink():
        return None
    return _safe_workspace_path(candidate, workspace_root)


def _mapped_cache_path(mapper: Any, resource_url: str, stage_dir: Path) -> Path:
    mapped = Path(
        mapper.webCache.urlToCacheFilepath(
            resource_url,
            cacheDir=str(stage_dir),
            useRedirectFallback=False,
        )
    )
    if not mapped.is_absolute():
        mapped = stage_dir / mapped
    try:
        relative = mapped.relative_to(stage_dir)
    except ValueError as exc:
        raise DependencyPreparationError(
            "unsafe_cache_mapping",
            f"Arelle mapped URL outside the private cache: {resource_url}",
            url=resource_url,
        ) from exc
    if any(part in {".", ".."} for part in relative.parts):
        raise DependencyPreparationError(
            "unsafe_cache_mapping",
            f"Arelle returned an unsafe cache path for {resource_url}",
            url=resource_url,
        )
    return mapped


def _store_bytes(
    state: _PreparationState,
    *,
    url: str,
    body: bytes,
    max_total_bytes: int,
) -> Path:
    path = _mapped_cache_path(state.mapper, url, state.stage_dir)
    previous = state.path_contents.get(path)
    if previous is not None:
        if previous != body:
            raise DependencyPreparationError(
                "cache_path_collision",
                f"different resource bytes map to the same cache path: {url}",
                url=url,
            )
        return path
    new_total = state.total_cache_bytes + len(body)
    if new_total > max_total_bytes:
        raise DependencyPreparationError(
            "total_size_limit", f"dependency cache exceeds {max_total_bytes} bytes", url=url
        )
    relative = path.relative_to(state.stage_dir)
    parent = state.stage_dir
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.exists() and parent.is_symlink():
            raise DependencyPreparationError(
                "unsafe_cache_path", f"symlink in cache path: {parent}", url=url
            )
        parent.mkdir(exist_ok=True)
    if path.exists() or path.is_symlink():
        raise DependencyPreparationError(
            "cache_path_exists", f"unexpected cache target exists: {path}", url=url
        )
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".resource-", dir=parent, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(body)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except (UnboundLocalError, OSError):
            pass
        raise DependencyPreparationError(
            "cache_write_failed", f"cannot write private cache file: {exc}", url=url
        ) from exc
    state.path_contents[path] = body
    state.total_cache_bytes = new_total
    return path


def _record_resource(
    state: _PreparationState,
    *,
    requested_url: str,
    final_url: str,
    body: bytes,
    redirect_chain: tuple[str, ...],
    max_total_bytes: int,
    transport_url: str | None = None,
    relative_base_url: str | None = None,
) -> Path:
    requested_path = _store_bytes(
        state, url=requested_url, body=body, max_total_bytes=max_total_bytes
    )
    final_path = _store_bytes(state, url=final_url, body=body, max_total_bytes=max_total_bytes)
    state.cache_paths[requested_url] = requested_path
    state.cache_paths[final_url] = final_path
    state.path_origins[requested_path] = requested_url
    state.path_origins[final_path] = final_url
    state.records.append(
        DependencyRecord(
            requested_url=requested_url,
            final_url=final_url,
            local_path=requested_path,
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            redirect_chain=redirect_chain,
            transport_url=transport_url,
            relative_base_url=relative_base_url,
        )
    )
    return final_path


class _ArelleCacheMapper:
    """Small initialized Arelle WebCache holder; never opens a URL or writes cache data."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.controller: Any = None
        self.webCache: Any = None

    def __enter__(self) -> _ArelleCacheMapper:
        _ARELLE_CACHE_LOCK.acquire()
        try:
            from arelle.CntlrCmdLine import CntlrCmdLine
        except ImportError as exc:
            _ARELLE_CACHE_LOCK.release()
            raise DependencyPreparationError(
                "parser_dependency_missing",
                "install arelle-release==2.46.0 to map private Arelle cache paths",
            ) from exc
        try:
            self.controller = CntlrCmdLine(
                logFileName="logToBuffer",
                disable_persistent_config=True,
            )
            self.webCache = self.controller.webCache
            self.webCache.cacheDir = str(self.cache_dir)
            self.webCache.workOffline = True
            return self
        except Exception:
            try:
                if self.controller is not None:
                    self.controller.close(saveConfig=False)
            finally:
                _ARELLE_CACHE_LOCK.release()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self.controller is not None:
                self.controller.close(saveConfig=False)
        finally:
            _ARELLE_CACHE_LOCK.release()


def arelle_cache_path(url: str, cache_dir: Path) -> Path:
    """Map an HTTP(S) resource URI to the private Arelle cache pathname.

    This performs path mapping only: it does not create the target, resolve
    redirects, or make a network request. The returned path is guaranteed to be
    lexically beneath ``cache_dir``.
    """
    cache_root = Path(cache_dir).absolute()
    _assert_no_symlink_components(cache_root)
    # Mapping utilities have no fetch authority; a one-host allowlist performs URL
    # syntax validation without turning this helper into a network permission.
    canonical = _canonical_url(url, frozenset({_hostname_only(url)}))
    with _ArelleCacheMapper(cache_root) as mapper:
        return _mapped_cache_path(mapper, canonical, cache_root)


def _hostname_only(url: str) -> str:
    try:
        host = urlsplit(url).hostname
    except ValueError as exc:
        raise DependencyPreparationError(
            "invalid_url", f"invalid resource URL: {url}", url=url
        ) from exc
    if not host:
        raise DependencyPreparationError("invalid_url", f"resource URL has no host: {url}", url=url)
    return host.rstrip(".").encode("idna").decode("ascii").lower()


def offline_runtime_options(entrypoint: Path, cache_dir: Path) -> Any:
    """Build Arelle 2.46 RuntimeOptions isolated to an explicit offline cache."""
    raw_cache_root = Path(cache_dir).absolute()
    raw_entry = Path(entrypoint).absolute()
    _assert_no_symlink_components(raw_cache_root)
    _assert_no_symlink_components(raw_entry)
    cache_root = raw_cache_root.resolve(strict=True)
    entry = raw_entry.resolve(strict=True)
    try:
        entry.relative_to(cache_root)
    except ValueError as exc:
        raise DependencyPreparationError(
            "unsafe_entrypoint", "offline entrypoint must be inside the prepared private cache"
        ) from exc
    _assert_no_symlink_components(entry)
    try:
        from arelle.RuntimeOptions import RuntimeOptions
    except ImportError as exc:
        raise DependencyPreparationError(
            "parser_dependency_missing", "install arelle-release==2.46.0 for offline RuntimeOptions"
        ) from exc
    return RuntimeOptions(
        entrypointFile=str(entry),
        internetConnectivity="offline",
        cacheDirectory=str(cache_root),
        keepOpen=True,
        disablePersistentConfig=True,
        logFile="logToBuffer",
    )


def _proven_transport_only_upgrade(
    *,
    requested_url: str,
    final_url: str,
    transport_url: str | None,
    redirects: tuple[str, ...],
    request_url_confirmed: bool,
    allowed_hosts: frozenset[str],
) -> bool:
    """Prove only an exact same-host/path HTTP-origin to HTTPS transport upgrade."""
    if not request_url_confirmed or transport_url is None:
        return False
    requested = urlsplit(requested_url)
    if requested.scheme != "http" or requested.port is not None:
        return False
    expected_transport = _canonical_url(
        urlunsplit(("https", requested.netloc, requested.path or "/", "", "")),
        allowed_hosts,
    )
    known_hops = {requested_url, expected_transport}
    return (
        transport_url == expected_transport
        and final_url == expected_transport
        and all(redirect in known_hops for redirect in redirects)
    )


def _fetch_response(
    fetch: Callable[[str], FetchResponseLike],
    requested_url: str,
    allowed_hosts: frozenset[str],
) -> tuple[bytes, str, tuple[str, ...], str | None, bool]:
    try:
        response = fetch(requested_url)
    except DependencyPreparationError:
        raise
    except Exception as exc:
        raise DependencyPreparationError(
            "missing_dependency",
            f"fetch callback failed for {requested_url}: {exc}",
            url=requested_url,
        ) from exc
    body = getattr(response, "body", None)
    if not isinstance(body, bytes):
        raise DependencyPreparationError(
            "invalid_fetch_response",
            "fetch callback must return a response with immutable bytes body",
            url=requested_url,
        )
    # Optional response metadata is read structurally for backward compatibility.
    response_request_url = getattr(response, "request_url", None)
    request_url_confirmed = response_request_url is not None
    if request_url_confirmed:
        canonical_request = _canonical_url(response_request_url, allowed_hosts)
        if canonical_request != requested_url:
            raise DependencyPreparationError(
                "fetch_request_mismatch",
                f"fetch response request URL does not match requested resource {requested_url}",
                url=requested_url,
            )
    final_url_value = getattr(response, "final_url", None)
    final_url = _canonical_url(final_url_value or requested_url, allowed_hosts)
    transport_url_value = getattr(response, "transport_url", None)
    transport_url = (
        _canonical_url(transport_url_value, allowed_hosts)
        if transport_url_value is not None
        else None
    )
    redirect_chain = getattr(response, "redirect_chain", ()) or ()
    redirects = tuple(_canonical_url(item, allowed_hosts) for item in redirect_chain)
    transport_alias_proven = _proven_transport_only_upgrade(
        requested_url=requested_url,
        final_url=final_url,
        transport_url=transport_url,
        redirects=redirects,
        request_url_confirmed=request_url_confirmed,
        allowed_hosts=allowed_hosts,
    )
    return body, final_url, redirects, transport_url, transport_alias_proven


def _new_private_stage(cache_dir: Path, parent: Path) -> Path:
    if cache_dir.exists() or cache_dir.is_symlink():
        raise DependencyPreparationError(
            "cache_not_empty_or_private", "cache_dir must not exist before preparation"
        )
    try:
        stage = Path(tempfile.mkdtemp(prefix=f".{cache_dir.name}.stage-", dir=parent))
    except OSError as exc:
        raise DependencyPreparationError(
            "cache_unavailable", f"cannot create private staging cache: {exc}"
        ) from exc
    return stage.resolve(strict=True)


def prepare_dependencies(
    entrypoints: Mapping[str, Path],
    *,
    workspace_root: Path,
    cache_dir: Path,
    fetch: Callable[[str], FetchResponseLike],
    allowed_hosts: Collection[str],
    max_dependencies: int = 500,
    max_total_bytes: int = 128 * 1024 * 1024,
) -> PreparedDependencies:
    """Resolve a bounded XML/XBRL dependency graph into a fresh Arelle offline cache.

    ``entrypoints`` keys are the original source URLs of already-copied input files.
    Relative resource references are joined against those original URLs, while
    relative references that exist beside a source in ``workspace_root`` are read
    there instead of invoking ``fetch``. The callback is the only possible network
    boundary. This function performs no downloads by itself.

    Input bytes are never changed. Preparation writes only to a new cache directory
    outside ``workspace_root`` and publishes the complete tree only after every
    discovered resource has been checked and copied. HTTP-to-HTTPS rewriting and
    unknown redirect aliases are deliberately not inferred; each URL and redirect
    host must pass the exact allowlist.
    """
    if not entrypoints:
        raise DependencyPreparationError(
            "empty_entrypoints", "at least one source entrypoint is required"
        )
    if max_dependencies < 1 or max_total_bytes < 1:
        raise DependencyPreparationError(
            "invalid_budget", "dependency and byte budgets must be positive"
        )
    host_allowlist = _host_allowlist(allowed_hosts)
    workspace = Path(workspace_root).resolve(strict=True)
    if not workspace.is_dir():
        raise DependencyPreparationError("unsafe_path", "workspace_root must be a directory")
    final_cache_dir, cache_parent = _cache_root_path(Path(cache_dir), workspace)
    stage_dir = _new_private_stage(final_cache_dir, cache_parent)
    before_hashes: dict[Path, str] = {}
    queue: list[_XmlDocument] = []
    queued_urls: set[str] = set()
    expanded_urls: set[str] = set()
    state: _PreparationState | None = None

    try:
        with _ArelleCacheMapper(stage_dir) as mapper:
            state = _PreparationState(stage_dir=stage_dir, mapper=mapper)
            canonical_entrypoints: dict[str, tuple[tuple[str, ...], Path]] = {}
            for original_url, source_path in entrypoints.items():
                original_alias = str(original_url)
                canonical = _canonical_url(original_alias, host_allowlist)
                local_source = _safe_workspace_path(Path(source_path), workspace)
                previous = canonical_entrypoints.get(canonical)
                if previous is not None:
                    aliases, previous_source = previous
                    if previous_source != local_source:
                        raise DependencyPreparationError(
                            "entrypoint_url_collision",
                            f"multiple files were assigned to {canonical}",
                            url=canonical,
                        )
                    canonical_entrypoints[canonical] = (
                        (*aliases, original_alias),
                        local_source,
                    )
                else:
                    canonical_entrypoints[canonical] = ((original_alias,), local_source)

            if len(canonical_entrypoints) > max_dependencies:
                raise DependencyPreparationError(
                    "dependency_limit", f"entrypoints exceed max_dependencies={max_dependencies}"
                )

            for source_url, (original_aliases, local_source) in sorted(
                canonical_entrypoints.items()
            ):
                body = local_source.read_bytes()
                before_hashes[local_source] = hashlib.sha256(body).hexdigest()
                local_path = _record_resource(
                    state,
                    requested_url=source_url,
                    final_url=source_url,
                    body=body,
                    redirect_chain=(),
                    max_total_bytes=max_total_bytes,
                )
                queue.append(
                    _XmlDocument(
                        requested_url=source_url,
                        effective_url=source_url,
                        source_path=local_source,
                        body=body,
                        depth=0,
                        primary=True,
                    )
                )
                queued_urls.add(source_url)
                # local_path is deliberately registered through the same Arelle
                # WebCache mapper used for every downloaded dependency.
                state.cache_paths[source_url] = local_path
                for original_alias in original_aliases:
                    if original_alias != source_url:
                        state.cache_paths[original_alias] = local_path

            while queue:
                document = queue.pop(0)
                if document.effective_url in expanded_urls:
                    continue
                root = _parse_xml_resource(
                    document.body,
                    url=document.effective_url,
                    primary=document.primary,
                )
                references = _resource_references(root, url=document.effective_url)
                scheme_relative = _scheme_relative_resource_references(references)
                if scheme_relative:
                    raise DependencyPreparationError(
                        "unsupported_scheme_relative_resource",
                        f"scheme-relative XML resource reference is unsupported: "
                        f"{scheme_relative[0]}",
                        url=document.effective_url,
                    )
                expanded_urls.add(document.effective_url)
                document_base_url = document.relative_base_url or document.effective_url
                for reference in references:
                    if reference.startswith("#"):
                        continue
                    resolved_url = _canonical_url(
                        urljoin(document_base_url, reference), host_allowlist
                    )
                    if resolved_url in state.cache_paths:
                        # A known requested/final URL is already mirrored. Its graph
                        # has either been expanded or is waiting in the queue.
                        continue
                    if resolved_url in queued_urls:
                        continue
                    if len(queued_urls) >= max_dependencies:
                        raise DependencyPreparationError(
                            "dependency_limit",
                            f"dependency graph exceeds max_dependencies={max_dependencies}",
                            url=resolved_url,
                        )
                    if document.depth >= _MAX_XML_DEPTH:
                        raise DependencyPreparationError(
                            "dependency_depth_limit",
                            f"dependency graph exceeds depth {_MAX_XML_DEPTH}",
                            url=resolved_url,
                        )

                    companion = _relative_local_companion(
                        reference, document.source_path, workspace
                    )
                    if companion is not None:
                        body = companion.read_bytes()
                        before_hashes[companion] = hashlib.sha256(body).hexdigest()
                        local_path = _record_resource(
                            state,
                            requested_url=resolved_url,
                            final_url=resolved_url,
                            body=body,
                            redirect_chain=(),
                            max_total_bytes=max_total_bytes,
                        )
                        queue.append(
                            _XmlDocument(
                                requested_url=resolved_url,
                                effective_url=resolved_url,
                                source_path=companion,
                                body=body,
                                depth=document.depth + 1,
                            )
                        )
                        queued_urls.add(resolved_url)
                        state.cache_paths[resolved_url] = local_path
                        continue

                    (
                        body,
                        final_url,
                        redirects,
                        transport_url,
                        transport_alias_proven,
                    ) = _fetch_response(fetch, resolved_url, host_allowlist)
                    transport_changed_base = (
                        transport_url is not None and transport_url != resolved_url
                    )
                    redirect_changed_base = any(
                        redirect not in {resolved_url, final_url} for redirect in redirects
                    )
                    if final_url != resolved_url or transport_changed_base or redirect_changed_base:
                        redirected_root = _parse_xml_resource(body, url=resolved_url)
                        redirected_references = _resource_references(
                            redirected_root, url=resolved_url
                        )
                        scheme_relative = _scheme_relative_resource_references(
                            redirected_references
                        )
                        if scheme_relative:
                            raise DependencyPreparationError(
                                "unsupported_scheme_relative_resource",
                                "scheme-relative XML resource references are unsupported: "
                                f"{scheme_relative[0]}",
                                url=resolved_url,
                            )
                        relative_references = _relative_resource_references(redirected_references)
                        if relative_references and not transport_alias_proven:
                            requested_parts = urlsplit(resolved_url)
                            final_parts = urlsplit(final_url)
                            requested_directory = requested_parts.path.rpartition("/")[0]
                            final_directory = final_parts.path.rpartition("/")[0]
                            if (
                                requested_parts.hostname != final_parts.hostname
                                or requested_directory != final_directory
                            ):
                                reason = "redirect changed host or directory"
                            else:
                                reason = (
                                    "redirected relative-resource aliases are not verified for "
                                    "the parser's requested-URL origin"
                                )
                            raise DependencyPreparationError(
                                "unsupported_redirect_relative_dependency",
                                f"{reason}: {resolved_url} -> {final_url}",
                                url=resolved_url,
                            )
                    child_relative_base_url = resolved_url if transport_alias_proven else final_url
                    if final_url in queued_urls and final_url != resolved_url:
                        # The requested alias remains separately mapped; the final URL
                        # must resolve to byte-identical content if already prepared.
                        existing_path = state.cache_paths.get(final_url)
                        existing_body = (
                            state.path_contents.get(existing_path) if existing_path else None
                        )
                        if existing_body is not None and existing_body != body:
                            raise DependencyPreparationError(
                                "redirect_content_conflict",
                                f"redirect alias returned conflicting bytes for {final_url}",
                                url=resolved_url,
                            )
                    local_path = _record_resource(
                        state,
                        requested_url=resolved_url,
                        final_url=final_url,
                        body=body,
                        redirect_chain=redirects,
                        max_total_bytes=max_total_bytes,
                        transport_url=transport_url,
                        relative_base_url=child_relative_base_url,
                    )
                    queued_urls.add(resolved_url)
                    queued_urls.add(final_url)
                    # A proven HTTP-to-HTTPS transport alias keeps the original
                    # request URI as the base; other resources use their final URI.
                    final_path = state.cache_paths[final_url]
                    queue.append(
                        _XmlDocument(
                            requested_url=resolved_url,
                            effective_url=final_url,
                            source_path=final_path,
                            body=body,
                            depth=document.depth + 1,
                            relative_base_url=child_relative_base_url,
                        )
                    )
                    del local_path

            # Re-read all original inputs before publishing; a concurrent mutation
            # makes this run fail rather than declaring a mixed-source cache complete.
            for source_path, expected_hash in before_hashes.items():
                actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    raise DependencyPreparationError(
                        "input_changed", f"source bytes changed during preparation: {source_path}"
                    )

        if final_cache_dir.exists() or final_cache_dir.is_symlink():
            raise DependencyPreparationError(
                "cache_not_empty_or_private", "cache_dir appeared during preparation"
            )
        os.rename(stage_dir, final_cache_dir)
        final_paths = {
            url: final_cache_dir / path.relative_to(stage_dir)
            for url, path in state.cache_paths.items()
        }
        final_origins = {
            final_cache_dir / path.relative_to(stage_dir): origin
            for path, origin in state.path_origins.items()
        }
        final_hashes = {
            final_cache_dir / path.relative_to(stage_dir): hashlib.sha256(body).hexdigest()
            for path, body in state.path_contents.items()
        }
        records = tuple(
            DependencyRecord(
                requested_url=record.requested_url,
                final_url=record.final_url,
                local_path=final_cache_dir / record.local_path.relative_to(stage_dir),
                sha256=record.sha256,
                size=record.size,
                redirect_chain=record.redirect_chain,
                transport_url=record.transport_url,
                relative_base_url=record.relative_base_url,
            )
            for record in state.records
        )
        return PreparedDependencies(
            cache_dir=final_cache_dir,
            records=records,
            url_map=MappingProxyType(final_paths),
            origin_map=MappingProxyType(final_origins),
            expected_hashes=MappingProxyType(final_hashes),
        )
    except DependencyPreparationError:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise DependencyPreparationError(
            "preparation_failed", f"dependency preparation failed: {type(exc).__name__}: {exc}"
        ) from exc
