"""The Hardcover GraphQL API, as far as identifying a book and writing its progress need it.

Endpoint `https://api.hardcover.app/v1/graphql`, `Authorization: Bearer ***,
one document per request. Four things shape this code.

**`editions` carries `book_id`.** So `editions(where: {isbn_13: {_eq: …}})` answers with
the edition *and* the book it belongs to in one field. The alternative — walking
`books(where: {slug: …}) { editions }`, which is what autoCalibre-Web does — needs a slug
the relay has no way to derive from an ISBN.

**Top-level fields are rate limit, not style.** Hardcover's Free plan allows a burst of 10
top-level fields per request and rejects anything larger with a 403 rather than
throttling, so a document that spends two round trips where one would do costs real
budget. `me` counts once however much is selected underneath it, which is why the
account's default privacy setting rides along on the library lookup instead of getting a
request of its own, and why the read in progress and its edition's page count arrive
together.

**A refusal must never read as an absence.** A 401, a 403 `insufficient_scope`, and a
GraphQL `errors` array in an HTTP 200 are all errors. Anything else would answer "this
book is not in Hardcover" for a token problem and quietly enqueue the whole library for
manual confirmation — which is precisely the failure criterion 5 exists to prevent.

**Hardcover has no sessions, and its server writes its own journal.** A sitting can only
become progress on a read plus, at the end, a status; and `insert_reading_journal` is
deliberately absent from this client, because the guide is explicit that the server writes
the start/progress/finish entries and a client that writes one makes it appear twice.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .retry import RETRYABLE_STATUSES

HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"

# Hardcover asks scripts to identify themselves.
USER_AGENT = "pagewire/0.0.1 (+https://github.com/bcosynot/pagewire)"

# Hardcover's own status ids. 2 is "Currently Reading" and setting it is what begins a
# read; 3 is "Read" and setting it is what finishes one — the server keeps the read in step
# with the entry, which is why the relay never writes a read's dates itself.
WANT_TO_READ = 1
CURRENTLY_READING = 2
READ_STATUS = 3

# Selecting `book_id` on the edition is the whole trick: one top-level field, both ids.
EDITIONS_BY_ISBN = """
query EditionByIsbn($isbn: String!) {
  editions(where: {isbn_13: {_eq: $isbn}}, limit: 1) {
    id
    book_id
  }
}
"""

# `me` is a list in Hardcover's schema, and `account_privacy_setting_id` is what an insert
# needs — fetched here rather than separately, since it costs nothing extra under `me`.
LIBRARY_ENTRY = """
query LibraryEntry($bookId: Int!, $editionId: Int!) {
  me {
    account_privacy_setting_id
    user_books(where: {book_id: {_eq: $bookId}, edition_id: {_eq: $editionId}}, limit: 1) {
      id
    }
  }
}
"""

# The guide's own "Find the Read in Progress": "progress goes on the read that's started
# and not yet finished", and the edition hangs off the read, so the page count the mapping
# needs comes from the same field as the read id.
READ_IN_PROGRESS = """
query ReadInProgress($bookId: Int!, $editionId: Int!) {
  me {
    user_books(where: {book_id: {_eq: $bookId}, edition_id: {_eq: $editionId}}, limit: 1) {
      id
      status_id
      user_book_reads(where: {finished_at: {_is_null: true}}, order_by: {id: desc}, limit: 1) {
        id
        started_at
        progress_pages
        edition {
          id
          pages
        }
      }
    }
  }
}
"""

# The object is written inline rather than as a typed variable, which is the shape both the
# guide and autoCalibre-Web's working client use: the input type's name is not documented,
# and `action_at` is dated back to when the reading happened.
UPDATE_READ_PROGRESS = """
mutation UpdateReadProgress($id: Int!, $pages: Int, $actionAt: String) {
  update_user_book_read(id: $id, object: {
    progress_pages: $pages,
    action_at: $actionAt
  }) {
    error
    user_book_read {
      id
      progress_pages
      started_at
      finished_at
    }
  }
}
"""

MARK_READ = """
mutation MarkRead($id: Int!) {
  update_user_book(id: $id, object: {status_id: 3}) {
    error
    user_book {
      id
      status_id
    }
  }
}
"""

ADD_USER_BOOK = """
mutation AddUserBook($object: UserBookCreateInput!) {
  insert_user_book(object: $object) {
    error
    user_book {
      id
    }
  }
}
"""

# Exact title match, deliberately. Hardcover disables `_ilike` and the other fuzzy
# operators, and its `search` answers in a Typesense document whose shape the docs do not
# give — so this offers what it can prove, and the page always allows a pasted id.
BOOKS_BY_TITLE = """
query BooksByTitle($title: String!, $limit: Int!) {
  books(where: {title: {_eq: $title}}, limit: $limit) {
    id
    title
    editions(limit: 1) {
      id
    }
  }
}
"""


class HardcoverError(RuntimeError):
    """Hardcover refused the call, reported an error, or could not be reached.

    `transient` is the fact a retry decision needs and the status code alone cannot carry:
    a 429 is the service asking to be asked again, a 403 is the service saying no. It is
    read by `retry.RetryPolicy`'s callers, never by this client, which does not retry
    anything itself.
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
class HardcoverEdition:
    """The pair of ids a library entry needs: the book, and the edition of it."""

    book_id: str
    edition_id: str


