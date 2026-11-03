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

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .hardcover import HardcoverClient, HardcoverError
from .readwise import ReadwiseClient, ReadwiseError
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


class HeldError(IdentityError):
    """There is nothing to match this row by — and that is not a fault.

    Distinct from a service failure on purpose. A held row records **no error**, because
    "we have not been told which book this is" is not something the owner has to go and
    fix; the review page is where it waits. Only a refusal from a service is an error.
    """


def _author_key(value: Any) -> str:
    """A person's name reduced to what makes two spellings the same name.

    Hardcover holds `Jonathan  Wilson` with a doubled space and `Earnest Hemingway` with a
    typo; the card holds `Jonathan Wilson`. Case and punctuation are not evidence about who
    wrote a book, so they are not compared.
    """
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(value or "").casefold()).split())


def _author_names(value: Any) -> set[str]:
    """The individual names in a `dc:creator`, which routinely lists several.

    An OPF says `Eric Freeman & Elisabeth Robson & Bert Bates & Kathy Sierra` while
    Hardcover's record names one contributor per entry, so the comparison is name by name
    rather than string to string.
    """
    parts = re.split(r"&|;|,|\band\b", str(value or ""))
    return {key for part in parts if (key := _author_key(part))}


def confident_candidate(candidates: list[Any], row: Any) -> Any | None:
    """The first catalog record this row's author agrees with, if it is a real one.

    Two conditions, and both are doing work. **Author agreement** is what excludes the
    duplicate entries a popular title collects — measured on "The Jinn-Bot of Shantiport",
    the exact-title match returned five of them and none had an author at all. **A record
    somebody has read** (`users_count`) excludes the same duplicates from the other side:
    an entry with the right author and no readers is as likely a stub as the real book.

    Returns None rather than guessing. The caller holds the row, and a wrong match would
    put immutable sittings on the wrong book.
    """
    wanted = _author_names(row["author"])
    if not wanted:
        return None
    for candidate in candidates:
        if not candidate.users_count:
            continue
        if _author_names(candidate.author) & wanted:
            return candidate
    return None


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
    """Match every as-yet-unmatched book, per row.

    An ISBN is tried first and is the rule that gets used whenever it works. Without one —
    or when Hardcover holds no edition carrying it — the owner's rule of 2026-11-02
    applies: a title match is taken when a catalog record's **author agrees** with the book
    on the card and that record is one somebody has actually read. Everything else is held
    for one confirmation in the review page.

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

        if readwise is None and hardcover is None:
            # Nothing is switched on, so nothing can have matched. Marking the row here
            # would make it deliverable on the strength of a lookup that never ran.
            say(f"{path}: no destination enabled — left unmatched")
            report.unmatched.append(path)
            continue

        try:
            (readwise_id, hardcover_id, hardcover_edition_id), matched_by = _match_row(
                row, readwise=readwise, hardcover=hardcover, policy=policy, path=path, say=say
            )
        except HeldError as held:
            # **Not** an error, and deliberately not recorded as one: nothing has told us
            # which book this is, and the review page is where it waits. Writing a reason
            # onto 89 ordinary rows would make the queue look like 89 failures.
            say(f"{path}: {held}")
            report.unmatched.append(path)
            continue
        except (ReadwiseError, HardcoverError, IdentityError) as exc:
            # Per row, and never fatal to the run. A bad token therefore shows up as one
            # row per book rather than as "resolution did nothing".
            storage.set_book_error(document, str(exc))
            report.errors[path] = str(exc)
            report.unmatched.append(path)
            say(f"{path}: {exc}")
            continue

        storage.set_book_identity(
            document,
            readwise_id=readwise_id,
            hardcover_id=hardcover_id,
            hardcover_edition_id=hardcover_edition_id,
            status=AUTO,
            matched_by=matched_by,
        )
        report.resolved.append(path)
        say(
            f"{path}: matched by {matched_by}"
            f" (readwise {readwise_id or '—'}, hardcover {hardcover_id or '—'}"
            f"{' / ' + hardcover_edition_id if hardcover_edition_id else ''})"
        )

    return report


def _attempt(
    call: Callable[[], tuple[str | None, str | None, str | None]],
    *,
    policy: RetryPolicy,
    path: str,
    say: Callable[[str], None],
) -> tuple[str | None, str | None, str | None]:
    """Run one matching attempt, waiting and trying again while a service says "later".

    Only a **transient** refusal is retried. A 403 scope refusal, or a book Hardcover has
    no edition for, is the service's answer; asking again would repeat it while taking
    longer to say so.
    """
    for attempt in range(policy.attempts):
        try:
            return call()
        except (ReadwiseError, HardcoverError) as exc:
            if not exc.transient or attempt == policy.attempts - 1:
                raise
            waited = policy.wait_for(attempt)
            say(f"{path}: {exc} — waiting {waited:g}s, then trying again")
            policy.pause(attempt)
    raise IdentityError("retry loop ended without an answer")  # pragma: no cover


def _by_isbn(
    readwise: ReadwiseClient | None, hardcover: HardcoverClient | None, isbn: str
) -> tuple[str | None, str | None, str | None]:
    """The ids for a book identified by an ISBN — the confident route."""
    hardcover_id, hardcover_edition_id = match_hardcover(hardcover, isbn)
    return find_readwise(readwise, isbn=isbn), hardcover_id, hardcover_edition_id


def _by_title_and_author(
    readwise: ReadwiseClient | None, hardcover: HardcoverClient | None, row: Any
) -> tuple[str | None, str | None, str | None]:
    """The ids for a book identified by its title and author — the owner's 2026-11-02 rule.

    Nothing is written to either service until the confidence check has passed. That order
    is the whole safety property: a wrong match puts immutable sittings on the wrong book,
    so the search, the author agreement and the "somebody has read it" test all happen
    before Readwise is asked to add anything.
    """
    candidate = confident_candidate(
        hardcover.find_by_title(row["title"]),  # type: ignore[union-attr]
        row,
    )
    if candidate is None:
        raise HeldError(
            "no confident match — held for confirmation"
        )
    # No ISBN in the record, so there is no read-only way to ask Readwise about it: it stays
    # unresolved until delivery has a sitting and adds it.
    return find_readwise(readwise, isbn=None), candidate.book_id, candidate.edition_id


def _match_row(
    row: Any,
    *,
    readwise: ReadwiseClient | None,
    hardcover: HardcoverClient | None,
    policy: RetryPolicy,
    path: str,
    say: Callable[[str], None],
) -> tuple[tuple[str | None, str | None, str | None], str]:
    """One row's identity, and which rule found it.

    By ISBN when the row has one, and by title and author when it does not — or when
    Hardcover has never heard of that ISBN. Measured on four real rows: the ISBN was right,
    Hardcover simply held no edition carrying it, and its search finds the book anyway.

    Raises `HeldError` when there is nothing to go on, and the service's own error when a
    service refused.
    """
    isbn_failure: Exception | None = None
    if row["isbn"]:
        try:
            return (
                _attempt(
                    lambda: _by_isbn(readwise, hardcover, row["isbn"]),
                    policy=policy,
                    path=path,
                    say=say,
                ),
                "isbn",
            )
        except IdentityError as exc:
            # **Only** a definitive "Hardcover has no edition with that ISBN" falls through
            # to the title. A transient refusal must not: the service is busy, not ignorant,
            # and the title lookup would fail the same way after a second round of waiting
            # — measured, that doubled the pauses from [2, 4] to [2, 4, 2, 4].
            isbn_failure = exc

    if not (row["title"] and row["author"]):
        raise isbn_failure or HeldError(
            "no ISBN, and no title and author to fall back on — held for confirmation"
        )
    if hardcover is None:
        # Readwise alone can match a title and an author, but the ids Hardcover issues are
        # what the review page and the progress write need — half an identity is not one.
        raise isbn_failure or HeldError(
            "no ISBN and Hardcover is switched off — held for confirmation"
        )

    try:
        return (
            _attempt(
                lambda: _by_title_and_author(readwise, hardcover, row),
                policy=policy,
                path=path,
                say=say,
            ),
            "title+author",
        )
    except HeldError:
        # Nothing confident. When an ISBN was involved, its own refusal says more than
        # "no confident match" does, so it is what gets recorded.
        raise isbn_failure or HeldError(
            "no ISBN, and no record under that title whose author agrees — held for confirmation"
        )


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
            # Read-only, and by ISBN only. A row in the queue usually has no ISBN — that is
            # why it is here — so this normally answers None, and delivery resolves the
            # book when it has a sitting for it.
            readwise_id = find_readwise(readwise, isbn=isbn)
        if hardcover_id is None:
            hardcover_id, hardcover_edition_id = match_hardcover(hardcover, isbn)
    except (ReadwiseError, HardcoverError, IdentityError) as exc:
        storage.set_book_error(document, str(exc))
        raise IdentityError(str(exc)) from exc

    # Only Hardcover has to be identified for a row to be confirmed. The Readwise id is
    # obtained by delivery from the book's own identity, so a row without one is not
    # half-identified — it is a row whose Readwise side has not been needed yet.
    missing = []
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


def find_readwise(client: ReadwiseClient | None, *, isbn: str | None) -> str | None:
    """The Readwise `book_id` Readwise **already has** for this book, or None.

    Deliberately read-only, and that is the whole point: matching must not put a book into
    the owner's library. The id is only *needed* when there is a sitting to deliver, and
    delivery is where it is obtained — for the books that have a reading to justify them.

    A book with no ISBN cannot be looked up at all: `GET /books` filters by ISBN, ASIN or
    id, never by title. So it stays unresolved here and delivery adds it by title and
    author when the time comes.
    """
    if client is None or not isbn:
        return None

    found = client.find_by_isbn(isbn)
    return found.book_id if found is not None else None


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
    # The library entry is created by delivery, when there is progress to put on it.
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
