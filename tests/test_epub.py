"""Reading a book's own metadata out of its OPF.

The sweep's whole reason for existing is that `reader.session` carries an SD path and a
hash but no title, author or ISBN. Everything here is what the card can actually hold:
identifier blocks in four different shapes, creators with role attributes, books with
no title at all.
"""

from __future__ import annotations

from pathlib import Path

from pagewire.epub import read_metadata

from device_fixtures import make_epub_bytes


def _book(tmp_path: Path, name: str, **kwargs: object) -> Path:
    path = tmp_path / name
    path.write_bytes(make_epub_bytes(**kwargs))  # type: ignore[arg-type]
    return path


def test_title_author_and_isbn_come_from_the_opf(tmp_path):
    path = _book(
        tmp_path,
        "anything.epub",
        title="The Jinn-Bot of Shantiport",
        author="Samit Basu",
        identifiers=["urn:isbn:9781915590012"],
    )

    metadata = read_metadata(path)

    assert metadata.title == "The Jinn-Bot of Shantiport"
    assert metadata.author == "Samit Basu"
    assert metadata.isbn == "9781915590012"


def test_an_isbn_is_normalised_however_the_card_punctuates_it(tmp_path):
    """Cards carry `urn:isbn:`, hyphens, and bare digits for the same book."""
    for written in [
        "urn:isbn:978-0-441-01359-3",
        "9780441013593",
        "ISBN 978-0-441-01359-3",
        "URN:ISBN:9780441013593",
    ]:
        path = _book(tmp_path, f"{abs(hash(written))}.epub", identifiers=[written])
        assert read_metadata(path).isbn == "9780441013593", written


def test_an_isbn10_is_kept_with_the_shapes_that_matter(tmp_path):
    path = _book(tmp_path, "a.epub", identifiers=["urn:isbn:0441013597"])
    assert read_metadata(path).isbn == "0441013597"


def test_a_uuid_identifier_is_not_mistaken_for_an_isbn(tmp_path):
    """Most EPUBs carry a UUID as well as, or instead of, an ISBN."""
    path = _book(
        tmp_path,
        "a.epub",
        identifiers=["urn:uuid:0b6dd0e4-1c0a-4c1f-9d0e-3a1f2b3c4d5e"],
    )
    assert read_metadata(path).isbn is None


def test_the_isbn_is_found_when_it_sits_beside_a_uuid(tmp_path):
    path = _book(
        tmp_path,
        "a.epub",
        identifiers=[
            "urn:uuid:0b6dd0e4-1c0a-4c1f-9d0e-3a1f2b3c4d5e",
            "urn:isbn:9780441013593",
        ],
    )
    assert read_metadata(path).isbn == "9780441013593"


def test_a_book_with_no_identifiers_still_yields_its_title_and_author(tmp_path):
    """Criterion 5's shape: the row is still worth having without an ISBN."""
    path = _book(tmp_path, "a.epub", title="Zebra", author="Nobody")

    metadata = read_metadata(path)

    assert metadata.title == "Zebra"
    assert metadata.author == "Nobody"
    assert metadata.isbn is None


def test_a_book_with_no_creator_has_no_author_rather_than_an_empty_string(tmp_path):
    """Absent must read as absent, never as `''` — the ingest layer's rule, applied here."""
    path = tmp_path / "a.epub"
    path.write_bytes(make_epub_bytes(title="Solo", author=""))

    metadata = read_metadata(path)

    assert metadata.title == "Solo"
    assert metadata.author is None
