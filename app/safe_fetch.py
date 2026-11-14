"""Download a web page or image from a user-supplied URL, safely.

Guards against server-side request forgery (SSRF): only public internet
addresses are contacted, every redirect is re-checked, and each connection
goes to the exact IP address we validated (so DNS can't be swapped mid-request).
"""

# http.client gives us low-level control over each HTTP connection.
import http.client

# ipaddress classifies IPs as public, private, loopback, etc.
import ipaddress

# socket resolves hostnames and opens connections.
import socket

# ssl verifies HTTPS certificates.
import ssl

# time enforces an overall deadline.
import time

# zlib decompresses gzip responses with a size cap.
import zlib

# dataclass holds a finished response.
from dataclasses import dataclass

# urllib.parse splits and joins URLs.
from urllib.parse import urljoin, urlsplit


ALLOWED_SCHEMES = {"http": 80, "https": 443}
ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 5.0   # seconds to open a connection
READ_TIMEOUT = 10.0     # seconds of silence before giving up
TOTAL_TIMEOUT = 20.0    # seconds for the whole request, redirects included
CHUNK = 64 * 1024

USER_AGENT = "Mozilla/5.0 (compatible; FamilyTableRecipeImporter/1.0; personal recipe box)"


class FetchError(Exception):
    """A download that failed; `reason` is a short code the UI turns into a message."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class FetchResult:
    url: str            # final URL after redirects
    content_type: str   # e.g. "text/html"
    charset: str | None
    body: bytes


# ---- Address checks ----

def is_public_ip(ip: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # Unwrap IPv4 hidden inside IPv6 (e.g. ::ffff:127.0.0.1).
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    # is_global excludes private, loopback, link-local (incl. 169.254.169.254 metadata),
    # shared/CGNAT, reserved, and unspecified ranges.
    return addr.is_global and not addr.is_multicast


def resolve_host(host: str, port: int) -> list[str]:
    """Look up every IP for a hostname (separate function so tests can stub DNS)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_url(url: str) -> tuple[str, str, int, str, list[str]]:
    """Validate a URL and resolve it; return (scheme, host, port, path, safe IPs)."""
    if len(url) > 2000:
        raise FetchError("invalid_url")
    try:
        parts = urlsplit(url.strip())
        port = parts.port or ALLOWED_SCHEMES.get(parts.scheme.lower(), 0)
    except ValueError:
        raise FetchError("invalid_url")

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").rstrip(".").lower()
    if scheme not in ALLOWED_SCHEMES or not host:
        raise FetchError("invalid_url")
    if parts.username or parts.password:
        raise FetchError("invalid_url")  # no credentials in URLs
    if port not in ALLOWED_PORTS:
        raise FetchError("blocked")

    try:
        host.encode("idna")
        ips = resolve_host(host, port)
    except (UnicodeError, OSError):
        raise FetchError("dns")
    # Reject if ANY address is unsafe; a public name pointing at 10.x is suspicious.
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise FetchError("blocked")

    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path, ips


# ---- Connections pinned to a validated IP ----

class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, ip: str):
        super().__init__(host, port, timeout=CONNECT_TIMEOUT)
        self.pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), CONNECT_TIMEOUT)
        self.sock.settimeout(READ_TIMEOUT)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, ip: str):
        super().__init__(host, port, timeout=CONNECT_TIMEOUT, context=ssl.create_default_context())
        self.pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self.pinned_ip, self.port), CONNECT_TIMEOUT)
        # The certificate is still checked against the real hostname.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(READ_TIMEOUT)


def _open(scheme: str, host: str, port: int, ips: list[str]) -> http.client.HTTPConnection:
    """Connect to the first reachable validated IP."""
    cls = _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    last_error: Exception | None = None
    for ip in ips[:4]:
        conn = cls(host, port, ip)
        try:
            conn.connect()
            return conn
        except (OSError, ssl.SSLError) as exc:
            conn.close()
            last_error = exc
    if isinstance(last_error, (TimeoutError, socket.timeout)):
        raise FetchError("timeout")
    raise FetchError("network")


# ---- Reading with limits ----

def _read_body(resp: http.client.HTTPResponse, max_bytes: int, deadline: float) -> bytes:
    """Stream the body, stopping as soon as it exceeds max_bytes (after decompression)."""
    encoding = (resp.getheader("Content-Encoding") or "identity").lower().strip()
    if encoding not in ("identity", "gzip", "x-gzip"):
        raise FetchError("unsupported")
    length = resp.getheader("Content-Length")
    if encoding == "identity" and length and length.isdigit() and int(length) > max_bytes:
        raise FetchError("too_large")

    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding != "identity" else None
    out = bytearray()
    while True:
        if time.monotonic() > deadline:
            raise FetchError("timeout")
        try:
            chunk = resp.read1(CHUNK) if hasattr(resp, "read1") else resp.read(CHUNK)
        except (TimeoutError, socket.timeout):
            raise FetchError("timeout")
        except (OSError, http.client.HTTPException):
            raise FetchError("network")
        if not chunk:
            break
        if decoder:
            try:
                # max_length stops a tiny "zip bomb" from expanding into gigabytes.
                chunk = decoder.decompress(chunk, max_bytes + 1 - len(out))
            except zlib.error:
                raise FetchError("network")
        out += chunk
        if len(out) > max_bytes:
            raise FetchError("too_large")
    return bytes(out)


def _parse_content_type(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "", None
    media, _, params = value.partition(";")
    charset = None
    for param in params.split(";"):
        key, _, val = param.strip().partition("=")
        if key.lower() == "charset" and val:
            charset = val.strip("\"' ").lower()
    return media.strip().lower(), charset


def fetch(url: str, max_bytes: int, accept: str = "text/html,application/xhtml+xml") -> FetchResult:
    """GET a URL with SSRF protection, redirect re-checks, timeouts, and size limits."""
    deadline = time.monotonic() + TOTAL_TIMEOUT

    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path, ips = check_url(url)
        conn = _open(scheme, host, port, ips)
        try:
            try:
                conn.request("GET", path, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                    "Accept-Language": "en-US,en;q=0.8",
                    "Accept-Encoding": "gzip",
                    "Connection": "close",
                })
                resp = conn.getresponse()
            except (TimeoutError, socket.timeout):
                raise FetchError("timeout")
            except (OSError, ssl.SSLError, http.client.HTTPException):
                raise FetchError("network")

            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise FetchError("http", resp.status)
                url = urljoin(url, location)  # re-validated at the top of the loop
                continue
            if resp.status != 200:
                raise FetchError("http", resp.status)

            media, charset = _parse_content_type(resp.getheader("Content-Type"))
            body = _read_body(resp, max_bytes, deadline)
            return FetchResult(url=url, content_type=media, charset=charset, body=body)
        finally:
            conn.close()

    raise FetchError("redirects")
