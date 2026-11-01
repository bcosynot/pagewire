"""The relay's HTTP surface: one ingest endpoint and a health check.

Deliberately a factory rather than a module-level `app = FastAPI()`, because creating the
storage at import time would make importing this module a side effect — it would create a
database file wherever Python happened to be. Run it with uvicorn's factory flag:

    uvicorn pagewire.app:create_app --factory

There is no KOSync endpoint here and there never will be: the design decided against
serving KOSync, so nothing in this service touches autoCalibre-Web.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .ingest import IngestError, normalise, validate
from .storage import Storage

BEARER = "Bearer "


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app around one settings object and one storage file."""
    settings = settings or Settings.from_env()
    store = Storage(settings.db_path)

    app = FastAPI(title="Pagewire relay", version=__version__)

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = settings.device_token
        if not expected:
            # Refuse rather than accept. An endpoint that writes to the database and is
            # reachable on the LAN with no credential is worse than one that is obviously
            # misconfigured, and a misconfiguration should be loud.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no device token configured; refusing to accept events",
            )
        if not authorization or not authorization.startswith(BEARER):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
        if not hmac.compare_digest(authorization[len(BEARER):], expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "events": store.count_events(),
            "destinations": {
                "readwise": settings.readwise_enabled,
                "hardcover": settings.hardcover_enabled,
            },
        }

    @app.post("/events/session", dependencies=[Depends(require_token)])
    async def ingest(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001 — any parse failure is the same answer
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not a JSON object")

        raw = payload
        payload = normalise(payload)
        try:
            validate(payload)
        except IngestError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"error": str(exc), "field": exc.field},
            ) from exc

        # The normalised payload fills the columns; the raw body is what gets stored, so
        # the text the device actually sent survives for a later re-derivation.
        stored = store.store_event(payload, raw=raw)
        # A duplicate is acknowledged, never rejected. Delivery is at-least-once, the
        # device's outbox only clears once this answers, and the spike caught the device
        # sending the same event twice — so a repeat is the contract, not a fault.
        return JSONResponse({"ok": True, "stored": stored, "id": payload["id"]})

    return app
