"""Delivering finalised sittings to both destinations, and this is the entry point.

Both halves live behind one command because deploy wants one timer and one ledger, and
because the two destinations are independent by design: each has its own switch, its own
credential, and its own rules — see `hardcover_delivery` for the second one — so one
service being unreachable must not stop the other from being delivered.

The design's rule is that nothing is delivered while a sitting can still grow: Readwise
records only finished sessions, and a recorded session is immutable except for its caption.
So this module's input is `storage.sittings_to_deliver()` — finalised sittings of
*identified* books, joined in one query so that neither gate can be forgotten here.

Four decisions carry most of the weight, and each exists because of something the service
documents about itself:

**The claim is written before the POST.** `POST /sessions` has no idempotency key and the
docs are explicit that "sending the same request twice records two sessions". A ledger
written *after* the call would leave exactly the crash window the design's idempotence rule
is about, so the row goes in first and is marked delivered only once Readwise answers with
a session id.

**A claim with no outcome is reconciled, not re-sent.** Because the claim is written first,
a process that dies mid-call leaves a row that says "intent recorded, nothing known". The
only honest way out is to ask: `GET /sessions?book_ids=…` answers whether that sitting is
in the account, and matching on `started_at` is what pairs it with the claim. Found → the
row is marked delivered with the id Readwise issued. Confirmed absent → it is sent. Cannot
ask → it stays pending and the reason is reported; guessing is how a duplicate is born.

**A 5xx is not a refusal.** A 4xx means the service heard the call and recorded nothing, so
retrying it is free; a 5xx (or no answer at all) may have written the session, so the retry
goes through the same reconciliation check first. Criteria 2 and 4 both depend on this
distinction, which is why the ledger keeps `error_status`.

**A credential failure stops the run.** Retrying a 401 or a 403 `insufficient_scope` once
per sitting would turn one bad token into minutes of sleeping and a log full of the same
line. It is a global fact, so the run stops after recording it on every pending target.

Deliberately absent: no retry of a sitting the service refused per item (a `missing` or
`invalid_value` will say the same thing next run), and no second rule for what
"identified" means — the gate is `books.status`, exactly as identity left it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Mapping, Sequence

from .hardcover import HardcoverClient
from .hardcover_delivery import deliver_hardcover, pending_report
from .readwise import (
    MAX_SESSIONS_PER_REQUEST,
    ReadwiseClient,
    ReadwiseError,
)
from .storage import Storage

# The session destination. The book-level finish is its own ledger destination, because a
# re-run must no more re-send the PATCH than it may re-send a session.
DESTINATION = "readwise"
FINISH_DESTINATION = "readwise:finished"

FORMAT = "ebook"
BASIS_POINTS = 10_000

# A refusal that is expected to stop being true quickly. Both are 4xx, so nothing was
# recorded and a retry needs no reconciliation.
THROTTLE_STATUSES = frozenset({408, 429})
# A credential problem is global to the run, not particular to one sitting.
AUTH_STATUSES = frozenset({401, 403})


class _Undeliverable(RuntimeError):
    """This sitting cannot be expressed as a valid Readwise session, and never will be."""


class _Unconfirmed(RuntimeError):
    """A previous attempt's outcome could not be established, so nothing may be sent."""


@dataclass
class DeliveryReport:
    """What one delivery run did, in the shape the CLI prints and a test asserts on."""

    enabled: bool = True
    delivered: list[tuple[str, int]] = field(default_factory=list)
    reconciled: list[tuple[str, int]] = field(default_factory=list)
    failed: dict[tuple[str, int], str] = field(default_factory=dict)
    finished: list[str] = field(default_factory=list)
    aborted: str | None = None


@dataclass(frozen=True)
class _Policy:
    """The tunables one run uses, gathered so the arithmetic is not repeated."""

    timezone: str
    device_type: str
    device_vendor: str
    device_model: str
    batch_size: int
    retry_attempts: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float
    # Injected so a test can wait for nothing and a run can wait for real.
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class _Entry:
    """One sitting, ready to be a request item."""

    document: str
    started_at: int
    ended_at: int
    book_id: str
    finished: bool
    payload: dict[str, Any]

    @property
    def key(self) -> tuple[str, int]:
        return (self.document, self.started_at)


