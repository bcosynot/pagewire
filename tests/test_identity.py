"""Identity resolution: matching a swept book to a service's own idea of it.

The rule under test is Vivek's: a confident match — an ISBN — is added automatically, and
anything ambiguous waits for him to confirm it once in the review page. A wrong match
sends immutable sittings to the wrong book, which is the failure this whole ticket exists
to avoid, so "cannot tell" must resolve to `unmatched` rather than to a guess.

Both services are real HTTP servers (`service_fixtures`), so the resolution path is
exercised end to end: Readwise's `Token` header and `books` endpoint, Hardcover's GraphQL
over POST.
"""

from __future__ import annotations

import pytest

from pagewire.hardcover import HardcoverClient
from pagewire.identity import IdentityError, confirm_book, main, resolve_books
from pagewire.readwise import ReadwiseClient
from pagewire.sessions import Sitting
from pagewire.storage import BookRecord, Storage

from service_fixtures import HardcoverServer, ReadwiseServer

JINN_BOT_ISBN = "9781915590012"
GOOD_ISBN = "9780316492485"

JINN_BOT = {"id": 4790033, "title": "The Jinn-Bot of Shantiport", "author": "Samit Basu"}
JINN_BOT_HC = {
    "id": 328491,
    "title": "The Jinn-Bot of Shantiport",
    "editions": [{"id": 990011, "isbn_13": JINN_BOT_ISBN}],
}
BURNING_GOD_HC = {
    "id": 118822,
    "title": "Burning God",
    "editions": [{"id": 640044, "isbn_13": None}],
}

JINN_PATH = "/Samit Basu - The Jinn-Bot of Shantiport.epub"
GOD_PATH = "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"


def _swept(storage: Storage, **overrides: object) -> None:
    """One row, as the sweep would have written it for a book that carries an ISBN."""
    fields: dict[str, object] = {
        "document": "doc-jinn-bot",
        "hash_method": "content",
        "book_path": JINN_PATH,
        "title": "The Jinn-Bot of Shantiport",
        "author": "Samit Basu",
        "isbn": JINN_BOT_ISBN,
    }
    storage.upsert_books([BookRecord(**{**fields, **overrides})])  # type: ignore[arg-type]


def _no_isbn_row(storage: Storage, document: str = "doc-burning-god") -> None:
    storage.upsert_books(
        [
            BookRecord(
                document=document,
                hash_method="content",
                book_path=GOD_PATH,
                title="Burning God",
                author="R. F. Kuang",
                isbn=None,
            )
        ]
    )


def _row(storage: Storage, document: str):
    """A row that is known to exist, so the test can read its columns."""
    row = storage.book(document)
    assert row is not None, f"no row for {document}"
    return row


def _readwise(server: ReadwiseServer) -> ReadwiseClient:
    return ReadwiseClient("rw-token", base_url=server.address)


def _hardcover(server: HardcoverServer) -> HardcoverClient:
    return HardcoverClient("hc-token", endpoint=f"{server.address}/v1/graphql")


# ---------------------------------------------------------------- criteria 1 and 2


def test_an_isbn_both_services_know_matches_the_row_automatically():
    """Criteria 1 and 2: the row ends up carrying both services' ids, with no gate."""
    storage = Storage(":memory:")
    _swept(storage)

    with ReadwiseServer(library={JINN_BOT_ISBN: {**JINN_BOT, "isbn": JINN_BOT_ISBN}}) as rw:
        with HardcoverServer(catalog=[JINN_BOT_HC], library={}) as hc:
            report = resolve_books(
                storage, readwise=_readwise(rw), hardcover=_hardcover(hc)
            )

    row = _row(storage, "doc-jinn-bot")
    assert row["status"] == "auto"
    assert row["readwise_id"] == "4790033"
    assert row["hardcover_id"] == "328491"
    assert row["hardcover_edition_id"] == "990011"
    assert row["error"] is None
    assert report.resolved == [JINN_PATH]
    # Criterion 2 asks for the `user_books` entry to exist, not just the ids.
    assert [entry["book_id"] for entry in hc.library.values()] == [328491]


