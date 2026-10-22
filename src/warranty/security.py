"""Server-owned sessions, source permissions and opt-in bounded HTTP access."""
from dataclasses import dataclass
from datetime import date
import copy
import hashlib
import hmac
import http.client
import ipaddress
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from warranty.errors import ServiceError


def _deny(code="FORBIDDEN", status=403):
    raise ServiceError(code, "요청이 허용되지 않았습니다.", status)


def _origin(value):
    if (not isinstance(value, str) or len(value) > 256 or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)):
        _deny("CSRF_INVALID")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            _deny("CSRF_INVALID")
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, TypeError):
        _deny("CSRF_INVALID")


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Session:
    id: str
    actor: str
    csrf_token: str
    expires_at: float
    origin: tuple


class SessionStore:
    """Opaque in-memory identities; production identity issuance is unconfigured."""
    def __init__(self, demo_mode=True, ttl_seconds=3600, clock=time.monotonic):
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("Invalid session lifetime")
        self.demo_mode, self.ttl_seconds, self._clock = demo_mode, ttl_seconds, clock
        self._sessions, self._lock = {}, threading.RLock()

    def create_demo(self, actor, client_host, origin):
        normalized = _origin(origin)
        if (not self.demo_mode or not _loopback(client_host) or not _loopback(normalized[1])
                or actor not in {"buyer", "seller", "admin", "outsider"}):
            _deny("DEMO_LOGIN_DISABLED")
        session = Session(secrets.token_urlsafe(32), actor, secrets.token_urlsafe(32),
                          self._clock() + self.ttl_seconds, normalized)
        with self._lock:
            now = self._clock()
            self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}
            # A local demo still has a finite memory budget.
            if len(self._sessions) >= 10000:
                _deny("SESSION_LIMIT", 429)
            self._sessions[self._key(session.id)] = session
        return session

    @staticmethod
    def _key(session_id):
        if not isinstance(session_id, str) or not 20 <= len(session_id) <= 128:
            _deny("AUTH_REQUIRED", 401)
        return hashlib.sha256(session_id.encode()).digest()

    def resolve(self, session_id):
        key = self._key(session_id)
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session.expires_at <= self._clock():
                self._sessions.pop(key, None)
                _deny("AUTH_REQUIRED", 401)
            return session

    def require_mutation(self, session_id, csrf_token, origin, request_origin):
        session = self.resolve(session_id)
        if (not isinstance(csrf_token, str) or len(csrf_token) > 128
                or not hmac.compare_digest(session.csrf_token.encode(), csrf_token.encode())
                or _origin(origin) != session.origin or _origin(request_origin) != session.origin):
            _deny("CSRF_INVALID")
        return session

    def revoke(self, session_id):
        with self._lock:
            self._sessions.pop(self._key(session_id), None)


ACTIONS = frozenset({"view_link", "fetch", "store_body", "extract", "local_embed",
                     "external_process", "display_excerpt", "public_redistribute"})
BASES = frozenset({"FACT_ONLY", "LICENSE", "STATUTORY_EXCEPTION", "OTHER_RECORDED_BASIS"})


