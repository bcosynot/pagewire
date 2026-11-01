"""Delivering derived sittings to Readwise, against a server speaking the documented shapes.

Every test here names a criterion of the delivery ticket:

* 1 — a finalised sitting appears with the expected start, end, positions and pause;
* 2 — delivering the same sitting twice creates exactly one session;
* 3 — `publish_to_feed` is false on every session created;
* 4 — a 4xx or 5xx retries with backoff and never drops the sitting or marks it delivered;
* 5 — 100 % is finished with a finish date, 99 % is not;
* 6 — with the destination switched off, no request reaches Readwise.

The server is a real HTTP server (`service_fixtures.ReadwiseServer`), not a stub: the
relay's whole job at this point is the JSON it puts on the wire, and a mock would only
prove that the mock and the client agree.
"""

from __future__ import annotations

import pytest

from pagewire.delivery import deliver_readwise
from pagewire.readwise import ReadwiseClient
from pagewire.sessions import Sitting
from pagewire.storage import BookRecord, Storage

from service_fixtures import ReadwiseServer

DOC = "doc-jinn-bot"
OTHER = "doc-burning-god"
BOOK_ID = 4790033
ISBN = "9781915590012"

# 2025-10-09T08:53:20Z — one sitting, and its fields are the ones the payload must carry.
START = 1_760_000_000
END = START + 1800

JINN_BOT = {
    "id": BOOK_ID,
    "title": "The Jinn-Bot of Shantiport",
    "author": "Samit Basu",
    "isbn": ISBN,
    "status": "currently_reading",
    "product": {"isbn": ISBN},
}


def _server(**overrides) -> ReadwiseServer:
    values: dict = {"library": {ISBN: dict(JINN_BOT)}}
    values.update(overrides)
    return ReadwiseServer(**values)


def _client(server: ReadwiseServer, token: str = "rw-token") -> ReadwiseClient:
    return ReadwiseClient(token, base_url=server.address)


def _store(*, identified: bool = True, status: str = "auto") -> Storage:
    """One swept book, optionally identified, in an empty relay database."""
    store = Storage(":memory:")
    store.upsert_books(
        [
            BookRecord(
                document=DOC,
                hash_method="content",
                book_path="/books/samit-basu/jinn-bot.epub",
                title="The Jinn-Bot of Shantiport",
                author="Samit Basu",
                isbn=ISBN,
            )
        ]
    )
    if identified:
        store.set_book_identity(DOC, readwise_id=str(BOOK_ID), status=status)
    return store


def _sitting(**overrides) -> Sitting:
    values: dict = {
        "document": DOC,
        "started_at": START,
        "ended_at": END,
        "active_seconds": 1500,
        "paused_seconds": 300,
        "start_progress_bp": 4120,
        "end_progress_bp": 4470,
        "finalised": True,
        "event_ids": ("event-1",),
        "finished": False,
    }
    values.update(overrides)
    return Sitting(**values)


def _deliver(store: Storage, server: ReadwiseServer, **overrides):
    """Deliver with sleeps recorded rather than taken, so retries cost nothing."""
    sleeps: list[float] = []
    overrides.setdefault("sleep", sleeps.append)
    overrides.setdefault("timezone", "America/New_York")
    report = deliver_readwise(store, _client(server), **overrides)
    return report, sleeps


# -- criterion 1: the sitting arrives ---------------------------------------


def test_a_finalised_sitting_is_recorded_with_the_devices_own_numbers():
    """Criterion 1: book id, start, end, the percent pair, the pause and the device."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report, _ = _deliver(store, server)

    assert report.delivered == [(DOC, START)]
    assert len(server.sessions) == 1
    session = server.sessions[0]
    assert session["book_id"] == BOOK_ID
    assert session["format"] == "ebook"
    assert session["started_at"] == "2025-10-09T08:53:20Z"
    assert session["ended_at"] == "2025-10-09T09:23:20Z"
    assert session["paused_seconds"] == 300
    assert session["timezone"] == "America/New_York"
    assert session["device"] == {"type": "E-Reader", "vendor": "Xteink", "model": "X4"}


def test_the_positions_are_the_devices_basis_points_over_ten_thousand():
    """Criterion 1: `percent_at_*` is a fraction, and the device counts basis points."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(start_progress_bp=4120, end_progress_bp=4470)])
        _deliver(store, server)

    assert server.sessions[0]["percent_at_start"] == 0.412
    assert server.sessions[0]["percent_at_end"] == 0.447


