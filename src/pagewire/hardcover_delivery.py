"""Delivering finalised sittings to Hardcover.

Hardcover has no session concept at all — verified as an absence across its documented
schemas — so this destination is progress and finished by necessity rather than by
preference. A sitting's end position is mapped onto its edition's page count and written as
`progress_pages` on the read in progress, dated back to when the reading stopped; a book the
device finished is marked Read.

Four decisions carry the weight, and each comes from something Hardcover documents about
itself:

**The page is the edition's, never the device's.** The X4 counts reading positions in basis
points, and Hardcover counts pages in the edition the reader is on, so the sitting's end
position becomes `round(edition_pages × end_bp ÷ 10000)` — the mapping Hardcover's own
showcase plugin uses, and the one its guide describes ("to show a percentage, work it out
from the edition's `pages`"). A sitting with no position, or a book whose edition has no
page count, is refused locally with the reason on the row: a fabricated page would sit in
the reading record forever.

**The relay writes no journal entry.** The guide is explicit — "starting, progress and
finishing each get a journal entry; don't write those entries yourself, or they'll appear
twice". The server keeps a read in step with its entry's status too, which is why the relay
sends a status and never dates a read itself.

**One write per book a minute.** Hardcover's Free plan allows 60 requests a minute, so a
book's progress is coalesced to a single write per run and held back until the throttle
window since its last successful write has passed. Progress is a mirror, not a record: the
later sitting's page supersedes the earlier one's, and only the furthest is sent. Nothing is
lost locally — the derived sittings stay in the table, which is the record of what was read.

**The claim is still written before the call.** A progress write is idempotent in a way a
Readwise session is not (sending the same page twice says the same thing), but the ordering
is kept for the same reason: a row marked delivered before a crash would lose a write that
never happened, whereas a claim with no outcome is simply re-sent next run.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .hardcover import HardcoverClient, HardcoverError
from .storage import Storage

# Progress and the finished status are separate ledger destinations, because a re-run must
# no more re-send the status flip than it may re-send a progress write.
DESTINATION = "hardcover"
FINISH_DESTINATION = "hardcover:finished"

BASIS_POINTS = 10_000

# A credential problem is global to the run: retrying it once per book would turn one bad
# token into a run full of the same line. Sits here with the same meaning the Readwise half
# gives it.
AUTH_STATUSES = frozenset({401, 403})

# A refusal the relay made itself, without calling Hardcover at all. Recorded as a 4xx so a
# later run re-tries the decision rather than asking the service about a request that was
# never made.
LOCAL_REFUSAL = 400

# The refusals worth naming, worded once here so the run's log, the report and the ledger
# row all say the same sentence.
NO_IDS = (
    "the row is matched for hardcover but carries no book and edition id, so there is"
    " nothing to write against"
)
NO_POSITION = "the sitting has no end reading position, so there is no page to write"
NO_ENTRY = (
    "hardcover has no library entry for this book, so there is nothing to write against;"
    " add it on hardcover.app or re-check the match on the review page"
)
NO_READ = (
    "hardcover has no read in progress for this book, so its progress has nowhere to go;"
    " start it on hardcover.app, then re-run"
)


@dataclass
class HardcoverReport:
    """What one delivery run did, in the shape the CLI prints and a test asserts on.

    `written` and `coalesced` are counts of sittings, not of requests: every sitting a
    progress write carried is `written`, and the ones a later sitting's page superseded are
    also `coalesced`, so `len(written) - len(coalesced)` is the number of requests made.
    """

    enabled: bool = True
    written: list[tuple[str, int]] = field(default_factory=list)
    coalesced: list[tuple[str, int]] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    throttled: list[str] = field(default_factory=list)
    failed: dict[tuple[str, int], str] = field(default_factory=dict)
    aborted: str | None = None

    @property
    def requests(self) -> int:
        return len(self.written) - len(self.coalesced)


def deliver_hardcover(
    storage: Storage,
    client: HardcoverClient | None,
    *,
    throttle_seconds: int = 60,
    timezone: str = "America/New_York",
    now: Callable[[], float] = time.time,
    log: Callable[[str], None] | None = None,
) -> HardcoverReport:
    """Write every deliverable sitting's progress to Hardcover.

    `client=None` means the destination is switched off, not that it failed: nothing is
    read, nothing is claimed, and no request is made.
    """
    say = log or (lambda _line: None)
    report = HardcoverReport(enabled=client is not None)
    if client is None:
        say("hardcover is switched off — nothing is delivered")
        return report

    window = max(0.0, float(throttle_seconds))
    last_writes = _last_write_times(storage)

    for document, rows in _by_document(storage.sittings_to_deliver()):
        # Progress and the finished status come due independently of one another. A run
        # that wrote the progress and was then refused the status change must not leave the
        # book off the Read shelf for good, so the status has its own ledger row and is
        # looked for on its own rather than as a side effect of a progress write that has
        # already been recorded as delivered.
        pending = _undelivered(storage, rows, DESTINATION)
        finisher = _outstanding_finish(storage, rows)
        if not pending and finisher is None:
            continue

        path = rows[0]["book_path"] or document
        book_id = rows[0]["hardcover_id"]
        edition_id = rows[0]["hardcover_edition_id"]

        if not book_id or not edition_id:
            # Identity's ruling is that a matched row carries both ids. If one somehow does
            # not, the other id addresses nothing, so nothing is written against a book the
            # relay cannot name.
            _refuse_all(storage, pending, report, say, NO_IDS)
            _finish_unavailable(storage, document, finisher, report, say, NO_IDS)
            continue

        # Local first: a sitting with no position cannot become a page whatever Hardcover
        # says, so it is refused without spending a request on finding that out.
        positioned = [row for row in pending if row["end_progress_bp"] is not None]
        _refuse_all(
            storage,
            [row for row in pending if row["end_progress_bp"] is None],
            report,
            say,
            NO_POSITION,
        )
        if not positioned and finisher is None:
            continue

        written_at = last_writes.get(document)
        if written_at is not None and now() - written_at < window:
            held = int(now() - written_at)
            report.throttled.append(document)
            say(
                f"{path}: written to {held}s ago — holding to the {int(window)}s throttle;"
                " nothing is dropped"
            )
            continue

        # The read is the one thing both halves need, so it is fetched once. An entry with
        # no read in progress is a refusal for the progress write and nothing more: the
        # status change addresses the entry, and the server may have finished the read
        # itself the moment progress reached the edition's last page.
        try:
            read = client.read_state(book_id, edition_id)
        except HardcoverError as exc:
            message = str(exc)
            _fail_all(
                storage, positioned, report, say, message, status_code=exc.status_code
            )
            _finish_unavailable(
                storage, document, finisher, report, say, message, status_code=exc.status_code
            )
            if exc.status_code in AUTH_STATUSES:
                report.aborted = message
                break
            continue

        if read is None:
            _refuse_all(storage, positioned, report, say, NO_ENTRY)
            _finish_unavailable(storage, document, finisher, report, say, NO_ENTRY)
            continue

        if read.read_id is None:
            _refuse_all(storage, positioned, report, say, NO_READ)
        elif not _write_progress(
            storage,
            client,
            path,
            positioned,
            read_id=read.read_id,
            edition_pages=read.edition_pages,
            edition_id=edition_id,
            timezone=timezone,
            report=report,
            say=say,
        ):
            break

        if not _finish_book(storage, client, document, read.user_book_id, finisher, report, say):
            break

    return report


# -- the sittings this run may write ----------------------------------------


def _by_document(rows: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    """Group sittings by book, preserving the query's order."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row["document"], []).append(row)
    return list(grouped.items())


