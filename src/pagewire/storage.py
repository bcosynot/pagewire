"""SQLite storage for the relay.

Raw events are kept verbatim and keyed on the device's own event id, because that is the
dedupe key the firmware promises: "a per-boot nonce plus a counter, so it never repeats
across queued events or reboots". The spike confirmed the format (`f55fb3e8-1`) and caught
the same id arriving twice, so idempotence is enforced by the primary key rather than by a
check-then-insert that would race.

The metadata sweep fills `books`; identity resolution writes the ids the two services
issued onto those rows; the delivery tickets fill `send_log`. All four tables are created
here so the schema is visible in one place.

`CREATE TABLE IF NOT EXISTS` is a lie about an existing database — it does nothing to a
table that is already there — so a column added to `SCHEMA` would never reach the deployed
relay's file. Additions therefore also go through `_migrate`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# The two statuses that mean something identified the book. Delivery gates on these; every
# other value — `unmatched` above all — must stay undelivered.
MATCHED_STATUSES = ("auto", "confirmed")

SCHEMA = """
-- Raw events, exactly as they arrived. Kept even after they are turned into sittings, so
-- a change to the derivation can be replayed over history instead of losing it.
CREATE TABLE IF NOT EXISTS raw_events (
    id          TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    ts_raw      TEXT,
    book        TEXT,
    document    TEXT,
    received_at INTEGER NOT NULL,
    payload     TEXT NOT NULL
);

-- One row per book, keyed on the device's content hash. `hash_method` records which of
-- the two methods keyed the row (content or filename), because the device can be
-- switched between them and a reset would silently re-key everything.
--
-- `readwise_id`, `hardcover_id` and `hardcover_edition_id` are the ids the services
-- issued for this book, written once resolution has them. `status` says how they were
-- arrived at: `unmatched` (nothing matched it yet), `auto` (a confident match), or
-- `confirmed` (Vivek accepted a match in the review page). `error` holds the last
-- failure's own words, so a refused call is visible rather than a silent skip.
CREATE TABLE IF NOT EXISTS books (
    document       TEXT PRIMARY KEY,
    hash_method    TEXT,
    book_path      TEXT,
    title          TEXT,
    author         TEXT,
    isbn           TEXT,
    readwise_id    TEXT,
    hardcover_id   TEXT,
    hardcover_edition_id TEXT,
    status         TEXT NOT NULL DEFAULT 'unmatched',
    error          TEXT,
    first_seen_at  INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- Sittings derived from raw events. `event_ids` is the JSON list of constituents, which
-- is what makes derivation idempotent: re-deriving replaces this row rather than adding
-- another.
CREATE TABLE IF NOT EXISTS derived_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    document         TEXT NOT NULL,
    started_at       INTEGER NOT NULL,
    ended_at         INTEGER NOT NULL,
    active_seconds   INTEGER NOT NULL,
    paused_seconds   INTEGER NOT NULL,
    start_progress_bp INTEGER,
    end_progress_bp  INTEGER,
    finalised        INTEGER NOT NULL DEFAULT 0,
    event_ids        TEXT NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (document, started_at)
);

