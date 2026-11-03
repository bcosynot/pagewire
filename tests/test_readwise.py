"""The Readwise Books API client, against a server speaking the documented shapes.

Criterion 1 of the identity ticket: a row whose ISBN exists in Readwise resolves via
`GET /books?isbns=` and, where absent, is created via `POST /books`, and the row carries
the returned `readwise_book_id`. These pin the client half of that; `test_identity.py`
pins the row.
"""

from __future__ import annotations

import pytest

from pagewire.readwise import ReadwiseClient, ReadwiseError

from service_fixtures import ReadwiseServer

JINN_BOT = {
    "id": 4790033,
    "title": "The Jinn-Bot of Shantiport",
    "author": "Samit Basu",
    "isbn": "9781915590012",
    "status": "currently_reading",
    "product": {"isbn": "9781915590012"},
}


def _client(server: ReadwiseServer, token: str = "rw-token") -> ReadwiseClient:
    return ReadwiseClient(token, base_url=server.address)


def test_an_isbn_readwise_already_has_resolves_to_that_book_without_creating_one():
    """Criterion 1, the hit case: the id comes back from `GET /books?isbns=`."""
    with ReadwiseServer(library={JINN_BOT["isbn"]: JINN_BOT}) as server:
        found = _client(server).find_by_isbn("9781915590012")

    assert found is not None
    assert str(found.book_id) == "4790033"
    assert server.creations == []


def test_an_isbn_readwise_does_not_have_comes_back_as_nothing_to_find():
    """A miss is not an error: it is the signal to create the book."""
    with ReadwiseServer(library={}) as server:
        assert _client(server).find_by_isbn("9781915590012") is None


def test_adding_an_isbn_readwise_does_not_have_returns_the_id_it_issued():
    """Criterion 1, the create case: `POST /books` and the `book_id` it answers with."""
    with ReadwiseServer(library={}) as server:
        added = _client(server).add(isbn="9781915590012", status="currently_reading")

    assert str(added.book_id) == "4790034"
    assert len(server.creations) == 1
    assert server.creations[0]["product"] == {"isbn": "9781915590012"}
    assert server.creations[0]["status"] == "currently_reading"


def test_adding_a_book_readwise_already_has_returns_the_same_id_not_a_second_copy():
    """The documented idempotence, and what makes a re-run of resolution harmless.

    Readwise answers `existing` with the same `book_id` and updates the book instead of
    adding a copy, so the relay may add a book it is unsure about rather than reading it
    back first.
    """
    with ReadwiseServer(library={JINN_BOT["isbn"]: JINN_BOT}) as server:
        first = _client(server).add(isbn="9781915590012")
        second = _client(server).add(isbn="9781915590012")

    assert str(first.book_id) == str(second.book_id) == "4790033"
    assert len(server.library) == 1


def test_a_book_with_no_isbn_is_added_by_title_and_author():
    """The confirm path for a row no ISBN can match.

    `POST /books` tries `id`, `isbn`, `asin`, then `title` with `author`, so a confirmed
    row still gets a real `book_id` rather than none.
    """
    with ReadwiseServer(library={}) as server:
        added = _client(server).add(title="Burning God", author="R. F. Kuang")

    assert str(added.book_id) == "4790034"
    assert server.creations[0]["product"] == {"title": "Burning God", "author": "R. F. Kuang"}


def test_the_token_goes_in_the_documented_header():
    """`Authorization: Token …` for a personal access token — not Bearer."""
    with ReadwiseServer(library={JINN_BOT["isbn"]: JINN_BOT}) as server:
        _client(server).find_by_isbn("9781915590012")

    assert server.headers[-1]["authorization"] == "Token rw-token"


def test_a_refused_token_is_an_error_carrying_the_services_own_words():
    """Criterion 5: a 401 is reported, never swallowed into "not found".

    A client that treated a refusal as a miss would answer "no match" for every book and
    quietly put the entire library into the confirmation queue.
    """
    with ReadwiseServer(library={JINN_BOT["isbn"]: JINN_BOT}) as server:
        with pytest.raises(ReadwiseError) as caught:
            ReadwiseClient("wrong-token", base_url=server.address).find_by_isbn(
                "9781915590012"
            )

    assert caught.value.status_code == 401
    assert "unauthenticated" in str(caught.value)


def test_an_under_scoped_token_is_an_error_that_names_the_scope():
    """The second documented refusal: 403 `insufficient_scope`, with the scope listed."""
    with ReadwiseServer(library={}, write_allowed=False) as server:
        with pytest.raises(ReadwiseError) as caught:
            _client(server).add(isbn="9781915590012")

    assert caught.value.status_code == 403
    assert caught.value.code == "insufficient_scope"
    assert "write" in str(caught.value)


def test_no_configured_token_is_refused_without_calling_readwise_at_all():
    """An unconfigured destination must not look like a destination that said no.

    Nothing is sent, so there is no chance of a request going out with an empty
    credential and coming back as a confusing 401 the owner would read as a bad token.
    """
    with ReadwiseServer(library={}) as server:
        with pytest.raises(ReadwiseError) as caught:
            ReadwiseClient("", base_url=server.address).find_by_isbn("9781915590012")

    assert "no readwise token" in str(caught.value).lower()
    assert server.requests == []


def test_a_service_that_cannot_be_reached_is_an_error_not_an_empty_result():
    """A dead host is the ordinary offline case; it must not read as "not in your library"."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]

    with pytest.raises(ReadwiseError):
        ReadwiseClient("rw-token", base_url=f"http://127.0.0.1:{dead}").find_by_isbn(
            "9781915590012"
        )


# --------------------------------------------------------- how it refused


def test_a_rate_limit_is_marked_transient_so_a_caller_can_choose_to_retry():
    with ReadwiseServer(library={}) as server:
        server.status_plan = [429]
        with pytest.raises(ReadwiseError) as raised:
            _client(server).find_by_isbn("9781915590012")

    assert raised.value.status_code == 429
    assert raised.value.transient is True


def test_a_book_is_added_with_no_shelf_claim_unless_one_is_asked_for():
    """The shelf says what the owner is doing, so it is not asserted by default.

    The cost of the old default, measured: 50 books on Readwise's Currently Reading shelf
    because matching added them while nothing was reading them. Readwise defaults `status`
    to `want_to_read` when the key is absent, which is the honest answer for a book the
    relay has merely identified.
    """
    with ReadwiseServer(library={}) as server:
        _client(server).add(isbn="9781915590012")

    assert "status" not in server.creations[0], server.creations[0]


def test_a_shelf_can_still_be_claimed_when_there_is_a_reading_to_justify_it():
    with ReadwiseServer(library={}) as server:
        _client(server).add(isbn="9781915590012", status="currently_reading")

    assert server.creations[0]["status"] == "currently_reading"


def test_an_insufficient_scope_refusal_is_not_transient():
    """A token missing its write scope is a 403 every time: an answer, not a delay."""
    with ReadwiseServer(library={}, write_allowed=False) as server:
        with pytest.raises(ReadwiseError) as raised:
            _client(server).add(isbn="9781915590012")

    assert raised.value.status_code == 403
    assert raised.value.transient is False
