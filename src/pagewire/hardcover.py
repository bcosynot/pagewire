"""The Hardcover GraphQL API, as far as identifying a book needs it.

Endpoint `https://api.hardcover.app/v1/graphql`, `Authorization: Bearer <token>`, one
document per request. Three things shape this code.

**`editions` carries `book_id`.** So `editions(where: {isbn_13: {_eq: …}})` answers with
the edition *and* the book it belongs to in one field. The alternative — walking
`books(where: {slug: …}) { editions }`, which is what autoCalibre-Web does — needs a slug
the relay has no way to derive from an ISBN.

**Top-level fields are rate limit, not style.** Hardcover's Free plan allows a burst of 10
top-level fields per request and rejects anything larger with a 403 rather than
throttling, so a document that spends two round trips where one would do costs real
budget. `me` counts once however much is selected underneath it, which is why the
account's default privacy setting rides along on the library lookup instead of getting a
request of its own.

**A refusal must never read as an absence.** A 401, a 403 `insufficient_scope`, and a
GraphQL `errors` array in an HTTP 200 are all errors. Anything else would answer "this
book is not in Hardcover" for a token problem and quietly enqueue the whole library for
manual confirmation — which is precisely the failure criterion 5 exists to prevent.

Nothing here writes progress or journal entries; that is the delivery ticket's job. The
one write is `insert_user_book`, which puts a book on the `Currently Reading` shelf —
Hardcover's status 2, the status that starts a read.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"

# Hardcover asks scripts to identify themselves.
USER_AGENT = "pagewire/0.0.1 (+https://github.com/bcosynot/pagewire)"

# Hardcover's own status ids. 2 is "Currently Reading", and setting it is what begins a
# read — which is the right state for a book the relay has seen reading events for.
CURRENTLY_READING = 2

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
    """Hardcover refused the call, reported an error, or could not be reached."""

    def __init__(
        self, message: str, *, status_code: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class HardcoverEdition:
    """The pair of ids a library entry needs: the book, and the edition of it."""

    book_id: str
    edition_id: str


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
    ) -> None:
        self.token = token
        self.endpoint = endpoint
        self.timeout = timeout
        self.user_agent = user_agent

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
        return HardcoverError(message, status_code=exc.code, code=code)


def _int(value: str | int) -> int:
    """Hardcover's `book_id`/`edition_id` are `Int!`, so strings are not accepted."""
    return value if isinstance(value, int) else int(value)


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
