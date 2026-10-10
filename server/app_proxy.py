"""HTTP and websocket proxying shared by the lodge and app gateway."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, StreamingResponse

from apps import get_app, running_apps


# The one identity header an app may trust. The gateway always sets it after
# its own checks and drops any copy the visitor sent.
IDENTITY_HEADER = "x-woltspace-user"

# Hop-by-hop headers belong to one connection, never forwarded.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

# Once an app has started answering, its stream (SSE, long polls, downloads)
# may stay quiet for as long as it likes. Everything before that is bounded:
# connecting, sending the request body, and the wait for response headers.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
RESPONSE_START_TIMEOUT = 60.0

# Close codes an endpoint may put on the wire (RFC 6455 + IANA registry):
# the assigned protocol codes, then 3000-4999 for libraries and apps.
_SENDABLE_PROTOCOL_CODES = {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}


def _strip_identity_cookies(value: str) -> str:
    """Drop Cloudflare Access cookies; keep every cookie the app set itself."""
    kept = []
    for part in value.split(";"):
        part = part.strip()
        if part and part.partition("=")[0].strip().lower() != "cf_authorization":
            kept.append(part)
    return "; ".join(kept)


def upstream_headers(headers, email: str | None) -> dict[str, str]:
    """Headers for the app: no visitor credentials, one trusted identity.

    An app must never hold the visitor's Access token. Every app shares one
    Access audience, so a token handed to app A opens app B as that visitor.
    """
    result: dict[str, str] = {}
    for key, value in headers.items():
        # Some servers fold "_" into "-", so X_Woltspace_User would join the
        # real header there: compare names with underscores folded.
        name = key.lower().replace("_", "-")
        if (name in _HOP_BY_HOP or name.startswith("cf-access-")
                or name == IDENTITY_HEADER):
            continue
        if name == "cookie":
            value = _strip_identity_cookies(value)
            if not value:
                continue
        result[key.lower()] = value
    if email:
        result[IDENTITY_HEADER] = email
    return result


def _raw_target(scope) -> str:
    """The request target exactly as the visitor sent it (no decoding).

    Rebuilding it from the decoded path turns /a%3Fx into /a?x and
    /dir%2Fsub into /dir/sub.
    """
    raw = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
    target = raw.decode("latin-1")
    query = scope.get("query_string") or b""
    return f"{target}?{query.decode('latin-1')}" if query else target


def _relative_location(location: str, port: int) -> str:
    """Keep a redirect to the app's own loopback address inside the gateway."""
    try:
        parsed = urlsplit(location)
        host, location_port = (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return location
    if host in {"localhost", "127.0.0.1"} and location_port == port:
        return urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    return location


def running_app(name: str) -> dict | None:
    return next((item for item in running_apps() if item.get("name") == name), None)


def _host_only_set_cookie(value: str) -> str:
    """Remove a cookie Domain attribute so an app cannot affect sibling hosts."""
    parts = value.split(";")
    kept = [parts[0]]
    kept.extend(
        part for part in parts[1:]
        if part.strip().partition("=")[0].strip().lower() != "domain"
    )
    return ";".join(kept)


def missing_app_response(name: str, *, unknown_is_stopped: bool = False):
    if not unknown_is_stopped and get_app(name) is None:
        return HTMLResponse("<h1>App not found</h1>", status_code=404)
    if unknown_is_stopped:
        return HTMLResponse(f"<h1>App '{name}' is not running</h1>", status_code=503)
    return HTMLResponse(
        f"<h1>{name} is stopped</h1><p>Ask the lodge owner to start this app.</p>",
        status_code=503,
    )


async def proxy_app_http(
    request: Request, app_name: str, *, unknown_is_stopped: bool = False,
):
    run_state = running_app(app_name)
    if not run_state:
        return missing_app_response(app_name, unknown_is_stopped=unknown_is_stopped)
    port = run_state["port"]
    upstream = f"http://127.0.0.1:{port}{_raw_target(request.scope)}"
    client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
    headers = upstream_headers(request.headers, request.scope.get("state", {}).get("access_email"))
    headers["host"] = f"localhost:{port}"
    try:
        upstream_request = client.build_request(
            # Streamed, never buffered whole: an upload can't fill the gateway.
            request.method, upstream, headers=headers, content=request.stream(),
        )
        response = await asyncio.wait_for(
            client.send(upstream_request, stream=True, follow_redirects=False),
            timeout=RESPONSE_START_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await client.aclose()
        return HTMLResponse(f"<h1>{app_name} did not answer in time</h1>", status_code=504)
    except httpx.HTTPError:
        await client.aclose()
        return HTMLResponse(f"<h1>Cannot reach {app_name}</h1>", status_code=502)
    except BaseException:
        # Cancelled (the visitor left, shutdown): still release the client.
        await client.aclose()
        raise
    # The body is passed through exactly as the app encoded it (aiter_raw), so
    # content-encoding and content-length stay true. Decoding it while keeping
    # the app's content-length truncated every gzip response to nothing.
    response_headers = {
        key: value for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP | {"set-cookie"}
    }
    if "location" in response_headers:
        response_headers["location"] = _relative_location(response_headers["location"], port)

    async def stream_body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    downstream = StreamingResponse(
        stream_body(), status_code=response.status_code, headers=response_headers,
    )
    for cookie in response.headers.get_list("set-cookie"):
        downstream.headers.append("set-cookie", _host_only_set_cookie(cookie))
    return downstream


def _sendable_close_code(code: int | None) -> int:
    """A close code we may forward; anything abnormal becomes 1011."""
    if code is None or code == 1005:
        return 1000
    if code in _SENDABLE_PROTOCOL_CODES or 3000 <= code <= 4999:
        return code
    return 1011


async def proxy_app_websocket(ws: WebSocket, app_name: str, path: str):
    run_state = running_app(app_name)
    if not run_state:
        await ws.close(code=1008)
        return
    port = run_state["port"]
    subprotocols = [
        value.strip()
        for value in ws.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    # The URL names localhost (that is the Host the app sees, once); the
    # socket goes to 127.0.0.1.
    upstream_url = f"ws://localhost:{port}{_raw_target(ws.scope)}"
    email = ws.scope.get("state", {}).get("access_email")
    identity = {IDENTITY_HEADER: email} if email else {}
    accepted = False
    # What the visitor is told when the bridge ends. Default: the app could
    # not be reached or the bridge failed.
    close_code, close_reason = 1011, ""
    try:
        async with websockets.connect(
            upstream_url,
            host="127.0.0.1",
            port=port,
            subprotocols=subprotocols or None,
            additional_headers=identity,
        ) as upstream:
            # Accept only now, with the subprotocol the app actually chose.
            await ws.accept(subprotocol=upstream.subprotocol)
            accepted = True
            async def client_to_upstream():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        code = message.get("code") or 1000
                        await upstream.close(_sendable_close_code(code))
                        return "visitor"
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_text(message)
                except websockets.ConnectionClosed:
                    pass
                return "app"

            # Whichever side ends first ends the bridge. Every child task is
            # cancelled and awaited in `finally`, so a cancelled handler (the
            # visitor's connection dropped, shutdown) never leaves one behind.
            tasks = [
                asyncio.ensure_future(client_to_upstream()),
                asyncio.ensure_future(upstream_to_client()),
            ]
            try:
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in tasks:
                    task.cancel()
                # Retrieves every child's result, so no failure goes unread.
                await asyncio.gather(*tasks, return_exceptions=True)
            ended = next(iter(done))
            if ended.cancelled() or ended.exception() is not None:
                pass  # the bridge itself failed: the visitor gets 1011
            elif ended.result() == "visitor":
                return
            else:
                close_code = _sendable_close_code(upstream.close_code)
                close_reason = upstream.close_reason or ""
    except (OSError, ValueError, websockets.WebSocketException):
        pass
    try:
        if not accepted:
            await ws.close(code=1011)
            return
        await ws.close(code=close_code, reason=close_reason)
    except Exception:
        pass