def test_an_isbn_readwise_does_not_have_is_created_there():
    """Criterion 1's other half: `POST /books` supplies the id the row keeps."""
    storage = Storage(":memory:")
    _swept(storage)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[JINN_BOT_HC], library={}) as hc:
            resolve_books(storage, readwise=_readwise(rw), hardcover=_hardcover(hc))

    assert _row(storage, "doc-jinn-bot")["readwise_id"] == "4790034"
    assert rw.creations[0]["product"] == {"isbn": JINN_BOT_ISBN}


def test_the_same_book_is_not_created_twice_by_a_second_run():
    """Resolution is re-runnable: a second pass over a resolved row costs nothing."""
    storage = Storage(":memory:")
    _swept(storage)

    with ReadwiseServer(library={JINN_BOT_ISBN: {**JINN_BOT, "isbn": JINN_BOT_ISBN}}) as rw:
        with HardcoverServer(catalog=[JINN_BOT_HC], library={}) as hc:
            readwise, hardcover = _readwise(rw), _hardcover(hc)
            resolve_books(storage, readwise=readwise, hardcover=hardcover)
            rw.requests.clear()
            second = resolve_books(storage, readwise=readwise, hardcover=hardcover)

    assert second.resolved == []
    assert rw.requests == []
    assert rw.creations == []


# ---------------------------------------------------------------- criterion 3


def test_a_book_with_no_isbn_is_left_unmatched_and_no_service_is_asked_about_it():
    """Criterion 3: the ambiguous row waits, and nothing is guessed on its behalf.

    Neither service is asked *about* the book either, because title-and-author matching is
    the ambiguous kind and the design holds those for the owner. Appearing in the queue is
    the point; being quietly added somewhere is the failure this avoids.

    **The zero-request assertions alone do not pin the guard**, which is worth knowing
    because it looks like they do. `resolve_books` passes `match_readwise` nothing but the
    ISBN — deliberately, since a title-and-author lookup is exactly what ruling 1 refuses —
    so with the guard removed the row *still* makes no request: `match_readwise` refuses it
    as "no ISBN, no title and no author" and the refusal is caught and reported. Held and
    failed are the two states that differ, so the assertions that pin the guard are the two
    at the end: a held row carries no error, and the run's `errors` is empty.
    """
    storage = Storage(":memory:")
    _no_isbn_row(storage)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            report = resolve_books(
                storage, readwise=_readwise(rw), hardcover=_hardcover(hc)
            )

    row = _row(storage, "doc-burning-god")
    assert row["status"] == "unmatched"
    assert row["readwise_id"] is None and row["hardcover_id"] is None
    assert report.unmatched == [GOD_PATH]
    assert rw.requests == [] and rw.creations == []
    assert hc.queries == []
    assert [r["book_path"] for r in storage.books_needing_confirmation()] == [GOD_PATH]
    # Held, not refused: the guard is what makes the difference, and it is the only
    # difference, because the failure path refuses the same row for the same reason.
    assert row["error"] is None
    assert report.errors == {}


def test_confirming_a_row_once_writes_both_ids_and_moves_it_to_confirmed():
    """Criterion 3: one confirmation is enough, and it is where the ambiguity ends.

    The owner supplies the Hardcover ids — that lookup is the one the relay cannot make
    confidently, which is why the row was in the queue — while Readwise is matched by
    title and author, which its own API documents as a supported identity.
    """
    storage = Storage(":memory:")
    _no_isbn_row(storage)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            confirm_book(
                storage,
                "doc-burning-god",
                readwise=_readwise(rw),
                hardcover=_hardcover(hc),
                hardcover_id="118822",
                hardcover_edition_id="640044",
            )

    row = _row(storage, "doc-burning-god")
    assert row["status"] == "confirmed"
    assert row["hardcover_id"] == "118822"
    assert row["hardcover_edition_id"] == "640044"
    assert row["readwise_id"] == "4790034"
    assert rw.creations[0]["product"] == {"title": "Burning God", "author": "R. F. Kuang"}
    assert storage.books_needing_confirmation() == []


