"""Delivering derived sittings to Hardcover, against a server speaking the documented shapes.

Hardcover holds no sessions at all, so this destination is progress and finished by
necessity rather than by preference: a sitting's end position is mapped onto the edition's
page count and lands as `progress_pages` on the read in progress, and a book the device
finished is marked Read.

The shapes come from Hardcover's own guide, "Updating a Book's Progress and Dates Read"
(read 2026-11-01) — the read in progress is
`user_book_reads(where: {finished_at: {_is_null: true}}, order_by: {id: desc}, limit: 1)`,
the write is `update_user_book_read(id:, object: {progress_pages:, action_at:})`, and
finishing is `update_user_book(id:, object: {status_id: 3})`. Two of the relay's rules
come from the same page: the server keeps reads in step with the entry's status, and the
server writes the journal entries itself — "don't write those entries yourself, or they'll
appear twice".
"""

from __future__ import annotations

import pytest

from pagewire.hardcover import HardcoverClient, HardcoverError, READ_STATUS
from pagewire.hardcover_delivery import DESTINATION, FINISH_DESTINATION, deliver_hardcover
from pagewire.sessions import Sitting
from pagewire.storage import BookRecord, Storage

from service_fixtures import HardcoverServer, top_level_fields

DOC = "doc-jinn-bot"
OTHER = "doc-burning-god"
BOOK_ID = 328491
EDITION_ID = 990011
ISBN = "9781915590012"
PAGES = 448

# 2025-10-09T08:53:20Z — the same sitting the Readwise half is tested with.
START = 1_760_000_000
END = START + 1800
STOPPED_AT = "2025-10-09T05:23:20-04:00"

JINN_BOT = {
    "id": BOOK_ID,
    "title": "The Jinn-Bot of Shantiport",
    "editions": [{"id": EDITION_ID, "isbn_13": ISBN, "pages": PAGES}],
}

READ = {
    "id": 90001,
    "started_at": "2025-10-09",
    "finished_at": None,
    "edition_id": EDITION_ID,
    "progress_pages": 0,
}

ENTRY = {
    "id": 7001,
    "book_id": BOOK_ID,
    "edition_id": EDITION_ID,
    "status_id": 2,
    "user_book_reads": [],
}


def _entry(*reads: dict) -> dict:
    """A library entry on the Currently Reading shelf, with its reads."""
    return {**ENTRY, "user_book_reads": [dict(read) for read in (reads or (READ,))]}


def _server(**overrides) -> HardcoverServer:
    values: dict = {"catalog": [JINN_BOT], "library": {7001: _entry()}}
    values.update(overrides)
    return HardcoverServer(**values)


def _client(server: HardcoverServer, token: str = "hc-token") -> HardcoverClient:
    return HardcoverClient(token, endpoint=f"{server.address}/v1/graphql")


def _store(*, identified: bool = True, status: str = "auto") -> Storage:
    """One swept book, optionally identified for Hardcover alone.

    The Readwise id is deliberately left unset: the two destinations are independent, so
    a Hardcover delivery must work for a row Readwise knows nothing about.
    """
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
        store.set_book_identity(
            DOC,
            hardcover_id=str(BOOK_ID),
            hardcover_edition_id=str(EDITION_ID),
            status=status,
        )
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


def _deliver(store: Storage, server: HardcoverServer, **overrides):
    overrides.setdefault("timezone", "America/New_York")
    return deliver_hardcover(store, _client(server), **overrides)


# -- criterion 1: the sitting lands as progress on the open read ------------


def test_a_sitting_lands_as_progress_pages_on_the_read_in_progress():
    """Criterion 1: the end position mapped to a page, dated when he stopped.

    The mapping is the KOReader plugin's — `round(percent × edition pages)` — because it
    is the one Hardcover's own showcase plugin uses and the one its docs describe
    ("to show a percentage, work it out from the edition's pages").
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == [(DOC, START)]
    assert len(server.progress_writes) == 1
    write = server.progress_writes[0]
    assert write["variables"]["id"] == 90001
    assert write["object"]["progress_pages"] == 200  # round(448 × 0.447)
    assert write["object"]["action_at"] == STOPPED_AT


def test_a_book_with_no_library_entry_gets_one_when_there_is_progress_to_write():
    """Matching stopped creating entries, so the first sitting creates it.

    Identified books used to arrive in the account already, shelf and all, whether or not
    anything had been read — which is how 60 entries ended up on Currently Reading. The
    entry is only needed when there is progress to put on it, so that is when it is made.
    """
    with _server(library={}) as server:      # Hardcover has no entry for this book
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == [(DOC, START)], report.failed
    assert server.library, "the library entry should have been created on the way"
    assert len(server.progress_writes) == 1


def test_the_progress_write_goes_to_the_book_the_identity_resolved():
    """The write addresses the read the library issued, not a number the relay invented."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)

    assert server.library[7001]["book_id"] == BOOK_ID
    assert server.reads(7001)[0]["id"] == 90001