def deliver_readwise(
    storage: Storage,
    client: ReadwiseClient | None,
    *,
    timezone: str = "America/New_York",
    device_type: str = "E-Reader",
    device_vendor: str = "Xteink",
    device_model: str = "X4",
    batch_size: int = MAX_SESSIONS_PER_REQUEST,
    retry_attempts: int = 5,
    retry_backoff_seconds: float = 2.0,
    retry_backoff_max_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> DeliveryReport:
    """Deliver every deliverable sitting Readwise does not already have.

    `client=None` means the destination is switched off, not that it failed: nothing is
    read, nothing is claimed, and no request is made.
    """
    say = log or (lambda _line: None)
    report = DeliveryReport(enabled=client is not None)
    if client is None:
        say("readwise is switched off — nothing is delivered")
        return report

    policy = _Policy(
        timezone=timezone,
        device_type=device_type,
        device_vendor=device_vendor,
        device_model=device_model,
        # The API refuses more than ten and a batch of zero is not a request at all.
        batch_size=max(1, min(int(batch_size), MAX_SESSIONS_PER_REQUEST)),
        retry_attempts=max(1, int(retry_attempts)),
        retry_backoff_seconds=max(0.0, float(retry_backoff_seconds)),
        retry_backoff_max_seconds=max(0.0, float(retry_backoff_max_seconds)),
        sleep=sleep,
    )

    # Reconciliation answers are cached for the run, keyed on (book, sitting): the same
    # sitting can be asked about more than once when a retry comes round.
    cache: dict[tuple[str, int], str | None] = {}
    already_finished = {
        row["document"]
        for row in storage.send_log(FINISH_DESTINATION)
        if row["sent_at"] is not None
    }

    for document, rows in _by_document(storage.sittings_to_deliver()):
        book_id = rows[0]["readwise_id"]
        path = rows[0]["book_path"] or document
        if not book_id:
            # Identity's ruling is that a matched row carries an id. If one somehow does
            # not, inventing an id would address the wrong book, so the row is reported
            # and left alone.
            for row in rows:
                report.failed[(document, row["started_at"])] = (
                    "the row is matched but carries no readwise id, so Readwise cannot be"
                    " told about it"
                )
            say(f"{path}: matched but has no readwise id — nothing sent")
            continue

        entries = _entries(storage, rows, book_id=str(book_id), policy=policy, report=report, say=say)
        delivered_paths: list[_Entry] = []
        for batch in _batches(entries, policy.batch_size):
            delivered_paths.extend(
                _deliver_batch(storage, client, batch, policy, cache, report, say)
            )

        if report.aborted is not None:
            # A credential refusal is the same fact for every remaining book.
            break

        if document not in already_finished and any(entry.finished for entry in delivered_paths):
            finishing = next(entry for entry in delivered_paths if entry.finished)
            _finish_book(storage, client, document, str(book_id), finishing, report, say)

    return report


# -- building the request items --------------------------------------------


def _by_document(rows: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    """Group sittings by book, preserving the query's order."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(row["document"], []).append(row)
    return list(grouped.items())


def _entries(
    storage: Storage,
    rows: Sequence[Any],
    *,
    book_id: str,
    policy: _Policy,
    report: DeliveryReport,
    say: Callable[[str], None],
) -> list[_Entry]:
    """The sittings that still need sending, as request items.

    A sitting already in the ledger as delivered is dropped here silently: that is the
    idempotent case, not an error, and reporting it every run would bury the ones that
    matter.
    """
    entries: list[_Entry] = []
    for row in rows:
        ledger = storage.delivery_row(row["document"], row["started_at"], DESTINATION)
        if ledger is not None and ledger["sent_at"] is not None:
            continue
        try:
            payload = _payload(row, book_id=book_id, policy=policy)
        except _Undeliverable as exc:
            # Nothing is sent and nothing may be invented, so the sitting is recorded as
            # undeliverable and named. The claim carries a 400 so a later run retries the
            # decision without asking Readwise about a request that was never made.
            storage.claim_delivery(row["document"], row["started_at"], DESTINATION)
            storage.mark_delivery_failed(
                row["document"], row["started_at"], DESTINATION, str(exc), status_code=400
            )
            report.failed[(row["document"], row["started_at"])] = str(exc)
            say(f"{row['book_path'] or row['document']}: {exc}")
            continue
        entries.append(
            _Entry(
                document=row["document"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                book_id=book_id,
                finished=bool(row["finished"]),
                payload=payload,
            )
        )
    return entries


def _batches(entries: Sequence[_Entry], size: int) -> list[list[_Entry]]:
    return [list(entries[index : index + size]) for index in range(0, len(entries), size)]


def _payload(row: Any, *, book_id: str, policy: _Policy) -> dict[str, Any]:
    """One `CreateSessionItem`, in the documented shape.

    `percent_at_start`/`percent_at_end` are required for `format: ebook`, so a sitting with
    no readings positions cannot become a valid session. The relay refuses it and records
    why rather than sending a body Readwise would reject, and rather than inventing a
    position — a made-up percentage is a lie in the reading record.
    """
    start_bp = row["start_progress_bp"]
    end_bp = row["end_progress_bp"]
    if start_bp is None or end_bp is None:
        raise _Undeliverable(
            "the sitting has no start/end reading position, and an ebook session requires"
            " the percent pair"
        )
    try:
        numeric_book_id = int(book_id)
    except (TypeError, ValueError) as exc:
        raise _Undeliverable(
            f"the readwise id {book_id!r} is not a number, so no session can name it"
        ) from exc

    return {
        "book_id": numeric_book_id,
        "started_at": _iso(row["started_at"]),
        "ended_at": _iso(row["ended_at"]),
        "paused_seconds": int(row["paused_seconds"] or 0),
        "format": FORMAT,
        "percent_at_start": _fraction(start_bp),
        "percent_at_end": _fraction(end_bp),
        "timezone": policy.timezone,
        "device": {
            "type": policy.device_type,
            "vendor": policy.device_vendor,
            "model": policy.device_model,
        },
        # Always explicit: the field defaults to false server-side, and a default that
        # happens to match is not the same as saying it.
        "publish_to_feed": False,
    }


# -- sending ----------------------------------------------------------------


def _deliver_batch(
    storage: Storage,
    client: ReadwiseClient,
    entries: list[_Entry],
    policy: _Policy,
    cache: dict[tuple[str, int], str | None],
    report: DeliveryReport,
    say: Callable[[str], None],
) -> list[_Entry]:
    """Send one batch, retrying with backoff. Returns the entries now known delivered."""
    delivered: list[_Entry] = []
    unresolved = list(entries)

    for attempt in range(policy.retry_attempts):
        sendable: list[_Entry] = []
        for entry in unresolved:
            try:
                landed = _reconciled_session(storage, client, entry, cache, say)
            except _Unconfirmed as exc:
                # A previous attempt may have written this sitting and the question could
                # not be put to Readwise. Sending now is how a duplicate is made, so the
                # sitting waits and the reason is visible.
                storage.mark_delivery_failed(
                    entry.document, entry.started_at, DESTINATION, str(exc)
                )
                report.failed[entry.key] = str(exc)
                say(f"{entry.document} @ {entry.started_at}: {exc}")
                continue
            if landed is not None:
                storage.mark_delivered(entry.document, entry.started_at, DESTINATION, landed)
                report.reconciled.append(entry.key)
                delivered.append(entry)
                say(f"{entry.document} @ {entry.started_at}: already in Readwise as {landed}")
                continue
            sendable.append(entry)

        unresolved = sendable
        if not unresolved:
            return delivered

        for entry in unresolved:
            storage.claim_delivery(entry.document, entry.started_at, DESTINATION)

        try:
            results = client.record_sessions([entry.payload for entry in unresolved])
        except ReadwiseError as exc:
            say(f"sessions request failed: {exc}")
            for entry in unresolved:
                storage.mark_delivery_failed(
                    entry.document,
                    entry.started_at,
                    DESTINATION,
                    str(exc),
                    status_code=exc.status_code,
                )
                report.failed[entry.key] = str(exc)
            if exc.status_code in AUTH_STATUSES:
                # Global, and sleeping on it once per sitting only makes the run slower.
                report.aborted = str(exc)
                return delivered
            if attempt == policy.retry_attempts - 1:
                return delivered
            _pause(policy, attempt)
            continue

        for entry, result in zip(unresolved, results):
            if result.created:
                storage.mark_delivered(
                    entry.document, entry.started_at, DESTINATION, result.session_id
                )
                report.delivered.append(entry.key)
                delivered.append(entry)
                say(f"{entry.document} @ {entry.started_at}: session {result.session_id}")
                continue
            # An item the service named as invalid will say the same thing next run, so it
            # is reported rather than retried in this one.
            message = result.error or "Readwise refused this session"
            storage.mark_delivery_failed(
                entry.document, entry.started_at, DESTINATION, message
            )
            report.failed[entry.key] = message
            say(f"{entry.document} @ {entry.started_at}: {message}")

        # Every item in the batch was answered: created, or refused with a reason that a
        # retry would only repeat.
        return delivered

    return delivered


def _reconciled_session(
    storage: Storage,
    client: ReadwiseClient,
    entry: _Entry,
    cache: dict[tuple[str, int], str | None],
    say: Callable[[str], None],
) -> str | None:
    """The session id if this sitting already landed, None if it certainly did not.

    Only asked when an earlier attempt could have written it. A 4xx in the ledger means the
    service heard the request and recorded nothing, so the answer is already known and no
    call is made.
    """
    ledger = storage.delivery_row(entry.document, entry.started_at, DESTINATION)
    if ledger is None:
        return None
    if ledger["sent_at"] is not None:
        return ledger["external_id"]
    if ledger["error"] is not None and ledger["error_status"] is not None:
        if ledger["error_status"] < 500:
            return None

    if entry.key in cache:
        return cache[entry.key]

    try:
        sessions = client.sessions_for_book(
            entry.book_id, started_after=_iso(entry.started_at - 60)
        )
    except ReadwiseError as exc:
        raise _Unconfirmed(
            f"could not confirm whether an earlier attempt recorded this sitting: {exc}"
        ) from exc

    for session in sessions:
        started = _epoch(session.get("started_at"))
        if started is None or started != entry.started_at:
            continue
        session_id = session.get("id")
        found = None if session_id is None else str(session_id)
        cache[entry.key] = found
        if found is None:
            raise _Unconfirmed(
                "Readwise lists a session for this sitting but its id could not be read"
            )
        return found

    cache[entry.key] = None
    say(f"{entry.document} @ {entry.started_at}: not at Readwise — sending")
    return None


def _finish_book(
    storage: Storage,
    client: ReadwiseClient,
    document: str,
    book_id: str,
    entry: _Entry,
    report: DeliveryReport,
    say: Callable[[str], None],
) -> None:
    """Put the book on the finished shelf, once its finishing session is delivered."""
    storage.claim_delivery(document, entry.started_at, FINISH_DESTINATION)
    try:
        client.finish_book(book_id, finished_at=_iso(entry.ended_at))
    except ReadwiseError as exc:
        storage.mark_delivery_failed(
            document,
            entry.started_at,
            FINISH_DESTINATION,
            str(exc),
            status_code=exc.status_code,
        )
        report.failed[(document, entry.started_at)] = str(exc)
        say(f"{document}: could not mark the book finished: {exc}")
        return
    storage.mark_delivered(document, entry.started_at, FINISH_DESTINATION, "finished")
    report.finished.append(document)
    say(f"{document}: finished {_iso(entry.ended_at)}")


# -- small helpers ----------------------------------------------------------


def _pause(policy: _Policy, attempt: int) -> None:
    """Wait before the next attempt, doubling from the configured backoff."""
    delay = min(
        policy.retry_backoff_seconds * (2**attempt), policy.retry_backoff_max_seconds
    )
    policy.sleep(delay)


def _fraction(basis_points: Any) -> float:
    """The device's basis points as the API's 0–1 fraction, clamped to the accepted range."""
    value = int(basis_points)
    value = min(max(value, 0), BASIS_POINTS)
    return value / BASIS_POINTS


def _iso(epoch: Any) -> str:
    """Epoch seconds as the ISO-8601 UTC string the API takes."""
    return datetime.fromtimestamp(int(epoch), dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> int | None:
    """An ISO-8601 timestamp from the API as epoch seconds, or None if unreadable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return int(parsed.timestamp())


def main(argv: list[str] | None = None) -> int:
    """Deliver from the command line — the shape a systemd timer can run.

        python -m pagewire.delivery                # deliver what is finalised and identified
        python -m pagewire.delivery --dry-run       # print the requests, send nothing
        python -m pagewire.delivery --show-log      # what the ledger says, offline

    Derivation is separate and must run first (`python -m pagewire.sessions`); this reads
    the derived table rather than the raw events.
    """
    import argparse
    import json

    from .config import Settings

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--db", default="", help="SQLite path (default: PAGEWIRE_DB)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the session bodies that would be posted, without posting them",
    )
    parser.add_argument(
        "--show-log", action="store_true", help="print the send log and exit, offline"
    )
    arguments = parser.parse_args(argv)

    settings = Settings.from_env()
    storage = Storage(arguments.db or settings.db_path)

    if arguments.show_log:
        return _show_log(storage)

    client = (
        ReadwiseClient(
            settings.readwise_token,
            base_url=settings.readwise_base_url,
            timeout=settings.service_timeout_seconds,
        )
        if settings.readwise_enabled
        else None
    )

    if arguments.dry_run:
        code = _dry_run(storage, settings, client)
        print(f"\nhardcover: {'on' if settings.hardcover_enabled else 'off'}")
        for line in pending_report(
            storage, throttle_seconds=settings.hardcover_throttle_seconds
        ):
            print(line)
        return code

    print(f"readwise: {'on' if client is not None else 'off'}")
    report = deliver_readwise(
        storage,
        client,
        timezone=settings.timezone,
        device_type=settings.device_type,
        device_vendor=settings.device_vendor,
        device_model=settings.device_model,
        batch_size=settings.delivery_batch_size,
        retry_attempts=settings.delivery_retry_attempts,
        retry_backoff_seconds=settings.delivery_retry_backoff_seconds,
        retry_backoff_max_seconds=settings.delivery_retry_backoff_max_seconds,
        log=lambda line: print(line, flush=True),
    )
    print(
        f"\nreadwise: {len(report.delivered)} sent, {len(report.reconciled)} already present,"
        f" {len(report.failed)} failed, {len(report.finished)} finished"
    )
    if report.aborted:
        print(f"  stopped: {report.aborted}")
    if report.failed:
        print("\n  not delivered (fix the cause, then re-run):")
        for (document, started_at), message in report.failed.items():
            print(f"    {document} @ {started_at}\n      {message}")

    # The two destinations are independent by design — either may be the only one switched
    # on — so one unreachable service must not stop the other from being delivered.
    hardcover = _run_hardcover(storage, settings)

    return 1 if (report.failed or hardcover.failed) else 0


def _run_hardcover(storage: Storage, settings: Any) -> Any:
    """Run the second destination, which has its own switch and its own credential."""
    print(f"\nhardcover: {'on' if settings.hardcover_enabled else 'off'}")
    client = (
        HardcoverClient(
            settings.hardcover_token,
            endpoint=settings.hardcover_endpoint,
            timeout=settings.service_timeout_seconds,
        )
        if settings.hardcover_enabled
        else None
    )
    report = deliver_hardcover(
        storage,
        client,
        throttle_seconds=settings.hardcover_throttle_seconds,
        timezone=settings.timezone,
        log=lambda line: print(f"  {line}", flush=True),
    )
    print(
        f"  {report.requests} write(s) for {len(report.written)} sitting(s),"
        f" {len(report.throttled)} book(s) throttled, {len(report.finished)} finished"
    )
    if report.aborted:
        print(f"  stopped: {report.aborted}")
    if report.failed:
        print("\n  not written (fix the cause, then re-run):")
        for (document, started_at), message in report.failed.items():
            print(f"    {document} @ {started_at}\n      {message}")
    return report


def _dry_run(storage: Storage, settings: Any, client: ReadwiseClient | None) -> int:
    """Print the exact bodies, so a run can be inspected before it writes anything."""
    import json

    if client is None:
        print("readwise is switched off — nothing would be delivered")
        return 0

    policy = _Policy(
        timezone=settings.timezone,
        device_type=settings.device_type,
        device_vendor=settings.device_vendor,
        device_model=settings.device_model,
        batch_size=max(1, min(settings.delivery_batch_size, MAX_SESSIONS_PER_REQUEST)),
        retry_attempts=max(1, settings.delivery_retry_attempts),
        retry_backoff_seconds=settings.delivery_retry_backoff_seconds,
        retry_backoff_max_seconds=settings.delivery_retry_backoff_max_seconds,
    )
    payloads: list[dict[str, Any]] = []
    for document, rows in _by_document(storage.sittings_to_deliver()):
        book_id = rows[0]["readwise_id"]
        if not book_id:
            continue
        for row in rows:
            ledger = storage.delivery_row(document, row["started_at"], DESTINATION)
            if ledger is not None and ledger["sent_at"] is not None:
                continue
            try:
                payloads.append(_payload(row, book_id=str(book_id), policy=policy))
            except _Undeliverable as exc:
                print(f"would refuse {document} @ {row['started_at']}: {exc}")
    for index in range(0, len(payloads), policy.batch_size):
        batch = payloads[index : index + policy.batch_size]
        print(json.dumps({"items": batch}, indent=2, sort_keys=True))
    print(f"\n{len(payloads)} session(s) would be posted in"
          f" {max(1, -(-len(payloads) // policy.batch_size))} request(s)")
    return 0


def _show_log(storage: Storage) -> int:
    """The ledger, as the owner would want to read it when something looks wrong."""
    rows = storage.send_log()
    if not rows:
        print("the send log is empty")
        return 0
    print(f"{len(rows)} delivery target(s)\n")
    for row in rows:
        if row["sent_at"] is not None:
            state = f"delivered {row['external_id'] or ''}".strip()
        elif row["error"]:
            state = f"pending — {row['error']}"
        else:
            state = "claimed, outcome unknown (re-checked on the next run)"
        print(f"  {row['document']} @ {row['started_at']}  {row['destination']}: {state}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand against the service
    raise SystemExit(main())