def test_a_confirmed_row_moves_out_of_the_queue_and_becomes_deliverable():
    """Criterion 3's after-state, and the gate in front of delivery."""
    storage = Storage(":memory:")
    _no_isbn_row(storage)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            readwise, hardcover = _readwise(rw), _hardcover(hc)
            assert storage.deliverable_books() == []
            confirm_book(
                storage,
                "doc-burning-god",
                readwise=readwise,
                hardcover=hardcover,
                hardcover_id="118822",
                hardcover_edition_id="640044",
            )

    assert [row["document"] for row in storage.deliverable_books()] == ["doc-burning-god"]


def test_confirming_without_the_hardcover_ids_changes_nothing_and_says_why():
    """Criterion 3 cannot be met by half a match.

    Confirmation means "this row is identified". If an enabled destination still has no id
    when the form is submitted, the row stays in the queue with the reason rather than
    moving to `confirmed` with an id missing.
    """
    storage = Storage(":memory:")
    _no_isbn_row(storage)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            with pytest.raises(IdentityError) as caught:
                confirm_book(
                    storage,
                    "doc-burning-god",
                    readwise=_readwise(rw),
                    hardcover=_hardcover(hc),
                )

    assert "hardcover" in str(caught.value).lower()
    row = _row(storage, "doc-burning-god")
    assert row["status"] == "unmatched"
    assert row["hardcover_id"] is None


# ---------------------------------------------------------------- criterion 4


def test_an_unmatched_row_is_not_delivered_however_many_sittings_it_has():
    """Criterion 4: the gate is the status, not the traffic.

    Sittings are derived against a book nobody has identified, and it must still be
    unreachable by delivery — a wrong match cannot be taken back, so no amount of reading
    makes an unidentified book deliverable.

    The send-log half of this criterion belongs to the delivery ticket, which is what
    writes `send_log`; what is provable here is that the row never reaches it.
    """
    storage = Storage(":memory:")
    _no_isbn_row(storage)
    storage.replace_sittings(
        [
            Sitting(
                document="doc-burning-god",
                started_at=1_760_000_000,
                ended_at=1_760_003_000,
                active_seconds=3000,
                paused_seconds=0,
                start_progress_bp=1000,
                end_progress_bp=2000,
                finalised=True,
                event_ids=("e-1",),
            )
        ]
    )

    assert len(storage.sittings()) == 1
    assert storage.deliverable_books() == []


# ---------------------------------------------------------------- criterion 5


def test_an_under_scoped_token_is_recorded_on_the_row_and_is_visible():
    """Criterion 5: a 403 `insufficient_scope` lands on the row, not in a log nobody reads.

    Readwise resolved here and Hardcover refused, and the row must not be half matched:
    the ids from the success are not written, so a later run — after the token is fixed —
    resolves the book cleanly instead of delivering to one service only.
    """
    storage = Storage(":memory:")
    _swept(storage)

    with ReadwiseServer(library={JINN_BOT_ISBN: {**JINN_BOT, "isbn": JINN_BOT_ISBN}}) as rw:
        with HardcoverServer(catalog=[JINN_BOT_HC], library={}, write_scope=False) as hc:
            report = resolve_books(
                storage, readwise=_readwise(rw), hardcover=_hardcover(hc)
            )

    row = _row(storage, "doc-jinn-bot")
    assert row["status"] == "unmatched"
    assert row["readwise_id"] is None
    assert "insufficient_scope" in row["error"]
    assert "write:library" in row["error"]
    assert [r["book_path"] for r in storage.books_with_errors()] == [JINN_PATH]
    assert report.errors == {JINN_PATH: row["error"]}