def test_the_write_dates_the_sitting_in_when_he_actually_stopped():
    """Criterion 1: `action_at` is the sitting's end, rendered with its offset.

    The guide writes the field as an offset-bearing timestamp (`2026-09-24T21:00:00-06:00`),
    which is the shape the relay produces from the same `timezone` the sessions use.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(ended_at=END, start_progress_bp=185, end_progress_bp=4470)])
        _deliver(store, server, timezone="America/New_York")

    assert server.progress_writes[0]["object"]["action_at"] == "2025-10-09T05:23:20-04:00"

def test_the_read_lookup_costs_one_top_level_field():
    """The burst limit again: one `me` field carries the entry, the read and its edition."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)

    assert top_level_fields(server.queries[0]) == ["me"]


def test_the_progress_goes_on_the_open_read_and_not_a_finished_one():
    """"Progress goes on the read that's started and not yet finished."

    A book can hold more than one read, and the finished one is not always the older one:
    marking a book Read and then setting an earlier read going again leaves a closed read
    with the higher id. Writing to that one would put this sitting's page on a read-through
    that is over, so the lookup asks for the open read rather than the newest.
    """
    archived = {**READ, "id": 90099, "finished_at": "2025-09-01", "progress_pages": PAGES}
    with _server(library={7001: _entry(archived, READ)}) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)

    assert server.progress_writes[0]["variables"]["id"] == 90001  # the open one, not the newest by id


def test_a_book_with_no_open_read_is_reported_rather_than_invented():
    """No read means nothing to write to, and a read the relay invents is not the reader's.

    Hardcoded rather than papered over: the reason is recorded on the row and named in the
    run's report, and no request is made that would have to guess an id.
    """
    with _server(library={7001: _entry()}) as server:
        server.library[7001]["user_book_reads"] = []
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert START in {started for _, started in report.failed}
    assert "no read in progress" in report.failed[(DOC, START)]
    assert server.progress_writes == []
    # Still pending, so the next run picks it up once a read exists.
    assert store.delivery_row(DOC, START, DESTINATION)["sent_at"] is None


def test_a_book_whose_edition_has_no_page_count_is_refused_locally():
    """The page cannot be mapped without the edition's count, and a guess is a lie.

    Refused here rather than sent to earn a GraphQL error, on the same rule the sessions
    half follows for a missing position: a fabricated page would sit in the reading record
    forever. The refusal is recorded so it is visible, and carries a 4xx so a later run
    retries the decision without asking Hardcover about a request never made.
    """
    catalog = [{**JINN_BOT, "editions": [{"id": EDITION_ID, "isbn_13": ISBN}]}]
    with _server(catalog=catalog) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "page count" in report.failed[(DOC, START)]
    assert server.progress_writes == []
    ledger = store.delivery_row(DOC, START, DESTINATION)
    assert ledger["error_status"] == 400


def test_an_edition_with_zero_pages_is_refused_like_a_missing_one():
    """Hardcover's catalog is crowd-edited, so `pages` is sometimes 0.

    Zero pages is not "no pages read": it means the relay cannot count pages in this
    edition, so the sitting is refused rather than written as page 0 forever.
    """
    catalog = [{**JINN_BOT, "editions": [{"id": EDITION_ID, "isbn_13": ISBN, "pages": 0}]}]
    with _server(catalog=catalog) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "page count" in report.failed[(DOC, START)]
    assert server.progress_writes == []


