# gzip builds a compressed "zip bomb" response.
import gzip

# socket is wrapped to record where connections really go.
import socket

# threading runs the local test server in the background.
import threading

# time makes the slow endpoints slow.
import time

# http.server provides a tiny real HTTP server for tests.
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# pytest provides fixtures and assertions.
import pytest

# The module under test.
from app import safe_fetch
from app.safe_fetch import FetchError, check_url, fetch, is_public_ip


# Fake DNS for test hostnames; everything else uses the real resolver.
FAKE_DNS = {
    "intranet.test": ["192.168.1.10"],
    "metadata.test": ["169.254.169.254"],
    "loopback6.test": ["::1"],
    "mapped.test": ["::ffff:10.0.0.1"],
    "cgnat.test": ["100.64.0.1"],
    "mixed.test": ["93.184.216.34", "10.0.0.1"],
    "public.test": ["93.184.216.34"],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen_hosts: list[str] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status=200, body=b"", headers=None):
        self.send_response(status)
        headers = {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)), **(headers or {})}
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Handler.seen_hosts.append(self.headers.get("Host", ""))
        port = self.server.server_address[1]
        path = self.path
        if path == "/recipe":
            self._send(body=b"<html><title>Pie</title></html>")
        elif path == "/redirect-ok":
            self._send(302, headers={"Location": "/recipe"})
        elif path == "/redirect-private":
            self._send(302, headers={"Location": "http://10.0.0.5/admin"})
        elif path == "/redirect-loopback":
            self._send(301, headers={"Location": f"http://127.0.0.2:{port}/recipe"})
        elif path == "/redirect-file":
            self._send(302, headers={"Location": "file:///etc/passwd"})
        elif path == "/loop":
            self._send(302, headers={"Location": "/loop"})
        elif path == "/big":
            self._send(body=b"x" * (2 * 1024 * 1024))
        elif path == "/big-chunked":
            # No Content-Length: the limit must be enforced while streaming.
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for _ in range(40):
                chunk = b"y" * 65536
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/gzip-bomb":
            body = gzip.compress(b"\0" * (50 * 1024 * 1024))
            self._send(body=body, headers={"Content-Encoding": "gzip"})
        elif path == "/gzip-ok":
            self._send(body=gzip.compress(b"<html>hello gzip</html>"), headers={"Content-Encoding": "gzip"})
        elif path == "/slow":
            time.sleep(2)
            self._send(body=b"late")
        elif path == "/drip":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                self.wfile.write(b"z")
                self.wfile.flush()
                time.sleep(0.05)
        elif path == "/forbidden":
            self._send(403, b"no")
        else:
            self._send(404, b"missing")


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # clients hanging up mid-response is expected in the size-limit tests


@pytest.fixture(scope="module")
def server():
    srv = QuietServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def fake_dns(monkeypatch):
    real = safe_fetch.resolve_host
    monkeypatch.setattr(safe_fetch, "resolve_host", lambda host, port: FAKE_DNS.get(host) or real(host, port))


@pytest.fixture
def local(monkeypatch, server, fake_dns):
    """Treat 127.0.0.1 (our test server) as a 'public' site; every other check stays real."""
    real_check = safe_fetch.is_public_ip
    monkeypatch.setattr(safe_fetch, "is_public_ip", lambda ip: ip == "127.0.0.1" or real_check(ip))
    monkeypatch.setattr(safe_fetch, "ALLOWED_PORTS", {80, 443, server})
    return f"http://127.0.0.1:{server}"


def reason(url, **kwargs):
    with pytest.raises(FetchError) as info:
        fetch(url, kwargs.pop("max_bytes", 1024 * 1024), **kwargs)
    return info.value.reason


# ---- URL and address validation ----