def test_the_sending_time_is_kept_off_the_feed():
    """Criterion 3, on the wire: `publish_to_feed` is false on every session."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(), _sitting(started_at=START + 90_000, ended_at=END + 90_000)])
        _deliver(store, server)

    assert len(server.sessions) == 2
    assert all(session["publish_to_feed"] is False for session in server.sessions)


def test_the_delivered_session_id_is_written_to_the_send_log():
    """The ledger holds what Readwise issued, which is the only evidence for the book."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)
        row = store.send_log()[0]

    assert row["destination"] == "readwise"
    assert row["external_id"] == str(server.sessions[0]["id"])
    assert row["sent_at"] is not None
    assert row["error"] is None


# -- what must NOT be delivered ---------------------------------------------


def test_a_sitting_that_is_still_open_is_not_delivered():
    """Readwise sessions are immutable, so nothing goes out while a sitting can still grow."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(finalised=False)])
        report, _ = _deliver(store, server)

    assert server.sessions == []
    assert report.delivered == []


def test_a_book_nothing_identified_is_never_delivered_however_many_sittings_it_has():
    """Criterion 4's identity half, and the gate identity's ruling put in front of delivery.

    The row is `unmatched`, so its sittings are not in the deliverable set at all: nothing
    is claimed in the send log, and no request is made. A wrong match cannot be taken back,
    so an unidentified book has to stay invisible to a destination.

    The row deliberately **keeps a `readwise_id`** from an earlier identification and is
    re-queued (`unmatched`, id intact — which `set_book_identity`'s COALESCE makes a real
    state). Otherwise this test would pass on the strength of the missing id rather than
    the status, and the status is the gate: it is the only thing that survives an id being
    present but no longer trusted.
    """
    with _server() as server:
        store = _store()
        store.set_book_identity(DOC, status="unmatched")
        re_queued = store.book(DOC)
        assert re_queued is not None and re_queued["readwise_id"] == str(BOOK_ID)
        store.replace_sittings([_sitting(), _sitting(started_at=START + 4000, ended_at=END + 4000)])
        report, _ = _deliver(store, server)

    assert server.sessions == []
    assert store.send_log() == []
    assert report.delivered == []


def test_an_identified_book_whose_row_carries_no_readwise_id_is_not_sent():
    """A row marked matched but with no id: refuse, and say so. Never a guess."""
    with _server() as server:
        store = _store(identified=False)
        # Matched without a Readwise id is a state identity's own rule should prevent, so
        # delivery must treat it as undeliverable rather than invent an id.
        store.set_book_identity(DOC, status="confirmed")
        store.replace_sittings([_sitting()])
        report, _ = _deliver(store, server)

    assert server.sessions == []
    assert report.delivered == []
    assert any("readwise id" in reason.lower() for reason in report.failed.values())
    # Nothing was attempted, so nothing may be claimed: a send-log row here would read
    # later as "we tried and never learned the outcome" about a request that was never made.
    assert store.send_log() == []


def test_an_item_the_service_refuses_is_not_recorded_as_delivered():
    """A 200 with a per-item error is still a refusal, and the ledger has to say so.

    Readwise answers `results[].status == "error"` inside an HTTP 200 for an item it will
    not take — a `book_id` that is not in the library, say. Reading the batch's 200 as
    success would mark this sitting delivered with no session behind it, and it would never
    be sent again.
    """
    # A library that does not hold this book: the row's id points at nothing Readwise knows.
    with ReadwiseServer(library={}) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report, _ = _deliver(store, server)
        row = store.send_log()[0]

    assert server.sessions == []
    assert row["sent_at"] is None
    assert row["error"] is not None
    assert report.delivered == []
    assert report.failed[(DOC, START)]


def test_a_sitting_with_no_positions_is_refused_rather_than_guessed():
    """An ebook session requires the percent pair, and inventing one would be a lie.

    The relay refuses locally, records why, and makes no request at all — rather than
    earning a `missing` from the service or, worse, sending a fabricated percentage that
    would sit in the reading record forever.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(start_progress_bp=None, end_progress_bp=None)])
        report, _ = _deliver(store, server)
        row = store.send_log()[0]

    assert server.session_attempts == []
    assert row["sent_at"] is None
    assert "percent" in (row["error"] or "")
    assert report.failed[(DOC, START)]


# -- criterion 2: exactly one session --------------------------------------