def test_a_position_past_the_end_cannot_write_a_page_that_does_not_exist():
    """A device that reports past the end is clamped, not trusted into a page 500 of 448."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=12_000)])
        _deliver(store, server)

    assert server.progress_writes[0]["object"]["progress_pages"] == PAGES


def test_a_row_matched_without_a_hardcover_edition_id_is_refused_not_written():
    """Both ids are needed and neither can be derived from the other.

    A sitting written against a book the relay cannot name is a wrong write into the
    reading record, so it is reported instead.
    """
    with _server() as server:
        store = _store(identified=False)
        # Matched for Hardcover, but with the book id alone: `set_book_identity` keeps
        # whatever a previous resolution wrote, so the row starts unidentified here.
        store.set_book_identity(DOC, hardcover_id=str(BOOK_ID))
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "no book and edition id" in report.failed[(DOC, START)]
    assert server.queries == []


def test_a_sitting_with_no_position_is_refused_rather_than_guessed():
    """Same rule, the other missing input: no end position means no page.

    Refused before any request is made, because the position is what is missing — no
    answer from Hardcover could supply it.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=None)])
        report = _deliver(store, server)

    assert report.written == []
    assert "no end reading position" in report.failed[(DOC, START)]
    assert server.progress_writes == []
    assert server.queries == []


def test_an_unidentified_book_is_never_delivered_however_many_sittings_it_has():
    """The gate is `books.status`, exactly as identity left it, and it is one gate.

    A destination must not invent a second rule for what "identified" means: a wrong match
    cannot be taken back.
    """
    with _server() as server:
        store = _store(identified=False)
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert report.failed == {}
    assert server.progress_writes == []


# -- criterion 2: finishing -------------------------------------------------


def test_a_sitting_at_the_end_marks_the_book_read_and_dates_the_read():
    """Criterion 2: status Read, and the read carries a finish date."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=10_000, finished=True)])
        report = _deliver(store, server)

    assert server.progress_writes[0]["object"]["progress_pages"] == PAGES
    assert server.library[7001]["status_id"] == READ_STATUS
    assert server.reads(7001)[0]["finished_at"] is not None
    assert report.finished == [DOC]


def test_a_sitting_at_ninety_nine_percent_does_not_finish_the_book():
    """Criterion 2's other half: 99% is not 100%, however close."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=9_900, finished=False)])
        report = _deliver(store, server)

    assert server.progress_writes[0]["object"]["progress_pages"] == 444
    assert not any("update_user_book(" in query for query in server.queries)
    assert server.library[7001]["status_id"] == 2
    assert report.finished == []


def test_a_refused_finish_is_retried_even_though_its_progress_landed():
    """Criterion 2, and the reason the status keeps its own ledger row.

    The progress write and the status change land independently. If the finish were only
    ever attempted beside a progress write, a run that wrote the page and was then refused
    the status would leave the book off the Read shelf with nothing left to retry.
    """
    with _server(finish_error="cannot set that status right now") as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=10_000, finished=True)])
        first = _deliver(store, server)

        server.finish_error = None  # the service recovers
        second = _deliver(store, server, now=lambda: 9_999_999_999)

    assert first.finished == []
    assert "cannot set that status" in first.failed[(DOC, START)]
    assert len(server.progress_writes) == 1  # the page landed once, and is not re-sent
    assert second.finished == [DOC]
    assert server.library[7001]["status_id"] == READ_STATUS
    assert server.reads(7001)[0]["finished_at"] is not None


def test_the_book_is_marked_read_once_and_not_again_on_the_next_run():
    """The finish is its own ledger destination, so a re-run does not flip the status twice.

    The second run's clock is moved past the throttle deliberately: run immediately, the
    book would be held back by the window and the status would never be re-sent — which
    would make this test pass whether or not the finish is recorded as written.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting(end_progress_bp=10_000, finished=True)])
        _deliver(store, server)
        first = len([q for q in server.queries if "update_user_book(" in q])
        report = _deliver(store, server, now=lambda: 9_999_999_999)

    second = len([q for q in server.queries if "update_user_book(" in q])
    assert first == 1
    assert second == 1
    assert report.finished == []
    assert store.delivery_row(DOC, START, FINISH_DESTINATION)["sent_at"] is not None


# -- criterion 3: never a journal entry -------------------------------------


def test_the_relay_writes_no_journal_entry_of_its_own():
    """Criterion 3: the server writes them, so a client that does too makes them appear twice.

    The double is able to record a journal entry, which is what makes its emptiness
    evidence rather than a property of the fixture.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings(
            [
                _sitting(),
                _sitting(started_at=END + 1, ended_at=END + 1800, end_progress_bp=10_000, finished=True),
            ]
        )
        _deliver(store, server)

    assert server.journals == []
    assert not any("reading_journal" in query for query in server.queries)


