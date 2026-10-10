"""HTTP and websocket proxying shared by the lodge and app gateway."""

from __future__ import annotations

import asyncio

import httpx
import websockets
from fastapi import Request, WebSocket, WebSocketDisconnect
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

# Streams (SSE, long polls, downloads) may stay quiet for a long time; only
# connecting to the app and waiting for a pooled connection are bounded.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=10.0)


def _strip_identity_cookies(value: str) -> str:
    """Drop Cloudflare Access cookies; keep every cookie the app set itself."""
    kept = [
        part.strip() for part in value.split(";")
        if part.strip() and not part.strip().lower().startswith("cf_authorization")
    ]
    return "; ".join(kept)


def upstream_headers(headers, email: str | None) -> dict[str, str]:
    """Headers for the app: no visitor credentials, one trusted identity.

    An app must never hold the visitor's Access token. Every app shares one
    Access audience, so a token handed to app A opens app B as that visitor.
    """
    result: dict[str, str] = {}
    for key, value in headers.items():
        name = key.lower()
        if (name in _HOP_BY_HOP or name.startswith("cf-access-")
                or name == IDENTITY_HEADER):
            continue
        if name == "cookie":
            value = _strip_identity_cookies(value)
            if not value:
                continue
        result[name] = value
    if email:
        result[IDENTITY_HEADER] = email
    return result


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
    query = f"?{request.url.query}" if request.url.query else ""
    upstream = f"http://127.0.0.1:{port}{request.url.path}{query}"
    client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
    headers = upstream_headers(request.headers, request.scope.get("state", {}).get("access_email"))
    headers["host"] = f"localhost:{port}"
    try:
        upstream_request = client.build_request(
            request.method, upstream, headers=headers, content=await request.body(),
        )
        response = await client.send(upstream_request, stream=True, follow_redirects=False)
    except httpx.HTTPError:
        await client.aclose()
        return HTMLResponse(f"<h1>Cannot reach {app_name}</h1>", status_code=502)
    excluded = _HOP_BY_HOP | {"content-encoding"}
    response_headers = {
        key: value for key, value in response.headers.items()
        if key.lower() not in excluded | {"set-cookie"}
    }

    async def stream_body():
        try:
            async for chunk in response.aiter_bytes():
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


async def proxy_app_websocket(ws: WebSocket, app_name: str, path: str):
    run_state = running_app(app_name)
    if not run_state:
        await ws.close(code=1008)
        return
    port = run_state["port"]
    subprotocols = [
        value for value in ws.headers.get("sec-websocket-protocol", "").split(", ")
        if value
    ]
    query = f"?{ws.query_params}" if ws.query_params else ""
    upstream_url = f"ws://127.0.0.1:{port}/{path.lstrip('/')}{query}"
    email = ws.scope.get("state", {}).get("access_email")
    identity = {IDENTITY_HEADER: email} if email else {}
    await ws.accept(subprotocol=subprotocols[0] if subprotocols else None)
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols or None,
            additional_headers={"host": f"localhost:{port}", **identity},
        ) as upstream:
            async def client_to_upstream():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await ws.send_bytes(message)
                    else:
                        await ws.send_text(message)

            # Whichever side ends first ends the bridge; the other is cancelled
            # instead of waiting forever on a peer that is gone.
            tasks = {
                asyncio.ensure_future(client_to_upstream()),
                asyncio.ensure_future(upstream_to_client()),
            }
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass
