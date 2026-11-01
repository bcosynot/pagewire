"""The document id: the hash the device reports for a book, computed locally.

The relay keys the books table on this value, so it has to agree with the device
exactly. Ported from the firmware's `lib/KOReaderSync/KOReaderDocumentId.cpp` and
verified against real hardware during the device spike: for one book the device
reported `8f49706e45186c3185a9a7fcbcabe6f5` and this file reproduces it.

The firmware has **two** methods and picks one per a device setting:

* `content_document_id` — a partial MD5 over bytes sampled from the file. This is what
  the device in use is configured for, and the one the relay keys on.
* `filename_document_id` — a plain MD5 of the file's basename. Under this method a
  rename on the card re-keys the book, which is why the stored row records which method
  produced it.

The offsets, from the firmware header: `1024 << (2*i)` for i = -1 to 10, where
`bit.lshift(1024, -2)` overflows 32 bits to 0 — so the first offset is 0, not 256. An
offset at or beyond the file size is skipped, and a chunk near the end is truncated
rather than padded.

The sampling has a consequence worth stating plainly: only the sampled chunks are read,
so two EPUBs agreeing across every one of those offsets are the same book as far as
identity is concerned. That is inherent to KOSync's scheme.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024
OFFSET_COUNT = 12  # i = -1 .. 10
OFFSETS: tuple[int, ...] = tuple(
    0 if i < 0 else CHUNK_SIZE << (2 * i) for i in range(-1, OFFSET_COUNT - 1)
)

CONTENT_METHOD = "content"
FILENAME_METHOD = "filename"


def content_document_id(path: str | Path) -> str:
    """The content-sampled partial MD5 the firmware reports as `document`."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for offset in OFFSETS:
            if offset >= size:
                continue
            handle.seek(offset)
            chunk = handle.read(min(CHUNK_SIZE, size - offset))
            if chunk:
                digest.update(chunk)
    return digest.hexdigest()


def filename_document_id(path: str | Path) -> str:
    """The filename-based MD5, the firmware's other document-id setting."""
    return hashlib.md5(Path(path).name.encode("utf-8")).hexdigest()
