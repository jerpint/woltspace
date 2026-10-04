"""HTTP and websocket proxying shared by the lodge and app gateway."""

from __future__ import annotations

import asyncio

import httpx
import websockets
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse

from apps import get_app, running_apps


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
    client = httpx.AsyncClient()
    headers = dict(request.headers)
    headers["host"] = f"localhost:{port}"
    try:
        upstream_request = client.build_request(
            request.method, upstream, headers=headers, content=await request.body(),
        )
        response = await client.send(upstream_request, stream=True, follow_redirects=False)
    except httpx.ConnectError:
        await client.aclose()
        return HTMLResponse(f"<h1>Cannot reach {app_name}</h1>", status_code=502)
    excluded = {"transfer-encoding", "content-encoding"}
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
    await ws.accept(subprotocol=subprotocols[0] if subprotocols else None)
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols or None,
            additional_headers={"host": f"localhost:{port}"},
        ) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        await upstream.send(await ws.receive_text())
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        await ws.send_text(message)
                except Exception:
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