def test_one_book_failing_does_not_stop_the_others_being_resolved():
    """A per-row failure, not a run failure: the queue is long and one book is not it.

    A sweep is deliberately all-or-nothing because a half-swept card is misleading.
    Resolution is the opposite: one book a service refuses must not hold up the other
    ninety-nine, and each row carries its own outcome.
    """
    storage = Storage(":memory:")
    _swept(storage)
    storage.upsert_books(
        [
            BookRecord(
                document="doc-good",
                hash_method="content",
                book_path="/Good Book.epub",
                title="Good Book",
                author="Someone",
                isbn=GOOD_ISBN,
            )
        ]
    )

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(
            catalog=[
                JINN_BOT_HC,
                {"id": 5, "title": "Good Book", "editions": [{"id": 9, "isbn_13": GOOD_ISBN}]},
            ],
            library={},
        ) as hc:
            rw.fail_isbns.add(JINN_BOT_ISBN)
            report = resolve_books(
                storage, readwise=_readwise(rw), hardcover=_hardcover(hc)
            )

    assert _row(storage, "doc-good")["status"] == "auto"
    assert _row(storage, "doc-jinn-bot")["status"] == "unmatched"
    assert _row(storage, "doc-jinn-bot")["error"] is not None
    assert report.resolved == ["/Good Book.epub"]


def test_with_neither_destination_enabled_nothing_is_asked_and_nothing_is_matched():
    """A destination that is switched off is not a destination that failed.

    With both off there is nothing to match against, so every row stays in the queue —
    and no book is marked `auto` on the strength of a lookup that never happened.
    """
    storage = Storage(":memory:")
    _swept(storage)

    report = resolve_books(storage)

    assert _row(storage, "doc-jinn-bot")["status"] == "unmatched"
    assert report.resolved == []
    assert report.unmatched == [JINN_PATH]


# ---------------------------------------------------------------- the command line


def test_the_cli_resolves_the_queue_against_the_configured_destinations(
    tmp_path, monkeypatch, capsys
):
    """The command the owner (or a timer) actually runs, configured the way it deploys.

    Everything comes from `PAGEWIRE_*` environment variables, so this is the deployed
    code path rather than a hand-wired one — which is what makes its output usable as
    evidence.
    """
    path = str(tmp_path / "pagewire.db")
    _swept(Storage(path))

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[JINN_BOT_HC], library={}) as hc:
            monkeypatch.setenv("PAGEWIRE_DB", path)
            monkeypatch.setenv("PAGEWIRE_READWISE_ENABLED", "true")
            monkeypatch.setenv("PAGEWIRE_READWISE_TOKEN", "rw-token")
            monkeypatch.setenv("PAGEWIRE_READWISE_BASE_URL", rw.address)
            monkeypatch.setenv("PAGEWIRE_HARDCOVER_ENABLED", "true")
            monkeypatch.setenv("PAGEWIRE_HARDCOVER_TOKEN", "hc-token")
            monkeypatch.setenv("PAGEWIRE_HARDCOVER_ENDPOINT", f"{hc.address}/v1/graphql")

            code = main([])

    printed = capsys.readouterr().out
    assert code == 0
    assert JINN_PATH in printed
    assert "matched" in printed
    assert _row(Storage(path), "doc-jinn-bot")["status"] == "auto"


def test_the_cli_can_list_the_queue_without_calling_anything(tmp_path, capsys):
    """`--list` is the read-only half: what is waiting, and why, with no network.

    Useful for exactly the reason the page shows errors: the first question about a queue
    that is not moving is which rows the services refused.
    """
    path = str(tmp_path / "pagewire.db")
    store = Storage(path)
    _no_isbn_row(store)
    store.set_book_error("doc-burning-god", "Hardcover has no edition with ISBN 9780316492485")

    code = main(["--db", path, "--list"])

    printed = capsys.readouterr().out
    assert code == 0
    assert GOD_PATH in printed
    assert "no edition with ISBN" in printed
