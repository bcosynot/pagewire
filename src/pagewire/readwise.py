"""The Readwise Books API, as far as identifying a book needs it.

Two calls, both documented at https://readwise.io/books_api:

- `GET /books?isbns=…` asks whether a book is already in the library. That is the read
  half of "does Readwise know this ISBN".
- `POST /books` adds a book and answers with the `book_id` the sessions will be posted
  against. It is **idempotent**: a book already in the library comes back `existing` with
  the same `book_id` and is updated rather than duplicated. That is what lets the relay
  add a book it is unsure about instead of reading it back first — the alternative, a
  read-then-write, races against nothing here but costs a request per book.
- `POST /books` matches `id`, then `isbn`, then `asin`, then `title` with `author`. Only
  the ISBN is treated as confident by the relay's own rule (see `identity.py`); title and
  author are what a *confirmed* row is matched by, because that matching happens at the
  service and can land on the wrong book.

Auth is `Authorization: Token …` for a personal access token, which is what the deployed
relay holds. An OAuth token would use `Bearer` and need the `write` scope for POSTs.

Every failure is a `ReadwiseError` carrying the service's own words and status code. This
matters more than usual: a client that turned a 401 into "empty result" would answer "no
match" for every book in the library and put the whole card into the confirmation queue.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .retry import RETRYABLE_STATUSES

READWISE_BASE_URL = "https://readwise.io"
BOOKS_PATH = "/books/api/v1/books"
SESSIONS_PATH = "/books/api/v1/sessions"

# `POST /sessions` records 1 to 10 finished sessions per request and refuses more. The
# limit is the API's, so it lives here beside the paths rather than in configuration.
MAX_SESSIONS_PER_REQUEST = 10

# The shelf a newly matched book goes on. It is what the owner is actually doing with it:
# the relay only ever identifies books it has seen reading events for.
DEFAULT_SHELF = "currently_reading"


class ReadwiseError(RuntimeError):
    """Readwise refused the call, or could not be reached.

    `status_code` and `code` are kept because they are what the owner needs to see: a 403
    `insufficient_scope` is a token to re-mint, a 401 is a token that is wrong, and a
    connection error is neither.

    `transient` separates "ask again in a moment" from "this is the answer": a 429 or a
    5xx is worth waiting out, and a 403 or a 401 is not. Callers decide; this client does
    not retry anything itself.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.transient = transient


@dataclass(frozen=True)
class SessionRecord:
    """One result of `POST /sessions`: the session id, or why that item was refused.

    Per item, because the endpoint answers per item. A batch can partly succeed, and
    treating a whole batch as failed because one book was wrong would mean re-sending the
    nine that landed — which records them twice.
    """

    session_id: str | None
    error: str | None = None
    error_code: str | None = None

    @property
    def created(self) -> bool:
        return self.session_id is not None and self.error is None


@dataclass(frozen=True)
class ReadwiseBook:
    """A book as Readwise identifies it — the `readwise_id` a row carries.

    Readwise does not expose a per-book web URL on this API, so `book_id` is the whole
    identity: it is what `POST /sessions` is addressed to.
    """

    book_id: str
    title: str | None = None
    author: str | None = None
    status: str | None = None


