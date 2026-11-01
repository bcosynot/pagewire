#!/usr/bin/env python3
"""Regenerate PER-43's evidence from the repository alone.

Run it with the relay's virtualenv:

    .venv/bin/python docs/evidence/per-43-identity/reproduce.py

What it does, and why each piece is here:

1. Rebuilds a books table from `docs/evidence/per-42-sweep/books-rows.tsv` — the committed
   record of a real sweep of the card, 100 books with their content hashes, titles and
   ISBNs. No scratch directory and no device are needed, so this stays runnable.
2. Starts the two service doubles from `tests/service_fixtures.py` — real HTTP servers
   speaking Readwise's Books API and Hardcover's GraphQL, as the test suite uses them.
3. Runs the identity CLI the way it is deployed (configuration from `PAGEWIRE_*`), then
   serves the relay and screenshots the review page before and after a confirmation.

**What this evidence is and is not.** Every state change below happens against the
doubles, not against Readwise or Hardcover, because no service token exists in the
environment this was built in. It therefore proves the relay's behaviour — the calls it
makes, the states it writes, the page it serves — and proves nothing about either live
service. The live halves of criteria 1, 2 and 5 are listed as owner-gated in `README.md`.
"""

from __future__ import annotations

import csv
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))

from service_fixtures import HardcoverServer, ReadwiseServer  # noqa: E402

TOKEN = "evidence-token"
ROW_DB = HERE / "run-books.db"

# Two real books off the card: one carrying an ISBN (so it resolves without a human) and
# one that does not (so it lands in the queue and has to be confirmed).
RESOLVABLE_ISBN = "9781627790376"          # Algorithms to Live By — Brian Christian
CONFIRM_TITLE = "Ask For It"               # Linda Babcock, no ISBN in the OPF
CONFIRM_BOOK_HC = {"id": 4471, "title": CONFIRM_TITLE, "editions": [{"id": 8123, "isbn_13": None}]}


def catalog_from_sweep(connection: sqlite3.Connection) -> list[dict[str, object]]:
    """A Hardcover catalog that carries every ISBN the sweep found.

    Seeded from the card's own rows so the run below exercises the ordinary case — an ISBN
    both services know — rather than a catalog that happens to know one book. The
    no-catalog-entry path is a unit test (`test_identity.py`), not this.
    """
    connection.row_factory = sqlite3.Row
    catalog = []
    for index, row in enumerate(
        connection.execute(
            "SELECT title, isbn FROM books WHERE isbn IS NOT NULL ORDER BY book_path"
        )
    ):
        catalog.append(
            {
                "id": 90000 + index,
                "title": row["title"],
                "editions": [{"id": 30000 + index, "isbn_13": row["isbn"]}],
            }
        )
    return catalog


def build_database() -> Path:
    """A books table from the committed sweep rows, so this runs without the card."""
    ROW_DB.unlink(missing_ok=True)
    connection = sqlite3.connect(ROW_DB)
    connection.executescript(
        """
        CREATE TABLE books (
            document TEXT PRIMARY KEY, hash_method TEXT, book_path TEXT, title TEXT,
            author TEXT, isbn TEXT, readwise_id TEXT, hardcover_id TEXT,
            hardcover_edition_id TEXT, status TEXT NOT NULL DEFAULT 'unmatched',
            error TEXT, first_seen_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        """
    )
    with (REPO / "docs/evidence/per-42-sweep/books-rows.tsv").open() as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            connection.execute(
                "INSERT INTO books (document, hash_method, book_path, title, author, isbn,"
                " status, first_seen_at, updated_at) VALUES (?,?,?,?,?,?,'unmatched',1,1)",
                (
                    row["document"],
                    row["hash_method"],
                    row["book_path"],
                    row["title"],
                    row["author"],
                    row["isbn"] or None,
                ),
            )
    connection.commit()
    connection.close()
    return ROW_DB


def readback(connection: sqlite3.Connection, document: str) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM books WHERE document = ?", (document,)).fetchone()
    return dict(row) if row else {}


def document_for(connection: sqlite3.Connection, column: str, value: str) -> str:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        f"SELECT document FROM books WHERE {column} = ?", (value,)
    ).fetchone()
    assert row is not None, f"no swept book with {column} = {value!r}"
    return str(row["document"])


def write_report(name: str, lines: list[str]) -> None:
    (HERE / name).write_text("\n".join(lines) + "\n")
    print(f"wrote {name}")


def screenshot(url: str, name: str) -> None:
    """A real browser screenshot, so the page is inspected rather than described."""
    subprocess.run(
        [
            "chromium",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            "--virtual-time-budget=3000",
            "--window-size=1500,1400",
            f"--screenshot={HERE / name}",
            url,
        ],
        check=True,
        capture_output=True,
    )
    print(f"wrote {name}")


