"""Lodge usage over HTTP: token counts per wolt, counted fresh and read-only.

  GET /api/usage/data   the dashboard JSON the usage page draws
  GET /api/usage        a query (summary, table or limits) as JSON, CSV or text;
                        `woltspace usage` is a thin client of this route

Lodge-private like every lodge route: app subdomains are proxied away before
routing, so a public app share never reaches it. A count reads transcripts and
the saved history and writes nothing; only the wolf's nightly job saves history.
One count runs at a time, off the event loop, and requests that arrive while it
runs share it. A query string is validated before any count starts. Errors are
fixed codes that never echo the request or the reason a count failed.
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import usage_collector
import usage_query

from .config import WOLTS_DIR

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
_flight: asyncio.Future | None = None


def count_usage():
    """One fresh count of this lodge (blocking: a few seconds on a busy lodge)."""
    return usage_collector.count(WOLTS_DIR)


def _landed(flight):
    global _flight
    if _flight is flight:
        _flight = None


async def fresh_count():
    """The running count if there is one, else a new one. A finished count is never reused."""
    global _flight
    if _flight is None:
        _flight = asyncio.ensure_future(run_in_threadpool(count_usage))
        _flight.add_done_callback(_landed)
    return await asyncio.shield(_flight)


def _error(code, status):
    return JSONResponse({"error": code}, status_code=status, headers=NO_STORE)


async def _counted():
    """(data, None) or (None, error response). The log gets the failure's type only."""
    try:
        return await fresh_count(), None
    except usage_collector.HistoryZoneMismatch:
        print("[usage] history was saved in another zone; not re-bucketed")
        return None, _error("history_zone_mismatch", 503)
    except Exception as error:
        print(f"[usage] count failed: {type(error).__name__}")
        return None, _error("count_failed", 503)


@router.get("/api/usage/data")
async def usage_data():
    data, failed = await _counted()
    return failed or JSONResponse(data, headers=NO_STORE)


@router.get("/api/usage")
async def usage_api(request: Request):
    try:
        query = usage_query.http_options(request.url.query)
    except usage_query.BadQuery as error:
        return _error(str(error), 400)
    data, failed = await _counted()
    if failed:
        return failed
    try:
        result = usage_query.execute(data, query)
    except usage_query.BadQuery as error:
        return _error(str(error), 400)
    body, media_type = usage_query.format_result(result, query.output_format)
    return Response(body, media_type=media_type, headers=NO_STORE)
