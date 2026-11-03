# PER-45 — Hardcover progress delivery: evidence

Everything here is regenerated from the repository alone:

```sh
.venv/bin/python docs/evidence/per-45-delivery/reproduce.py
```

It rebuilds a `books` table from the committed sweep rows
(`docs/evidence/per-42-sweep/books-rows.tsv`), gives two of those books the Hardcover
`book_id`/`edition_id` pair a real ISBN resolution would have issued (that resolution is
PER-43's evidence, not repeated here), pumps real-shaped `reader.session` events through the
relay's own ingest → derivation → delivery, and runs delivery against the Hardcover double
in `tests/service_fixtures.py` — a real HTTP server speaking the shapes Hardcover's own
guide, "Updating a Book's Progress and Dates Read" (read 2026-11-01), documents. No device,
no scratch directory, no account.

## What each file is

| File | What it records |
|---|---|
| `criterion-1-readback.txt` | The derived sitting and the edition it was mapped through, the arithmetic, the `update_user_book_read` document the relay sent verbatim, and what the read holds afterwards. |
| `criterion-2-readback.txt` | The book the device finished: page written, entry `status_id`, the read's `finished_at`, and the status mutation. |
| `criterion-2-99-percent.txt` | The book that merely reached 99 %: page written, entry untouched, and no status change naming it. |
| `criterion-3-journals.txt` | `reading_journals` rows before and after a delivery that wrote both progress and a status. |
| `criterion-4-throttle.txt` | Three runs: two sittings inside a minute, a third arriving afterwards, the window holding it, and the write that follows the window. |
| `criterion-5-scope.txt` | The 403 `insufficient_scope` a write-scope-less token produces, the message the relay surfaced, and the ledger afterwards. |
| `criterion-6-switched-off.txt` | A run with the destination switched off: requests received, queries seen, ledger rows. |
| `request-log.txt` | The mutation log from the happy path, in order. |
| `send-log.txt` | The ledger after the happy path, both destinations. |
| `idempotence.txt` | A second run a minute later: what it wrote, and what it did not. |
| `runbook.md` | The numbered steps only the owner can run, because they need a real Hardcover token. |

## What it measures

- **The page is the edition's, computed from the device's basis points.** A sitting whose
  `end_progress_bp` is 4470, on an edition of 448 pages, writes `progress_pages: 200`
  (`round(448 × 4470 ÷ 10000)`), and `action_at` is the sitting's own `ended_at` rendered
  with the reader's UTC offset (`2025-10-09T06:13:20-04:00`) — an instant, as the guide
  writes it, not a date.
- **The lookup costs one top-level field.** `me` carries the entry, its status, the read in
  progress and that read's edition's `pages`, so Hardcover's 10-field burst limit is spent
  once per book.
- **100 % finishes the book, 99 % does not.** At 10 000 basis points the page equals the
  edition's 448 and the entry moves to `status_id: 3`; at 9900 the page is 317 of 320 and
  nothing moves the entry off Currently Reading (2).
- **The relay writes no journal entry.** `journals` goes 0 → 0 across a delivery that landed
  both a progress write and a status change. The double implements `insert_reading_journal`
  and would record a row, so the emptiness is evidence rather than a property of the fixture.
- **Two sittings inside a minute produce one write.** Both are recorded against the same
  write — `written` has two entries, `coalesced` one — and only the further sitting's page
  (233, not the 4470 sitting's 200) is sent. Both derived rows stay in the local table.
- **The throttle defers, and is per book.** A third sitting arriving after that write is held
  when the next run happens 30 s later — *held*, not dropped: it keeps its derived row and
  its ledger row is never written — and is sent on the run 61 s after the last write (page
  273). The throttle reads the ledger's record of when a write actually landed, so a failed
  run does not hold the next one back.
- **A second run a minute later writes nothing** (`idempotence.txt`).
- **A 403 `insufficient_scope` stops the run and is visible**: the message names the status,
  the service's own words and the scope it wanted (`[needs scope write:library]`), and no
  sitting is marked written.
- **Switched off means no request**: 0 requests received, 0 queries, 0 ledger rows.

The suite at this commit is **166 passed** (134 before this ticket), and 14 deliberate
one-line breakages of the guards in this code were each caught by a named test — see the
ticket's completion comment for the list and what the two survivors that were *not* caught
at first turned out to be.

## What this does NOT prove

**Nothing here has been sent to Hardcover.** No token exists in the environment this was
built in, so every state change above happens against the double. The relay's half of every
criterion is pinned; Hardcover's acceptance of it is not. Specifically still unproven until
`runbook.md` is run:

- that a real account accepts the `update_user_book_read` document at all — **and in
  particular that `action_at` takes a `String` variable holding an offset-bearing timestamp**.
  The guide writes the field with exactly such a literal, and that is the whole basis for it;
  the input type's name is undocumented, which is why the object is an inline literal rather
  than a typed variable. If the scalar refuses it, the fix is one word in
  `UPDATE_READ_PROGRESS` and the runbook's first step is what says so.
- that a real read in progress is the one the relay's query returns (criterion 1's readback);
- that a real edition's `pages` produces the page the relay computed (criterion 1);
- that progress reaching the edition's last page really does finish the read and mark the
  book Read server-side, and that the explicit `status_id: 3` write is accepted (criterion 2);
- that a real account records **no** `reading_journals` row for the relay's writes — the
  double's silence is the relay's behaviour, not Hardcover's (criterion 3);
- that a real token without `write:library` answers the 403 shape the double and the docs
  both describe (criterion 5).

Criterion 4's request log and criterion 6's zero-request count are genuinely offline, so both
are closed by this directory. Criteria 1, 2, 3 and 5 are not.

**Update 2026-11-02:** criterion 1 is **partly** covered live — see below. Criteria 2, 3 and 5
are still not. The open risk named above about `action_at` is now answered, in both directions.

## Live readback (2026-11-02)

`criterion-1-live-hardcover-readback.txt` is the live half of criterion 1: a real sitting was
delivered to a real Hardcover account, and the account's own readback is the evidence.

- **Criterion 1 — page half closed, `action_at` half not readable.** Book 770867 / edition
  30772692, entry 20311675, in-progress read **7498150** with `progress_pages: 100` on an
  edition of **416** pages = 24.04 %, against the device's own 24.15 % (0.46 of a page off).
  So the percent → page mapping through a real edition is confirmed. A **second sitting** then
  updated the same read to `progress_pages: 113` = 27.16 % against the device's 27.19 %
  (0.13 of a page), and **no second read was created** — progress accumulates on one read.
  - The `action_at` worry in the section above is **resolved: the mutation was accepted** and
    the page landed. But `action_at` **cannot be read back** — introspecting the live schema,
    `user_book_reads` exposes only `started_at`, `finished_at` and `paused_at` (all `date`);
    there is no `action_at` field. The criterion's `action_at` half is therefore left open as
    *unverifiable by readback* rather than called met.
- **Criterion 2 — still open.** No book has been finished through the relay on a real account.
- **Criterion 3 — still open, and not currently verifiable.** The token carries no
  `read:journal` scope, so `reading_journals` answers
  `{"error":"insufficient_scope","error_description":"Missing scopes: read:journal"}`.
- **Criterion 5 — still open.** No 403 has been produced by a real token.
