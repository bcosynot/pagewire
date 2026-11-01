"""The review page: where an ambiguous row is confirmed once, by a human.

Criterion 3 asks for a row with no ISBN to appear here with its SD path and to move to
`confirmed` with both ids written once it is confirmed. Criterion 5 asks for a refused
call to be *visible in the page* rather than a silent skip.

The app is built the way it is deployed — from `Settings`, with the two service clients
pointed at servers that speak the real APIs, and the database at a real path the test
also opens — so these tests exercise the route, the page and the write, rather than a
hand-assembled app.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from pagewire.app import create_app
from pagewire.config import Settings
from pagewire.storage import BookRecord, Storage

from service_fixtures import HardcoverServer, ReadwiseServer

TOKEN = "device-token"
GOD_PATH = "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"
GOD_DOCUMENT = "doc-burning-god"

BURNING_GOD_HC = {
    "id": 118822,
    "title": "Burning God",
    "editions": [{"id": 640044, "isbn_13": None}],
}


def _settings(db_path: str, *, readwise: str = "", hardcover: str = "") -> Settings:
    """Settings pointed at the doubles and at the database under test."""
    return Settings(
        db_path=db_path,
        device_token=TOKEN,
        readwise_enabled=bool(readwise),
        readwise_token="rw-token",
        readwise_base_url=readwise,
        hardcover_enabled=bool(hardcover),
        hardcover_token="hc-token",
        hardcover_endpoint=hardcover,
    )


def _seed(store: Storage, **overrides: object) -> None:
    fields: dict[str, object] = {
        "document": GOD_DOCUMENT,
        "hash_method": "content",
        "book_path": GOD_PATH,
        "title": "Burning God",
        "author": "R. F. Kuang",
        "isbn": None,
    }
    store.upsert_books([BookRecord(**{**fields, **overrides})])  # type: ignore[arg-type]


def _row(store: Storage, document: str):
    row = store.book(document)
    assert row is not None, f"no row for {document}"
    return row


def test_an_unmatched_book_appears_with_the_sd_path_and_its_ids_empty(tmp_path):
    """Criterion 3's before state: the row is visible, and marked as not identified."""
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC]) as hc:
            app = create_app(
                _settings(store.path, readwise=rw.address, hardcover=f"{hc.address}/v1/graphql")
            )
            with TestClient(app) as client:
                page = client.get(f"/review?token={TOKEN}")

    assert page.status_code == 200
    assert GOD_PATH in page.text
    assert "unmatched" in page.text
    # The form is what makes it confirmable: one input per id a row can carry.
    assert 'name="hardcover_id"' in page.text
    assert 'name="hardcover_edition_id"' in page.text
    assert 'name="readwise_id"' in page.text


def test_a_refused_call_is_shown_on_the_row_in_the_page(tmp_path):
    """Criterion 5: the error text is on the page, not only in the database.

    A silent skip is the failure mode here — an under-scoped token would otherwise look
    exactly like a library where nothing matched.
    """
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)
    store.set_book_error(
        GOD_DOCUMENT,
        "Hardcover refused the call: HTTP 403 insufficient_scope [needs scope write:library]",
    )

    with HardcoverServer(catalog=[BURNING_GOD_HC]) as hc:
        app = create_app(_settings(store.path, hardcover=f"{hc.address}/v1/graphql"))
        with TestClient(app) as client:
            page = client.get(f"/review?token={TOKEN}")

    assert "insufficient_scope" in page.text
    assert "write:library" in page.text


def test_confirming_from_the_page_writes_both_ids_and_clears_the_queue(tmp_path):
    """Criterion 3's after state, through the page the owner actually uses.

    Readwise is resolved by title and author; the Hardcover ids are the ones he supplies,
    which is the decision the page exists to collect.
    """
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            app = create_app(
                _settings(store.path, readwise=rw.address, hardcover=f"{hc.address}/v1/graphql")
            )
            with TestClient(app) as client:
                response = client.post(
                    f"/review/{GOD_DOCUMENT}",
                    data={
                        "token": TOKEN,
                        "readwise_id": "",
                        "hardcover_id": "118822",
                        "hardcover_edition_id": "640044",
                    },
                    follow_redirects=False,
                )

    assert response.status_code == 303
    row = _row(store, GOD_DOCUMENT)
    assert row["status"] == "confirmed"
    assert row["hardcover_id"] == "118822"
    assert row["hardcover_edition_id"] == "640044"
    assert row["readwise_id"] == "4790034"
    assert store.books_needing_confirmation() == []