def main() -> int:
    build_database()
    connection = sqlite3.connect(ROW_DB)
    isbn_document = document_for(connection, "isbn", RESOLVABLE_ISBN)
    confirm_document = document_for(connection, "title", CONFIRM_TITLE)

    from pagewire.app import create_app
    from pagewire.config import Settings

    with ReadwiseServer(library={}, token="rw-evidence-token") as readwise:
        with HardcoverServer(
            catalog=[*catalog_from_sweep(connection), CONFIRM_BOOK_HC],
            library={},
            token="hc-evidence-token",
        ) as hardcover:
            endpoint = f"{hardcover.address}/v1/graphql"

            # 1. The CLI, configured exactly as a deployment would configure it.
            environment = {
                **os.environ,
                "PAGEWIRE_DB": str(ROW_DB),
                "PAGEWIRE_READWISE_ENABLED": "true",
                "PAGEWIRE_READWISE_TOKEN": "rw-evidence-token",
                "PAGEWIRE_READWISE_BASE_URL": readwise.address,
                "PAGEWIRE_HARDCOVER_ENABLED": "true",
                "PAGEWIRE_HARDCOVER_TOKEN": "hc-evidence-token",
                "PAGEWIRE_HARDCOVER_ENDPOINT": endpoint,
            }

            def run_cli() -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, "-m", "pagewire.identity"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    cwd=str(REPO),
                )

            # 1a. Criterion 5, for real: a token without `write:library` is refused by
            # Hardcover, and every row it blocked says so rather than being skipped.
            hardcover.write_scope = False
            refused = run_cli()
            blocked = readback(connection, isbn_document)
            write_report(
                "criterion-5-refused-calls.log",
                [
                    "# A Hardcover token with no write scope, against the same 100 books",
                    "# Every row it could not finish carries the service's own words and",
                    "# stays unmatched — no row is silently skipped, and no row is half matched.",
                    "",
                    refused.stdout.rstrip(),
                    "",
                    "# The row it blocked, read back:",
                    *[f"  {key:24} {value!r}" for key, value in blocked.items()],
                ],
            )

            # 1b. The same run with the scope fixed. Nothing else changed.
            hardcover.write_scope = True
            resolved = run_cli()
            write_report(
                "run-resolve.log",
                [
                    "# python -m pagewire.identity, against the two service doubles",
                    "# 100 swept books; only the 15 carrying an ISBN resolve without a human",
                    "",
                    resolved.stdout.rstrip(),
                    resolved.stderr.rstrip(),
                ],
            )
            write_report(
                "criterion-1-and-2-readback.txt",
                [
                    "# The one row both services knew, read back out of the database",
                    f"# document {isbn_document} (ISBN {RESOLVABLE_ISBN})",
                    "",
                    *[f"  {key:24} {value!r}" for key, value in readback(connection, isbn_document).items()],
                    "",
                    "# What the doubles were asked, in order — the calls the relay makes:",
                    *[f"  readwise    {request}" for request in readwise.requests],
                    *[f"  hardcover   POST /v1/graphql  ({' '.join(q.split())[:70]}…)" for q in hardcover.queries],
                ],
            )

            # 2. The page, against the same two doubles.
            settings = Settings(
                db_path=str(ROW_DB),
                device_token=TOKEN,
                readwise_enabled=True,
                readwise_token="rw-evidence-token",
                readwise_base_url=readwise.address,
                hardcover_enabled=True,
                hardcover_token="hc-evidence-token",
                hardcover_endpoint=endpoint,
            )
            address = _serve(create_app(settings))
            try:
                queue = f"{address}/review?token={TOKEN}"
                screenshot(queue, "review-queue.png")
                screenshot(
                    f"{address}/review/{confirm_document}/candidates?token={TOKEN}",
                    "review-candidates.png",
                )

                before = readback(connection, confirm_document)
                confirm_url = f"{address}/review/{confirm_document}"
                request = urllib.request.Request(
                    confirm_url,
                    data=urllib.parse.urlencode(
                        {
                            "token": TOKEN,
                            "readwise_id": "",
                            "hardcover_id": str(CONFIRM_BOOK_HC["id"]),
                            "hardcover_edition_id": str(CONFIRM_BOOK_HC["editions"][0]["id"]),
                        }
                    ).encode(),
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    assert response.status == 200, response.status
                screenshot(queue, "review-queue-after-confirm.png")
                after = readback(connection, confirm_document)

                write_report(
                    "criterion-3-before-after.txt",
                    [
                        "# Confirming one queued book, once — readback either side of the POST",
                        f"# document {confirm_document} ({CONFIRM_TITLE}), no ISBN in the OPF",
                        "",
                        "before:",
                        *[f"  {key:24} {value!r}" for key, value in before.items()],
                        "",
                        "after:",
                        *[f"  {key:24} {value!r}" for key, value in after.items()],
                        "",
                        "# The Readwise id in `after` was issued by the double, matched on",
                        "# title + author, which is the identity its own API documents.",
                    ],
                )

                waiting = connection.execute(
                    "SELECT count(*) FROM books WHERE status = 'unmatched' AND isbn IS NULL"
                ).fetchone()[0]
                counts = {
                    status: connection.execute(
                        "SELECT count(*) FROM books WHERE status = ?", (status,)
                    ).fetchone()[0]
                    for status in ("auto", "confirmed", "unmatched")
                }
                failed = connection.execute(
                    "SELECT count(*) FROM books WHERE error IS NOT NULL"
                ).fetchone()[0]
                write_report(
                    "queue-after-confirm.txt",
                    [
                        f"books in the confirmation queue:   {waiting}",
                        f"books marked auto by the CLI run:  {counts['auto']}",
                        f"books confirmed:                   {counts['confirmed']}",
                        f"books that failed, with a reason:  {failed}",
                    ],
                )
            finally:
                _stop()

    connection.close()
    return 0


_SERVER = None


def _serve(app):
    """Serve the relay on a loopback port, in a thread, the way uvicorn runs it."""
    import uvicorn

    global _SERVER
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    _SERVER = uvicorn.Server(config)
    threading.Thread(target=_SERVER.run, daemon=True).start()

    address = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{address}/healthz", timeout=1):
                return address
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"the relay did not come up on {address}")


def _stop() -> None:
    if _SERVER is not None:
        _SERVER.should_exit = True


if __name__ == "__main__":
    raise SystemExit(main())