def _delivered(storage: Storage, row: Any, destination: str) -> bool:
    """Whether this sitting is already in the ledger as delivered to that destination."""
    ledger = storage.delivery_row(row["document"], row["started_at"], destination)
    return ledger is not None and ledger["sent_at"] is not None


def _undelivered(storage: Storage, rows: Sequence[Any], destination: str) -> list[Any]:
    """The sittings on this destination that no earlier run has written.

    A row already delivered is dropped silently: that is the idempotent case, not an error,
    and reporting it every run would bury the ones that matter.
    """
    return [row for row in rows if not _delivered(storage, row, destination)]


def _outstanding_finish(storage: Storage, rows: Sequence[Any]) -> Any | None:
    """The furthest finished sitting whose Read status has not been written yet, if any.

    Its own selector rather than a by-product of a progress write, because those two land
    independently: a progress write that succeeded and a status change that was refused
    would otherwise leave the book off the Read shelf with nothing left to retry.
    """
    waiting = [
        row
        for row in rows
        if row["finished"] and not _delivered(storage, row, FINISH_DESTINATION)
    ]
    if not waiting:
        return None
    return max(waiting, key=lambda row: row["ended_at"])


def _last_write_times(storage: Storage) -> dict[str, int]:
    """When each book last landed a progress write, read from the ledger.

    The throttle reads the record of what actually reached Hardcover rather than a
    timestamp of its own, so a run that failed to write does not hold the next one back.
    """
    times: dict[str, int] = {}
    for row in storage.send_log(DESTINATION):
        if row["sent_at"] is None:
            continue
        times[row["document"]] = max(times.get(row["document"], 0), row["sent_at"])
    return times


