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
import html
import urllib.parse
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__
from .config import Settings
from .hardcover import HardcoverClient
from .identity import IdentityError, confirm_book, resolve_books
from .ingest import IngestError, normalise, validate
from .readwise import ReadwiseClient
from .storage import Storage

BEARER = "Bearer "


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app around one settings object and one storage file."""
    settings = settings or Settings.from_env()
    store = Storage(settings.db_path)

    # Built once, from the frozen settings. A destination that is switched off is None
    # here, which is what tells identity resolution that it was not configured rather
    # than that it failed.
    readwise = (
        ReadwiseClient(
            settings.readwise_token,
            base_url=settings.readwise_base_url,
            timeout=settings.service_timeout_seconds,
        )
        if settings.readwise_enabled
        else None
    )
    hardcover = (
        HardcoverClient(
            settings.hardcover_token,
            endpoint=settings.hardcover_endpoint,
            timeout=settings.service_timeout_seconds,
        )
        if settings.hardcover_enabled
        else None
    )

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

    # -- the review page ---------------------------------------------------
    #
    # Identity's human half. A row no ISBN can match waits here, and one confirmation is
    # all it should ever need. Both the page and its writes are gated on the same shared
    # token the device uses, for the reason the ingest endpoint gives above: this returns
    # the owner's reading and writes the ids that decide where immutable sittings go, and
    # the relay answers on a bare LAN address.

    def require_review_token(token: str | None) -> str:
        expected = settings.device_token
        if not expected:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="no device token configured; refusing to open the review page",
            )
        if not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
        return token

    @app.get("/review", response_class=HTMLResponse)
    async def review(token: str | None = None) -> HTMLResponse:
        require_review_token(token)
        return HTMLResponse(_review_page(store, token=token or ""))

    @app.post("/review/resolve", response_class=HTMLResponse)
    async def resolve(request: Request) -> RedirectResponse:
        """Re-run automatic matching, which is the point of showing errors at all.

        A 403 on the page is a token to fix; this is what turns the page from a report
        into the next step.
        """
        fields = await _form(request)
        token = require_review_token(fields.get("token"))
        resolve_books(store, readwise=readwise, hardcover=hardcover)
        return _back_to_review(token)

    @app.get("/review/{document}/candidates", response_class=HTMLResponse)
    async def candidates(document: str, token: str | None = None) -> HTMLResponse:
        """What Hardcover has under this book's title, as one-click confirmations.

        A suggestion, never a decision: each candidate is its own form, and pressing it is
        the confirmation. Nothing here writes anything.
        """
        require_review_token(token)
        row = store.book(document)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no book with that document")
        found = hardcover.find_by_title(row["title"]) if (hardcover and row["title"]) else []
        return HTMLResponse(
            _candidates_page(row, found, token=token or "", hardcover_enabled=hardcover is not None)
        )

    @app.post("/review/{document}", response_class=HTMLResponse)
    async def confirm(document: str, request: Request) -> HTMLResponse:
        fields = await _form(request)
        token = require_review_token(fields.get("token"))
        if store.book(document) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no book with that document")
        try:
            confirm_book(
                store,
                document,
                readwise=readwise,
                hardcover=hardcover,
                readwise_id=fields.get("readwise_id") or None,
                hardcover_id=fields.get("hardcover_id") or None,
                hardcover_edition_id=fields.get("hardcover_edition_id") or None,
            )
        except IdentityError as exc:
            return HTMLResponse(_error_page(str(exc)), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return _back_to_review(token)

    return app


async def _form(request: Request) -> dict[str, str]:
    """The form body, parsed by hand.

    FastAPI's `Form(...)` requires `python-multipart` to be installed. Five text fields
    posted by one page do not justify a new runtime dependency in a service that is
    deployed to a NixOS box and already carries no HTTP client beyond `urllib`.
    """
    body = (await request.body()).decode("utf-8", "replace")
    fields = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {key: values[-1] for key, values in fields.items()}


def _back_to_review(token: str) -> RedirectResponse:
    """Post/redirect/get, so a refresh does not re-submit a confirmation."""
    return RedirectResponse(
        url=f"/review?token={urllib.parse.quote(token)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _review_page(store: Storage, *, token: str) -> str:
    """The queue: every row no ISBN could match, plus every row that failed.

    Both kinds belong here for the same reason — they are the rows whose identity is not
    settled — and the error column is what makes criterion 5's refusal something the owner
    can act on rather than a log line.
    """
    waiting = list(store.books_needing_confirmation())
    errored = {row["document"]: row for row in store.books_with_errors()}
    for document, row in errored.items():
        if all(other["document"] != document for other in waiting):
            waiting.append(row)

    escaped_token = html.escape(token, quote=True)
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Pagewire — books to confirm</title>",
        f"<style>{_PAGE_CSS}</style></head><body>",
        "<h1>Books to confirm</h1>",
        (
            f'<p class="lede">{len(waiting)} book(s) no ISBN could match. Confirming one '
            "writes the ids both services issued, and it is then deliverable.</p>"
        ),
        # The retry exists because the common failure is a token: fix it, press this.
        '<form method="post" action="/review/resolve" class="toolbar">',
        f'<input type="hidden" name="token" value="{escaped_token}">',
        "<button>Re-run automatic matching</button>",
        "</form>",
    ]

    if not waiting:
        parts.append("<p>Nothing waiting. Every swept book is identified.</p>")
    else:
        parts.append("<table><thead><tr><th>SD path</th><th>Title</th><th>Author</th>")
        parts.append("<th>ISBN</th><th>Status</th><th>Problem</th><th>Confirm</th></tr></thead><tbody>")
        for row in waiting:
            parts.append(_review_row(row, token=escaped_token))
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _review_row(row: Any, *, token: str) -> str:
    """One queued book, with the form that settles it."""
    document = row["document"]
    problem = row["error"] or ""
    cell = lambda value: html.escape(str(value)) if value is not None else "—"  # noqa: E731
    return (
        "<tr>"
        f"<td><code>{cell(row['book_path'])}</code></td>"
        f"<td>{cell(row['title'])}</td>"
        f"<td>{cell(row['author'])}</td>"
        f"<td>{cell(row['isbn'])}</td>"
        f"<td>{cell(row['status'])}</td>"
        f'<td class="problem">{cell(problem)}</td>'
        "<td>"
        f'<form method="post" action="/review/{html.escape(document, quote=True)}">'
        f'<input type="hidden" name="token" value="{token}">'
        f'<input name="readwise_id" placeholder="readwise id" value="{cell(row["readwise_id"]) if row["readwise_id"] else ""}">'
        f'<input name="hardcover_id" placeholder="hardcover book id" value="{cell(row["hardcover_id"]) if row["hardcover_id"] else ""}">'
        f'<input name="hardcover_edition_id" placeholder="hardcover edition id" value="{cell(row["hardcover_edition_id"]) if row["hardcover_edition_id"] else ""}">'
        "<button>Confirm</button>"
        "</form>"
        f'<a href="/review/{html.escape(document, quote=True)}/candidates?token={token}">'
        "look up the title on Hardcover</a>"
        "</td>"
        "</tr>"
    )


def _candidates_page(row: Any, candidates: list[Any], *, token: str, hardcover_enabled: bool) -> str:
    """Hardcover's exact-title matches, each as a button that confirms it."""
    document = html.escape(str(row["document"]), quote=True)
    title = html.escape(str(row["title"] or "—"))
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>Pagewire — candidates for {title}</title>",
        f"<style>{_PAGE_CSS}</style></head><body>",
        f"<h1>Candidates for {title}</h1>",
        f'<p class="lede"><code>{html.escape(str(row["book_path"] or ""))}</code></p>',
    ]
    if not hardcover_enabled:
        parts.append("<p>Hardcover is switched off, so there is nothing to look up.</p>")
    elif not candidates:
        parts.append(
            "<p>Hardcover has no book under exactly that title. Find it on hardcover.app "
            "and paste its ids into the form instead.</p>"
        )
    else:
        parts.append('<ul class="candidates">')
        for candidate in candidates:
            edition = "" if candidate.edition_id is None else html.escape(str(candidate.edition_id))
            book = html.escape(str(candidate.book_id))
            parts.append(
                "<li>"
                f"<span>{html.escape(str(candidate.title or '—'))}</span> "
                f'<form method="post" action="/review/{document}">'
                f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
                f'<input type="hidden" name="hardcover_id" value="{book}">'
                f'<input type="hidden" name="hardcover_edition_id" value="{edition}">'
                f"<button>Confirm as book {book}"
                f"{'' if not edition else f' / edition {edition}'}</button>"
                "</form></li>"
            )
        parts.append("</ul>")
    parts.append(f'<p><a href="/review?token={html.escape(token, quote=True)}">back to the queue</a></p>')
    parts.append("</body></html>")
    return "\n".join(parts)