# -- criterion 4: one progress write a minute -------------------------------


def test_two_sittings_within_a_minute_produce_one_progress_write():
    """Criterion 4: the later sitting's progress supersedes the earlier one's.

    Progress is a mirror, not a record of sittings: two writes a few seconds apart would
    say the same thing twice and spend the once-a-minute budget Hardcover's Free plan
    allows, so the furthest sitting is the one written.
    """
    with _server() as server:
        store = _store()
        store.replace_sittings(
            [
                _sitting(started_at=START, ended_at=END, end_progress_bp=4470),
                _sitting(started_at=END + 10, ended_at=END + 1810, end_progress_bp=5200),
            ]
        )
        report = _deliver(store, server)

    assert len(server.progress_writes) == 1
    assert server.progress_writes[0]["object"]["progress_pages"] == 233  # round(448 × 0.52)
    assert len(report.written) == 2
    assert len(report.coalesced) == 1


def test_neither_sitting_is_lost_from_the_local_record():
    """Criterion 4: both sittings stay in the derived table, whatever the destination got."""
    with _server() as server:
        store = _store()
        store.replace_sittings(
            [
                _sitting(started_at=START, ended_at=END),
                _sitting(started_at=END + 10, ended_at=END + 1810, end_progress_bp=5200),
            ]
        )
        _deliver(store, server)

    assert {row["started_at"] for row in store.sittings()} == {START, END + 10}


def test_a_write_is_not_repeated_within_the_throttle_window():
    """Criterion 4: a second run seconds later makes no second write, and loses nothing."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)
        written_at = store.delivery_row(DOC, START, DESTINATION)["sent_at"]

        store.replace_sittings([_sitting(started_at=END + 10, ended_at=END + 1810, end_progress_bp=5200)])
        report = _deliver(store, server, now=lambda: written_at + 30)

    assert len(server.progress_writes) == 1
    assert report.throttled == [DOC]
    # Held, not dropped: the sitting is still pending for the next run.
    assert store.delivery_row(DOC, END + 10, DESTINATION) is None


def test_a_sitting_after_the_throttle_window_is_written():
    """The window is a delay, not a refusal: once a minute has passed, progress goes."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)
        written_at = store.delivery_row(DOC, START, DESTINATION)["sent_at"]

        store.replace_sittings([_sitting(started_at=END + 10, ended_at=END + 1810, end_progress_bp=5200)])
        report = _deliver(store, server, now=lambda: written_at + 61)

    assert len(server.progress_writes) == 2
    assert report.written == [(DOC, END + 10)]
    assert report.throttled == []


def test_the_throttle_is_per_book_not_per_run():
    """One book's recent write must not hold back another book's first one."""
    with _server() as server:
        store = _store()
        store.upsert_books(
            [
                BookRecord(
                    document=OTHER,
                    hash_method="content",
                    book_path="/books/other.epub",
                    title="Other",
                    isbn="9780316492485",
                )
            ]
        )
        store.set_book_identity(OTHER, hardcover_id=str(BOOK_ID), hardcover_edition_id=str(EDITION_ID))
        store.replace_sittings([_sitting()])
        _deliver(store, server)
        written_at = store.delivery_row(DOC, START, DESTINATION)["sent_at"]

        store.replace_sittings(
            [
                _sitting(started_at=END + 10, ended_at=END + 1810),
                _sitting(document=OTHER, started_at=START + 5, ended_at=END + 5),
            ]
        )
        report = _deliver(store, server, now=lambda: written_at + 30)

    # Two writes in total: the first run's, and the other book's. The throttled book's new
    # sitting waits its turn rather than holding anything else up.
    assert len(server.progress_writes) == 2
    assert report.throttled == [DOC]
    assert report.written == [(OTHER, START + 5)]


def test_the_same_sitting_is_not_written_twice():
    """The ledger is what makes a re-run idempotent, as it is for sessions."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server)
        report = _deliver(store, server, now=lambda: 9_999_999_999)

    assert len(server.progress_writes) == 1
    assert report.written == []


def test_the_throttle_window_is_the_configured_number():
    """A tunable, not a constant: the window is whatever the deployment says it is."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        _deliver(store, server, throttle_seconds=300)
        written_at = store.delivery_row(DOC, START, DESTINATION)["sent_at"]

        store.replace_sittings([_sitting(started_at=END + 10, ended_at=END + 1810)])
        report = _deliver(store, server, throttle_seconds=300, now=lambda: written_at + 61)

    assert len(server.progress_writes) == 1
    assert report.throttled == [DOC]


