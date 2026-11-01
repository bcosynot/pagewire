"""Storage-level invariants that are not about any one caller."""

from __future__ import annotations

import sqlite3

from pagewire.storage import BookRecord, Storage


def test_an_in_memory_database_keeps_its_schema_between_calls():
    """Regression: `:memory:` must be one database, not a fresh one per connection.

    An in-memory SQLite database is owned by its connection. Opening a new connection per
    call therefore gave every read an empty database with no tables — which surfaced as
    `no such table` at best, and as a silently empty result set wherever the caller did
    not assert a count. Files are shared by path and never had the problem.
    """
    storage = Storage(":memory:")
    assert storage.store_event({"id": "e-1", "event": "reader.exit"}, received_at=100) is True

    assert storage.count_events() == 1
    assert len(storage.events()) == 1
    assert storage.store_event({"id": "e-1", "event": "reader.exit"}, received_at=100) is False
    assert storage.count_events() == 1


# ---------------------------------------------------------------- identity rows


def _book(
    document: str = "doc-1",
    path: str = "/Samit Basu - The Jinn-Bot of Shantiport.epub",
    title: str | None = "The Jinn-Bot of Shantiport",
    author: str | None = "Samit Basu",
    isbn: str | None = "9781915590012",
) -> BookRecord:
    """A book as the metadata sweep would have recorded it."""
    return BookRecord(
        document=document,
        hash_method="content",
        book_path=path,
        title=title,
        author=author,
        isbn=isbn,
    )


def test_the_ids_the_two_services_issued_are_read_back_off_the_row():
    """Criterion 1 and 2: the row carries the ids, so delivery needs no second lookup."""
    storage = Storage(":memory:")
    storage.upsert_books([_book()])

    storage.set_book_identity(
        "doc-1",
        readwise_id="4790033",
        hardcover_id="328491",
        hardcover_edition_id="990011",
        status="auto",
    )

    row = storage.book("doc-1")
    assert row["readwise_id"] == "4790033"
    assert row["hardcover_id"] == "328491"
    assert row["hardcover_edition_id"] == "990011"
    assert row["status"] == "auto"


def test_a_refused_call_is_recorded_against_the_row_rather_than_swallowed():
    """Criterion 5: an under-scoped token has to be visible on the row it blocked.

    The status must not move: a book whose resolution failed is not matched, and the
    delivery gate reads the status. The reason lives beside it, in its own column, so an
    error can never make a book deliverable.
    """
    storage = Storage(":memory:")
    storage.upsert_books([_book()])

    storage.set_book_error("doc-1", "403 insufficient_scope from Hardcover: write:library")

    row = storage.book("doc-1")
    assert "insufficient_scope" in row["error"]
    assert row["status"] == "unmatched"
    assert [r["document"] for r in storage.deliverable_books()] == []


def test_a_later_successful_match_clears_the_error_it_replaces():
    """An error is the state of the last attempt, not a permanent mark on the book."""
    storage = Storage(":memory:")
    storage.upsert_books([_book()])
    storage.set_book_error("doc-1", "503 from Readwise")

    storage.set_book_identity("doc-1", readwise_id="4790033", status="auto")

    row = storage.book("doc-1")
    assert row["error"] is None
    assert row["status"] == "auto"


def test_a_row_is_deliverable_only_once_something_matched_it():
    """Criterion 4's gate: a book no service has identified is never delivered.

    `unmatched` is the state the sweep writes and the state a failed or ambiguous match
    leaves behind, so it is the one state delivery must refuse. `auto` and `confirmed`
    are the two the design names as matched.
    """
    storage = Storage(":memory:")
    storage.upsert_books(
        [
            _book(document="doc-unmatched", isbn=None),
            _book(document="doc-auto"),
            _book(document="doc-confirmed"),
        ]
    )
    storage.set_book_identity("doc-auto", readwise_id="1", status="auto")
    storage.set_book_identity("doc-confirmed", readwise_id="2", status="confirmed")

    deliverable = {row["document"] for row in storage.deliverable_books()}

    assert deliverable == {"doc-auto", "doc-confirmed"}
    assert "doc-unmatched" not in deliverable


