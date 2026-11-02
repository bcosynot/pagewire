"""Identity resolution: which book, as each service knows it.

Readwise wants a `book_id` it issued; Hardcover wants its own `book_id` plus an
`edition_id`. This module is where a swept row gets them, under one rule, decided by
Vivek on 2026-10-31:

    **A confident match — an ISBN — is added automatically and with no gate. Anything
    ambiguous is held for him to confirm once in the review page.**

That asymmetry is the whole design. A Readwise sitting cannot be moved to a different
book after it has been written, so a wrong automatic match is a permanent, silent
corruption of his reading record; a row left in the confirmation queue is merely a chore.
Every judgement call here therefore resolves toward the queue.

Three consequences worth stating plainly, because they are choices rather than accidents:

- **A row with no ISBN is not sent to either service.** Not even a lookup. Title-and-author
  matching is the ambiguous kind, and "we asked and it guessed" is not better than "we
  did not ask".
- **An enabled destination that cannot produce an id leaves the row unmatched.** So the
  row becomes `auto` only when *everything switched on* matched it. A book Readwise knows
  and Hardcover does not is not half-identified: it is a book for the queue, with the
  reason recorded. `unmatched` then has one meaning — "not identified" — and delivery can
  gate on it without exceptions.
- **One row's failure is that row's failure.** The metadata sweep is all-or-nothing
  because a half-swept card misleads; resolution is the opposite, because a queue of 85
  books must not be held up by one. Each row carries its own error (criterion 5).

Errors are stored on the row, in `books.error`, and never in the status: the status is
what delivery gates on, so an error must not be able to change whether a book is
deliverable. A later successful run clears it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .hardcover import HardcoverClient, HardcoverError
from .readwise import DEFAULT_SHELF, ReadwiseClient, ReadwiseError
from .retry import RetryPolicy
from .storage import Storage

# The three states a row's `status` can hold. `unmatched` is the only one delivery refuses.
UNMATCHED = "unmatched"
AUTO = "auto"
CONFIRMED = "confirmed"


class IdentityError(RuntimeError):
    """A booking could not be identified, and the row says why.

    Raised for the cases the services do not report themselves: a row with no ISBN and no
    title to search on, or an enabled destination with nothing to match against.
    """


@dataclass(frozen=True)
class IdentityReport:
    """What one resolution pass did, in the shape a run log wants to print."""

    resolved: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def resolve_books(
    storage: Storage,
    *,
    readwise: ReadwiseClient | None = None,
    hardcover: HardcoverClient | None = None,
    retry: RetryPolicy | None = None,
    log: Callable[[str], None] | None = None,
) -> IdentityReport:
    """Match every as-yet-unmatched book that carries an ISBN, per row.

    `readwise=None` / `hardcover=None` mean that destination is switched off, not that it
    failed — a destination nobody configured must not make rows look broken.

    `retry` bounds how long a service that answers "later" is argued with. Defaulted so a
    caller that has no opinion still gets the behaviour the deployed run wants.
    """
    say = log or (lambda _line: None)
    policy = retry or RetryPolicy()
    report = IdentityReport()

    for row in storage.books():
        if row["status"] != UNMATCHED:
            continue

        document = row["document"]
        path = row["book_path"] or document

        if not row["isbn"]:
            # The design's queue, and deliberately not a lookup: see the module docstring.
            say(f"{path}: no ISBN — held for confirmation")
            report.unmatched.append(path)
            continue

        if readwise is None and hardcover is None:
            # Nothing is switched on, so nothing can have matched. Marking the row here
            # would make it deliverable on the strength of a lookup that never ran.
            say(f"{path}: no destination enabled — left unmatched")
            report.unmatched.append(path)
            continue

        readwise_id, hardcover_id, hardcover_edition_id, failure = _match_with_retries(
            row, readwise=readwise, hardcover=hardcover, policy=policy, path=path, say=say
        )
        if failure is not None:
            # Per row, and never fatal to the run. A bad token therefore shows up as one
            # row per book rather than as "resolution did nothing".
            storage.set_book_error(document, str(failure))
            report.errors[path] = str(failure)
            report.unmatched.append(path)
            say(f"{path}: {failure}")
            continue

        storage.set_book_identity(
            document,
            readwise_id=readwise_id,
            hardcover_id=hardcover_id,
            hardcover_edition_id=hardcover_edition_id,
            status=AUTO,
        )
        report.resolved.append(path)
        say(
            f"{path}: matched"
            f" (readwise {readwise_id or '—'}, hardcover {hardcover_id or '—'}"
            f"{' / ' + hardcover_edition_id if hardcover_edition_id else ''})"
        )

    return report


def _match_with_retries(
    row: Any,
    *,
    readwise: ReadwiseClient | None,
    hardcover: HardcoverClient | None,
    policy: RetryPolicy,
    path: str,
    say: Callable[[str], None],
) -> tuple[str | None, str | None, str | None, Exception | None]:
    """One row's matching, waiting and trying again while a service says "later".

    Returns the ids, or the failure to record — never raises, because one book's refusal
    must not end the run. Only a **transient** refusal is retried: a 403 scope refusal, or
    a book Hardcover has no edition for, is the service's answer, and asking again would
    repeat it while taking longer to say so.
    """
    for attempt in range(policy.attempts):
        try:
            hardcover_id, hardcover_edition_id = match_hardcover(hardcover, row["isbn"])
            return (
                match_readwise(readwise, isbn=row["isbn"]),
                hardcover_id,
                hardcover_edition_id,
                None,
            )
        except (ReadwiseError, HardcoverError) as exc:
            if not exc.transient or attempt == policy.attempts - 1:
                return None, None, None, exc
            waited = policy.wait_for(attempt)
            say(f"{path}: {exc} — waiting {waited:g}s, then trying again")
            policy.pause(attempt)
        except IdentityError as exc:
            return None, None, None, exc
    return None, None, None, IdentityError("retry loop ended without an answer")


def confirm_book(
    storage: Storage,
    document: str,
    *,
    readwise: ReadwiseClient | None = None,
    hardcover: HardcoverClient | None = None,
    readwise_id: str | None = None,
    hardcover_id: str | None = None,
    hardcover_edition_id: str | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    """Confirm one ambiguous row, once, and write what identifies it.

    Ids passed in are the owner's decision and are used as given. Anything not passed is
    resolved: Readwise can match a title and an author by itself, and Hardcover cannot —
    which is exactly why the row was waiting. If an enabled destination still has no id
    when this returns, nothing is written and the reason is left on the row, because
    "confirmed" has to mean identified rather than nearly.
    """
    say = log or (lambda _line: None)
    row = storage.book(document)
    if row is None:
        raise IdentityError(f"no book row for document {document!r}")

    path = row["book_path"] or document
    isbn = row["isbn"]

    try:
        if readwise_id is None:
            readwise_id = match_readwise(
                readwise,
                # A row still in the queue usually has no ISBN to match on — that is why
                # it is here — but a row queued by a failure may have one, and an ISBN is
                # the better identity whenever it exists.
                isbn=isbn,
                title=row["title"],
                author=row["author"],
            )
        if hardcover_id is None:
            hardcover_id, hardcover_edition_id = match_hardcover(hardcover, isbn)
    except (ReadwiseError, HardcoverError, IdentityError) as exc:
        storage.set_book_error(document, str(exc))
        raise IdentityError(str(exc)) from exc

    missing = []
    if readwise is not None and not readwise_id:
        missing.append("readwise")
    if hardcover is not None and not hardcover_id:
        missing.append("hardcover")
    if missing:
        # A destination with no id cannot be reached, so confirming would produce a row
        # that looks identified and is half-deliverable. Refuse, and say which id is out.
        message = (
            f"cannot confirm {path}: no id from {' and '.join(missing)}. "
            "It has no ISBN to match on, so its ids have to be supplied."
        )
        storage.set_book_error(document, message)
        raise IdentityError(message)

    storage.set_book_identity(
        document,
        readwise_id=readwise_id,
        hardcover_id=hardcover_id,
        hardcover_edition_id=hardcover_edition_id,
        status=CONFIRMED,
    )
    say(
        f"{path}: confirmed"
        f" (readwise {readwise_id or '—'}, hardcover {hardcover_id or '—'}"
        f"{' / ' + hardcover_edition_id if hardcover_edition_id else ''})"
    )


def match_readwise(
    client: ReadwiseClient | None,
    *,
    isbn: str | None,
    title: str | None = None,
    author: str | None = None,
) -> str | None:
    """The Readwise `book_id` for this book, creating it if Readwise does not have it.

    With an ISBN this asks whether it is already there before adding anything, which is
    the order criterion 1 names. Without one it can only add-and-see: `POST /books` matches
    title and author itself, and answers `existing` when the result is already in the
    library.
    """
    if client is None:
        return None

    if isbn:
        found = client.find_by_isbn(isbn)
        if found is not None:
            return found.book_id
        return client.add(isbn=isbn, status=DEFAULT_SHELF).book_id

    if not (title or author):
        raise IdentityError(
            "Readwise cannot match a book with no ISBN, no title and no author"
        )
    return client.add(title=title, author=author, status=DEFAULT_SHELF).book_id


def match_hardcover(
    client: HardcoverClient | None, isbn: str | None
) -> tuple[str | None, str | None]:
    """The `(book_id, edition_id)` Hardcover has for this ISBN.

    Hardcover has no title-and-author path the relay is willing to trust — its documented
    `search` answers in a Typesense document, and the design holds ambiguous matches for
    the owner anyway — so an ISBN (or a supplied id) is the only automatic route, and the
    library entry the ids point at is created here if it does not exist.
    """
    if client is None:
        return None, None

    if not isbn:
        raise IdentityError(
            "Hardcover has no ISBN to match on; supply its ids on the review page"
        )

    found = client.resolve_isbn(isbn)
    if found is None:
        raise IdentityError(
            f"Hardcover has no edition with ISBN {isbn}; supply its ids on the review page"
        )
    client.ensure_library_entry(book_id=found.book_id, edition_id=found.edition_id)
    return found.book_id, found.edition_id


def main(argv: list[str] | None = None) -> int:
    """Resolve what can be resolved, from the command line.

        python -m pagewire.identity            # match every pending book that has an ISBN
        python -m pagewire.identity --list      # show the queue and the reasons, offline

    This is the path deploy can put on a timer. Deliberately the same code the review
    page calls, so a run here and a press there cannot disagree.
    """
    import argparse

    from .config import Settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="", help="SQLite path (default: PAGEWIRE_DB)")
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the confirmation queue — and every recorded reason — without calling "
        "either service",
    )
    arguments = parser.parse_args(argv)

    settings = Settings.from_env()
    storage = Storage(arguments.db or settings.db_path)

    if arguments.list:
        return _list_queue(storage)

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
            min_interval_seconds=settings.hardcover_min_interval_seconds,
        )
        if settings.hardcover_enabled
        else None
    )
    print(
        f"destinations: readwise {'on' if readwise else 'off'},"
        f" hardcover {'on' if hardcover else 'off'}"
    )

    report = resolve_books(
        storage,
        readwise=readwise,
        hardcover=hardcover,
        retry=RetryPolicy(
            attempts=settings.identity_retry_attempts,
            backoff_seconds=settings.identity_retry_backoff_seconds,
            backoff_max_seconds=settings.identity_retry_backoff_max_seconds,
        ),
        log=lambda line: print(line, flush=True),
    )
    print(
        f"\n{len(report.resolved)} matched, {len(report.unmatched)} left for confirmation,"
        f" {len(report.errors)} failed"
    )
    if report.errors:
        print("\nrefused by a service (fix the cause, then re-run):")
        for path, message in report.errors.items():
            print(f"  {path}\n    {message}")
    if report.unmatched:
        print("\nthe review page lists these: /review on the relay")
    return 0


def _list_queue(storage: Storage) -> int:
    """Print the queue, and every recorded reason, without touching the network."""
    waiting = list(storage.books_needing_confirmation())
    errored = {row["document"]: row for row in storage.books_with_errors()}
    for document, row in errored.items():
        if all(other["document"] != document for other in waiting):
            waiting.append(row)

    conflicted = set(errored)
    print(f"{len(waiting)} book(s) waiting for confirmation\n")
    for row in waiting:
        path = row["book_path"] or row["document"]
        print(f"  {path}")
        print(
            f"    {row['document']}  status={row['status']}"
            f"  isbn={row['isbn'] or '—'}  title={row['title'] or '—'}"
        )
        if row["document"] in conflicted:
            print(f"    problem: {row['error']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand against the services
    raise SystemExit(main())
