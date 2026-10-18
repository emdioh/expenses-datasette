"""Datasette plugin: reject requests whose Host header is not a loopback name (DNS rebinding).

The server only listens on 127.0.0.1, but a web page on another origin could reach it through a
DNS name that is re-pointed to 127.0.0.1. Such requests carry a foreign Host header, so only
127.0.0.1, localhost and [::1] (any port) are served; everything else gets 403.
"""

from datasette import hookimpl

ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})


def hostname_of(host: str) -> str:
    """Host header without the port: 'localhost:8001' -> 'localhost', '[::1]:8001' -> '[::1]'."""
    host = host.strip().lower()
    if host.startswith("["):  # IPv6 literal
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_allowed_host(host: str | None) -> bool:
    return host is not None and hostname_of(host) in ALLOWED_HOSTNAMES


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        async def host_checked(scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                headers = dict(scope.get("headers") or [])
                host = headers.get(b"host", b"").decode("latin-1") or None
                if not is_allowed_host(host):
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 1008})
                        return
                    await send({
                        "type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                    })
                    await send({"type": "http.response.body", "body": b"Forbidden: bad Host header"})
                    return
            await app(scope, receive, send)

        return host_checked

    return wrap
