"""Storage-level invariants that are not about any one caller."""

from __future__ import annotations

from pagewire.storage import Storage


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
