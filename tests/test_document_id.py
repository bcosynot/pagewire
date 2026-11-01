"""The document id: the key the whole books table hangs on.

KOSync's partial MD5 reads a fixed set of byte offsets and hashes whatever is there.
The offsets come from `lib/KOReaderSync/KOReaderDocumentId.cpp`; the spike verified the
result against real hardware. These tests pin the parts that are easy to get subtly
wrong — which offsets are read, that an offset past the end is skipped rather than
padded, and that a chunk near the end is truncated.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pagewire.document_id import (
    OFFSETS,
    content_document_id,
    filename_document_id,
)


def _write(tmp_path: Path, body: bytes, name: str = "book.epub") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def test_the_offsets_are_the_ones_the_firmware_documents():
    """`1024 << (2*i)` for i = -1..10, where i = -1 overflows to 0."""
    assert OFFSETS[0] == 0
    assert OFFSETS[1:4] == (1024, 4096, 16384)
    assert OFFSETS[-1] == 1073741824


def test_only_the_offsets_that_land_inside_the_file_are_read(tmp_path):
    """A 5 KiB file is hashed from offsets 0, 1024 and 4096 — nothing else exists."""
    body = bytes(range(256)) * 20  # 5120 bytes
    assert len(body) == 5120

    digest = content_document_id(_write(tmp_path, body))

    expected = hashlib.md5(
        body[0:1024] + body[1024:2048] + body[4096:5120]
    ).hexdigest()
    assert digest == expected


def test_an_offset_exactly_at_the_file_size_is_skipped(tmp_path):
    """`offset >= size` is skipped, so a 1 KiB file is hashed once, not twice."""
    body = b"a" * 1024
    assert content_document_id(_write(tmp_path, body)) == hashlib.md5(body).hexdigest()


def test_a_file_smaller_than_one_chunk_is_hashed_whole(tmp_path):
    body = b"short"
    assert content_document_id(_write(tmp_path, body)) == hashlib.md5(body).hexdigest()


def test_two_files_that_agree_across_the_sampled_bytes_hash_the_same(tmp_path):
    """The sampling is a known property of the scheme, pinned rather than assumed.

    Only the sampled chunks are read, so two EPUBs agreeing across every offset the
    device reads are indistinguishable to identity, however different the rest of them
    is. That is inherent to KOSync's scheme, not something the relay can fix — the
    design's answer is to store the path and title beside the hash and flag a known
    hash arriving with a different path, not to pretend the hash is unique.
    """
    shared = b"c" * 2048
    first = _write(tmp_path, shared + b"AAAA", "first.epub")
    second = _write(tmp_path, shared + b"ZZZZ", "second.epub")

    assert first.read_bytes() != second.read_bytes()
    assert content_document_id(first) == content_document_id(second)


def test_the_filename_method_hashes_the_basename_not_the_content(tmp_path):
    """The device's other setting. A rename re-keys the book under this method."""
    body = b"whatever"
    one = _write(tmp_path, body, "A Book.epub")
    two = _write(tmp_path, body, "A Renamed Book.epub")

    assert filename_document_id(one) != filename_document_id(two)
    assert filename_document_id(one) == hashlib.md5(b"A Book.epub").hexdigest()
    # The content method is indifferent to the rename, which is why it is the one used.
    assert content_document_id(one) == content_document_id(two)