# -- the arithmetic ---------------------------------------------------------


def _page(basis_points: Any, edition_pages: Any) -> int | None:
    """The device's end position as a page of the edition, or None if it cannot be one.

    The mapping is Hardcover's own: the fraction of the edition read, times its pages. The
    position is clamped to the basis points a reading position can be, which is what keeps
    the page inside the edition — a device that reports past the end cannot write a page
    that does not exist. Either input missing, or an edition with no pages to count, is a
    refusal rather than a guess: a fabricated page sits in the reading record forever.
    """
    if basis_points is None or edition_pages is None:
        return None
    try:
        position = int(basis_points)
        pages = int(edition_pages)
    except (TypeError, ValueError):
        return None
    if pages <= 0:
        return None
    position = min(max(position, 0), BASIS_POINTS)
    return round(pages * position / BASIS_POINTS)


def _local_iso(epoch: Any, timezone: str) -> str:
    """Epoch seconds as an offset-bearing timestamp, in the sitting's own zone.

    Hardcover's guide writes `action_at` as `2026-09-24T21:00:00-06:00` — an instant with
    the reader's offset, which is the unit the field is dated in and the reason a
    retroactive sync can say when a sitting ended rather than when the relay ran.
    """
    moment = datetime.fromtimestamp(int(epoch), dt_timezone.utc)
    try:
        moment = moment.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown zone name is not a reason to refuse a write: UTC is unambiguous even
        # when it is not the reader's offset.
        pass
    return moment.isoformat()


# -- recording --------------------------------------------------------------


def _refuse_all(
    storage: Storage,
    rows: Sequence[Any],
    report: HardcoverReport,
    say: Callable[[str], None],
    message: str,
) -> None:
    """Record a sitting the relay will not write, and name it.

    Nothing is sent and nothing is invented, so the claim carries a 4xx: the decision is
    retried on a later run without Hardcover being asked about a request never made.
    """
    for row in rows:
        storage.claim_delivery(row["document"], row["started_at"], DESTINATION)
        storage.mark_delivery_failed(
            row["document"], row["started_at"], DESTINATION, message, status_code=LOCAL_REFUSAL
        )
        report.failed[(row["document"], row["started_at"])] = message
        say(f"{row['book_path'] or row['document']}: {message}")


def _fail_all(
    storage: Storage,
    rows: Sequence[Any],
    report: HardcoverReport,
    say: Callable[[str], None],
    message: str,
    *,
    status_code: int | None,
) -> None:
    """Record a write Hardcover refused or never answered. Never marks one delivered."""
    for row in rows:
        storage.claim_delivery(row["document"], row["started_at"], DESTINATION)
        storage.mark_delivery_failed(
            row["document"], row["started_at"], DESTINATION, message, status_code=status_code
        )
        report.failed[(row["document"], row["started_at"])] = message
    say(f"{message}")


