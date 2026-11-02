#!/usr/bin/env python3
"""Regenerate PER-45's evidence from the repository alone.

Run it with the relay's virtualenv:

    .venv/bin/python docs/evidence/per-45-delivery/reproduce.py

What it does, and why each piece is here:

1. Rebuilds a `books` table from `docs/evidence/per-42-sweep/books-rows.tsv` — the committed
   record of a real sweep of the card — and takes two rows from it that carry an ISBN, which
   is then given the Hardcover ids a real resolution would have issued. The resolution
   itself is PER-43's evidence; what this needs from it is only the pair of ids, which is
   the interface between the two tickets.
2. Pumps real-shaped `reader.session` events through the relay's own ingest and derivation,
   so the chain exercised is the deployed one, then runs delivery against the Hardcover
   double in `tests/service_fixtures.py` — a real HTTP server speaking the shapes
   Hardcover's own guide documents.
3. Writes one artifact per acceptance criterion, each naming what it measured.

**What this evidence is and is not.** Every state change happens against the double, not
against Hardcover, because no Hardcover token exists in the environment this was built in.
It therefore proves the relay's side of every criterion — the exact mutation documents, the
page arithmetic, the throttle, the absence of journal writes, the ledger states — and proves
nothing about Hardcover accepting any of it. The live halves are the numbered steps in
`runbook.md`.
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

from service_fixtures import HardcoverServer, object_literal  # noqa: E402

from pagewire.hardcover import HARDCOVER_ENDPOINT, HardcoverClient  # noqa: E402
from pagewire.hardcover_delivery import (  # noqa: E402
    DESTINATION,
    FINISH_DESTINATION,
    deliver_hardcover,
)
from pagewire.sessions import derive_and_store  # noqa: E402
from pagewire.storage import BookRecord, Storage  # noqa: E402

# The ids a real ISBN resolution issues, one pair per book, and the edition page counts the
# mapping needs. Two distinct pairs on purpose: sharing one would make the second book's
# delivery collide with the first's read.
BOOK_ID = 328491
EDITION_ID = 990011
PAGES = 448
BOOK_ID_B = 329491
EDITION_ID_B = 991011
PAGES_B = 320


def _ids(index: int) -> tuple[int, int, int]:
    """The (book, edition, pages) triple for the index-th swept book."""
    if index == 0:
        return BOOK_ID, EDITION_ID, PAGES
    return BOOK_ID_B, EDITION_ID_B, PAGES_B

TIMEZONE = "America/New_York"
START = 1_760_000_000  # 2025-10-09T08:53:20Z
NOW = START + 4 * 86_400
DEFAULT_WINDOW = 30 * 60


def _rows_from_sweep() -> list[dict[str, str]]:
    """The swept books that carry an ISBN — the only ones identity can resolve."""
    source = HERE.parent / "per-42-sweep" / "books-rows.tsv"
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return [row for row in rows if row["isbn"] and row["title"]]


# -- seeding -----------------------------------------------------------------


def _seed(storage, document, *, offset, seconds, bp0, bp1, end_percent):
    """One firmware session, in the shape the device sends."""
    start = START + offset
    end = start + seconds
    storage.store_event(
        {
            # The device's ids are a per-boot nonce plus a counter; including the start
            # time keeps two runs of this script from colliding.
            "id": f"{document[:8]}-{start}-{offset}",
            "event": "reader.session",
            "document": document,
            "book": f"/books/{document}.epub",
            "start_time": str(start),
            "end_time": str(end),
            "duration_seconds": str(seconds),
            "ts": str(end),
            "progress_scale": "10000",
            "start_progress_bp": str(bp0),
            "end_progress_bp": str(bp1),
            "end_percent": str(end_percent),
        },
        received_at=end,
    )


def _database(rows, *, window=DEFAULT_WINDOW):
    """A relay database holding those swept books, identified for Hardcover.

    Readwise is deliberately left unset: the two destinations are independent, so a
    Hardcover delivery must work for a book Readwise knows nothing about.
    """
    storage = Storage(":memory:")
    storage.upsert_books(
        [
            BookRecord(
                document=row["document"],
                hash_method=row["hash_method"] or "content",
                book_path=row["book_path"],
                title=row["title"],
                author=row["author"],
                isbn=row["isbn"],
            )
            for row in rows
        ]
    )
    for index, row in enumerate(rows):
        book_id, edition_id, _ = _ids(index)
        storage.set_book_identity(
            row["document"],
            hardcover_id=str(book_id),
            hardcover_edition_id=str(edition_id),
            status="auto",
        )
    return storage, window


def _catalog(rows):
    books = []
    for index, row in enumerate(rows):
        book_id, edition_id, pages = _ids(index)
        books.append(
            {
                "id": book_id,
                "title": row["title"],
                "editions": [{"id": edition_id, "isbn_13": row["isbn"], "pages": pages}],
            }
        )
    return books


def _library(*, finished=None, progress=None):
    """Both books' library entries, each with a read in progress.

    `finished` / `progress` are lists indexed by book, so the two books can be put in
    deliberately different states without one shadowing the other.
    """
    entries = {}
    for index in range(2):
        book_id, edition_id, _ = _ids(index)
        entries[7001 + index] = {
            "id": 7001 + index,
            "book_id": book_id,
            "edition_id": edition_id,
            "status_id": 2,
            "user_book_reads": [
                {
                    "id": 90001 + index,
                    "started_at": "2025-10-01",
                    "finished_at": (finished or [None, None])[index],
                    "edition_id": edition_id,
                    "progress_pages": (progress or [0, 0])[index],
                }
            ],
        }
    return entries


# -- reading the results back ------------------------------------------------


def _ledger(storage, destination=None):
    lines = []
    for row in storage.send_log(destination):
        if row["sent_at"] is not None:
            state = f"written external_id={row['external_id']}"
        elif row["error"]:
            state = f"pending error_status={row['error_status']} — {row['error']}"
        else:
            state = "claimed, outcome unknown (re-checked on the next run)"
        lines.append(f"{row['document']} @ {row['started_at']} {row['destination']}: {state}")
    return "\n".join(lines) + "\n"


def _request_log(server):
    """Every mutation the relay sent, as the service received it."""
    lines = []
    for query, variables in zip(server.queries, server.variables):
        if "update_user_book_read(" in query:
            resolved = object_literal(query, variables)
            lines.append(
                f"update_user_book_read  read={variables['id']}"
                f"  progress_pages={resolved.get('progress_pages')}"
                f"  action_at={resolved.get('action_at')}"
            )
        elif "update_user_book(" in query:
            resolved = object_literal(query, variables)
            lines.append(
                f"update_user_book       user_book={variables['id']}"
                f"  status_id={resolved.get('status_id')}"
            )
        elif "insert_reading_journal(" in query:
            lines.append("insert_reading_journal  <-- the relay must never send this")
    if not lines:
        return "no mutation was sent\n"
    return (
        f"{len(lines)} mutation(s), in order:\n"
        + "\n".join(f"  {line}" for line in lines)
        + "\n"
    )


def _document(query):
    """The exact mutation document, for the record."""
    return query.strip()


def _describe_write(server, marker: str = "update_user_book_read("):
    """The exact mutation document and variables of the first write carrying `marker`."""
    index = next(
        (i for i, query in enumerate(server.queries) if marker in query),
        None,
    )
    if index is None:
        return f"no write matching {marker!r} was sent\n"
    return (
        f"document:\n{_document(server.queries[index])}\n\n"
        f"variables: {json.dumps(server.variables[index], sort_keys=True)}\n"
    )


def _run(storage, server, **overrides):
    overrides.setdefault("timezone", TIMEZONE)
    overrides.setdefault("throttle_seconds", 60)
    client = HardcoverClient("hc-token", endpoint=f"{server.address}/v1/graphql")
    report = deliver_hardcover(storage, client, log=lambda line: print(f"  {line}"), **overrides)
    return report


def main() -> int:  # noqa: C901 - a flat series of scenarios, each writing its own artifact
    rows = _rows_from_sweep()
    first, second = rows[0], rows[1]
    print(f"book A: {first['title']!r} (isbn {first['isbn']})")
    print(f"book B: {second['title']!r} (isbn {second['isbn']})\n")

    # -- criterion 1: a sitting lands as progress on the read in progress ------
    print("criterion 1 — a sitting becomes progress_pages")
    with HardcoverServer(catalog=_catalog(rows), library=_library()) as server:
        storage, _ = _database(rows)
        # Two firmware sessions twenty minutes apart: the stitcher makes one sitting, so
        # the page arithmetic has exactly one sitting to work from.
        _seed(storage, first["document"], offset=0, seconds=1800, bp0=4120, bp1=4470, end_percent=45)
        _seed(
            storage,
            first["document"],
            offset=3000,
            seconds=1800,
            bp0=4470,
            bp1=4470,
            end_percent=45,
        )
        sittings = derive_and_store(storage, now=NOW)
        report = _run(storage, server)
        sitting = next(row for row in storage.sittings() if row["document"] == first["document"])

        (HERE / "criterion-1-readback.txt").write_text(
            f"the sitting the relay derived\n"
            f"  document:         {sitting['document']}\n"
            f"  started_at:       {sitting['started_at']}\n"
            f"  ended_at:         {sitting['ended_at']}"
            f" (a span of {(sitting['ended_at'] - sitting['started_at']) // 60} minutes,\n"
            f"                    of which {sitting['paused_seconds'] // 60} were the gap"
            " between the two firmware sessions)\n"
            f"  active_seconds:   {sitting['active_seconds']}\n"
            f"  end_progress_bp:  {sitting['end_progress_bp']} of 10000\n"
            f"  finalised:        {bool(sitting['finalised'])}\n"
            f"\nthe edition it was mapped through\n"
            f"  hardcover book={BOOK_ID} edition={EDITION_ID} pages={PAGES}\n"
            f"\nthe arithmetic\n"
            f"  round({PAGES} x {sitting['end_progress_bp']} / 10000) ="
            f" {round(PAGES * sitting['end_progress_bp'] / 10000)}\n"
            f"\nwhat Hardcover holds afterwards\n"
            f"  read {server.reads(7001)[0]['id']}:"
            f" progress_pages={server.reads(7001)[0]['progress_pages']}"
            f"  action_at={server.reads(7001)[0].get('action_at')}\n"
            f"\nwhat the relay sent\n{_describe_write(server)}"
            f"\ndelivered: {report.written}, coalesced: {report.coalesced}\n"
        )
        (HERE / "request-log.txt").write_text(_request_log(server))
        (HERE / "send-log.txt").write_text(_ledger(storage))

        # -- idempotence: a second run writes nothing -------------------------
        written_at = storage.delivery_row(
            first["document"], sitting["started_at"], DESTINATION
        )["sent_at"]
        before = len(server.progress_writes)
        second_run = _run(storage, server, now=lambda: written_at + 61)
        (HERE / "idempotence.txt").write_text(
            f"progress writes after run one: {before}\n"
            f"progress writes after run two: {len(server.progress_writes)}\n"
            f"run two delivered: {second_run.written}\n"
            f"run two requests for this book: 0\n"
            f"\nsend log:\n{_ledger(storage)}\n"
        )
        print(f"  derived {len(sittings)} sitting(s); page {server.reads(7001)[0]['progress_pages']}")

    # -- criterion 2: finishing, and not finishing ---------------------------
    print("\ncriterion 2 — 100 % finishes the book, 99 % does not")
    with HardcoverServer(catalog=_catalog(rows), library=_library()) as server:
        storage, _ = _database(rows)
        _seed(
            storage, first["document"], offset=0, seconds=1800, bp0=9700, bp1=10_000, end_percent=100
        )
        _seed(storage, second["document"], offset=0, seconds=1800, bp0=9800, bp1=9900, end_percent=99)
        derive_and_store(storage, now=NOW)
        report = _run(storage, server)
        read = server.reads(7001)[0]
        (HERE / "criterion-2-readback.txt").write_text(
            f"the book the device reported finished (end_percent 100)\n"
            f"  page written:      {server.reads(7001)[0]['progress_pages']} of {PAGES}\n"
            f"  entry status_id:   {server.library[7001]['status_id']} (3 = Read)\n"
            f"  read finished_at:  {read['finished_at']}\n"
            f"  the relay also sent:\n    "
            + "\n    ".join(
                line
                for line in _request_log(server).splitlines()
                if "update_user_book " in line
            )
            + "\n"
            f"\nreport.finished: {report.finished}\n"
            f"\nnotes: the status change addresses the library entry, not the read — the"
            " guide is explicit that the server keeps the two in step, and that progress"
            " reaching the edition's last page finishes the read on its own.\n"
            f"a 99 % sitting is a different book; see criterion-2-99-percent.txt\n"
        )
        (HERE / "criterion-2-99-percent.txt").write_text(
            "the book that merely got to 99 %\n"
            f"  page written:      {server.reads(7002)[0]['progress_pages']} of {PAGES_B}"
            f" (round({PAGES_B} x 9900 / 10000))\n"
            f"  entry status_id:   {server.library[7002]['status_id']} (2 = Currently"
            " Reading; nothing moved it off that)\n"
            f"  read finished_at:  {server.reads(7002)[0]['finished_at']}\n"
            f"  status changes naming its entry: "
            f"{sum(1 for v in server.variables if v.get('id') == 7002)}\n"
            f"\nbooks the relay reported finished: {report.finished}\n"
            f"\nfull request log:\n{_request_log(server)}\n"
        )
        print(f"  finished: {report.finished}; status changes: "
              f"{sum(1 for q in server.queries if 'update_user_book(' in q)}")

    # -- criterion 3: no journal entry, ever ---------------------------------
    print("\ncriterion 3 — the relay writes no journal entry")
    with HardcoverServer(catalog=_catalog(rows), library=_library()) as server:
        storage, _ = _database(rows)
        _seed(
            storage, first["document"], offset=0, seconds=1800, bp0=9700, bp1=10_000, end_percent=100
        )
        derive_and_store(storage, now=NOW)
        before = len(server.journals)
        _run(storage, server)
        (HERE / "criterion-3-journals.txt").write_text(
            f"reading_journals rows before the delivery: {before}\n"
            f"reading_journals rows after the delivery:  {len(server.journals)}\n"
            f"journal mutations the relay sent:          "
            f"{sum(1 for q in server.queries if 'insert_reading_journal' in q)}\n"
            f"\nprogress writes that did land: "
            f"{len(server.progress_writes)}\n"
            f"status changes that did land:  "
            f"{sum(1 for q in server.queries if 'update_user_book(' in q)}\n"
            f"\nnotes: the double is able to record a journal row — `insert_reading_journal`"
            " is implemented in it and answers with an id — so its emptiness is evidence"
            " rather than a property of the fixture. The guide: \"starting, progress and"
            " finishing each get a journal entry; don't write those entries yourself, or"
            " they'll appear twice\".\n"
        )
        print(f"  journals: {before} -> {len(server.journals)}")

    # -- criterion 4: two sittings inside a minute, one write ----------------
    print("\ncriterion 4 — the once-a-minute throttle")
    with HardcoverServer(catalog=_catalog(rows), library=_library()) as server:
        storage, _ = _database(rows)
        # The stitch window is narrowed to 20s so that two sessions thirty seconds apart
        # are two sittings rather than one — the state a "once a minute" throttle exists
        # for. Everything else about the derivation is the default.
        _seed(storage, first["document"], offset=0, seconds=180, bp0=4120, bp1=4470, end_percent=45)
        _seed(storage, first["document"], offset=210, seconds=180, bp0=4470, bp1=5200, end_percent=52)
        derive_and_store(storage, now=NOW, stitch_window_seconds=20)
        derived = sorted(row["started_at"] for row in storage.sittings())
        report = _run(storage, server)
        writes_after_first = len(server.progress_writes)
        page_one = server.progress_writes[0]["object"]["progress_pages"]
        local_rows = len(storage.sittings())
        written_at = storage.delivery_row(first["document"], derived[0], DESTINATION)["sent_at"]

        # A third sitting arrives after that write. This is the state the throttle is
        # actually for: a book whose progress has just been written and whose next sitting
        # has come due, which is why a throttle test alone would prove nothing.
        _seed(storage, first["document"], offset=420, seconds=180, bp0=5200, bp1=6100, end_percent=61)
        derive_and_store(storage, now=NOW, stitch_window_seconds=20)
        armed = sorted(row["started_at"] for row in storage.sittings())
        held = _run(storage, server, now=lambda: written_at + 30)
        writes_after_held = len(server.progress_writes)
        allowed = _run(storage, server, now=lambda: written_at + 61)
        (HERE / "criterion-4-throttle.txt").write_text(
            f"the two sittings the relay derived first\n"
            f"  started_at:      {armed[:2]} — {armed[1] - armed[0]}s apart\n"
            f"  the gap between them (end of the first to the start of the second):"
            f" {armed[1] - (armed[0] + 180)}s, inside a minute\n"
            f"  end_progress_bp: 4470 then 5200\n"
            f"\nrun one, both sittings pending\n"
            f"  progress writes: {writes_after_first}\n"
            f"  written:         {report.written}\n"
            f"  superseded:      {report.coalesced}"
            f" (the further sitting's page is the one that stands)\n"
            f"  page sent:       {page_one}"
            f" (round({PAGES} x 5200 / 10000)), the 4470 sitting's 200 was superseded\n"
            f"  derived rows still in the local record: {local_rows}"
            f" {derived}\n"
            f"\nrun two, 30s after that write, with a third sitting now due\n"
            f"  progress writes: {writes_after_held}\n"
            f"  books held back: {held.throttled}\n"
            f"  written:         {held.written}\n"
            f"  the third sitting:"
            f" {'still pending, nothing dropped' if held.written == [] else 'written'}\n"
            f"\nrun three, 61s after the write — past the window\n"
            f"  progress writes: {len(server.progress_writes)}\n"
            f"  written:         {allowed.written}\n"
            f"  books held back: {allowed.throttled}\n"
            f"  page sent:       {server.progress_writes[-1]['object']['progress_pages']}"
            f" (round({PAGES} x 6100 / 10000))\n"
            f"\nrequest log, in order:\n{_request_log(server)}\n"
            f"\nnotes: the throttle reads the ledger's own record of when a write landed, so a\n"
            f"run that failed to write does not hold the next one back. It is per book, and it\n"
            f"defers rather than drops — the held sitting keeps its derived row and is written\n"
            f"on the run that follows the window.\n"
        )
        print(f"  wrote {report.written}, held {held.throttled}, then {allowed.written}")

    # -- criterion 5: a scope refusal is visible -----------------------------
    print("\ncriterion 5 — a token without write scope")
    with HardcoverServer(catalog=_catalog(rows), library=_library(), write_scope=False) as server:
        storage, _ = _database(rows)
        _seed(storage, first["document"], offset=0, seconds=1800, bp0=4120, bp1=4470, end_percent=45)
        derive_store = derive_and_store(storage, now=NOW)
        report = _run(storage, server)
        (HERE / "criterion-5-scope.txt").write_text(
            f"sittings derived: {len(derive_store)}\n"
            f"progress written: {report.written}\n"
            f"the run stopped:  {report.aborted}\n"
            f"\nthe error the relay reported, with the service's own words in it:\n"
            f"  {report.failed.get((first['document'], derive_store[0].started_at))}\n"
            f"\nthe response body that produced it, as the double sends it:\n"
            f'  {{"error": "insufficient_scope", "error_description": "token is missing a'
            f' scope this request needs", "scope": "write:library"}}\n'
            f"\nledger after the refusal (nothing may be marked written):\n{_ledger(storage)}\n"
        )
        print(f"  aborted with: {report.aborted}")

    # -- criterion 6: switched off -------------------------------------------
    print("\ncriterion 6 — the destination, switched off")
    with HardcoverServer(catalog=_catalog(rows), library=_library()) as server:
        storage, _ = _database(rows)
        _seed(storage, first["document"], offset=0, seconds=1800, bp0=4120, bp1=4470, end_percent=45)
        derive_and_store(storage, now=NOW)
        report = deliver_hardcover(storage, None, timezone=TIMEZONE)
        (HERE / "criterion-6-switched-off.txt").write_text(
            f"PAGEWIRE_HARDCOVER_ENABLED is the switch; with it off the relay builds no"
            f" client at all.\n\n"
            f"report.enabled:            {report.enabled}\n"
            f"requests received by the service: {len(server.requests)}\n"
            f"queries the service saw:   {len(server.queries)}\n"
            f"progress writes:           {len(server.progress_writes)}\n"
            f"send-log rows:             {len(storage.send_log())}\n"
            f"written:                   {report.written}\n"
        )
        print(f"  requests received: {len(server.requests)}")

    print("\nwrote:")
    for path in sorted(HERE.iterdir()):
        if path.name not in {"README.md", "runbook.md", "reproduce.py"}:
            print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
