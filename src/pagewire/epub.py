"""Reading a book's metadata out of its own OPF.

The sweep exists because `reader.session` carries an SD path and a hash but no title,
author or ISBN. This is where those come from: the package document inside the EPUB,
which is the only place on the card that knows them.

Everything absent reads as `None`. That is the ingest layer's rule applied here — a
book with no `dc:creator` has no known author, and an empty string would be a claim
that the author is nobody.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path

DC = "http://purl.org/dc/elements/1.1/"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
CONTAINER_PATH = "META-INF/container.xml"

# `urn:isbn:`, an `ISBN` label, hyphens and spaces can all wrap the same digits.
_ISBN_PREFIX = re.compile(r"^\s*(?:urn\s*:\s*isbn\s*:|isbn\s*:?\s*)", re.IGNORECASE)
_NON_ISBN_CHARS = re.compile(r"[^0-9Xx]")


@dataclass(frozen=True)
class BookMetadata:
    """What the card knows about a book. Any of these may be unknown."""

    title: str | None
    author: str | None
    isbn: str | None


def read_metadata(path: str | Path) -> BookMetadata:
    """Read title, author and ISBN from an EPUB.

    Raises whatever `zipfile` and `ElementTree` raise for a file that is not a readable
    EPUB; the sweep turns that into a clean failure rather than a partial card.
    """
    with zipfile.ZipFile(path) as archive:
        opf_path = _opf_path(archive)
        package = ElementTree.fromstring(archive.read(opf_path))

    metadata = _metadata_element(package)
    if metadata is None:
        return BookMetadata(None, None, None)

    return BookMetadata(
        title=_first_text(metadata, f"{{{DC}}}title"),
        author=_first_text(metadata, f"{{{DC}}}creator"),
        isbn=_isbn(metadata),
    )


def _metadata_element(package: ElementTree.Element) -> ElementTree.Element | None:
    """The `<metadata>` element, with or without an OPF namespace prefix."""
    return next(
        (child for child in package if child.tag.rpartition("}")[2] == "metadata"), None
    )


def _opf_path(archive: zipfile.ZipFile) -> str:
    """The package document's path, from the container the EPUB spec requires."""
    container = ElementTree.fromstring(archive.read(CONTAINER_PATH))
    rootfile = container.find(f".//{{{CONTAINER}}}rootfile")
    if rootfile is None:
        raise KeyError(f"{CONTAINER_PATH} names no rootfile")
    full_path = rootfile.get("full-path")
    if not full_path:
        raise KeyError(f"{CONTAINER_PATH} rootfile has no full-path")
    return full_path


def _first_text(element: ElementTree.Element, tag: str) -> str | None:
    """The text of the first matching child, or None when it is absent or empty."""
    found = element.find(tag)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _isbn(metadata: ElementTree.Element) -> str | None:
    """The ISBN among the book's identifiers, or None when it carries none.

    EPUBs routinely carry several `dc:identifier` values — a UUID, the book's own ISBN,
    sometimes a publisher reference — so each one is normalised and shape-checked rather
    than the first being trusted.
    """
    for identifier in metadata.findall(f"{{{DC}}}identifier"):
        normalised = normalise_isbn(identifier.text)
        if normalised is not None:
            return normalised
    return None


def normalise_isbn(value: str | None) -> str | None:
    """`urn:isbn:978-0-441-01359-3` and `9780441013593` are the same ISBN.

    Shape-checked, not checksum-validated: a UUID strips to 32 hex characters and is
    rejected, but a typo in a real ISBN would pass. Catching that is the services' job,
    and rejecting an ISBN the device considers valid would be worse.
    """
    if not isinstance(value, str):
        return None
    digits = _NON_ISBN_CHARS.sub("", _ISBN_PREFIX.sub("", value))
    if len(digits) == 13 and digits.isdigit():
        return digits
    if len(digits) == 10 and digits[:9].isdigit() and digits[-1] in "0123456789Xx":
        return digits.upper()
    return None
