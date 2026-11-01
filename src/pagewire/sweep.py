"""The metadata sweep: read the card while the reader is in File Transfer mode.

`reader.session` carries an SD path and a content hash but no title, author or ISBN.
This is what fills that gap from the only place that knows: the books themselves, read
out of their OPFs over the device's own web server.

Three properties shape the code.

**It is a separate, triggered path, not part of ingest.** The device's web server only
runs in File Transfer or Calibre Wireless mode, so a sweep cannot happen during a normal
reading session and must not be attempted on the ingest path.

**It is all-or-nothing.** A half-swept card is worse than an unswept one: book rows that
exist for some books and not others look like a card that was read when it was not. So
every book is fetched and parsed first, and the table is written once at the end. Any
failure — the reader asleep, a book missing, an unreadable EPUB — leaves the books table
exactly as it found it.

**A second run costs nothing.** The listing carries no hash, and hashing a file means
downloading it, so "already identified" is decided by SD path: a path already tied to a
book is skipped. That is what makes a routine sweep cheap rather than a 400 MB re-fetch.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .device import CardEntry, DeviceClient, DeviceError
from .document_id import CONTENT_METHOD, content_document_id
from .epub import read_metadata
from .storage import BookRecord, Storage


class SweepError(RuntimeError):
    """The card could not be swept. Nothing has been written."""


class Device(Protocol):
    """What a sweep needs from the reader's web server."""

    address: str

    def list_files(self, path: str = "/") -> list[CardEntry]: ...

    def download(self, path: str, destination: str | Path) -> int: ...


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did, in the shape a run log wants to print."""

    epub_count: int
    downloaded: int
    skipped: int
    written: int
    bytes_downloaded: int
    needs_confirmation: list[str] = field(default_factory=list)


def list_epubs(device: Device) -> list[CardEntry]:
    """Every EPUB on the card, walking into every directory, in a stable order.

    The device lists one directory per call and has no recursive mode, so the walk is
    here. Order is by path so that two runs of the same card produce the same log.
    """
    found: list[CardEntry] = []
    pending = ["/"]
    while pending:
        path = pending.pop()
        for entry in device.list_files(path):
            if entry.is_directory:
                pending.append(entry.path)
            elif entry.is_epub:
                found.append(entry)
    return sorted(found, key=lambda entry: entry.path)


def sweep(
    device: Device,
    storage: Storage,
    *,
    workdir: str | Path | None = None,
    log: Callable[[str], None] | None = None,
) -> SweepReport:
    """Identify every book on the card that is not yet known, and record it.

    Returns what it did; raises `SweepError` — having written nothing — if any part of
    the card could not be read.
    """
    say = log or (lambda _line: None)

    try:
        epubs = list_epubs(device)
    except DeviceError as exc:
        raise SweepError(f"could not reach the device: {exc}") from exc

    known = storage.known_book_paths()
    pending = [entry for entry in epubs if entry.path not in known]
    say(f"{len(epubs)} EPUB(s) on the card, {len(pending)} not yet identified")

    work = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="pagewire-sweep-"))
    work.mkdir(parents=True, exist_ok=True)

    records: list[BookRecord] = []
    downloaded_bytes = 0
    for entry in pending:
        say(f"downloading {entry.path} ({entry.size:,} bytes)")
        scratch = work / f"{hashlib.sha256(entry.path.encode()).hexdigest()[:16]}.epub"
        try:
            downloaded_bytes += device.download(entry.path, scratch)
            document = content_document_id(scratch)
            metadata = read_metadata(scratch)
        except (DeviceError, zipfile.BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
            raise SweepError(f"could not read {entry.path!r}: {exc}") from exc
        finally:
            scratch.unlink(missing_ok=True)

        records.append(
            BookRecord(
                document=document,
                hash_method=CONTENT_METHOD,
                book_path=entry.path,
                title=metadata.title,
                author=metadata.author,
                isbn=metadata.isbn,
            )
        )
        say(f"  -> {document} {metadata.title or '?'!r}")

    # One write, after everything has been read. This is what makes a failure leave no
    # partial rows behind.
    written = storage.upsert_books(records)

    return SweepReport(
        epub_count=len(epubs),
        downloaded=len(pending),
        skipped=len(epubs) - len(pending),
        written=written,
        bytes_downloaded=downloaded_bytes,
        needs_confirmation=[r.book_path for r in records if r.isbn is None],
    )


def main(argv: list[str] | None = None) -> int:
    """Run a sweep from the command line, which is how the card gets identified.

    `python -m pagewire.sweep --address 192.168.0.217` while the reader is in File
    Transfer mode. Exits non-zero with a readable message when it cannot.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--address", required=True, help="the reader's LAN address or hostname")
    parser.add_argument("--db", default="", help="SQLite path (default: PAGEWIRE_DB or state/pagewire.db)")
    parser.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    arguments = parser.parse_args(argv)

    from .config import Settings

    settings = Settings.from_env()
    client = DeviceClient(
        arguments.address,
        timeout=arguments.timeout if arguments.timeout is not None else settings.sweep_timeout_seconds,
    )
    storage = Storage(arguments.db or settings.db_path)

    try:
        report = sweep(client, storage, log=lambda line: print(line, flush=True))
    except SweepError as exc:
        print(f"sweep failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"\ndone: {report.epub_count} on the card, {report.downloaded} downloaded"
        f" ({report.bytes_downloaded:,} bytes), {report.skipped} already known,"
        f" {report.written} written"
    )
    if report.needs_confirmation:
        print(f"\nneeds confirmation ({len(report.needs_confirmation)}):")
        for path in report.needs_confirmation:
            print(f"  {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand against the reader
    raise SystemExit(main())