def _write_progress(
    storage: Storage,
    client: HardcoverClient,
    path: str,
    positioned: Sequence[Any],
    *,
    read_id: str,
    edition_pages: int | None,
    edition_id: Any,
    timezone: str,
    report: HardcoverReport,
    say: Callable[[str], None],
) -> bool:
    """Write one book's progress as a single page. False when the run must stop.

    One write per book per run: progress is a mirror, not a record of sittings, so the
    furthest sitting's page is the one that stands and the earlier ones it supersedes are
    recorded against the same write rather than queued behind it.
    """
    if not positioned:
        return True

    chosen = max(positioned, key=lambda row: row["ended_at"])
    page = _page(chosen["end_progress_bp"], edition_pages)
    if page is None:
        _refuse_all(
            storage,
            positioned,
            report,
            say,
            f"hardcover's edition {edition_id} records no page count, so the sitting's"
            " position cannot be mapped to a page",
        )
        return True

    # Claimed before the call, as the sessions half does: a row marked delivered before a
    # crash would lose a write that never happened, whereas a claim with no outcome is
    # simply re-sent — which for a page is harmless, since sending the same one twice says
    # the same thing.
    for row in positioned:
        storage.claim_delivery(row["document"], row["started_at"], DESTINATION)
    try:
        client.update_progress(
            read_id,
            progress_pages=page,
            action_at=_local_iso(chosen["ended_at"], timezone),
        )
    except HardcoverError as exc:
        _fail_all(storage, positioned, report, say, str(exc), status_code=exc.status_code)
        if exc.status_code in AUTH_STATUSES:
            report.aborted = str(exc)
            return False
        return True

    for row in positioned:
        storage.mark_delivered(row["document"], row["started_at"], DESTINATION, read_id)
        report.written.append((row["document"], row["started_at"]))
        if row is not chosen:
            report.coalesced.append((row["document"], row["started_at"]))
    superseded = len(positioned) - 1
    say(
        f"{path}: page {page} of {edition_pages} on read {read_id}"
        + (f" ({superseded} earlier sitting(s) superseded)" if superseded else "")
    )
    return True


def _finish_book(
    storage: Storage,
    client: HardcoverClient,
    document: str,
    user_book_id: str,
    finisher: Any,
    report: HardcoverReport,
    say: Callable[[str], None],
) -> bool:
    """Put the book on the Read shelf. False when the run must stop.

    Whether the status still needs writing is decided by `_outstanding_finish`, so this
    only ever runs for a finish that is genuinely outstanding — a re-run after a successful
    one does not reach here at all.
    """
    if finisher is None:
        return True
    storage.claim_delivery(document, finisher["started_at"], FINISH_DESTINATION)
    try:
        client.mark_read(user_book_id)
    except HardcoverError as exc:
        storage.mark_delivery_failed(
            document,
            finisher["started_at"],
            FINISH_DESTINATION,
            str(exc),
            status_code=exc.status_code,
        )
        report.failed[(document, finisher["started_at"])] = str(exc)
        say(f"{document}: could not mark the book read: {exc}")
        if exc.status_code in AUTH_STATUSES:
            report.aborted = str(exc)
            return False
        return True
    storage.mark_delivered(document, finisher["started_at"], FINISH_DESTINATION, "read")
    report.finished.append(document)
    say(f"{document}: marked read on hardcover")
    return True


def _finish_unavailable(
    storage: Storage,
    document: str,
    finisher: Any,
    report: HardcoverReport,
    say: Callable[[str], None],
    message: str,
    *,
    status_code: int | None = LOCAL_REFUSAL,
) -> None:
    """Record a finish that could not be attempted, and leave it outstanding to retry.

    Nothing is sent, so the claim carries a 4xx: the decision is retried on a later run
    without the relay having pretended the book was finished.
    """
    if finisher is None:
        return
    storage.claim_delivery(document, finisher["started_at"], FINISH_DESTINATION)
    storage.mark_delivery_failed(
        document,
        finisher["started_at"],
        FINISH_DESTINATION,
        message,
        status_code=status_code,
    )
    report.failed[(document, finisher["started_at"])] = message
    say(f"{document}: could not mark the book read: {message}")