# -- criterion 5: a scope refusal is visible --------------------------------


def test_a_token_without_the_write_scope_is_reported_with_the_response_body():
    """Criterion 5: a 403 `insufficient_scope` is an error, never an absence.

    The service's own words and the scope it wanted are in the message, because "no
    progress was written" and "your token cannot write" are the same silence otherwise.
    """
    with _server(write_scope=False) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    message = report.failed[(DOC, START)]
    assert "403" in message
    assert "insufficient_scope" in message
    assert "write:library" in message
    assert "403" in report.aborted


def test_a_scope_refusal_stops_the_run_rather_than_repeating_itself():
    """A credential problem is global, so the run stops instead of once per book."""
    with _server(write_scope=False) as server:
        store = _store()
        store.upsert_books(
            [
                BookRecord(
                    document=OTHER,
                    hash_method="content",
                    book_path="/books/other.epub",
                    title="Other",
                    isbn="9780316492485",
                )
            ]
        )
        store.set_book_identity(OTHER, hardcover_id=str(BOOK_ID), hardcover_edition_id=str(EDITION_ID))
        store.replace_sittings([_sitting(), _sitting(document=OTHER, started_at=START + 5)])
        _deliver(store, server)

    # One attempt reached Hardcover and was refused. The second book's was never made:
    # the run stopped on the first, because a bad token is the same fact every time.
    assert len(server.progress_writes) == 1


def test_an_unreachable_hardcover_is_reported_and_leaves_the_sitting_pending():
    """No answer is not a refusal: the sitting waits, and says so."""
    store = _store()
    store.replace_sittings([_sitting()])
    client = HardcoverClient("hc-token", endpoint="http://127.0.0.1:1/v1/graphql")
    report = deliver_hardcover(store, client, timezone="America/New_York")

    assert report.written == []
    assert "could not reach Hardcover" in report.failed[(DOC, START)]
    assert store.delivery_row(DOC, START, DESTINATION)["sent_at"] is None


def test_a_hardcover_error_is_a_refusal_and_not_an_empty_library():
    """A GraphQL `errors` array in an HTTP 200 must not read as "no read in progress"."""
    with _server(graphql_errors=["field 'user_book_reads' not found"]) as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "not found" in report.failed[(DOC, START)]


def test_the_client_refuses_to_call_hardcover_without_a_token():
    """An unconfigured destination must not present as a 401 the owner reads as a bad token."""
    client = HardcoverClient("", endpoint="http://127.0.0.1:1/v1/graphql")
    with pytest.raises(HardcoverError):
        client.read_state(BOOK_ID, EDITION_ID)


def test_a_book_hardcover_will_not_take_is_reported():
    """No entry is a different problem from no read, and it is named as itself.

    The entry is created on the way now, so this is the case where Hardcover refuses to
    create it at all — nothing can be written against a book it will not take, and the relay
    says so rather than sending the owner looking for the wrong fix.
    """
    with _server(library={}, entry_error="edition is not in the catalog") as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "edition is not in the catalog" in report.failed[(DOC, START)]
    assert server.progress_writes == [], "nothing may be attempted without an entry"


def test_a_mutation_that_answers_with_its_own_error_is_not_read_as_success():
    """GraphQL reports application failures inside an HTTP 200, so the body decides.

    Writing a page the service refused, and recording it as written, is the quietest way
    to lose a sitting's progress: nothing raises, and the ledger says it landed.
    """
    with _server(write_error="progress_pages must be an integer") as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = _deliver(store, server)

    assert report.written == []
    assert "must be an integer" in report.failed[(DOC, START)]
    assert store.delivery_row(DOC, START, DESTINATION)["sent_at"] is None


# -- criterion 6: switching the destination off -----------------------------


def test_with_the_destination_switched_off_no_request_is_made():
    """Criterion 6: `client=None` means off, not broken."""
    with _server() as server:
        store = _store()
        store.replace_sittings([_sitting()])
        report = deliver_hardcover(store, None, timezone="America/New_York")

    assert report.enabled is False
    assert report.written == []
    assert server.queries == []
    assert store.send_log() == []