def _error_page(message: str) -> str:
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>Pagewire — cannot confirm</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        f"<h1>Cannot confirm that book</h1><p class=\"problem\">{html.escape(message)}</p>"
        "</body></html>"
    )


# Deliberately plain: a table, a monospace path, and one accent. No icon font, no
# framework, nothing fetched from a CDN — this page is served off a LAN address by a
# service that has no other front-end.
_PAGE_CSS = """
:root { color-scheme: light dark; }
body { font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; margin: 2rem auto;
       max-width: 68rem; padding: 0 1rem; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; }
.lede { color: #666; margin-top: 0; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid #8883; 
         vertical-align: top; }
th { font-weight: 600; font-size: .85rem; text-transform: uppercase; letter-spacing: .03em;
     color: #666; }
code { font: .85rem ui-monospace, SFMono-Regular, Menlo, monospace; }
.problem { color: #b3261e; max-width: 22rem; }
form { display: flex; flex-wrap: wrap; gap: .3rem; margin: 0; }
input { font: inherit; padding: .2rem .3rem; width: 9.5rem; }
button { font: inherit; padding: .2rem .6rem; cursor: pointer; }
.toolbar { margin: 1rem 0; }
.candidates { list-style: none; padding: 0; }
.candidates li { display: flex; gap: 1rem; align-items: center; padding: .3rem 0; }
a { color: inherit; }
"""