class SourcePolicy:
    """Explicit per-action records, supplied only by trusted server configuration.

    A recorded permission does not turn network access on or authorize paid calls.
    No method infers legal permission from an URL or from document/model text.
    """
    def __init__(self, registry=None, reviews=None, capabilities=None, network_enabled=False):
        self.network_enabled = network_enabled is True
        self._registry, self._reviews = {}, {}
        self._capabilities = {key: frozenset(value) for key, value in (capabilities or {}).items()}
        self._lock = threading.RLock()
        records = registry.values() if isinstance(registry, dict) else (registry or [])
        for source in records:
            self._put_source(source)
        records = reviews.values() if isinstance(reviews, dict) else (reviews or [])
        for review in records:
            self._put_review(review)

    def require_capability(self, actor, capability="DATA_MANAGER"):
        if capability not in self._capabilities.get(actor, ()):
            _deny()

    def _put_source(self, source):
        value = copy.deepcopy(source)
        source_id = value.get("source_id")
        if not isinstance(source_id, str) or not source_id or len(source_id) > 128:
            raise ValueError("Invalid source record")
        parsed = _split_https(value.get("url", value.get("canonical_url", "")))
        hosts = value.get("allowed_hosts", [parsed.hostname])
        if not isinstance(hosts, (list, tuple)) or not hosts:
            raise ValueError("Invalid source hosts")
        normalized = frozenset(_host(host) for host in hosts)
        if parsed.hostname not in normalized:
            raise ValueError("Canonical host missing")
        value["allowed_hosts"] = tuple(sorted(normalized))
        approved = value.get("approved_urls", [parsed.geturl()])
        if not isinstance(approved, (list, tuple)) or not approved:
            raise ValueError("Invalid approved URLs")
        approved = tuple(sorted({_normalize_url(url) for url in approved}))
        if (_normalize_url(parsed.geturl()) not in approved
                or any(urlsplit(url).hostname not in normalized for url in approved)):
            raise ValueError("Invalid approved URLs")
        value["approved_urls"] = approved
        limits = value.get("http_limits", {})
        valid_limits = {"min_interval_seconds", "max_retries", "max_bytes", "connect_timeout",
                        "read_timeout", "max_redirects", "user_agent"}
        if not isinstance(limits, dict) or set(limits) - valid_limits:
            raise ValueError("Unsupported source HTTP limits")
        for key, number in limits.items():
            if key == "user_agent":
                if not isinstance(number, str) or not number.strip() or any(ord(c) < 32 for c in number):
                    raise ValueError("Invalid source User-Agent")
            elif (isinstance(number, bool) or not isinstance(number, (int, float))
                  or not 0 <= number <= (10 * 1024 * 1024 if key == "max_bytes" else 86400)
                  or (key not in {"max_retries", "max_redirects"} and number == 0)
                  or (key in {"max_retries", "max_redirects", "max_bytes"} and not isinstance(number, int))):
                raise ValueError("Invalid source HTTP limit")
        self._registry[source_id] = value

    def _put_review(self, review):
        value = copy.deepcopy(review)
        required = {"source_id", "action", "status", "basis_type", "basis_url", "reviewed_on",
                    "scope", "conditions", "user_scope", "processing_environment"}
        if not required.issubset(value) or value["source_id"] not in self._registry:
            raise ValueError("Incomplete source review")
        if (value["action"] not in ACTIONS or value["status"] not in {"ALLOWED", "BLOCKED", "UNRESOLVED"}
                or value["basis_type"] not in BASES):
            raise ValueError("Invalid source review")
        if any(not isinstance(value[key], str) or not value[key].strip() for key in required):
            raise ValueError("Empty source review")
        date.fromisoformat(value["reviewed_on"])
        _split_https(value["basis_url"])
        version_id = value.get("version_id", value.get("document_version_id"))
        if version_id is not None and (not isinstance(version_id, str) or not version_id):
            raise ValueError("Invalid review version")
        self._reviews[(value["source_id"], version_id, value["action"])] = value

    def register_source(self, actor, source):
        self.require_capability(actor)
        with self._lock:
            self._put_source(source)
            # A changed host/document registration requires a fresh action review.
            source_id = source["source_id"]
            self._reviews = {key: value for key, value in self._reviews.items() if key[0] != source_id}

    def set_review(self, actor, review):
        self.require_capability(actor)
        with self._lock:
            self._put_review(review)

    def source(self, source_id):
        with self._lock:
            if source_id not in self._registry:
                _deny("SOURCE_BLOCKED")
            return copy.deepcopy(self._registry[source_id])

    def require(self, source_id, action, actor=None, version_id=None):
        with self._lock:
            if action not in ACTIONS or source_id not in self._registry:
                _deny("SOURCE_BLOCKED")
            review = self._reviews.get((source_id, version_id, action))
            if review is None:
                review = self._reviews.get((source_id, None, action))
            if review is None or review["status"] != "ALLOWED":
                _deny("SOURCE_BLOCKED")
            # A fact-only record cannot grant full-document or database processing.
            if review["basis_type"] == "FACT_ONLY" and action not in {"view_link", "display_excerpt"}:
                _deny("SOURCE_BLOCKED")
            return copy.deepcopy(review)

    def allows(self, source_id, action, actor=None, version_id=None):
        try:
            self.require(source_id, action, actor, version_id)
            return True
        except ServiceError:
            return False