-- One row per delivery attempt target, so a crash between sending and recording cannot
-- produce a second session at the service.
CREATE TABLE IF NOT EXISTS send_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    document        TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    destination     TEXT NOT NULL,
    external_id     TEXT,
    sent_at         INTEGER,
    error           TEXT,
    UNIQUE (document, started_at, destination)
);
"""


@dataclass(frozen=True)
class BookRecord:
    """One book, as the metadata sweep found it on the card.

    `document` is the device's own content hash, which is why it is the primary key:
    it survives a rename, a move, or a reorganised card. `hash_method` records which of
    the device's two methods produced it, because the setting can change and a device
    reset would otherwise silently re-key every row.
    """

    document: str
    hash_method: str
    book_path: str
    title: str | None = None
    author: str | None = None
    isbn: str | None = None
    status: str = "unmatched"


class Storage:
    """A thin wrapper over one SQLite file. One connection per call is fine here."""

    def __init__(self, path: str) -> None:
        self.path = path
        # An in-memory database lives and dies with its connection, so one has to be kept
        # for the lifetime of this object. Without it every `_connect()` opens a fresh
        # empty database, the schema created by `_init` is invisible to the next call, and
        # reads come back empty — which reads as "nothing was stored" rather than as an
        # error. Files do not have this problem: they are shared by path.
        self._memory: sqlite3.Connection | None = None
        if path == ":memory:":
            self._memory = sqlite3.connect(":memory:")
        else:
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = self._memory or sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        # WAL keeps a reader (the review page) from blocking the ingest path.
        if self._memory is None:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            _migrate(connection)

    def store_event(
        self,
        payload: dict[str, Any],
        received_at: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> bool:
        """Store an event. Returns True if it was new, False if it was a duplicate.

        A duplicate is not an error: delivery is at-least-once by design, and the device's
        outbox only clears once the relay acknowledges. So this returns a boolean for the
        caller to report, and never raises for a repeat.

        `payload` is the normalised event the columns are read from. `raw` is the body as
        it arrived, before placeholders were blanked, and is what gets stored in the
        `payload` column — "raw events are kept verbatim" is only true if the text the
        device actually sent survives, and an unsubstituted `{event.end_percent}` is part
        of that text.
        """
        received = int(time.time()) if received_at is None else received_at
        verbatim = payload if raw is None else raw
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO raw_events
                    (id, event, ts_raw, book, document, received_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["event"],
                    _text(payload.get("ts")),
                    _text(payload.get("book")),
                    _text(payload.get("document")),
                    received,
                    json.dumps(verbatim, sort_keys=True),
                ),
            )
            return cursor.rowcount == 1

    def count_events(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])

    def events(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(connection.execute("SELECT * FROM raw_events ORDER BY received_at, id"))

    def sittings(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM derived_sessions ORDER BY document, started_at"
                )
            )

    def replace_sittings(self, sittings: Iterable[Any]) -> None:
        """Replace the whole derived set with `sittings`, in one transaction.

        Derived sessions are a function of the raw events, which are kept forever, so the
        table is a cache and is rewritten rather than appended to. That is what makes a
        replay idempotent: deriving twice cannot produce a second row for the same
        reading, and a rule change re-derives history instead of leaving stale rows behind.
        """
        updated_at = int(time.time())
        rows = [
            (
                sitting.document,
                sitting.started_at,
                sitting.ended_at,
                sitting.active_seconds,
                sitting.paused_seconds,
                sitting.start_progress_bp,
                sitting.end_progress_bp,
                int(sitting.finalised),
                json.dumps(list(sitting.event_ids)),
                updated_at,
            )
            for sitting in sittings
        ]
        with self._connect() as connection:
            connection.execute("DELETE FROM derived_sessions")
            connection.executemany(
                """
                INSERT INTO derived_sessions
                    (document, started_at, ended_at, active_seconds, paused_seconds,
                     start_progress_bp, end_progress_bp, finalised, event_ids, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    # -- books (the metadata sweep's table) --------------------------------

    def upsert_books(self, records: Iterable[BookRecord]) -> int:
        """Insert or refresh book rows, in one transaction. Returns how many were written.

        Written as an upsert rather than an insert so that a second sweep over a card
        whose books have moved or been re-read refreshes the stored path and metadata
        instead of failing on the primary key. Existing values win where the new sweep
        has none: a sweep that could not read an ISBN must not erase one already known.

        `status` is deliberately *not* refreshed from the record. A sweep over a card
        cannot know what resolution decided, and letting the record's default win would
        silently un-match every book the relay had already identified — observed as a
        second sweep resetting the whole queue. Only a row's first insert takes its
        status from the record.
        """
        updated_at = int(time.time())
        rows = [
            (
                record.document,
                record.hash_method,
                record.book_path,
                record.title,
                record.author,
                record.isbn,
                record.status,
                updated_at,
                updated_at,
            )
            for record in records
        ]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO books
                    (document, hash_method, book_path, title, author, isbn, status,
                     first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document) DO UPDATE SET
                    hash_method = excluded.hash_method,
                    book_path   = excluded.book_path,
                    title       = COALESCE(excluded.title, books.title),
                    author      = COALESCE(excluded.author, books.author),
                    isbn        = COALESCE(excluded.isbn, books.isbn),
                    updated_at  = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def known_book_paths(self) -> set[str]:
        """Every SD path already tied to a book, so a sweep can skip re-downloading it.

        Keyed on the path rather than the hash deliberately: the listing carries names
        and sizes but no hash, and hashing a file means downloading it — which is the
        cost this exists to avoid on a second run.
        """
        with self._connect() as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT book_path FROM books WHERE book_path IS NOT NULL"
                )
            }

    def books(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return list(connection.execute("SELECT * FROM books ORDER BY book_path"))

    def count_books(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM books").fetchone()[0])

    def book(self, document: str) -> sqlite3.Row | None:
        """One book row by its content hash, or None when no row carries that hash."""
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM books WHERE document = ?", (document,)
            ).fetchone()

    def books_needing_confirmation(self) -> list[sqlite3.Row]:
        """Rows no ISBN can resolve, so a human has to confirm the match once.

        The design's rule is that only a confident match is added automatically, and an
        ISBN is what makes a match confident. Without one the row waits here rather than
        being guessed at, because a Readwise sitting cannot be moved to another book
        after it has been written.
        """
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM books WHERE isbn IS NULL AND status = 'unmatched'"
                    " ORDER BY book_path"
                )
            )

    def books_with_errors(self) -> list[sqlite3.Row]:
        """Rows whose last resolution attempt failed, so the page can show why."""
        with self._connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM books WHERE error IS NOT NULL ORDER BY book_path"
                )
            )

    def deliverable_books(self) -> list[sqlite3.Row]:
        """Rows a destination may be told about: the ones something actually matched.

        `unmatched` covers both a book no ISBN could resolve and one whose resolution
        failed, and neither may be delivered — no matter how many sittings are derived
        against them. A wrong match sends immutable sittings to the wrong book, so the
        gate is the status, and only the two statuses that mean "identified" pass it.
        """
        placeholders = ", ".join("?" for _ in MATCHED_STATUSES)
        with self._connect() as connection:
            return list(
                connection.execute(
                    f"SELECT * FROM books WHERE status IN ({placeholders})"
                    " ORDER BY book_path",
                    MATCHED_STATUSES,
                )
            )

    # -- identity (what the two services issued) ---------------------------

    def set_book_identity(
        self,
        document: str,
        *,
        readwise_id: str | None = None,
        hardcover_id: str | None = None,
        hardcover_edition_id: str | None = None,
        status: str = "auto",
    ) -> None:
        """Record the ids a service issued, and mark the row matched.

        Only the ids actually passed are written — the rest keep whatever they had — so a
        Readwise-only or Hardcover-only configuration works without inventing ids for the
        destination that is switched off. `error` is cleared, because a row that resolved
        is no longer in a failed state and a stale reason on screen would misreport it.
        """
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE books SET
                    readwise_id          = COALESCE(?, readwise_id),
                    hardcover_id         = COALESCE(?, hardcover_id),
                    hardcover_edition_id = COALESCE(?, hardcover_edition_id),
                    status               = ?,
                    error                = NULL,
                    updated_at           = ?
                WHERE document = ?
                """,
                (
                    readwise_id,
                    hardcover_id,
                    hardcover_edition_id,
                    status,
                    int(time.time()),
                    document,
                ),
            )

    def set_book_error(self, document: str, message: str) -> None:
        """Record why a row could not be resolved. Never touches the status.

        The status is what delivery gates on, so the reason is kept in its own column: a
        book that failed to resolve stays undeliverable, and a book that resolved stays
        deliverable, whatever the last attempt had to say.
        """
        with self._connect() as connection:
            connection.execute(
                "UPDATE books SET error = ?, updated_at = ? WHERE document = ?",
                (message, int(time.time()), document),
            )


def _migrate(connection: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema, in place.

    `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it was, so a column
    added to `SCHEMA` reaches a fresh database and no other one. The deployed relay's file
    already holds every hash-to-book mapping the sweep took 14 minutes to collect, so the
    column is added to it rather than the file being rebuilt.
    """
    columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
    if "error" not in columns:
        connection.execute("ALTER TABLE books ADD COLUMN error TEXT")


def _text(value: Any) -> str | None:
    """Columns hold the device's own strings, or NULL when it sent nothing usable."""
    return value if isinstance(value, str) else None
