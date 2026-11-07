"""Security tests: binding, the local-only middleware, safe links and redaction.

The middleware is called directly as an ASGI app, so these run without a server.
"""
import asyncio

import pytest

from gui.security import (InsecureBindError, LocalOnlyMiddleware, redact, require_loopback,
                         safe_url)

PORT = 8080


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_hosts_are_allowed(host):
    require_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "example.com", ""])
def test_network_hosts_are_refused(host):
    with pytest.raises(InsecureBindError):
        require_loopback(host)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
async def _inner(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def call(kind="http", host=f"127.0.0.1:{PORT}", origin=None, client="127.0.0.1"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    scope = {"type": kind, "path": "/", "headers": headers, "client": (client, 50000)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    asyncio.run(LocalOnlyMiddleware(_inner, port=PORT)(scope, receive, send))
    return sent


def status(sent):
    first = sent[0]
    if first["type"] == "websocket.accept":
        return "accepted"
    if first["type"] == "websocket.close":
        return "closed"
    return first["status"]


def test_own_page_is_served_with_security_headers():
    sent = call()
    assert status(sent) == 200
    headers = dict(sent[0]["headers"])
    assert headers[b"x-frame-options"] == b"DENY"
    csp = headers[b"content-security-policy"].decode()
    assert "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080"])
def test_own_hosts_pass(host):
    assert status(call(host=host, origin=f"http://{host}")) == 200


@pytest.mark.parametrize("host", ["evil.example:8080", "127.0.0.1:9999", "", "127.0.0.1"])
def test_foreign_host_is_rejected(host):
    """DNS rebinding reaches the server under the attacker's hostname."""
    assert status(call(host=host)) == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:9999",
                                    "http://localhost.evil.example:8080"])
def test_cross_site_origin_is_rejected(origin):
    assert status(call(origin=origin)) == 403


def test_request_from_another_computer_is_rejected():
    assert status(call(client="192.168.1.20")) == 403


def test_websocket_from_own_page_is_accepted():
    assert status(call("websocket", origin=f"http://127.0.0.1:{PORT}")) == "accepted"


def test_cross_site_websocket_is_closed_before_accepting():
    assert status(call("websocket", origin="https://evil.example")) == "closed"


def test_lifespan_events_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(LocalOnlyMiddleware(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# Links and secrets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://www.adzuna.com/land/ad/123?se=abc", "http://example.com/x"])
def test_plain_links_are_kept(url):
    assert safe_url(url) == url


@pytest.mark.parametrize("url", [
    None, "", "javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)",
    "java\tscript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///C:/Windows",
    "vbscript:x", "https://", "//evil.example/x", "https://ex ample.com", "https://example.com/\nx",
    "https://example.com/" + "a" * 3000,
])
def test_unsafe_links_are_dropped(url):
    assert safe_url(url) is None


def test_redact_removes_key_values_and_key_params():
    text = ("GET https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=abc123&app_key=SECRETKEY99&what=x "
            "failed; gemini key AIzaFAKEKEY123456")
    out = redact(text, secrets=["AIzaFAKEKEY123456"])
    assert "SECRETKEY99" not in out and "abc123" not in out and "AIzaFAKEKEY123456" not in out
    assert "what=x" in out


def test_redact_uses_env_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret-value")
    assert "gem-secret-value" not in redact("error: gem-secret-value rejected")