def test_confirming_without_the_hardcover_ids_leaves_the_row_in_the_queue(tmp_path):
    """A half-confirmed row is refused, and the answer says which id is missing."""
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC]) as hc:
            app = create_app(
                _settings(store.path, readwise=rw.address, hardcover=f"{hc.address}/v1/graphql")
            )
            with TestClient(app) as client:
                response = client.post(
                    f"/review/{GOD_DOCUMENT}",
                    data={
                        "token": TOKEN,
                        "readwise_id": "",
                        "hardcover_id": "",
                        "hardcover_edition_id": "",
                    },
                )

    assert response.status_code == 422
    assert "hardcover" in response.text.lower()
    assert _row(store, GOD_DOCUMENT)["status"] == "unmatched"


def test_an_already_matched_book_is_not_offered_for_confirmation(tmp_path):
    """The list is the queue, not the library: a matched row is done being asked about."""
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(
        store,
        document="doc-matched",
        book_path="/Already Matched.epub",
        isbn="9781915590012",
        status="auto",
    )

    with ReadwiseServer(library={}) as rw:
        app = create_app(_settings(store.path, readwise=rw.address))
        with TestClient(app) as client:
            page = client.get(f"/review?token={TOKEN}")

    assert page.status_code == 200
    assert "/Already Matched.epub" not in page.text


def test_the_page_and_the_write_both_need_the_device_token(tmp_path):
    """The page shows the owner's reading and writes identity; it is not open.

    Same reasoning as the ingest endpoint: an endpoint reachable on the LAN with no
    credential, whose writes decide where immutable sittings are sent, is worse than one
    that is obviously refusing.
    """
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)
    app = create_app(_settings(store.path))

    with TestClient(app) as client:
        assert client.get("/review").status_code == 401
        assert client.get("/review?token=wrong").status_code == 401
        assert (
            client.post(f"/review/{GOD_DOCUMENT}", data={"hardcover_id": "118822"}).status_code
            == 401
        )

    assert _row(store, GOD_DOCUMENT)["status"] == "unmatched"


def test_confirming_an_unknown_document_is_a_404_rather_than_a_new_row(tmp_path):
    """A stale bookmark must not be able to invent a book."""
    store = Storage(str(tmp_path / "pagewire.db"))
    app = create_app(_settings(store.path))

    with TestClient(app) as client:
        response = client.post(
            "/review/doc-that-does-not-exist",
            data={"token": TOKEN, "hardcover_id": "1", "hardcover_edition_id": "2"},
        )

    assert response.status_code == 404
    assert store.count_books() == 0


def test_a_hardcover_candidate_is_offered_as_a_one_click_confirmation(tmp_path):
    """The 85-row problem: a queue that takes two lookups per book does not get worked.

    Hardcover's exact-title match is prefilled as ids, so confirming is one press rather
    than a trip to hardcover.app per book. Nothing is written by looking; the press is
    the decision.
    """
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)

    with ReadwiseServer(library={}) as rw:
        with HardcoverServer(catalog=[BURNING_GOD_HC], library={}) as hc:
            app = create_app(
                _settings(store.path, readwise=rw.address, hardcover=f"{hc.address}/v1/graphql")
            )
            with TestClient(app) as client:
                page = client.get(f"/review/{GOD_DOCUMENT}/candidates?token={TOKEN}")
                # Looking is read-only: the row must be untouched until it is confirmed.
                assert _row(store, GOD_DOCUMENT)["status"] == "unmatched"
                response = client.post(
                    f"/review/{GOD_DOCUMENT}",
                    data={
                        "token": TOKEN,
                        "hardcover_id": "118822",
                        "hardcover_edition_id": "640044",
                    },
                    follow_redirects=False,
                )

    assert page.status_code == 200
    assert "118822" in page.text and "640044" in page.text
    assert response.status_code == 303
    assert _row(store, GOD_DOCUMENT)["status"] == "confirmed"


def test_the_candidates_page_says_when_hardcover_has_no_book_under_that_title(tmp_path):
    """An empty result has to read as an answer, or the page looks broken."""
    store = Storage(str(tmp_path / "pagewire.db"))
    _seed(store)

    with HardcoverServer(catalog=[{"id": 1, "title": "Something Else", "editions": []}]) as hc:
        app = create_app(_settings(store.path, hardcover=f"{hc.address}/v1/graphql"))
        with TestClient(app) as client:
            page = client.get(f"/review/{GOD_DOCUMENT}/candidates?token={TOKEN}")

    assert page.status_code == 200
    assert "no book under exactly that title" in page.text
    assert _row(store, GOD_DOCUMENT)["status"] == "unmatched"