def _host(host):
    if (not isinstance(host, str) or not host or host.endswith(".")
            or any(character in host for character in "%\\/@: \t\r\n")):
        _deny("URL_BLOCKED")
    try:
        normalized = host.encode("idna").decode("ascii").lower()
        if not all(part and len(part) <= 63 and part[0] != "-" and part[-1] != "-"
                   and all(char.isalnum() or char == "-" for char in part) for part in normalized.split(".")):
            _deny("URL_BLOCKED")
        if len(normalized) > 253:
            _deny("URL_BLOCKED")
        return normalized
    except UnicodeError:
        _deny("URL_BLOCKED")


def _split_https(url):
    if (not isinstance(url, str) or not url or len(url) > 4096
            or any(ord(char) < 33 or ord(char) == 127 for char in url) or "\\" in url):
        _deny("URL_BLOCKED")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443} or parsed.fragment):
            _deny("URL_BLOCKED")
        _host(parsed.hostname)
        return parsed
    except (ValueError, TypeError):
        _deny("URL_BLOCKED")


def _normalize_url(url):
    parsed = _split_https(url)
    return urlunsplit(("https", _host(parsed.hostname), parsed.path or "/", parsed.query, ""))


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    addresses: tuple
    target: str


def validate_url(url, source_id, policy, resolver=socket.getaddrinfo):
    parsed = _split_https(url)
    hostname = _host(parsed.hostname)
    source = policy.source(source_id)
    if hostname not in source["allowed_hosts"] or _normalize_url(url) not in source["approved_urls"]:
        _deny("URL_BLOCKED")
    try:
        answers = resolver(hostname, 443, type=socket.SOCK_STREAM)
        addresses = tuple(sorted({answer[4][0] for answer in answers}))
        if not addresses or len(addresses) > 32:
            _deny("URL_BLOCKED")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # Transition mechanisms can route a globally labelled IPv6 address to IPv4.
            transition = isinstance(ip, ipaddress.IPv6Address) and (
                ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None
                or ip in ipaddress.ip_network("64:ff9b::/96")
                or ip in ipaddress.ip_network("64:ff9b:1::/48"))
            if not ip.is_global or transition:
                _deny("URL_BLOCKED")
    except (OSError, ValueError, TypeError, IndexError):
        _deny("URL_BLOCKED")
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return ValidatedURL(url, hostname, addresses, target)


