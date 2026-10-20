"""Reject cross-origin browser commands, including WebSocket handshakes."""
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class BrowserOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        browser_ws = kind == "websocket" and scope["path"].startswith("/ws/")
        mutation = kind == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS")
        if browser_ws or mutation:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            valid = False
            if origin:
                try:
                    source = urlsplit(origin)
                    scheme = "https" if scope["scheme"] in ("https", "wss") else "http"
                    target = urlsplit(scheme + "://" + headers.get("host", ""))
                    def port(url):
                        return url.port or (443 if url.scheme == "https" else 80)
                    valid = (source.scheme == target.scheme
                             and source.hostname is not None
                             and source.hostname == target.hostname
                             and port(source) == port(target)
                             and source.username is None and source.password is None
                             and not source.path and not source.query and not source.fragment)
                except ValueError:
                    pass
            # Browser terminal sockets always have an Origin. Native HTTP API
            # clients may omit it; Fetch Metadata still rejects cross-site forms.
            denied = (browser_ws and not valid) or (origin is not None and not valid)
            denied = denied or headers.get("sec-fetch-site") == "cross-site"
            if denied:
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"error": "Cross-origin request denied"}, status_code=403)(scope, receive, send)
                return
        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                overrides = {
                    b"x-frame-options": b"DENY",
                    b"content-security-policy": b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"cache-control": b"no-store",
                }
                headers = [(k, v) for k, v in headers if k.lower() not in overrides]
                message = {**message, "headers": headers + list(overrides.items())}
            await send(message)

        await self.app(scope, receive, secured_send if kind == "http" else send)