def pending_report(storage: Storage, *, throttle_seconds: int) -> list[str]:
    """What Hardcover's next run would do, one line per outstanding thing.

    The page itself cannot be shown without asking Hardcover for the edition's count, so
    this reports the end position and the ids the write would be addressed to — which is
    what a dry run is for. Shared with `pagewire.delivery --dry-run` so both entry points
    describe the same queue.
    """
    lines: list[str] = []
    for document, rows in _by_document(storage.sittings_to_deliver()):
        path = rows[0]["book_path"] or document
        for row in _undelivered(storage, rows, DESTINATION):
            lines.append(
                f"  {path} @ {row['started_at']}"
                f"  end={row['end_progress_bp']}/10000"
                f"  hardcover book={row['hardcover_id'] or '—'}"
                f" edition={row['hardcover_edition_id'] or '—'}"
                f"{'  finished' if row['finished'] else ''}"
            )
        finisher = _outstanding_finish(storage, rows)
        if finisher is not None:
            lines.append(f"  {path} @ {finisher['started_at']}  marked read (status not written)")
    if not lines:
        return ["hardcover: nothing is waiting to be written"]
    return (
        [f"hardcover: {len(lines)} thing(s) pending, not counting the throttle:"]
        + lines
        + [
            f"  throttle window: {throttle_seconds}s per book"
            " (PAGEWIRE_HARDCOVER_THROTTLE_SECONDS)"
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """Write progress from the command line — the shape a systemd timer can run.

        python -m pagewire.hardcover_delivery              # write what has not been written
        python -m pagewire.hardcover_delivery --dry-run     # print what would be read
        python -m pagewire.hardcover_delivery --show-log    # the ledger, offline

    Derivation is separate and must run first (`python -m pagewire.sessions`).
    """
    import argparse

    from .config import Settings
    from .hardcover import HARDCOVER_ENDPOINT

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--db", default="", help="SQLite path (default: PAGEWIRE_DB)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the sittings that would be written, and the pages they map to",
    )
    arguments = parser.parse_args(argv)

    settings = Settings.from_env()
    storage = Storage(arguments.db or settings.db_path)

    if arguments.dry_run:
        return _dry_run(storage, settings, HARDCOVER_ENDPOINT)

    client = (
        HardcoverClient(
            settings.hardcover_token,
            endpoint=settings.hardcover_endpoint,
            timeout=settings.service_timeout_seconds,
        )
        if settings.hardcover_enabled
        else None
    )
    print(f"hardcover: {'on' if client is not None else 'off'}")
    report = deliver_hardcover(
        storage,
        client,
        throttle_seconds=settings.hardcover_throttle_seconds,
        timezone=settings.timezone,
        log=lambda line: print(line, flush=True),
    )
    print(
        f"\n{report.requests} request(s) for {len(report.written)} sitting(s),"
        f" {len(report.throttled)} book(s) throttled, {len(report.finished)} finished"
    )
    if report.aborted:
        print(f"\nstopped: {report.aborted}")
    if report.failed:
        print("\nnot written (fix the cause, then re-run):")
        for (document, started_at), message in report.failed.items():
            print(f"  {document} @ {started_at}\n    {message}")
        return 1
    return 0


def _dry_run(storage: Storage, settings: Any, endpoint: str) -> int:
    """Print what each book's next write would say, reading nothing from Hardcover.

    The page needs the edition's count, which only Hardcover has, so this reports the end
    position and the book it belongs to and says plainly that the page itself needs the
    service.
    """
    print(f"{endpoint} — what a write would be addressed to")
    for line in pending_report(storage, throttle_seconds=settings.hardcover_throttle_seconds):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand against the service
    raise SystemExit(main())
