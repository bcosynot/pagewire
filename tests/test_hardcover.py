"""The Hardcover GraphQL client, against a server speaking its documented shapes.

Criterion 2 of the identity ticket: a row whose ISBN exists in Hardcover resolves through
`editions(where: {isbn_13: …})` to `book_id` and `edition_id`, creating the `user_books`
entry if missing, and the row carries both ids.

The GraphQL documents themselves are the thing under test here. Hardcover's Free plan
allows a **burst of 10 top-level fields** and rejects a request over it outright with a
403 rather than throttling, so a lookup that spends two round trips where one would do is
a real cost, and both ids have to come back from a single field.
"""

from __future__ import annotations

import re

import pytest

from pagewire.hardcover import HardcoverClient, HardcoverError

from service_fixtures import HardcoverServer, top_level_fields

JINN_BOT = {
    "id": 328491,
    "title": "The Jinn-Bot of Shantiport",
    "editions": [{"id": 990011, "isbn_13": "9781915590012"}],
}
BURNING_GOD = {
    "id": 118822,
    "title": "Burning God",
    "editions": [{"id": 640044, "isbn_13": "9780316492485"}],
}


def _client(server: HardcoverServer, token: str = "hc-token") -> HardcoverClient:
    return HardcoverClient(token, endpoint=f"{server.address}/v1/graphql")


# ---------------------------------------------------------------- criterion 2


def test_an_isbn_hardcover_carries_resolves_to_both_ids_in_one_query():
    """Criterion 2's happy path, and the burst limit: one field, both ids.

    `editions` exposes `book_id` as its own field, so the book and its edition arrive
    together. Walking `books { editions }` would need a slug the relay does not have.
    """
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        found = _client(server).resolve_isbn("9781915590012")

    assert found is not None
    assert str(found.book_id) == "328491"
    assert str(found.edition_id) == "990011"
    assert len(server.queries) == 1
    assert top_level_fields(server.queries[0]) == ["editions"]


def test_an_isbn_hardcover_does_not_carry_resolves_to_nothing():
    """A miss is a signal, not an error: the book is not in the catalog by that ISBN."""
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        assert _client(server).resolve_isbn("9780000000002") is None


def test_a_book_already_in_the_library_is_not_added_a_second_time():
    """The `user_books` lookup that stops a re-run creating a duplicate library entry."""
    with HardcoverServer(
        catalog=[JINN_BOT],
        library={7001: {"id": 7001, "book_id": 328491, "edition_id": 990011, "status_id": 2}},
    ) as server:
        entry = _client(server).ensure_library_entry(book_id=328491, edition_id=990011)

    assert str(entry) == "7001"
    assert not any("insert_user_book" in query for query in server.queries)


def test_a_book_not_in_the_library_gets_its_entry_created():
    """The insert, with the ids as integers — Hardcover's `book_id: Int!`, not a string."""
    with HardcoverServer(catalog=[JINN_BOT], library={}) as server:
        entry = _client(server).ensure_library_entry(book_id=328491, edition_id=990011)

    assert str(entry) == "7002"
    assert server.library[7002]["book_id"] == 328491
    assert server.library[7002]["edition_id"] == 990011
    assert server.library[7002]["status_id"] == 2


def test_the_privacy_setting_comes_back_with_the_library_lookup():
    """One round trip, not two: the account's default privacy rides along on `me`.

    autoCalibre-Web fetches this in a request of its own before it can insert a book.
    `me` counts as a single top-level field however much is selected under it, so the
    relay can have it for nothing.
    """
    with HardcoverServer(catalog=[JINN_BOT], library={}) as server:
        _client(server).ensure_library_entry(book_id=328491, edition_id=990011)

    lookups = [q for q in server.queries if "user_books(where:" in q]
    assert len(lookups) == 1
    assert "account_privacy_setting_id" in lookups[0]
    assert server.library[7002]["privacy_setting_id"] == 1


def test_a_title_lookup_offers_candidates_rather_than_choosing_one():
    """The review page's prefill for a row with no ISBN.

    A candidate is a suggestion a human confirms; nothing here writes an identity. The
    exact-title match is deliberate — Hardcover disables `_ilike` and friends, and its
    fuzzy `search` answers in a Typesense document whose shape is not documented.
    """
    with HardcoverServer(catalog=[JINN_BOT, BURNING_GOD]) as server:
        candidates = _client(server).find_by_title("Burning God")

    assert [(str(c.book_id), str(c.edition_id)) for c in candidates] == [("118822", "640044")]
    assert candidates[0].title == "Burning God"


def test_a_title_lookup_that_matches_nothing_offers_nothing():
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        assert _client(server).find_by_title("A Book With No ISBN") == []


# ---------------------------------------------------------------- criterion 5


