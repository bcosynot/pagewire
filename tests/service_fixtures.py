"""Fixtures for identity resolution: the two services, in-process and over real HTTP.

`DeviceServer` (in `device_fixtures`) exists because a stubbed client proves nothing
about the client. The same reasoning applies here, doubled: the relay's whole job at this
point is to get an identity out of Readwise and Hardcover, so both are served by real
HTTP servers speaking the shapes their own documentation specifies — the Readwise Books
API's `Authorization: Token …` and `results` envelope, and Hardcover's GraphQL-over-POST
with its `me` list, `insert_user_book` mutation and `insufficient_scope` refusal.

The shapes are taken from the vendor docs and from autoCalibre-Web's working Hardcover
client (`cps/services/hardcover.py`), not invented: `editions(where: {isbn_13: {_eq: …}})`,
`insert_user_book(object: UserBookCreateInput!)`, `me { user_books }`. A test that passes
against a made-up shape would only prove the fixture agrees with the client.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Hardcover's own status ids, spelled out here rather than imported from the relay: a
# double that read its constants out of the code under test could not disagree with it.
WANT_TO_READ = 1
CURRENTLY_READING = 2
READ_STATUS = 3


def top_level_fields(query: str) -> list[str]:
    """The field names selected at the top level of a GraphQL document.

    These are what Hardcover counts against the burst limit, so a test can assert the
    document costs what it claims to cost. Argument lists are stripped first: `where` and
    `limit` sit inside parentheses and are arguments, not selections, and Hardcover does
    not count them.
    """
    body = query.split("{", 1)[1].rsplit("}", 1)[0]
    without_arguments = re.sub(r"\([^()]*\)", "", body)

    depth = 0
    fields: list[str] = []
    for token in re.findall(r"[{}]|[A-Za-z_][A-Za-z_0-9]*", without_arguments):
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif depth == 0:
            fields.append(token)
    return fields


def normalise_title(value: str) -> str:
    """How Hardcover's search compares titles: case and punctuation do not matter.

    This is the difference the relay depends on. Measured against the real API, an exact
    `title: {_eq: …}` for "The Jinn-Bot of Shantiport" returned five duplicate stubs — no
    author, no cover, no ISBN, zero readers each — and missed the real record entirely
    (Samit Basu, 416pp, ISBN 9781250827517). `search` found it. So the double has to
    normalize too, or it could not show the two apart.
    """
    return " ".join(re.sub(r"[^0-9a-z]+", " ", value.casefold()).split())


def selects(query: str, field: str) -> bool:
    """Whether `field { … }` or `field(args) { … }` is one of a document's selections.

    Distinguishes a selection from an argument: `edition { pages }` selects the field and
    so does `user_book_reads(where: …) { id }`, while `edition_id: {_eq: $editionId}`
    merely names a column inside `where`.
    """
    return re.search(rf"\b{field}\s*(\([^()]*\))?\s*\{{", query) is not None


def object_literal(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """The `object: { field: … }` literal of a mutation, resolved against its variables.

    The relay writes its mutation arguments as an inline object literal, because
    Hardcover's input type name is not documented — so the double has to read the field
    names out of the document to know what the client meant. That is a feature: a document
    that misspells a field is a document whose values the double cannot see, and the test
    that reads them back fails.
    """
    match = re.search(r"object:\s*\{(.*?)\}", query, re.S)
    if match is None:
        return {}
    resolved: dict[str, Any] = {}
    for field, raw in re.findall(
        r"([A-Za-z_][A-Za-z_0-9]*)\s*:\s*(\$[A-Za-z_0-9]+|-?\d+|\"[^\"]*\")", match.group(1)
    ):
        if raw.startswith("$"):
            resolved[field] = variables.get(raw[1:])
        elif raw.startswith('"'):
            resolved[field] = raw.strip('"')
        else:
            resolved[field] = int(raw)
    return resolved


class _Server:
    """Lifecycle shared by both doubles: a real HTTP server on a free local port."""

    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    # Declared for the method below only; each double is a dataclass and gives it a real
    # per-instance list. An annotation rather than an assignment, because a mutable class
    # attribute would be shared by every instance.
    status_plan: list[int]

    def _planned_status(self) -> int | None:
        """The status this request should be answered with instead of handling it."""
        return self.status_plan.pop(0) if self.status_plan else None

    def __enter__(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def address(self) -> str:
        assert self._server is not None, "a server double must be used as a context manager"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # type: ignore[override]
                pass

            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
                parsed = urllib.parse.urlparse(self.path)
                outer.requests.append(parsed.path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                planned = outer._planned_status()
                if planned is not None:
                    self._send(planned, _json({"error": "rate_limited"}))
                    return
                code, body = outer.handle_get(parsed.path, urllib.parse.parse_qs(parsed.query))
                self._send(code, body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                outer.requests.append(urllib.parse.urlparse(self.path).path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = None
                planned = outer._planned_status()
                if planned is not None:
                    self._send(planned, _json({"error": "rate_limited"}))
                    return
                code, body = outer.handle_post(
                    urllib.parse.urlparse(self.path).path, payload, self.headers
                )
                self._send(code, body)

            def do_PATCH(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                outer.requests.append(urllib.parse.urlparse(self.path).path)
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                try:
                    payload = json.loads(raw or b"{}")
                except ValueError:
                    payload = None
                code, body = outer.handle_patch(
                    urllib.parse.urlparse(self.path).path, payload, self.headers
                )
                self._send(code, body)

            def _send(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        return Handler

    # Overridden by each double.
    def handle_get(self, path: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
        raise NotImplementedError

    def handle_post(
        self, path: str, payload: Any, headers: Any
    ) -> tuple[int, bytes]:
        raise NotImplementedError

    def handle_patch(
        self, path: str, payload: Any, headers: Any
    ) -> tuple[int, bytes]:
        raise NotImplementedError


@dataclass
class ReadwiseServer(_Server):
    """The Readwise Books API, as far as identity resolution uses it.

    `library` maps an ISBN to the book that already holds it, which is what `GET /books`
    filters on and what makes `POST /books` answer `existing` instead of adding a copy.
    """

    token: str = "rw-token"
    library: dict[str, dict[str, Any]] = field(default_factory=dict)
    write_allowed: bool = True
    # ISBNs this server refuses outright, so a test can prove one book's failure does not
    # take the rest of the queue with it.
    fail_isbns: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    creations: list[dict[str, Any]] = field(default_factory=list)
    # Status codes to answer with, in order, before the endpoint behaves — consumed by the
    # next request of any method. How a rate limit is simulated: the service refuses with
    # 429 until the plan runs out, without a flaky network or a real quota.
    status_plan: list[int] = field(default_factory=list)
    # Sessions recorded through `POST /sessions`, verbatim, in arrival order. The relay's
    # whole delivery half is about what it puts in these bodies.
    sessions: list[dict[str, Any]] = field(default_factory=list)
    # Every attempt that reached the session endpoint, including ones answered with a
    # failure, so a test can read the retry log the way the owner would.
    session_attempts: list[dict[str, Any]] = field(default_factory=list)
    # Status codes to answer with, in order, before the endpoint behaves. This is how a
    # 5xx-then-success retry is simulated without a flaky network.
    fail_plan: list[int] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    _next_id: int = 4790033
    _next_session_id: int = 90211876

    # -- the API -----------------------------------------------------------

    def handle_get(self, path: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
        refusal = self._refuse(write=False)
        if refusal is not None:
            return refusal

        if path == "/books/api/v1/books":
            wanted = params.get("isbns", []) + params.get("ids", [])
            results = [
                book
                for book in self.library.values()
                if str(book["id"]) in wanted or book.get("isbn") in wanted
            ]
            return 200, _json({"results": results, "next": None})

        if path == "/books/api/v1/sessions":
            # The reconciliation read: what the relay asks when it does not know whether
            # a claimed session landed. Filters are the documented ones.
            wanted_books = {str(v) for v in params.get("book_ids", [])}
            wanted_ids = {str(v) for v in params.get("ids", [])}
            after = params.get("started_after", [None])[0]
            results = [
                session
                for session in self.sessions
                if (not wanted_books or str(session["book_id"]) in wanted_books)
                and (not wanted_ids or str(session["id"]) in wanted_ids)
                and (after is None or session["started_at"] >= after)
            ]
            return 200, _json({"results": results, "next": None})

        return 404, _json({"error": "not_found"})

    def handle_post(self, path: str, payload: Any, headers: Any) -> tuple[int, bytes]:
        if path == "/books/api/v1/sessions":
            return self._record_sessions(payload)
        if path != "/books/api/v1/books":
            return 404, _json({"error": "not_found"})
        refusal = self._refuse(write=True)
        if refusal is not None:
            return refusal

        results = []
        for item in (payload or {}).get("items", []):
            self.creations.append(item)
            identity = item.get("product") or {}
            isbn = identity.get("isbn")
            if isbn in self.fail_isbns:
                return 500, _json(
                    {
                        "error": "internal_error",
                        "error_description": f"cannot add {isbn} right now",
                    }
                )
            existing = self.library.get(isbn) if isbn else None
            if existing is not None:
                # The documented behaviour when the matched product is already in the
                # library: update that book and answer `existing`, never a second copy.
                results.append(
                    {
                        "status": "existing",
                        "matched_by": "isbn",
                        "book_id": existing["id"],
                        "error": None,
                    }
                )
                continue
            self._next_id += 1
            book = {
                "id": self._next_id,
                "title": identity.get("title") or (existing or {}).get("title"),
                "author": identity.get("author"),
                "isbn": isbn,
                "status": item.get("status", "want_to_read"),
                "product": {"isbn": isbn} if isbn else None,
            }
            if isbn:
                self.library[isbn] = book
            results.append(
                {
                    "status": "created",
                    # An item with no ISBN is still added, with `matched_by` null: the
                    # catalog match happens in the background against title and author.
                    "matched_by": "isbn" if isbn else None,
                    "book_id": book["id"],
                    "error": None,
                }
            )
        return 200, _json({"results": results})

    def handle_patch(self, path: str, payload: Any, headers: Any) -> tuple[int, bytes]:
        """`PATCH /books`: the shelf and finished date, per the documented shape."""
        if path != "/books/api/v1/books":
            return 404, _json({"error": "not_found"})
        refusal = self._refuse(write=True)
        if refusal is not None:
            return refusal

        results = []
        for item in (payload or {}).get("items", []):
            self.patches.append(item)
            book = next(
                (b for b in self.library.values() if str(b["id"]) == str(item.get("id"))),
                None,
            )
            if book is None:
                results.append(
                    {
                        "id": item.get("id"),
                        "status": "error",
                        "error": {"code": "not_found", "message": "No such book."},
                    }
                )
                continue
            for field_name in ("status", "finished_reading_at", "started_reading_at"):
                if field_name in item:
                    book[field_name] = item[field_name]
            results.append({"id": book["id"], "status": "updated", "error": None})
        return 200, _json({"results": results})

    def _record_sessions(self, payload: Any) -> tuple[int, bytes]:
        """`POST /sessions`, with the documented constraints enforced.

        Recorded verbatim, because the point of the relay's payload is what it says. The
        constraints are the ones the docs state: 1–10 items, `ebook` requires the percent
        pair, `ended_at` no more than 24 hours after `started_at`.
        """
        items = (payload or {}).get("items", [])
        # Recorded before the refusal check: an attempt that was turned away is exactly
        # what a retry log is made of.
        self.session_attempts.append({"items": items})

        refusal = self._refuse(write=True)
        if refusal is not None:
            self.session_attempts[-1]["status_code"] = refusal[0]
            return refusal

        if not 1 <= len(items) <= 10:
            return 422, _json(
                {
                    "error": "invalid_value",
                    "error_description": "items must hold 1 to 10 sessions",
                }
            )

        planned = self.fail_plan.pop(0) if self.fail_plan else None
        if planned is not None:
            self.session_attempts[-1]["status_code"] = planned
            return planned, _json(
                {"error": "internal_error", "error_description": "the service said no"}
            )

        results = []
        for item in items:
            missing = [
                name
                for name in ("book_id", "started_at", "ended_at", "format")
                if item.get(name) is None
            ]
            if item.get("format") == "ebook":
                missing += [
                    name
                    for name in ("percent_at_start", "percent_at_end")
                    if item.get(name) is None
                ]
            if missing:
                results.append(
                    {
                        "status": "error",
                        "session_id": None,
                        "error": {"code": "missing", "message": ", ".join(missing)},
                    }
                )
                continue

            # "The book has to be in your library already. A book_id that is not in your
            # library results in an error with code not_found."
            if int(item["book_id"]) not in {int(book["id"]) for book in self.library.values()}:
                results.append(
                    {
                        "status": "error",
                        "session_id": None,
                        "error": {
                            "code": "not_found",
                            "message": "No book with that id is in your library.",
                        },
                    }
                )
                continue

            self._next_session_id += 1
            session = dict(item)
            session["id"] = self._next_session_id
            self.sessions.append(session)
            results.append(
                {"status": "created", "session_id": self._next_session_id, "error": None}
            )

        self.session_attempts[-1]["status_code"] = 200
        return 200, _json({"results": results})

    def _refuse(self, *, write: bool) -> tuple[int, bytes] | None:
        """The documented failures: `unauthenticated`, then `insufficient_scope`.

        The token arrives in the `Authorization` header as `Token …` for a personal access
        token, and an OAuth token must carry `write` for POST — the same 403 shape
        criterion 5 names.
        """
        authorization = next(
            (h.get("authorization") for h in reversed(self.headers) if "authorization" in h),
            None,
        )
        if authorization != f"Token {self.token}":
            return 401, _json({"error": "unauthenticated"})
        if write and not self.write_allowed:
            return 403, _json(
                {
                    "error": "insufficient_scope",
                    "error_description": "OAuth token is missing the write scope",
                    "scope": "write",
                }
            )
        return None


@dataclass
class HardcoverServer(_Server):
    """The Hardcover GraphQL API, as far as identity resolution and delivery use it.

    `catalog` is the public book/edition data: one entry per book, each with its editions,
    their ISBNs and their page counts. `library` is the owner's own shelves, keyed by the
    user book id; an entry may carry a `user_book_reads` list, and `status_id`.

    Two documented behaviours are modelled rather than stubbed, because the relay's
    delivery rules are about them and a double that could not show the difference would
    prove nothing: the server keeps reads in step with the entry's status ("setting 2
    starts a read, 3 finishes it", and progress reaching the edition's last page finishes
    the read on its own), and it writes journal entries itself.

    `journals` exists for the same reason inverted: the relay must never create a journal
    entry, and an absence can only be evidence if the double is able to record one.
    """

    token: str = "hc-token"
    catalog: list[dict[str, Any]] = field(default_factory=list)
    library: dict[int, dict[str, Any]] = field(default_factory=dict)
    read_scope: bool = True
    write_scope: bool = True
    graphql_errors: list[str] | None = None
    # A mutation's own `error` field, for the failures Hardcover reports inside an HTTP
    # 200 rather than as a status code. `write_error` refuses every write; `finish_error`
    # refuses only the status change, which is how the two halves are shown to land
    # independently.
    write_error: str | None = None
    finish_error: str | None = None
    requests: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    # Status codes to answer with, in order, before the endpoint behaves — consumed by the
    # next request of any method. How a rate limit is simulated: the service refuses with
    # 429 until the plan runs out, without a flaky network or a real quota.
    status_plan: list[int] = field(default_factory=list)
    # The variables each query arrived with, so a test can read the request log the way
    # the owner would: which mutation, with which numbers.
    variables: list[dict[str, Any]] = field(default_factory=list)
    # Every `insert_reading_journal` that reached the server. The relay's job is to leave
    # this empty — "the server writes those entries itself, or they appear twice".
    journals: list[dict[str, Any]] = field(default_factory=list)
    _next_user_book_id: int = 7001
    _next_read_id: int = 90001
    _next_journal_id: int = 5001

    # -- what the relay's writes did, for a test to read back ----------------

    @property
    def progress_writes(self) -> list[dict[str, Any]]:
        """Each `update_user_book_read` that landed: the document, its variables, and the
        `object` they spell out — the request log, read the way the owner would."""
        return [
            {
                "query": query,
                "variables": variables,
                "object": object_literal(query, variables),
            }
            for query, variables in zip(self.queries, self.variables)
            if "update_user_book_read(" in query
        ]

    def reads(self, user_book_id: int) -> list[dict[str, Any]]:
        """The reads hanging off one library entry, as a flat list."""
        entry = self.library.get(user_book_id) or {}
        return list(entry.get("user_book_reads") or [])

    def edition_pages(self, edition_id: Any) -> int | None:
        for book in self.catalog:
            for edition in book.get("editions") or []:
                if edition.get("id") == edition_id:
                    return edition.get("pages")
        return None

    # -- the API -----------------------------------------------------------

    def handle_get(self, path: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
        return 404, _json({"error": "not_found"})

    def handle_post(self, path: str, payload: Any, headers: Any) -> tuple[int, bytes]:
        if path != "/v1/graphql":
            return 404, _json({"error": "not_found"})
        authorization = next(
            (h.get("authorization") for h in reversed(self.headers) if "authorization" in h),
            None,
        )
        if authorization != f"Bearer {self.token}":
            return 401, _json(
                {"error": "invalid_token", "error_description": "Missing or invalid token"}
            )

        query = str((payload or {}).get("query", ""))
        variables = (payload or {}).get("variables") or {}
        self.queries.append(query)
        self.variables.append(variables)

        # Every documented mutation needs `write:library`, so each one is named here
        # rather than only the first the relay happened to grow.
        needs_write = any(
            mutation in query
            for mutation in (
                "insert_user_book",
                "update_user_book",
                "insert_reading_journal",
            )
        )
        if not self.read_scope or (needs_write and not self.write_scope):
            return 403, _json(
                {
                    "error": "insufficient_scope",
                    "error_description": "token is missing a scope this request needs",
                    "scope": "write:library" if needs_write else "read:library",
                }
            )

        if self.graphql_errors:
            # Hardcover answers some failures with HTTP 200 and an `errors` array.
            return 200, _json({"errors": [{"message": m} for m in self.graphql_errors]})

        return 200, _json({"data": self._data(query, variables)})

    def _book_view(self, book: dict[str, Any]) -> dict[str, Any]:
        """One catalog entry as the relay's title lookup selects it.

        The catalog entries the tests write are deliberately thin — id, title, editions —
        and everything else is absent unless a test set it, because a double that invented
        an author would let a page look right while showing nothing.
        """
        editions = book.get("editions") or []
        return {
            "id": book["id"],
            "title": book["title"],
            "subtitle": book.get("subtitle"),
            "description": book.get("description"),
            "release_year": book.get("release_year"),
            "pages": book.get("pages"),
            "users_count": book.get("users_count", 0),
            "users_read_count": book.get("users_read_count", 0),
            "ratings_count": book.get("ratings_count", 0),
            "editions_count": book.get("editions_count", len(editions)),
            "is_partial_book": book.get("is_partial_book", False),
            "default_ebook_edition_id": book.get("default_ebook_edition_id"),
            "image": book.get("image"),
            "contributions": (
                [{"author": {"name": book["author"]}}] if book.get("author") else []
            ),
            "editions": [
                {
                    "id": e["id"],
                    "isbn_13": e.get("isbn_13"),
                    "isbn_10": e.get("isbn_10"),
                    "pages": e.get("pages"),
                    "edition_format": e.get("edition_format"),
                    "release_year": e.get("release_year"),
                }
                for e in editions
            ],
        }

    def _data(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "editions(where: {isbn_13" in query:
            isbn = variables.get("isbn")
            return {
                "editions": [
                    {"id": edition["id"], "book_id": book["id"]}
                    for book in self.catalog
                    for edition in book["editions"]
                    if edition.get("isbn_13") == isbn
                ]
            }

        if "search(" in query:
            # Hardcover's own search. Normalising here is what makes it different from an
            # exact match, which is the whole reason the relay asks it instead.
            wanted = normalise_title(str(variables.get("query") or ""))
            found = [b for b in self.catalog if normalise_title(str(b["title"])) == wanted]
            return {"search": {"ids": [b["id"] for b in found]}}

        if "books(where: {id" in query:
            wanted = set(variables.get("ids") or [])
            return {
                "books": [
                    self._book_view(book)
                    for book in self.catalog
                    if book["id"] in wanted
                ]
            }

        if "user_books(where:" in query:
            book_id = variables.get("bookId")
            edition_id = variables.get("editionId")
            found = [
                self._entry_view(entry, query=query)
                for entry in self.library.values()
                if entry["book_id"] == book_id
                and (edition_id is None or entry.get("edition_id") == edition_id)
            ]
            # Hardcover's `me` answers as a list, which is why autoCalibre-Web reads
            # `response["me"][0]["user_books"]`.
            return {"me": [{"user_books": found, "account_privacy_setting_id": 1}]}

        if "update_user_book_read(" in query:
            return {"update_user_book_read": self._update_read(query, variables)}

        if "update_user_book(" in query:
            return {"update_user_book": self._update_library_entry(query, variables)}

        if "insert_reading_journal(" in query:
            entry_object = variables.get("object") or {}
            self._next_journal_id += 1
            journal = {
                "id": self._next_journal_id,
                "book_id": entry_object.get("book_id"),
                "event": entry_object.get("event"),
                "entry": entry_object.get("entry"),
                "action_at": entry_object.get("action_at"),
            }
            self.journals.append(journal)
            return {
                "insert_reading_journal": {
                    "errors": None,
                    "id": journal["id"],
                    "reading_journal": {"id": journal["id"], "entry": journal["entry"]},
                }
            }

        if "insert_user_book(" in query:
            entry_object = variables.get("object") or {}
            self._next_user_book_id += 1
            entry = {
                "id": self._next_user_book_id,
                "book_id": entry_object.get("book_id"),
                "edition_id": entry_object.get("edition_id"),
                "status_id": entry_object.get("status_id", 1),
                "privacy_setting_id": entry_object.get("privacy_setting_id"),
                "user_book_reads": [],
            }
            self.library[entry["id"]] = entry
            # "Setting a library entry's status to Currently Reading (2) starts a read."
            if entry["status_id"] == CURRENTLY_READING:
                self._start_read(entry)
            return {"insert_user_book": {"error": None, "user_book": {"id": entry["id"]}}}

        return {}

    # -- the library, read the way the documented queries read it ------------

    def _entry_view(self, entry: dict[str, Any], *, query: str) -> dict[str, Any]:
        """One `user_books` row, with the sub-selections the query actually asked for.

        Gated on what was selected on purpose: a double that volunteered fields the
        document never asked for would hide a client reading something it did not request.
        """
        view: dict[str, Any] = {
            "id": entry["id"],
            "status_id": entry.get("status_id", WANT_TO_READ),
        }
        if selects(query, "user_book_reads"):
            if "{_is_null: true}" in query:
                chosen = [
                    read
                    for read in self.reads(entry["id"])
                    if read.get("finished_at") is None
                ]
            else:
                chosen = self.reads(entry["id"])
            view["user_book_reads"] = [
                self._read_view(read, query=query)
                for read in sorted(chosen, key=lambda r: r["id"], reverse=True)[:1]
            ]
        return view

    def _read_view(self, read: dict[str, Any], *, query: str) -> dict[str, Any]:
        """One `user_book_reads` row, whose edition is the read's own edition."""
        view: dict[str, Any] = {
            "id": read["id"],
            "started_at": read.get("started_at"),
            "finished_at": read.get("finished_at"),
            "edition_id": read.get("edition_id"),
            "progress_pages": read.get("progress_pages"),
        }
        if selects(query, "edition"):
            view["edition"] = {
                "id": read.get("edition_id"),
                "pages": self.edition_pages(read.get("edition_id")),
            }
        return view

    def _start_read(self, entry: dict[str, Any]) -> dict[str, Any]:
        """A new `user_book_reads` row, which is what status Currently Reading means."""
        self._next_read_id += 1
        read = {
            "id": self._next_read_id,
            "started_at": "2025-10-09",
            "finished_at": None,
            "edition_id": entry.get("edition_id"),
            "progress_pages": 0,
        }
        entry.setdefault("user_book_reads", []).append(read)
        return read

    def _find_read(self, read_id: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for entry in self.library.values():
            for read in entry.get("user_book_reads") or []:
                if read["id"] == read_id:
                    return entry, read
        return None

    def _update_read(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """`update_user_book_read`, including the server's own progress-to-end rule."""
        if self.write_error is not None:
            return {"error": self.write_error, "user_book_read": None}
        found = self._find_read(variables.get("id"))
        if found is None:
            return {"error": "no such read", "user_book_read": None}
        entry, read = found
        entry_object = object_literal(query, variables)
        if "progress_pages" in entry_object:
            read["progress_pages"] = entry_object["progress_pages"]
        if "action_at" in entry_object:
            read["action_at"] = entry_object["action_at"]

        # "Progress that reaches the end of the edition finishes the read and marks the
        # book Read" — the server does this, not the client.
        pages = self.edition_pages(read.get("edition_id"))
        if pages and (read.get("progress_pages") or 0) >= pages:
            read["finished_at"] = read.get("finished_at") or "2025-10-09"
            entry["status_id"] = READ_STATUS
        return {
            "error": None,
            "user_book_read": {
                "id": read["id"],
                "progress_pages": read.get("progress_pages"),
                "started_at": read.get("started_at"),
                "finished_at": read.get("finished_at"),
            },
        }

    def _update_library_entry(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """`update_user_book`: the status change that starts or finishes a read."""
        if self.write_error is not None:
            return {"error": self.write_error, "user_book": None}
        if self.finish_error is not None:
            return {"error": self.finish_error, "user_book": None}
        user_book_id = variables.get("id")
        entry = self.library.get(user_book_id) if isinstance(user_book_id, int) else None
        if entry is None:
            return {"error": "no such user book", "user_book": None}
        entry_object = object_literal(query, variables)
        if "status_id" in entry_object:
            entry["status_id"] = entry_object["status_id"]
            if entry["status_id"] == READ_STATUS:
                for read in entry.get("user_book_reads") or []:
                    if read.get("finished_at") is None:
                        read["finished_at"] = "2025-10-09"
            elif entry["status_id"] == CURRENTLY_READING and not [
                read
                for read in entry.get("user_book_reads") or []
                if read.get("finished_at") is None
            ]:
                self._start_read(entry)
        return {
            "error": None,
            "user_book": {"id": entry["id"], "status_id": entry["status_id"]},
        }


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode()