@dataclass(frozen=True)
class FetchResult:
    body: bytes
    content_type: str
    final_url: str
    status: int


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        # Connect to the validated address, preserving the original TLS hostname.
        sock = socket.create_connection((self._address, 443), self.timeout)
        try:
            peer = ipaddress.ip_address(sock.getpeername()[0])
            if peer != ipaddress.ip_address(self._address) or not peer.is_global:
                _deny("URL_BLOCKED")
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class SafeFetcher:
    """No network unless explicitly enabled. Transport/resolver injection is for tests."""
    def __init__(self, policy, transport=None, resolver=socket.getaddrinfo,
                 max_bytes=10 * 1024 * 1024, timeout=5, read_timeout=20,
                 interval=1.0, max_retries=2, max_redirects=0,
                 user_agent="WarrantyLocalDemo/0.1", clock=time.monotonic, sleeper=time.sleep):
        if not 1 <= max_bytes <= 10 * 1024 * 1024 or not 0 < timeout <= 5 or not 0 < read_timeout <= 20:
            raise ValueError("Unsafe HTTP limits")
        if not 0 <= max_retries <= 2 or not 0 <= max_redirects <= 5 or interval < 1:
            raise ValueError("Unsafe HTTP limits")
        if not isinstance(user_agent, str) or not user_agent.strip() or any(ord(c) < 32 for c in user_agent):
            raise ValueError("Invalid User-Agent")
        self.policy, self._transport, self._resolver = policy, transport, resolver
        self.max_bytes, self.timeout, self.read_timeout = max_bytes, timeout, read_timeout
        self.interval, self.max_retries, self.max_redirects = interval, max_retries, max_redirects
        self.user_agent, self._clock, self._sleep = user_agent, clock, sleeper
        self._last, self._lock = {}, threading.RLock()

    def _limits(self, source):
        configured = source.get("http_limits", {})
        return {"interval": max(self.interval, configured.get("min_interval_seconds", self.interval)),
                "retries": min(self.max_retries, configured.get("max_retries", self.max_retries)),
                "redirects": min(self.max_redirects, configured.get("max_redirects", self.max_redirects)),
                "bytes": min(self.max_bytes, configured.get("max_bytes", self.max_bytes)),
                "timeout": min(self.timeout, configured.get("connect_timeout", self.timeout)),
                "read_timeout": min(self.read_timeout, configured.get("read_timeout", self.read_timeout)),
                "user_agent": configured.get("user_agent", self.user_agent)}

    def _request(self, validated, limits):
        connection = _PinnedHTTPSConnection(validated.hostname, validated.addresses[0], limits["timeout"])
        try:
            connection.connect()
            connection.sock.settimeout(limits["read_timeout"])
            connection.request("GET", validated.target, headers={"User-Agent": limits["user_agent"],
                               "Accept": "text/html, application/pdf", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            length = headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > limits["bytes"]):
                _deny("RESPONSE_TOO_LARGE")
            deadline, chunks, received = time.monotonic() + limits["read_timeout"], [], 0
            while received <= limits["bytes"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Read deadline exceeded")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, limits["bytes"] + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            return response.status, headers, b"".join(chunks)
        finally:
            connection.close()

    def fetch(self, source_id, url, actor=None, version_id=None):
        if not self.policy.network_enabled:
            _deny("NETWORK_DISABLED")
        self.policy.require(source_id, "fetch", actor, version_id)
        limits = self._limits(self.policy.source(source_id))
        redirects, retries, current_url = 0, 0, url
        while True:
            # Resolve afresh for every attempt and redirect. Transport uses these IPs.
            validated = validate_url(current_url, source_id, self.policy, self._resolver)
            with self._lock:
                delay = limits["interval"] - (self._clock() - self._last.get(validated.hostname, float("-inf")))
                if delay > 0:
                    if delay > 60:
                        raise ServiceError("RETRY_LATER", "출처의 요청 간격 대기가 필요합니다.", 429, True)
                    self._sleep(delay)
                self._last[validated.hostname] = self._clock()
            try:
                status, headers, body = (self._transport(validated) if self._transport is not None
                                         else self._request(validated, limits))
            except (OSError, http.client.HTTPException):
                if retries >= limits["retries"]:
                    raise ServiceError("FETCH_FAILED", "문서 요청에 실패했습니다.", 502, True) from None
                retries += 1
                self._sleep(2 ** (retries - 1))
                continue
            headers = {key.lower(): value for key, value in headers.items()}
            if not isinstance(body, bytes) or len(body) > limits["bytes"]:
                _deny("RESPONSE_TOO_LARGE")
            if status in {301, 302, 303, 307, 308}:
                if redirects >= limits["redirects"] or not headers.get("location"):
                    _deny("REDIRECT_BLOCKED")
                current_url = urljoin(current_url, headers["location"])
                redirects += 1
                continue
            if status == 429 or 500 <= status <= 599:
                if retries >= limits["retries"]:
                    raise ServiceError("FETCH_FAILED", "문서 요청에 실패했습니다.", 502, True)
                raw_delay = headers.get("retry-after", "")
                if raw_delay and not raw_delay.isdigit():
                    # Date-form Retry-After requires a later scheduled request; never retry early.
                    raise ServiceError("RETRY_LATER", "출처의 재시도 대기가 필요합니다.", 429, True)
                retry_delay = max(2 ** retries, int(raw_delay or 0))
                if retry_delay > 60:
                    raise ServiceError("RETRY_LATER", "출처의 재시도 대기가 필요합니다.", 429, True)
                self._sleep(retry_delay)
                retries += 1
                continue
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if (status != 200 or content_type not in {"text/html", "application/pdf"}
                    or headers.get("content-encoding", "identity").lower() not in {"", "identity"}):
                raise ServiceError("FETCH_FAILED", "지원하는 문서 응답이 아닙니다.", 502)
            return FetchResult(body, content_type, current_url, status)
