"""Security for the local UI.

Threat model: one user on their own laptop. The app holds API keys (.env), a
résumé profile (config.json) and the application tracker (matches.db), and it
displays text from outside sources (Adzuna postings, Gemini reasoning).

What this module guards against, and how:
  - Other devices on the network: the server only listens on 127.0.0.1, and
    require_loopback() refuses to start on any other address.
  - Websites open in your browser (DNS rebinding, cross-site websockets,
    clickjacking): LocalOnlyMiddleware rejects any request whose Host isn't this
    app, or whose Origin is another site, and sets headers that forbid framing
    and block the page from loading or sending data anywhere but this app.
  - Hostile posting text: shown with ui.label (escaped) only; links pass safe_url().
  - Leaking secrets into the UI: redact() scrubs API keys from any text shown.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from urllib.parse import urlsplit

log = logging.getLogger("internship_ui")

HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# NiceGUI's Vue front end compiles templates at run time and uses inline scripts,
# so 'unsafe-eval' and 'unsafe-inline' are required for scripts. Everything else is
# locked to this app: no external scripts, styles, fonts, images or connections,
# so even injected markup couldn't send your data anywhere.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])

SECURITY_HEADERS = [
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]

_LOOPBACK_CLIENTS = {"127.0.0.1", "::1"}


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------
class InsecureBindError(RuntimeError):
    pass


def require_loopback(host: str) -> None:
    """Refuse to serve on anything but this computer.

    The app has no login, which is only safe while nothing else can reach it.
    """
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise InsecureBindError(
        f"Refusing to listen on {host!r}: the app has no login, so it may only listen "
        f"on {HOST} (this computer). Remote access would need a password first.")


# ---------------------------------------------------------------------------
# Request filtering and security headers
# ---------------------------------------------------------------------------
class LocalOnlyMiddleware:
    """ASGI middleware that only lets this app's own pages talk to the server.

    Checks every HTTP request and websocket (the live connection NiceGUI uses):
      - the client must be on this computer
      - Host must be 127.0.0.1:<port> or localhost:<port>; a DNS-rebinding attack
        reaches the server under the attacker's hostname, so it fails here
      - Origin, when the browser sends one, must be this app; another site's
        script trying to open the websocket fails here
    and adds SECURITY_HEADERS to every HTTP response.
    """

    def __init__(self, app, port: int = DEFAULT_PORT):
        self.app = app
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)  # lifespan events

        reason = self._reject_reason(scope)
        if reason:
            log.warning("Blocked %s request to %s: %s", scope["type"], scope.get("path"), reason)
            return await self._reject(scope, send)

        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        return await self.app(scope, receive, send_with_headers)

    def _reject_reason(self, scope) -> str | None:
        client = scope.get("client")
        if not client or client[0] not in _LOOPBACK_CLIENTS:
            return f"client {client[0] if client else '?'} is not this computer"
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if host not in self.allowed_hosts:
            return f"unexpected Host {host!r}"
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return f"cross-site Origin {origin!r}"
        return None

    @staticmethod
    async def _reject(scope, send):
        if scope["type"] == "websocket":
            # Closing before accepting makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b"Forbidden: this app only accepts requests from its own pages on this computer."
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()),
                                *SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Untrusted content
# ---------------------------------------------------------------------------
def safe_url(url: str | None) -> str | None:
    """The URL if it's a plain http(s) link, else None.

    Posting URLs come from Adzuna. Anything else (javascript:, data:, file:, links
    with hidden whitespace or control characters) is never turned into a link.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
SECRET_ENV_VARS = ("GEMINI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")
_KEY_PARAM = re.compile(r"(?i)\b(app_key|app_id|api_key|key)=([^&\s\"']+)")


def secret_values() -> list[str]:
    return [v for v in (os.getenv(name) for name in SECRET_ENV_VARS) if v and len(v) >= 6]


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove API keys from text before it's shown or logged.

    Catches the actual key values from .env, plus anything that looks like a key
    in a URL query string (Adzuna puts app_id/app_key in the URL).
    """
    if not text:
        return text
    for value in sorted(secrets if secrets is not None else secret_values(), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return _KEY_PARAM.sub(lambda m: f"{m.group(1)}=[redacted]", text)