def test_a_token_missing_a_scope_is_an_error_that_names_the_scope():
    """Criterion 5's example: 403 `insufficient_scope` out of Hardcover.

    This is the failure the owner is most likely to hit, because progress writes need
    `write:library` and a token minted before Aug 2026 for a different purpose will not
    have it.
    """
    with HardcoverServer(catalog=[JINN_BOT], library={}, write_scope=False) as server:
        with pytest.raises(HardcoverError) as caught:
            _client(server).ensure_library_entry(book_id=328491, edition_id=990011)

    assert caught.value.status_code == 403
    assert caught.value.code == "insufficient_scope"
    assert "write:library" in str(caught.value)


def test_an_invalid_token_says_so_rather_than_reporting_no_match():
    """A 401 must not collapse into "this ISBN is not in Hardcover"."""
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        with pytest.raises(HardcoverError) as caught:
            HardcoverClient("wrong", endpoint=f"{server.address}/v1/graphql").resolve_isbn(
                "9781915590012"
            )

    assert caught.value.status_code == 401
    assert "invalid_token" in str(caught.value)


def test_no_configured_token_is_refused_without_calling_hardcover_at_all():
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        with pytest.raises(HardcoverError) as caught:
            HardcoverClient("", endpoint=f"{server.address}/v1/graphql").resolve_isbn(
                "9781915590012"
            )

    assert "no hardcover token" in str(caught.value).lower()
    assert server.requests == []


def test_a_graphql_error_in_a_200_response_is_still_an_error():
    """Hardcover answers some failures with HTTP 200 and an `errors` array.

    Treating that as an empty result would read as "not in the catalog" and quietly
    enqueue the book for a human — the failure mode criterion 5 exists to prevent.
    """
    with HardcoverServer(catalog=[JINN_BOT], graphql_errors=["validation failed"]) as server:
        with pytest.raises(HardcoverError) as caught:
            _client(server).resolve_isbn("9781915590012")

    assert "validation failed" in str(caught.value)


def test_the_token_goes_in_the_authorization_header_as_a_bearer():
    """Hardcover reads a personal access token from `Authorization: Bearer …`."""
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        _client(server).resolve_isbn("9781915590012")

    assert server.headers[-1]["authorization"] == "Bearer hc-token"
    assert server.headers[-1]["content-type"] == "application/json"


def test_a_service_that_cannot_be_reached_is_an_error_not_an_empty_result():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]

    with pytest.raises(HardcoverError):
        HardcoverClient("hc-token", endpoint=f"http://127.0.0.1:{dead}/v1/graphql").resolve_isbn(
            "9781915590012"
        )


# ------------------------------------------------------ being a good citizen


def test_the_client_spaces_its_calls_by_its_minimum_interval():
    """Hardcover's Free tier is a per-minute quota, so the relay paces itself from the
    first call rather than discovering the limit through a 429."""
    now = [100.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    with HardcoverServer(catalog=[JINN_BOT]) as server:
        client = HardcoverClient(
            "hc-token",
            endpoint=f"{server.address}/v1/graphql",
            min_interval_seconds=1.5,
            clock=clock,
            sleep=sleep,
        )
        client.resolve_isbn("9781915590012")
        client.resolve_isbn("9781915590012")
        client.resolve_isbn("9781915590012")

    # The first call goes straight out; each later one waits out the interval.
    assert slept == [1.5, 1.5]


def test_no_pacing_at_all_by_default():
    """Pacing is a tunable, not a surprise: with no interval nothing is ever waited out."""
    slept: list[float] = []

    with HardcoverServer(catalog=[JINN_BOT]) as server:
        client = HardcoverClient(
            "hc-token", endpoint=f"{server.address}/v1/graphql", sleep=slept.append
        )
        client.resolve_isbn("9781915590012")
        client.resolve_isbn("9781915590012")

    assert slept == []


def test_a_rate_limit_is_marked_transient_so_a_caller_can_choose_to_retry():
    """`transient` is the fact a retry decision needs, and the client is where it is known:
    the status code alone cannot say whether waiting would have helped."""
    with HardcoverServer(catalog=[JINN_BOT]) as server:
        server.status_plan = [429]
        with pytest.raises(HardcoverError) as raised:
            _client(server).resolve_isbn("9781915590012")

    assert raised.value.status_code == 429
    assert raised.value.transient is True


def test_a_graphql_error_is_not_marked_transient():
    """A typo in a document comes back the same way every time, so retrying repeats it."""
    with HardcoverServer(catalog=[JINN_BOT], graphql_errors=["field 'nope' not found"]) as server:
        with pytest.raises(HardcoverError) as raised:
            _client(server).resolve_isbn("9781915590012")

    assert raised.value.transient is False