def test_an_existing_database_gains_the_identity_columns_without_losing_books(tmp_path):
    """The deployed relay already has a `books` table; identity must not need a reset.

    Written as a migration rather than a schema rewrite because the alternative is asking
    the owner to delete a database that holds the only record of which hash is which book.
    """
    path = tmp_path / "pagewire.db"
    legacy = sqlite3.connect(path)
    # The `books` table exactly as it stood before identity resolution existed: no
    # `error` column.
    legacy.executescript(
        """
        CREATE TABLE books (
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
        INSERT INTO books (document, hash_method, book_path, title, author, isbn, status,
                           first_seen_at, updated_at)
        VALUES ('doc-swept', 'content', '/A Book.epub', 'A Book', 'Someone',
                '9781915590012', 'unmatched', 100, 100);
        """
    )
    legacy.commit()
    legacy.close()

    storage = Storage(str(path))

    row = storage.book("doc-swept")
    assert row["title"] == "A Book"
    storage.set_book_error("doc-swept", "403 insufficient_scope from Hardcover")
    assert "insufficient_scope" in storage.book("doc-swept")["error"]


# ---------------------------------------------------------------- the send log


def test_a_claim_is_exclusive_so_a_second_attempt_cannot_claim_the_same_target():
    """The ledger's one job: one row per (book, sitting, destination), whoever asks."""
    storage = Storage(":memory:")

    assert storage.claim_delivery("doc-1", 100, "readwise") is True
    assert storage.claim_delivery("doc-1", 100, "readwise") is False
    assert len(storage.send_log()) == 1


def test_a_failed_delivery_never_reads_as_delivered():
    """The state a retry depends on: pending, with the reason and the status it came with."""
    storage = Storage(":memory:")
    storage.claim_delivery("doc-1", 100, "readwise")

    storage.mark_delivery_failed(
        "doc-1", 100, "readwise", "HTTP 503 internal_error", status_code=503
    )

    row = storage.delivery_row("doc-1", 100, "readwise")
    assert row is not None
    assert row["sent_at"] is None
    assert row["external_id"] is None
    assert row["error_status"] == 503
    assert "503" in row["error"]


def test_marking_a_target_delivered_records_the_id_and_clears_the_failure():
    """A later success has to erase the earlier refusal, or the row reads as broken forever."""
    storage = Storage(":memory:")
    storage.claim_delivery("doc-1", 100, "readwise")
    storage.mark_delivery_failed("doc-1", 100, "readwise", "HTTP 500", status_code=500)

    storage.mark_delivered("doc-1", 100, "readwise", "90211876")

    row = storage.delivery_row("doc-1", 100, "readwise")
    assert row is not None
    assert row["external_id"] == "90211876"
    assert row["sent_at"] is not None
    assert row["error"] is None
    assert row["error_status"] is None


def test_an_existing_database_gains_the_delivery_columns_without_a_reset(tmp_path):
    """A deployed relay's `derived_sessions` and `send_log` predate this ticket's columns.

    `CREATE TABLE IF NOT EXISTS` would leave both tables exactly as they are, so the two
    new columns are added by the migration. The rows already in them have to survive: the
    send log is the only record of what Readwise has.
    """
    path = tmp_path / "pagewire.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE derived_sessions (
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
        INSERT INTO derived_sessions (document, started_at, ended_at, active_seconds,
                                      paused_seconds, finalised, event_ids, updated_at)
        VALUES ('doc-1', 100, 200, 60, 40, 1, '[]', 300);

        CREATE TABLE send_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            document        TEXT NOT NULL,
            started_at      INTEGER NOT NULL,
            destination     TEXT NOT NULL,
            external_id     TEXT,
            sent_at         INTEGER,
            error           TEXT,
            UNIQUE (document, started_at, destination)
        );
        INSERT INTO send_log (document, started_at, destination, external_id, sent_at)
        VALUES ('doc-already-sent', 50, 'readwise', '90211000', 60);
        """
    )
    legacy.commit()
    legacy.close()

    storage = Storage(str(path))

    assert storage.send_log()[0]["external_id"] == "90211000"
    sessions = {row["started_at"]: row for row in storage.sittings()}
    assert sessions[100]["finished"] == 0
    storage.claim_delivery("doc-1", 100, "readwise")
    storage.mark_delivery_failed("doc-1", 100, "readwise", "HTTP 502", status_code=502)
    failed = storage.delivery_row("doc-1", 100, "readwise")
    assert failed is not None and failed["error_status"] == 502
