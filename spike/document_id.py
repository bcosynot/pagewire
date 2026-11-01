#!/usr/bin/env python3
"""Reproduce the Crosspoint / KOReader document id, so we can key a book by its content.

The firmware has two methods and the device picks one per its own setting:

  * content  -- a partial MD5 over bytes sampled from the file
  * filename -- a plain MD5 of the file's basename

Both are implemented here because the spike cannot know in advance which is
configured, and the whole identity table depends on matching the device's choice.

Ported from `lib/KOReaderSync/KOReaderDocumentId.cpp` on the firmware's `develop`
branch. Its header states the offsets as:

    Offsets are calculated as: 1024 << (2*i) for i = -1 to 10. For i = -1,
    LuaJIT's bit.lshift(1024, -2) is 1024 << 30, which overflows 32 bits to 0,
    so the first offset is 0, not 256.
    Offsets: 0, 1024, 4096, 16384, 65536, 262144, 1048576, 4194304,
             16777216, 67108864, 268435456, 1073741824 bytes

    An offset at or beyond the file size is skipped; a chunk near the end is
    truncated.

Usage:

    python spike/document_id.py /path/to/book.epub                  # both methods
    python spike/document_id.py /path/to/book.epub --document HEX    # and check
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

CHUNK_SIZE = 1024
OFFSET_COUNT = 12  # i = -1 .. 10


def offset_for(i: int) -> int:
    """Offset for index i: 0 for i < 0, else 1024 << (2*i)."""
    if i < 0:
        return 0
    return CHUNK_SIZE << (2 * i)


def content_document_id(path: str) -> str:
    """The content-based partial MD5 the firmware reports as `document`."""
    size = os.path.getsize(path)
    digest = hashlib.md5()
    total = 0
    with open(path, "rb") as handle:
        for i in range(-1, OFFSET_COUNT - 1):
            offset = offset_for(i)
            if offset >= size:
                continue
            handle.seek(offset)
            chunk = handle.read(min(CHUNK_SIZE, size - offset))
            if chunk:
                digest.update(chunk)
                total += len(chunk)
    return digest.hexdigest()


def filename_document_id(path: str) -> str:
    """The filename-based MD5, the firmware's other document-id setting."""
    basename = os.path.basename(path)
    return hashlib.md5(basename.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", help="book file (the EPUB, as served by GET /download)")
    parser.add_argument(
        "--document",
        help="the `document` value the device reported, to check against",
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.file):
        print(f"no such file: {args.file}", file=sys.stderr)
        return 2

    content = content_document_id(args.file)
    filename = filename_document_id(args.file)
    size = os.path.getsize(args.file)

    print(f"file     : {args.file}")
    print(f"size     : {size} bytes")
    print(f"content  : {content}")
    print(f"filename : {filename}")
    print("offsets  : " + ", ".join(str(offset_for(i)) for i in range(-1, OFFSET_COUNT - 1)))

    if args.document:
        reported = args.document.strip().lower()
        if reported == content:
            print("verdict  : MATCH (content method) — the identity key can be computed locally")
        elif reported == filename:
            print("verdict  : MATCH (filename method) — the device is set to filename sync; "
                  "a book renamed on the card gets a new key")
        else:
            print(f"verdict  : NO MATCH — device reported {reported}")
            print("           this is a finding, not a failure: the hash cannot be reproduced "
                  "yet, and the identity design needs revisiting")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