def test_delivering_twice_records_one_session():
    """Criterion 2: the second run finds the ledger row and sends nothing."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        first, _ = _deliver(store, server)
        second, _ = _deliver(store, server)

    assert first.delivered == [(DOC, START)]
    assert second.delivered == []
    assert len(server.sessions) == 1
    assert len(server.session_attempts) == 1


def test_a_claim_that_landed_before_the_crash_is_reconciled_not_resent():
    """Criterion 2's crash case, the half a local log cannot answer by itself.

    The relay writes the claim before it posts, so a process that dies between the POST and
    the ledger write leaves a row with no outcome. Readwise has no idempotency key and the
    docs are explicit that a repeated POST records a *second* session, so the next run asks
    `GET /sessions` for that book and, finding the session there, records its id instead of
    posting again. Session count: one, before and after.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        assert store.claim_delivery(DOC, START, "readwise")
        # What the lost POST in fact did at the service.
        server.sessions.append(
            {
                "id": 90211877,
                "book_id": BOOK_ID,
                "started_at": "2025-10-09T08:53:20Z",
                "ended_at": "2025-10-09T09:23:20Z",
                "format": "ebook",
            }
        )
        report, _ = _deliver(store, server)
        row = store.send_log()[0]

    assert server.session_attempts == []
    assert len(server.sessions) == 1
    assert report.reconciled == [(DOC, START)]
    assert row["external_id"] == "90211877"
    assert row["sent_at"] is not None


def test_a_process_that_dies_between_the_post_and_the_ledger_write_does_not_duplicate():
    """Criterion 2 stated as it actually happens, not as a pre-seeded row.

    The relay claims the target before it posts, then a crash lands between the service
    recording the session and the relay recording the id. The next run must find one
    session, not two — which is only true because the claim was written first. If the
    ledger row were written after the call, this test would find two sessions.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        client = _client(server)
        real_record = client.record_sessions

        def crash_after_sending(sessions):
            real_record(sessions)
            raise KeyboardInterrupt("the process died here")

        client.record_sessions = crash_after_sending  # type: ignore[method-assign]
        with pytest.raises(KeyboardInterrupt):
            deliver_readwise(store, client, timezone="America/New_York")
        assert len(server.sessions) == 1
        assert store.send_log()[0]["sent_at"] is None

        # A new process, with a working client, must not post it again.
        report, _ = _deliver(store, server)

    assert len(server.sessions) == 1
    assert report.reconciled == [(DOC, START)]
    assert report.delivered == []


def test_a_claim_that_never_reached_readwise_is_sent_on_the_next_run():
    """The other half of the crash: nothing landed, so the sitting is not lost."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        assert store.claim_delivery(DOC, START, "readwise")
        report, _ = _deliver(store, server)

    assert len(server.sessions) == 1
    assert report.delivered == [(DOC, START)]


# -- criterion 4: retries ---------------------------------------------------


def test_a_5xx_retries_with_backoff_and_records_one_session():
    """Criterion 4: the attempt log carries the status codes, and the sitting survives."""
    with _server(fail_plan=[500, 503]) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report, sleeps = _deliver(store, server, retry_backoff_seconds=2.0)

    assert [attempt["status_code"] for attempt in server.session_attempts] == [500, 503, 200]
    assert len(server.sessions) == 1
    assert sleeps == [2.0, 4.0]
    assert report.delivered == [(DOC, START)]


def test_a_429_retries_too_because_being_asked_to_slow_down_is_not_a_refusal():
    """A throttle is the one 4xx that is worth waiting on."""
    with _server(fail_plan=[429]) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report, sleeps = _deliver(store, server, retry_backoff_seconds=1.0)

    assert [attempt["status_code"] for attempt in server.session_attempts] == [429, 200]
    assert sleeps == [1.0]
    assert report.delivered == [(DOC, START)]


def test_a_run_of_failures_leaves_the_sitting_pending_rather_than_delivered():
    """Criterion 4's second half: never dropped, never marked delivered."""
    with _server(fail_plan=[500, 500, 500]) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report, _ = _deliver(store, server, retry_attempts=3, retry_backoff_seconds=1.0)
        row = store.send_log()[0]

    assert server.sessions == []
    assert row["sent_at"] is None
    assert row["error"] is not None
    assert report.delivered == []
    assert (DOC, START) in report.failed