@dataclass(frozen=True)
class HardcoverRead:
    """The read a sitting's progress belongs on, and the pages it is counted in.

    `read_id` is what `update_user_book_read` addresses, and is None when the entry has no
    read in progress — a state worth distinguishing from having no entry at all, because
    the entry's own id still addresses a status change. `user_book_id` is that id; neither
    can be derived from the other. `edition_pages` is what Hardcover records, zero
    included: refusals live in the delivery module, so this reports the fact rather than
    judging it.
    """

    user_book_id: str
    read_id: str | None = None
    status_id: int | None = None
    edition_pages: int | None = None
    progress_pages: int | None = None


@dataclass(frozen=True)
class HardcoverCandidate:
    """A book the catalog has under a given title, for a human to accept or ignore."""

    book_id: str
    title: str | None
    edition_id: str | None


class HardcoverClient:
    """The relay's Hardcover client. No token, no calls."""

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = HARDCOVER_ENDPOINT,
        timeout: float = 30.0,
        user_agent: str = USER_AGENT,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.token = token
        self.endpoint = endpoint
        self.timeout = timeout
        self.user_agent = user_agent
        # Hardcover's Free tier is a per-minute quota, and a bulk resolve is exactly the
        # shape that trips it. Pacing here rather than in the caller means every request
        # this client makes is counted, whatever asked for it.
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock
        self._sleep = sleep
        self._last_call: float | None = None

    def _pace(self) -> None:
        """Wait long enough that this call is `min_interval_seconds` after the last one.

        Does nothing when no interval is configured, so the default is unthrottled and a
        test that does not care about pacing never has to know it exists.
        """
        if self.min_interval_seconds <= 0:
            return
        now = self._clock()
        if self._last_call is not None:
            wait = self._last_call + self.min_interval_seconds - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_call = now

    def resolve_isbn(self, isbn: str) -> HardcoverEdition | None:
        """The book and edition carrying this ISBN, or None when the catalog has neither."""
        data = self._execute(EDITIONS_BY_ISBN, {"isbn": isbn})
        editions = data.get("editions") or []
        if not editions:
            return None
        first = editions[0]
        if first.get("id") is None or first.get("book_id") is None:
            raise HardcoverError("Hardcover returned an edition without its ids")
        return HardcoverEdition(book_id=str(first["book_id"]), edition_id=str(first["id"]))

    def ensure_library_entry(
        self,
        *,
        book_id: str | int,
        edition_id: str | int | None = None,
        status_id: int = CURRENTLY_READING,
    ) -> str:
        """The owner's library entry id for this book, creating it if it is missing.

        Returns the `user_books` id, which is what a read and its progress hang off.
        """
        data = self._execute(
            LIBRARY_ENTRY,
            {
                "bookId": _int(book_id),
                "editionId": None if edition_id is None else _int(edition_id),
            },
        )
        account = _first(data.get("me")) or {}
        existing = account.get("user_books") or []
        if existing:
            return str(existing[0]["id"])

        created = self._execute(
            ADD_USER_BOOK,
            {
                "object": {
                    "book_id": _int(book_id),
                    "edition_id": None if edition_id is None else _int(edition_id),
                    "status_id": status_id,
                    "privacy_setting_id": account.get("account_privacy_setting_id") or 1,
                }
            },
        )
        result = created.get("insert_user_book") or {}
        if result.get("error"):
            raise HardcoverError(f"Hardcover refused the library entry: {result['error']}")
        entry = result.get("user_book") or {}
        if entry.get("id") is None:
            raise HardcoverError("Hardcover did not return the library entry it created")
        return str(entry["id"])

    def read_state(
        self, book_id: str | int, edition_id: str | int
    ) -> HardcoverRead | None:
        """The library entry's reading state: its read in progress, or its absence.

        None when the book is not in the library at all. An entry with no open read comes
        back with `read_id` None instead, because the two are different problems and only
        one of them stops a status change — `update_user_book` addresses the entry, so a
        book whose read the server has already finished can still be told it is Read.
        """
        data = self._execute(
            READ_IN_PROGRESS,
            {"bookId": _int(book_id), "editionId": _int(edition_id)},
        )
        account = _first(data.get("me")) or {}
        entry = _first(account.get("user_books"))
        if entry is None or entry.get("id") is None:
            return None
        read = _first(entry.get("user_book_reads"))
        edition = _first((read or {}).get("edition")) or {}
        return HardcoverRead(
            user_book_id=str(entry["id"]),
            read_id=None if (read or {}).get("id") is None else str((read or {})["id"]),
            status_id=_int_or_none(entry.get("status_id")),
            edition_pages=_int_or_none(edition.get("pages")),
            progress_pages=_int_or_none((read or {}).get("progress_pages")),
        )

    def update_progress(
        self, read_id: str | int, *, progress_pages: int, action_at: str | None = None
    ) -> None:
        """Put a page on the read in progress, dated to when the reading happened.

        `action_at` is what dates the journal entry the server writes, which is the whole
        reason a retroactive sync can say when the sitting ended rather than when the
        relay ran.
        """
        payload = self._execute(
            UPDATE_READ_PROGRESS,
            {"id": _int(read_id), "pages": int(progress_pages), "actionAt": action_at},
        )
        self._check_write(
            payload.get("update_user_book_read") or {}, "the progress write", "user_book_read"
        )

    def mark_read(self, user_book_id: str | int) -> None:
        """Set the entry's status to Read, which is what finishes its open read.

        The read's own `finished_at` is set by the server as part of this — the guide is
        explicit that the server keeps the two in step, so the relay does not date it.
        """
        payload = self._execute(MARK_READ, {"id": _int(user_book_id)})
        self._check_write(payload.get("update_user_book") or {}, "the finished status", "user_book")

    def find_by_title(self, title: str, *, limit: int = 5) -> list[HardcoverCandidate]:
        """Catalog books carrying exactly this title, for the review page to offer.

        A suggestion only: nothing that calls this writes an identity. Title matching is
        the ambiguous kind, and an ambiguous match is held for the owner by the design's
        own rule.
        """
        data = self._execute(BOOKS_BY_TITLE, {"title": title, "limit": limit})
        candidates = []
        for book in data.get("books") or []:
            if book.get("id") is None:
                continue
            edition = _first(book.get("editions")) or {}
            candidates.append(
                HardcoverCandidate(
                    book_id=str(book["id"]),
                    title=book.get("title"),
                    edition_id=None if edition.get("id") is None else str(edition["id"]),
                )
            )
        return candidates

    # -- internals ---------------------------------------------------------

    def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise HardcoverError(
                "no hardcover token configured; refusing to call Hardcover"
            )

        self._pace()
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            raise self._refusal(exc) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise HardcoverError(f"could not reach Hardcover: {exc}") from exc

        if not isinstance(body, dict):
            raise HardcoverError("Hardcover returned a body that is not a JSON object")
        if body.get("errors"):
            # A 200 carrying `errors` is a failure, not an empty result. Reading it as
            # "not in the catalog" would enqueue every book over a query typo.
            raise HardcoverError(f"Hardcover reported an error: {_messages(body['errors'])}")
        return body.get("data") or {}

    @staticmethod
    def _check_write(result: dict[str, Any], what: str, key: str) -> None:
        """A mutation's own `error` field is a failure, HTTP 200 or not.

        GraphQL answers most application-level problems inside the body, so a write the
        service refused would otherwise look exactly like one it accepted.
        """
        if result.get("error"):
            raise HardcoverError(f"Hardcover refused {what}: {result['error']}")
        if not result.get(key):
            raise HardcoverError(f"Hardcover answered {what} with no {key}")

    @staticmethod
    def _refusal(exc: urllib.error.HTTPError) -> HardcoverError:
        """Turn the documented error body into a message worth showing a human."""
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        code = body.get("error") or (body.get("errors") or [None])[0]
        message = f"Hardcover refused the call: HTTP {exc.code}"
        if code:
            message += f" {code}"
        detail = body.get("error_description") or body.get("message")
        if detail:
            message += f" ({detail})"
        if body.get("scope"):
            message += f" [needs scope {body['scope']}]"
        return HardcoverError(
            message,
            status_code=exc.code,
            code=code,
            transient=exc.code in RETRYABLE_STATUSES,
        )


def _int(value: str | int) -> int:
    """Hardcover's `book_id`/`edition_id` are `Int!`, so strings are not accepted."""
    return value if isinstance(value, int) else int(value)


def _int_or_none(value: Any) -> int | None:
    """An integer from Hardcover, or None when it is absent or unusable.

    Deliberately permissive about zero: a read sitting at page 0 is a real state, and an
    edition with 0 pages is a real (if useless) catalog entry. Deciding what those mean is
    the delivery module's job, not this one's — a client that quietly turned a fact into an
    absence would make the rule that refuses a zero-page edition unreachable.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(value: Any) -> dict[str, Any] | None:
    """The first element of a list-valued field, or the value itself if it is a dict."""
    if isinstance(value, list):
        return value[0] if value else None
    return value if isinstance(value, dict) else None


def _messages(errors: Any) -> str:
    """The messages out of a GraphQL `errors` array, which may hold strings or objects."""
    if not isinstance(errors, list):
        return str(errors)
    parts = [
        str(error.get("message", error)) if isinstance(error, dict) else str(error)
        for error in errors
    ]
    return "; ".join(parts)
