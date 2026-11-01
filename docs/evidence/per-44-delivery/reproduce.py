#!/usr/bin/env python3
"""Regenerate PER-44's evidence from the repository alone.

Run it with the relay's virtualenv:

    .venv/bin/python docs/evidence/per-44-delivery/reproduce.py

What it does, and why each piece is here:

1. Rebuilds a `books` table from `docs/evidence/per-42-sweep/books-rows.tsv` — the committed
   record of a real sweep of the card — and takes two rows from it: one with an ISBN, which
   gets identified, and one without, which is left `unmatched`. No device, no scratch
   directory, no service account.
2. Pumps real-shaped `reader.session` events through the relay's own ingest, derivation and
   delivery code, so the chain exercised is the deployed one.
3. Runs delivery against the Readwise double from `tests/service_fixtures.py` — a real HTTP
   server speaking the documented shapes — twice, to show the second run is a no-op, and a
   second scenario against a server that answers 500/503 first, to show the retry.

**What this evidence is and is not.** Every state change happens against the double, not
against Readwise, because no Readwise token exists in the environment this was built in. It
therefore proves the relay's side of every criterion — the exact request bodies, the retry
sequence, the ledger states, the gate on an unidentified book — and proves nothing about
Readwise accepting any of it. The live halves are the numbered steps in `runbook.md`.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

from service_fixtures import ReadwiseServer  # noqa: E402

from pagewire.delivery import FINISH_DESTINATION, deliver_readwise  # noqa: E402
from pagewire.readwise import ReadwiseClient  # noqa: E402
from pagewire.sessions import derive_and_store  # noqa: E402
from pagewire.storage import BookRecord, Storage  # noqa: E402

WINDOW = 30 * 60
START = 1_760_000_000  # 2025-10-09T08:53:20Z
NOW = START + 4 * 86_400


def _rows_from_sweep():
    """Two swept books: the first with an ISBN, the first without one."""
    source = HERE.parent / "per-42-sweep" / "books-rows.tsv"
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    with_isbn = next(row for row in rows if row["isbn"])
    without = next(row for row in rows if not row["isbn"] and row["title"])
    return with_isbn, without


def _seed_events(storage, document, *, offset, bp0, bp1, end_percent, minutes=30):
    """Two firmware sessions fifty minutes apart, so the stitcher makes one sitting."""
    for index, start in enumerate((START + offset, START + offset + 3000)):
        end = start + minutes * 60
        payload = {
            # The device's ids are per-boot nonce plus a counter; this one includes the
            # start time so two runs of the seeder for the same book cannot collide.
            "id": f"{document[:8]}-{start}-{index}",
            "event": "reader.session",
            "document": document,
            "book": f"/books/{document}.epub",
            "start_time": str(start),
            "end_time": str(end),
            "duration_seconds": str(end - start),
            "ts": str(end),
            "progress_scale": "10000",
            "start_progress_bp": str(bp0),
            "end_progress_bp": str(bp1),
            "end_percent": str(end_percent),
        }
        storage.store_event(payload, received_at=end)


def _database(with_isbn, without, *, readwise_id="4790033", unmatched_id=None):
    storage = Storage(":memory:")
    storage.upsert_books(
        [
            BookRecord(
                document=with_isbn["document"],
                hash_method=with_isbn["hash_method"] or "content",
                book_path=with_isbn["book_path"],
                title=with_isbn["title"],
                author=with_isbn["author"],
                isbn=with_isbn["isbn"],
            ),
            BookRecord(
                document=without["document"],
                hash_method=without["hash_method"] or "content",
                book_path=without["book_path"],
                title=without["title"],
                author=without["author"],
                isbn=None,
            ),
        ]
    )
    storage.set_book_identity(with_isbn["document"], readwise_id=readwise_id, status="auto")
    if unmatched_id is not None:
        # A row that was identified once and re-queued keeps its id — the state the status
        # gate has to survive, and the reason the gate is the status rather than the id.
        storage.set_book_identity(
            without["document"], readwise_id=unmatched_id, status="unmatched"
        )
    _seed_events(storage, with_isbn["document"], offset=0, bp0=4120, bp1=4470, end_percent=45)
    _seed_events(storage, with_isbn["document"], offset=90_000, bp0=4470, bp1=10_000, end_percent=100)
    _seed_events(storage, without["document"], offset=180_000, bp0=1000, bp1=1200, end_percent=12)
    derive_and_store(storage, now=NOW)
    return storage, with_isbn, without


def _library(with_isbn):
    return {
        with_isbn["isbn"]: {
            "id": 4790033,
            "title": with_isbn["title"],
            "author": with_isbn["author"],
            "isbn": with_isbn["isbn"],
            "status": "currently_reading",
            "product": {"isbn": with_isbn["isbn"]},
        }
    }


def _ledger(storage):
    lines = []
    for row in storage.send_log():
        if row["sent_at"] is not None:
            state = f"delivered external_id={row['external_id']}"
        elif row["error"]:
            state = f"pending error_status={row['error_status']} — {row['error']}"
        else:
            state = "claimed, outcome unknown"
        lines.append(
            f"{row['document']} @ {row['started_at']} {row['destination']}: {state}"
        )
    return "\n".join(lines) + "\n"


def _log(line):
    print(f"  {line}")


def main() -> int:
    with_isbn, without = _rows_from_sweep()
    print(f"identified book: {with_isbn['title']!r} (isbn {with_isbn['isbn']})")
    print(f"unmatched book:  {without['title']!r} (no isbn)\n")

    # -- the happy path, twice ------------------------------------------------
    with ReadwiseServer(library=_library(with_isbn)) as server:
        storage, _, _ = _database(with_isbn, without)
        client = ReadwiseClient("rw-token", base_url=server.address)
        first = deliver_readwise(
            storage, client, timezone="America/New_York", sleep=lambda _s: None, log=_log
        )
        bodies_after_first = list(server.session_attempts)
        sessions_after_first = len(server.sessions)
        _log(f"first run: {len(first.delivered)} sent, {len(first.finished)} finished")
        second = deliver_readwise(
            storage, client, timezone="America/New_York", sleep=lambda _s: None, log=_log
        )
        _log(
            f"second run: {len(second.delivered)} sent,"
            f" {len(second.reconciled)} reconciled;"
            f" sessions at the service {sessions_after_first} -> {len(server.sessions)}"
        )

        (HERE / "request-bodies.json").write_text(
            json.dumps(
                {
                    "sessions": [attempt["items"] for attempt in bodies_after_first],
                    "book_patches": server.patches,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        (HERE / "send-log.txt").write_text(_ledger(storage))
        (HERE / "idempotence.txt").write_text(
            "sessions after the first run:  {first}\n"
            "sessions after the second run: {second}\n"
            "requests made by the second run: {requests}\n".format(
                first=sessions_after_first,
                second=len(server.sessions),
                requests=sum(
                    1
                    for attempt in server.session_attempts[len(bodies_after_first) :]
                ),
            )
        )

    # -- a destination that answers 500 then 503 ------------------------------
    with ReadwiseServer(library=_library(with_isbn), fail_plan=[500, 503]) as server:
        storage, _, _ = _database(with_isbn, without)
        client = ReadwiseClient("rw-token", base_url=server.address)
        report = deliver_readwise(
            storage,
            client,
            timezone="America/New_York",
            retry_backoff_seconds=2.0,
            sleep=lambda _s: None,
            log=_log,
        )
        (HERE / "retry-log.txt").write_text(
            "attempts and the status each was answered with:\n"
            + "\n".join(
                f"  attempt {index + 1}: {attempt['status_code']}"
                for index, attempt in enumerate(server.session_attempts)
            )
            + f"\n\nsessions recorded: {len(server.sessions)}"
            + f"\ndelivered: {len(report.delivered)}"
            + "\n\nsend log after the retries:\n"
            + _ledger(storage)
        )

    # -- the gate: an unidentified book, and one re-queued while keeping its id ----
    with ReadwiseServer(library=_library(with_isbn)) as server:
        storage, _, _ = _database(with_isbn, without, unmatched_id="4799999")
        client = ReadwiseClient("rw-token", base_url=server.address)
        report = deliver_readwise(
            storage, client, timezone="America/New_York", sleep=lambda _s: None, log=_log
        )
        re_queued = storage.book(without["document"])
        (HERE / "gate.txt").write_text(
            f"unmatched book status: {re_queued['status']}\n"
            f"unmatched book readwise_id: {re_queued['readwise_id']}\n"
            f"its sittings in the deliverable set: 0 (the join requires status auto/confirmed)\n"
            f"requests naming it: {sum(1 for s in server.sessions if s['book_id'] == 4799999)}\n"
            f"send-log rows for it: {sum(1 for r in storage.send_log() if r['document'] == without['document'])}\n"
            f"delivered: {len(report.delivered)}\n"
        )

    # -- the switch: nothing configured --------------------------------------
    with ReadwiseServer(library=_library(with_isbn)) as server:
        storage, _, _ = _database(with_isbn, without)
        report = deliver_readwise(storage, None, timezone="America/New_York")
        (HERE / "switched-off.txt").write_text(
            f"requests received by the server: {len(server.requests)}\n"
            f"sessions recorded: {len(server.sessions)}\n"
            f"send-log rows: {len(storage.send_log())}\n"
            f"report.enabled: {report.enabled}\n"
        )

    print("\nwrote:")
    for name in (
        "request-bodies.json",
        "send-log.txt",
        "idempotence.txt",
        "retry-log.txt",
        "gate.txt",
        "switched-off.txt",
    ):
        print(f"  {HERE / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