def test_a_pending_sitting_is_retried_on_the_next_run():
    """The failure is per sitting and survives the process, so a later run finishes it."""
    with _server(fail_plan=[500]) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server, retry_attempts=1, retry_backoff_seconds=0.0)
        second, _ = _deliver(store, server)

    assert len(server.sessions) == 1
    assert second.delivered == [(DOC, START)]
    assert store.send_log()[0]["sent_at"] is not None


def test_a_refused_token_stops_the_run_instead_of_sleeping_once_per_book():
    """A 401 is global: retrying it per sitting multiplies one bad token into a slow run.

    The refusal still leaves every sitting pending with the service's own words, which is
    what the owner needs in order to fix the token and re-run.
    """
    with _server(write_allowed=False) as server:
        store = _store()
        store.replace_sittings([_sitting(), _sitting(started_at=START + 90_000, ended_at=END + 90_000)])
        report, sleeps = _deliver(store, server)

    assert len(server.session_attempts) == 1
    assert sleeps == []
    assert report.aborted is not None
    assert all(row["sent_at"] is None for row in store.send_log())
    assert all(row["error"] for row in store.send_log())


def test_an_unreachable_readwise_is_reported_and_leaves_the_sitting_pending():
    """A dead host is not a delivered sitting, and not a lost one either."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]

    store = _store()
    store.replace_sittings([_sitting()])
    sleeps: list[float] = []
    report = deliver_readwise(
        store,
        ReadwiseClient("rw-token", base_url=f"http://127.0.0.1:{dead}", timeout=1.0),
        timezone="America/New_York",
        retry_attempts=2,
        retry_backoff_seconds=0.0,
        sleep=sleeps.append,
    )

    assert report.delivered == []
    assert store.send_log()[0]["sent_at"] is None
    assert store.send_log()[0]["error"] is not None


# -- batching ---------------------------------------------------------------


def test_more_sittings_than_one_request_takes_are_sent_in_batches_of_ten():
    """Readwise records at most 10 sessions per request; an 11th would be refused."""
    with _server() as server:
        store = _store()
        store.replace_sittings(
            [_sitting(started_at=START + index * 90_000, ended_at=END + index * 90_000)
             for index in range(11)]
        )
        report, _ = _deliver(store, server)

    assert [len(attempt["items"]) for attempt in server.session_attempts] == [10, 1]
    assert len(server.sessions) == 11
    assert len(report.delivered) == 11


def test_a_batch_smaller_than_the_limit_is_sent_as_one_request():
    """The other side of the same tunable, so batching is not always a no-op."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server, batch_size=10)

    assert len(server.session_attempts) == 1


# -- criterion 5: finishing a book -----------------------------------------


def test_a_sitting_that_reached_the_end_marks_the_book_finished_with_a_date():
    """Criterion 5: the shelf changes, and with the date the reading actually ended."""
    with _server() as server:
        store = _store()
        store.set_book_identity(DOC, readwise_id=str(BOOK_ID), status="auto")
        store.replace_sittings([_sitting(finished=True, end_progress_bp=10_000)])
        report, _ = _deliver(store, server)

    assert len(server.patches) == 1
    assert server.patches[0]["id"] == BOOK_ID
    assert server.patches[0]["status"] == "finished"
    assert server.patches[0]["finished_reading_at"] == "2025-10-09T09:23:20Z"
    assert report.finished == [DOC]


def test_a_sitting_that_reached_ninety_nine_percent_does_not_finish_the_book():
    """Criterion 5's other half, and the reason the device's own flag is carried through."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(finished=False, end_progress_bp=9900)])
        report, _ = _deliver(store, server)

    assert server.patches == []
    assert report.finished == []
    assert len(server.sessions) == 1


def test_the_book_is_finished_only_after_its_session_is_recorded():
    """Order matters: a book that leaves the shelf before its sitting lands loses the time."""
    with _server(fail_plan=[500]) as server:
        store = _store()
        store.replace_sittings([_sitting(finished=True, end_progress_bp=10_000)])
        report, _ = _deliver(store, server, retry_attempts=1, retry_backoff_seconds=0.0)

    assert server.patches == []
    assert report.finished == []


# -- criterion 6: the switch ------------------------------------------------


def test_with_the_destination_switched_off_no_request_is_made():
    """Criterion 6: `readwise=None` is a destination that was not configured."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(finished=True, end_progress_bp=10_000)])
        report = deliver_readwise(store, None, timezone="America/New_York")

    assert server.requests == []
    assert server.sessions == []
    assert server.patches == []
    assert store.send_log() == []
    assert report.delivered == []
    assert report.enabled is False
