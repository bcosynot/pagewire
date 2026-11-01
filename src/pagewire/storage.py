"""SQLite storage for the relay.

Raw events are kept verbatim and keyed on the device's own event id, because that is the
dedupe key the firmware promises: "a per-boot nonce plus a counter, so it never repeats
across queued events or reboots". The spike confirmed the format (`f55fb3e8-1`) and caught
the same id arriving twice, so idempotence is enforced by the primary key rather than by a
check-then-insert that would race.

Later tickets fill the other three tables. They are created here so the schema is visible
in one place, and so the sweep that writes them does not need a migration.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

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


def _text(value: Any) -> str | None:
    """Columns hold the device's own strings, or NULL when it sent nothing usable."""
    return value if isinstance(value, str) else None
