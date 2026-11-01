"""The metadata sweep, against a real device web server.

Each test names the acceptance criterion it pins. The device comes from
`DeviceServer`, a real HTTP server speaking the device's own shapes, so the sweep's
client is exercised end to end rather than stubbed out.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pagewire.device import DeviceClient
from pagewire.document_id import content_document_id
from pagewire.storage import Storage
from pagewire.sweep import SweepError, sweep

from device_fixtures import DeviceServer, make_epub_bytes

ISBN_BOOK = make_epub_bytes(
    title="The Jinn-Bot of Shantiport",
    author="Samit Basu",
    identifiers=["urn:isbn:9781915590012"],
)
NO_ISBN_BOOK = make_epub_bytes(title="A Book With No ISBN", author="Someone Uncredited")


def _card() -> dict[str, bytes]:
    """A card shaped like the real one: author directories, plus a stray at the root."""
    return {
        "/Samit Basu - The Jinn-Bot of Shantiport.epub": ISBN_BOOK,
        "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub": NO_ISBN_BOOK,
        "/crash_report.txt": b"not a book, however much it looks like one",
    }


def _sweep(tmp_path: Path, card: dict[str, bytes], storage: Storage | None = None):
    """Run one sweep against a fresh device server. Returns (report, storage, server)."""
    store = storage or Storage(":memory:")
    with DeviceServer(dict(card)) as device:
        report = sweep(DeviceClient(device.address), store, workdir=tmp_path)
    return report, store, device


# ---------------------------------------------------------------- criterion 1


def test_a_sweep_records_title_author_and_isbn_for_every_epub_on_the_card(tmp_path):
    """Criterion 1: one row per EPUB, walking into the author directories."""
    report, store, _ = _sweep(tmp_path, _card())

    rows = {row["book_path"]: row for row in store.books()}

    assert set(rows) == {
        "/Samit Basu - The Jinn-Bot of Shantiport.epub",
        "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub",
    }
    # The .txt is not an EPUB and must not become a row.
    assert "/crash_report.txt" not in rows

    jinn_bot = rows["/Samit Basu - The Jinn-Bot of Shantiport.epub"]
    assert jinn_bot["title"] == "The Jinn-Bot of Shantiport"
    assert jinn_bot["author"] == "Samit Basu"
    assert jinn_bot["isbn"] == "9781915590012"
    assert report.written == 2
    assert report.epub_count == 2


# ---------------------------------------------------------------- criterion 2


def test_the_sweep_keys_each_row_on_the_content_hash_of_the_file_it_downloaded(tmp_path):
    """Criterion 2: the row's key is the hash the device reports as `document`.

    The device computes this over the file it has on the card. The sweep computes it
    over the copy it downloaded. If they differ the books table has no key and every
    event arrives attached to a book nobody has heard of.
    """
    card = _card()
    _, store, _ = _sweep(tmp_path, card)

    assert store.count_books() == 2
    for row in store.books():
        # The expected value is computed straight from the bytes the card holds, which
        # is what the device hashes too — the sweep only ever saw a copy.
        assert row["document"] == _content_hash(card[row["book_path"]])
        assert row["hash_method"] == "content"


def _content_hash(body: bytes) -> str:
    """The content hash of a book that exists only as bytes."""
    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as handle:
        handle.write(body)
        name = handle.name
    try:
        return content_document_id(name)
    finally:
        Path(name).unlink()


# ---------------------------------------------------------------- criterion 3


def test_a_second_sweep_downloads_nothing_it_has_already_identified(tmp_path):
    """Criterion 3: identity is remembered, so the card is not re-fetched."""
    card = _card()
    store = Storage(":memory:")

    with DeviceServer(dict(card)) as device:
        client = DeviceClient(device.address)
        first = sweep(client, store, workdir=tmp_path)
        downloads_after_first = len(device.downloads())
        device.requests.clear()
        second = sweep(client, store, workdir=tmp_path)
        downloads_on_second_run = len(device.downloads())

    assert first.downloaded == 2
    assert downloads_after_first == 2
    assert second.downloaded == 0
    assert downloads_on_second_run == 0
    assert second.skipped == 2
    assert second.written == 0
    assert store.count_books() == 2


# ---------------------------------------------------------------- criterion 4


def test_the_sweep_writes_nothing_when_the_device_web_server_is_not_up(tmp_path):
    """Criterion 4: the ordinary case while reading — no server, no partial state."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    store = Storage(":memory:")

    with pytest.raises(SweepError) as caught:
        sweep(DeviceClient(f"http://127.0.0.1:{dead_port}"), store, workdir=tmp_path)

    assert "127.0.0.1" in str(caught.value)
    assert store.count_books() == 0