@pytest.mark.parametrize("ip, expected", [
    ("93.184.216.34", True), ("8.8.8.8", True), ("2606:4700::1111", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fd00:ec2::254", False), ("::ffff:127.0.0.1", False),
    ("not-an-ip", False),
])
def test_is_public_ip(ip, expected):
    assert is_public_ip(ip) is expected


@pytest.mark.parametrize("url", [
    "ftp://example.com/recipe", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi",
    "http://", "recipe", "https://user:secret@example.com/", "http://exa mple.com:abc/",
])
def test_invalid_urls_rejected(url):
    with pytest.raises(FetchError) as info:
        check_url(url)
    assert info.value.reason == "invalid_url"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/", "http://2130706433/", "http://0x7f000001/",
    "http://intranet.test/", "http://metadata.test/", "http://loopback6.test/",
    "http://mapped.test/", "http://cgnat.test/", "http://mixed.test/",
    "http://public.test:22/", "http://public.test:8080/",
])
def test_private_and_unsafe_destinations_blocked(url, fake_dns):
    with pytest.raises(FetchError) as info:
        check_url(url)
    assert info.value.reason == "blocked", url


def test_public_host_passes(fake_dns):
    scheme, host, port, path, ips = check_url("https://public.test/pie?x=1")
    assert (scheme, host, port, path, ips) == ("https", "public.test", 443, "/pie?x=1", ["93.184.216.34"])


# ---- Real HTTP behavior against a local server ----

def test_fetch_success_and_redirect(local):
    result = fetch(local + "/redirect-ok", 1024 * 1024)
    assert result.url == local + "/recipe"
    assert result.content_type == "text/html" and result.charset == "utf-8"
    assert b"Pie" in result.body


def test_gzip_is_decoded(local):
    assert fetch(local + "/gzip-ok", 1024 * 1024).body == b"<html>hello gzip</html>"


@pytest.mark.parametrize("path", ["/redirect-private", "/redirect-loopback"])
def test_redirect_to_unsafe_address_blocked(local, path):
    assert reason(local + path) == "blocked"


def test_redirect_to_file_url_blocked(local):
    assert reason(local + "/redirect-file") == "invalid_url"


def test_redirect_loop_stops(local):
    assert reason(local + "/loop") == "redirects"


@pytest.mark.parametrize("path", ["/big", "/big-chunked", "/gzip-bomb"])
def test_oversized_responses_rejected(local, path):
    assert reason(local + path, max_bytes=1024 * 1024) == "too_large"


def test_read_timeout(local, monkeypatch):
    monkeypatch.setattr(safe_fetch, "READ_TIMEOUT", 0.3)
    assert reason(local + "/slow") == "timeout"


def test_overall_deadline_stops_slow_drip(local, monkeypatch):
    # Each byte arrives quickly enough to dodge the read timeout, but the total is too slow.
    monkeypatch.setattr(safe_fetch, "TOTAL_TIMEOUT", 0.5)
    assert reason(local + "/drip") == "timeout"


def test_http_errors_reported_with_status(local):
    with pytest.raises(FetchError) as info:
        fetch(local + "/forbidden", 1024)
    assert (info.value.reason, info.value.status) == ("http", 403)


def test_dns_rebinding_cannot_swap_the_address(local, server, monkeypatch):
    """DNS answers 'public' when checked, then 'private'; we must keep using the checked IP."""
    answers = iter([["127.0.0.1"]] + [["10.0.0.1"]] * 10)
    lookups = []

    def rebinding_dns(host, port):
        lookups.append(host)
        return next(answers)

    connected = []
    real_connect = socket.create_connection
    def recording_connect(address, *args, **kwargs):
        connected.append(address[0])
        return real_connect(address, *args, **kwargs)

    monkeypatch.setattr(safe_fetch, "resolve_host", rebinding_dns)
    monkeypatch.setattr(safe_fetch.socket, "create_connection", recording_connect)
    Handler.seen_hosts.clear()

    result = fetch(f"http://rebind.test:{server}/recipe", 1024 * 1024)
    assert b"Pie" in result.body
    assert lookups == ["rebind.test"]        # resolved exactly once
    assert connected == ["127.0.0.1"]        # connected to the validated IP
    assert Handler.seen_hosts == [f"rebind.test:{server}"]  # Host header kept the real name


def test_redirect_hop_is_resolved_and_checked_again(local, server, monkeypatch):
    """Each redirect is a fresh lookup + check, so a second hop to a private IP is caught."""
    calls = iter([["127.0.0.1"], ["10.0.0.1"]])
    monkeypatch.setattr(safe_fetch, "resolve_host", lambda host, port: next(calls))
    with pytest.raises(FetchError) as info:
        fetch(f"http://hop.test:{server}/redirect-ok", 1024)
    assert info.value.reason == "blocked"
