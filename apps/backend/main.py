"""
apps/backend/main.py — Eagle FastAPI backend entry point.

Phase 5 scaffold: health check + active tracks endpoint.

Endpoints
---------
GET /health
    Returns server + Redis connectivity status.
    Responds with {"status": "ok", "redis": "connected"} when healthy,
    or {"status": "degraded", "redis": "<error>"} when Redis is unreachable.

GET /tracks?camera_id=cam_01
    Returns active track IDs for the given camera by scanning Redis keys
    matching the pattern ``track:{camera_id}:*``.

GET /metrics
    Prometheus metrics scrape endpoint.

Import note
-----------
Routers are imported with a try/except so this module works both when run
from the project root (tests, local dev) and inside the Docker image where
the build context is ``apps/backend/`` and only that directory is on sys.path.
"""
from __future__ import annotations

import json
import logging
import os
import re

import redis as redis_sync
from fastapi import FastAPI, Query
from fastapi.responses import Response
from prometheus_client import generate_latest

try:
    # Inside Docker: build context is apps/backend/, only that dir is on sys.path.
    from routes.cameras import identity_router, router as cameras_router
    from routes.feedback import router as feedback_router
except ModuleNotFoundError:
    # From project root (tests, local dev): use the full package path.
    from apps.backend.routes.cameras import identity_router, router as cameras_router  # noqa: F401
    from apps.backend.routes.feedback import router as feedback_router  # noqa: F401

from libs.observability.metrics import frames_processed_total

logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Eagle Surveillance API",
    description="Real-time semantic surveillance — detection, tracking, and reasoning.",
    version="0.1.0",
)

# ── Redis (sync client for simple health / track-list queries) ────────────────

REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")

_redis: redis_sync.Redis | None = None

# Regex for safe camera_id values — prevents Redis glob injection.
_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _get_redis() -> redis_sync.Redis | None:
    """
    Return a lazily-initialised sync Redis client, or None if the connection
    has never succeeded.  Errors are logged but never raised — callers must
    handle a None return.

    Both ``socket_connect_timeout`` and ``socket_timeout`` are set so a
    stalled Redis server cannot block a worker indefinitely.
    """
    global _redis
    if _redis is None:
        try:
            client = redis_sync.from_url(
                REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            _redis = client
            logger.info("Redis connected at %s", REDIS_URL)
        except Exception as exc:
            logger.warning("Redis unavailable: %s", exc)
    return _redis


# Attempt connection at startup (non-fatal if Redis is down).
_get_redis()

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(cameras_router)
app.include_router(identity_router)
app.include_router(feedback_router)


# ── Health endpoint ───────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
def health() -> dict:
    """
    Return server and Redis health.

    Synchronous handler — avoids blocking the event loop with sync Redis
    calls (ping).

    Responses
    ---------
    Healthy::

        {"status": "ok", "redis": "connected"}

    Degraded (Redis unreachable)::

        {"status": "degraded", "redis": "<error message>"}
    """
    r = _get_redis()
    if r is None:
        return {"status": "degraded", "redis": "unavailable"}

    try:
        r.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as exc:
        # Redis was reachable at startup but is now down — reset so the
        # next request triggers a fresh connection attempt.
        global _redis
        _redis = None
        return {"status": "degraded", "redis": str(exc)}


# ── Tracks endpoint ───────────────────────────────────────────────────────────

@app.get("/tracks", tags=["tracks"])
def list_active_tracks(
    camera_id: str = Query(
        default="cam_01",
        description="Camera identifier",
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
) -> dict:
    """
    Return active track IDs for a camera.

    Synchronous handler — avoids blocking the event loop with sync Redis
    calls (scan_iter, get).

    Scans Redis for keys matching ``track:{camera_id}:*`` using a
    cursor-based SCAN (safe for large keyspaces) and returns the integer
    track IDs whose stored state is ``ACTIVE``.

    Query Parameters
    ----------------
    camera_id : str
        Camera identifier restricted to ``[A-Za-z0-9_-]`` to prevent
        Redis glob injection (default: ``cam_01``).

    Responses
    ---------
    Redis healthy::

        {"camera_id": "cam_01", "track_ids": [1, 3, 7]}

    Redis unavailable::

        {"camera_id": "cam_01", "track_ids": [], "error": "Redis unavailable"}
    """
    r = _get_redis()
    if r is None:
        return {"camera_id": camera_id, "track_ids": [], "error": "Redis unavailable"}

    try:
        pattern = f"track:{camera_id}:*"
        active_ids: list[int] = []

        # scan_iter uses cursor-based SCAN — O(1) per batch, never blocks Redis.
        for key in r.scan_iter(match=pattern, count=500):
            raw = r.get(key)
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.debug("Skipping corrupt record at key %s", key)
                continue

            # Guard against valid JSON that is not an object (e.g. list/string).
            if not isinstance(record, dict):
                logger.debug("Skipping non-object record at key %s", key)
                continue

            if record.get("state") != "ACTIVE":
                continue

            # Guard against missing or malformed track_id in a single record.
            try:
                active_ids.append(int(record.get("track_id")))
            except (TypeError, ValueError):
                logger.debug("Skipping record with invalid track_id: %s", record.get("track_id"))
                continue

        return {"camera_id": camera_id, "track_ids": sorted(active_ids)}

    except Exception as exc:
        logger.error("Failed to list tracks for %s: %s", camera_id, exc)
        # Reset stale client so the next request retries the connection.
        global _redis
        _redis = None
        return {"camera_id": camera_id, "track_ids": [], "error": str(exc)}


# ── Metrics endpoint ──────────────────────────────────────────────────────────

@app.get("/metrics", tags=["ops"], include_in_schema=False)
def metrics() -> Response:
    """Prometheus metrics scrape endpoint."""
    return Response(generate_latest(), media_type="text/plain")