def test_a_failure_partway_through_leaves_no_rows_at_all(tmp_path):
    """Atomicity: a half-swept card is worse than an unswept one.

    The second book is pulled out of the server's card after the listing is taken, so
    the sweep fails on a real 404 mid-run rather than on a connection that never
    opened. Nothing it had already gathered may be written.
    """
    card = _card()
    store = Storage(":memory:")

    with DeviceServer(dict(card)) as device:
        client = DeviceClient(device.address)
        original = client.download

        calls = {"n": 0}

        def flaky(path: str, destination: Path) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                device.files.pop(path, None)
            return original(path, destination)

        client.download = flaky  # type: ignore[method-assign]
        with pytest.raises(SweepError):
            sweep(client, store, workdir=tmp_path)

    assert store.count_books() == 0


# ---------------------------------------------------------------- criterion 5


def test_a_book_without_an_isbn_still_gets_a_row_and_needs_confirmation(tmp_path):
    """Criterion 5: no ISBN means no confident match, so it waits for Vivek.

    The design's own vocabulary: a row that cannot be matched appears in the review
    list. `unmatched` is that state, and the sweep never writes `auto` or `confirmed`
    — matching is the identity ticket's job, not the sweep's.
    """
    _, store, _ = _sweep(tmp_path, _card())

    rows = {row["book_path"]: row for row in store.books()}
    without = rows["/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"]

    assert without["title"] == "A Book With No ISBN"
    assert without["author"] == "Someone Uncredited"
    assert without["isbn"] is None
    assert without["status"] == "unmatched"

    queue = {row["book_path"] for row in store.books_needing_confirmation()}
    assert queue == {"/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"}


def test_a_book_with_an_isbn_is_not_in_the_confirmation_queue(tmp_path):
    _, store, _ = _sweep(tmp_path, _card())

    queue = {row["book_path"] for row in store.books_needing_confirmation()}
    assert "/Samit Basu - The Jinn-Bot of Shantiport.epub" not in queue


# ---------------------------------------------------------------- reporting


def test_the_report_counts_what_it_did_so_the_run_can_be_logged(tmp_path):
    card = _card()
    store = Storage(":memory:")

    with DeviceServer(dict(card)) as device:
        client = DeviceClient(device.address)
        report = sweep(client, store, workdir=tmp_path)

    assert report.epub_count == 2
    assert report.downloaded == 2
    assert report.skipped == 0
    assert report.written == 2
    assert report.bytes_downloaded == len(ISBN_BOOK) + len(NO_ISBN_BOOK)
    assert report.needs_confirmation == ["/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"]


def test_the_sweep_reports_each_book_as_it_goes_so_a_run_can_be_read_back(tmp_path):
    """Criterion 3's evidence is a run log, so the sweep has to be able to produce one."""
    lines: list[str] = []

    with DeviceServer(_card()) as device:
        sweep(
            DeviceClient(device.address),
            Storage(":memory:"),
            workdir=tmp_path,
            log=lines.append,
        )

    logged = "\n".join(lines)
    assert "/Samit Basu - The Jinn-Bot of Shantiport.epub" in logged
    assert "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub" in logged
    assert "2 EPUB(s) on the card, 2 not yet identified" in logged


def test_the_sweep_does_not_leave_downloaded_books_behind_on_disk(tmp_path):
    """A full card is hundreds of megabytes; each book is hashed then released."""
    work = tmp_path / "work"
    with DeviceServer(_card()) as device:
        sweep(DeviceClient(device.address), Storage(":memory:"), workdir=work)

    leftovers = [p for p in work.rglob("*") if p.is_file()]
    assert leftovers == []
