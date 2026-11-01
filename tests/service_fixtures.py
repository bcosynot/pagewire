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
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _Server:
    """Lifecycle shared by both doubles: a real HTTP server on a free local port."""

    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

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
                code, body = outer.handle_post(
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
    _next_id: int = 4790033

    # -- the API -----------------------------------------------------------

    def handle_get(self, path: str, params: dict[str, list[str]]) -> tuple[int, bytes]:
        if path != "/books/api/v1/books":
            return 404, _json({"error": "not_found"})
        refusal = self._refuse(write=False)
        if refusal is not None:
            return refusal
        wanted = params.get("isbns", []) + params.get("ids", [])
        results = [
            book
            for book in self.library.values()
            if str(book["id"]) in wanted or book.get("isbn") in wanted
        ]
        return 200, _json({"results": results, "next": None})

    def handle_post(self, path: str, payload: Any, headers: Any) -> tuple[int, bytes]:
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
    """The Hardcover GraphQL API, as far as identity resolution uses it.

    `catalog` is the public book/edition data: one entry per book, each with its editions
    and their ISBNs. `library` is the owner's own shelves, keyed by the user book id.
    """

    token: str = "hc-token"
    catalog: list[dict[str, Any]] = field(default_factory=list)
    library: dict[int, dict[str, Any]] = field(default_factory=dict)
    read_scope: bool = True
    write_scope: bool = True
    graphql_errors: list[str] | None = None
    requests: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    _next_user_book_id: int = 7001

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

        needs_write = "insert_user_book" in query
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

        if "books(where: {title" in query:
            title = variables.get("title")
            return {
                "books": [
                    {
                        "id": book["id"],
                        "title": book["title"],
                        "editions": [{"id": e["id"]} for e in book["editions"]],
                    }
                    for book in self.catalog
                    if book["title"] == title
                ]
            }

        if "user_books(where:" in query:
            book_id = variables.get("bookId")
            edition_id = variables.get("editionId")
            found = [
                {"id": entry["id"], "status_id": entry.get("status_id", 1)}
                for entry in self.library.values()
                if entry["book_id"] == book_id
                and (edition_id is None or entry.get("edition_id") == edition_id)
            ]
            # Hardcover's `me` answers as a list, which is why autoCalibre-Web reads
            # `response["me"][0]["user_books"]`.
            return {"me": [{"user_books": found, "account_privacy_setting_id": 1}]}

        if "insert_user_book(" in query:
            entry_object = variables.get("object") or {}
            self._next_user_book_id += 1
            entry = {
                "id": self._next_user_book_id,
                "book_id": entry_object.get("book_id"),
                "edition_id": entry_object.get("edition_id"),
                "status_id": entry_object.get("status_id", 1),
                "privacy_setting_id": entry_object.get("privacy_setting_id"),
            }
            self.library[entry["id"]] = entry
            return {"insert_user_book": {"error": None, "user_book": {"id": entry["id"]}}}

        return {}


def _json(payload: Any) -> bytes:
    return json.dumps(payload).encode()
