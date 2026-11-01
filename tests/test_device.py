"""The client that talks to the device's own File Transfer web server.

Every test here runs against a real HTTP server (`DeviceServer`) rather than a stub, so
the request shapes, the query encoding of a path with spaces and `&`, and the error
handling are all exercised rather than assumed.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from pagewire.device import CardEntry, DeviceClient, DeviceError

from device_fixtures import DeviceServer, make_epub_bytes

CARD = {
    "/Samit Basu - The Jinn-Bot of Shantiport.epub": make_epub_bytes(title="Jinn-Bot"),
    "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub": make_epub_bytes(title="Burning God"),
    "/Kuang, R. F_/Dragon Republic - R. F. Kuang.epub": make_epub_bytes(title="Dragon"),
    "/crash_report.txt": b"not a book",
}


def test_the_root_listing_names_directories_and_books_separately():
    with DeviceServer(CARD) as device:
        client = DeviceClient(device.address)
        entries = client.list_files("/")

    by_name = {e.name: e for e in entries}
    assert by_name["Samit Basu - The Jinn-Bot of Shantiport.epub"].is_epub is True
    assert by_name["Samit Basu - The Jinn-Bot of Shantiport.epub"].is_directory is False
    assert by_name["Kuang, R. F_"].is_directory is True
    assert by_name["crash_report.txt"].is_epub is False


def test_a_subdirectory_is_listed_by_its_path():
    with DeviceServer(CARD) as device:
        client = DeviceClient(device.address)
        entries = client.list_files("/Kuang, R. F_")

    names = {e.name for e in entries}
    assert names == {"Burning God, The - R. F. Kuang.epub", "Dragon Republic - R. F. Kuang.epub"}


def test_a_listed_entry_carries_its_full_path():
    """The sweep needs the path to fetch the book, not just the leaf name."""
    with DeviceServer(CARD) as device:
        client = DeviceClient(device.address)
        entries = client.list_files("/Kuang, R. F_")

    paths = {e.path for e in entries}
    assert "/Kuang, R. F_/Dragon Republic - R. F. Kuang.epub" in paths


def test_a_book_is_downloaded_to_a_file_with_its_bytes_intact(tmp_path: Path):
    """The path has spaces, a comma and an apostrophe-free quote — encoding matters."""
    target = tmp_path / "book.epub"
    wanted = "/Kuang, R. F_/Burning God, The - R. F. Kuang.epub"

    with DeviceServer(CARD) as device:
        client = DeviceClient(device.address)
        written = client.download(wanted, target)

    assert written == len(CARD[wanted])
    assert target.read_bytes() == CARD[wanted]


def test_the_client_accepts_a_bare_address_as_well_as_a_url():
    with DeviceServer(CARD) as device:
        bare = device.address.removeprefix("http://")
        assert DeviceClient(bare).list_files("/") == DeviceClient(device.address).list_files("/")


def test_a_device_with_no_web_server_running_is_a_clean_error():
    """This is the ordinary case during reading: the server is simply not up."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    client = DeviceClient(f"http://127.0.0.1:{dead_port}")

    with pytest.raises(DeviceError) as caught:
        client.list_files("/")

    assert "127.0.0.1" in str(caught.value)


def test_downloading_a_book_the_card_does_not_have_is_a_clean_error(tmp_path: Path):
    with DeviceServer(CARD) as device:
        client = DeviceClient(device.address)
        with pytest.raises(DeviceError):
            client.download("/Nope/missing.epub", tmp_path / "x.epub")


def test_a_card_entry_is_immutable():
    """Entries are read once and passed around; nothing should be able to rewrite one."""
    entry = CardEntry(path="/a.epub", name="a.epub", size=1, is_directory=False, is_epub=True)
    with pytest.raises(Exception):
        entry.name = "b.epub"  # type: ignore[misc]