class ReadwiseClient:
    """The relay's Readwise Books client. No token, no calls."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = READWISE_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def find_by_isbn(self, isbn: str) -> ReadwiseBook | None:
        """The book holding this ISBN, or None when Readwise has no such book."""
        body = self._request("GET", BOOKS_PATH, query={"isbns": isbn})
        results = body.get("results") or []
        if not results:
            return None
        return _book(results[0])

    def add(
        self,
        *,
        isbn: str | None = None,
        title: str | None = None,
        author: str | None = None,
        status: str = DEFAULT_SHELF,
    ) -> ReadwiseBook:
        """Add a book, or update the one that already matched. Returns its `book_id`.

        An ISBN is preferred and is the only thing used when it is known. Without one the
        title and author go instead — the same identity Readwise falls back to internally,
        offered here because a confirmed row has nothing better.
        """
        if isbn:
            product: dict[str, str] = {"isbn": isbn}
        else:
            product = {}
            if title:
                product["title"] = title
            if author:
                product["author"] = author
        if not product:
            raise ReadwiseError(
                "cannot match a book with neither an ISBN nor a title and author"
            )

        body = self._request(
            "POST", BOOKS_PATH, payload={"items": [{"product": product, "status": status}]}
        )
        results = body.get("results") or []
        if not results:
            raise ReadwiseError("Readwise answered a book request with no result")
        result = results[0]
        if result.get("error"):
            error = result["error"] or {}
            raise ReadwiseError(
                f"Readwise refused the book: {error.get('code') or error}",
                code=error.get("code"),
            )
        if result.get("book_id") is None:
            raise ReadwiseError("Readwise answered a book result with no book_id")
        return ReadwiseBook(
            book_id=str(result["book_id"]),
            title=title,
            author=author,
            status=status,
        )

    # -- sessions ----------------------------------------------------------

    def record_sessions(self, sessions: list[dict[str, Any]]) -> list[SessionRecord]:
        """Record finished reading sessions and return one result per session.

        `POST /sessions` is **not** idempotent — the docs say a repeated request records a
        second session — so this is only ever called for targets the caller has already
        claimed in its own send log. That is the send log's whole purpose, and it is why
        this method deliberately does no de-duplication of its own: it cannot know what a
        previous process managed to send.

        A whole-request failure raises `ReadwiseError` carrying the status code, which is
        what tells the caller whether anything may have been written (5xx: unknown) or not
        (4xx: nothing recorded). An item the service refuses comes back as a `SessionRecord`
        with `error` set, because the rest of the batch is still recorded.
        """
        items = [dict(session) for session in sessions]
        if not 1 <= len(items) <= MAX_SESSIONS_PER_REQUEST:
            raise ReadwiseError(
                f"Readwise records 1 to {MAX_SESSIONS_PER_REQUEST} sessions per request,"
                f" got {len(items)}"
            )

        body = self._request("POST", SESSIONS_PATH, payload={"items": items})
        results = body.get("results")
        if not isinstance(results, list) or len(results) != len(items):
            # An answer we cannot line up with what was sent is not a success. Raising
            # leaves the targets claimed-and-unconfirmed, which is the safe state: the next
            # attempt asks rather than re-sends.
            raise ReadwiseError(
                "Readwise answered a sessions request with results that do not match the"
                " number of sessions sent"
            )

        records: list[SessionRecord] = []
        for result in results:
            error = result.get("error")
            if error:
                code = error.get("code") if isinstance(error, dict) else None
                message = (
                    error.get("message") if isinstance(error, dict) else str(error)
                ) or "Readwise refused this session"
                records.append(SessionRecord(session_id=None, error=message, error_code=code))
                continue
            session_id = result.get("session_id")
            if session_id is None:
                records.append(
                    SessionRecord(
                        session_id=None,
                        error="Readwise answered a created session with no session_id",
                    )
                )
                continue
            records.append(SessionRecord(session_id=str(session_id)))
        return records

    def sessions_for_book(
        self, book_id: str, *, started_after: str | None = None
    ) -> list[dict[str, Any]]:
        """The sessions already recorded against one book.

        The reconciliation read: because `POST /sessions` cannot be made idempotent, the
        only way to know whether a claimed session landed is to ask. `started_after` keeps
        the answer small by asking from just before the sitting in question.
        """
        query: dict[str, Any] = {"book_ids": str(book_id)}
        if started_after is not None:
            query["started_after"] = started_after
        body = self._request("GET", SESSIONS_PATH, query=query)
        results = body.get("results")
        return results if isinstance(results, list) else []

    def finish_book(self, book_id: str, *, finished_at: str) -> None:
        """Put a book on the `finished` shelf, with the date the reading ended.

        `PATCH /books` takes the book id the library issued. It answers per item, so a
        refusal has to be read out of the result rather than inferred from the HTTP status.
        """
        try:
            numeric_id = int(book_id)
        except (TypeError, ValueError) as exc:
            raise ReadwiseError(
                f"cannot finish a book whose readwise id is not a number: {book_id!r}"
            ) from exc

        body = self._request(
            "PATCH",
            BOOKS_PATH,
            payload={
                "items": [
                    {
                        "id": numeric_id,
                        "status": "finished",
                        "finished_reading_at": finished_at,
                    }
                ]
            },
        )
        results = body.get("results") or []
        if not results:
            raise ReadwiseError("Readwise answered a book update with no result")
        result = results[0]
        error = result.get("error")
        if error or result.get("status") == "error":
            code = error.get("code") if isinstance(error, dict) else None
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            ) or "Readwise refused the book update"
            raise ReadwiseError(f"Readwise refused the book update: {message}", code=code)

    # -- internals ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.token:
            # Refused here rather than at the service, so an unconfigured destination
            # never presents as a 401 the owner would read as a bad token.
            raise ReadwiseError("no readwise token configured; refusing to call Readwise")

        url = f"{self.base_url}{path}"
        if query:
            # `doseq` so a filter that takes several values (the API's `ids` and `isbns`
            # are arrays) is spelled the way the docs spell it.
            url += "?" + urllib.parse.urlencode(query, doseq=True)

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._refusal(exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ReadwiseError(f"could not reach Readwise: {exc}") from exc

    @staticmethod
    def _refusal(exc: urllib.error.HTTPError) -> ReadwiseError:
        """Turn the documented error body into a message worth showing a human."""
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        code = body.get("error")
        message = f"Readwise refused the call: HTTP {exc.code}"
        if code:
            message += f" {code}"
        detail = body.get("error_description") or body.get("detail")
        if detail:
            message += f" ({detail})"
        if body.get("scope"):
            message += f" [needs scope {body['scope']}]"
        return ReadwiseError(
            message,
            status_code=exc.code,
            code=code,
            transient=exc.code in RETRYABLE_STATUSES,
        )


def _book(result: dict[str, Any]) -> ReadwiseBook:
    """One entry of the `results` envelope, as a book."""
    return ReadwiseBook(
        book_id=str(result["id"]),
        title=result.get("title"),
        author=result.get("author"),
        status=result.get("status"),
    )
