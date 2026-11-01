# PER-44 — Readwise session delivery: evidence

Everything here is regenerated from the repository alone:

```sh
.venv/bin/python docs/evidence/per-44-delivery/reproduce.py
```

It rebuilds a `books` table from the committed sweep rows
(`docs/evidence/per-42-sweep/books-rows.tsv`), pumps real-shaped `reader.session` events
through ingest → derivation → delivery, and runs delivery against the Readwise double in
`tests/service_fixtures.py` — a real HTTP server speaking the shapes
`https://readwise.io/books_api` documents. No device, no scratch directory, no account.

## What each file is

| File | What it records |
|---|---|
| `request-bodies.json` | The exact `POST /sessions` items and the `PATCH /books` body the relay produced, verbatim as the server received them. |
| `send-log.txt` | The ledger after a successful run: what Readwise issued, per sitting, and the book-level finish as its own destination. |
| `idempotence.txt` | Session count at the service after run one and run two, and the number of requests the second run made. |
| `retry-log.txt` | A run against a server answering 500 then 503: the status of each attempt, the sessions that ended up recorded, and the final ledger. |
| `gate.txt` | A book left `unmatched` while still carrying a `readwise_id`: its sittings in the deliverable set, the requests naming it, and its ledger rows. |
| `switched-off.txt` | A run with the destination switched off: requests received, sessions recorded, ledger rows. |
| `runbook.md` | The numbered steps only the owner can run, because they need a real Readwise token. |

## What it measures

- **Two readings, two sessions, one request.** Both sittings of one book go in a single
  `POST` (the API takes up to 10), each carrying `book_id`, `started_at`, `ended_at`,
  `format: ebook`, the percent pair derived from the device's basis points
  (4120 → 0.412, 9999 → 0.999…, 10000 → 1.0), `paused_seconds`, `timezone` and
  `device: {type: E-Reader, vendor: Xteink, model: X4}`.
- **`publish_to_feed` is `false` on every item**, explicitly, not by omission.
- **A second run records nothing**: 2 sessions before, 2 after, 0 requests.
- **A 500 and a 503 are survived**: attempts answer 500 → 503 → 200, with a reconciliation
  `GET /sessions` before each retry, and exactly 2 sessions exist afterwards. The ledger
  shows both delivered with their session ids.
- **A book at 100 % is finished, once**, with `finished_reading_at` equal to the finishing
  sitting's `ended_at`, after its session was recorded; the finish is its own ledger row so
  a re-run cannot PATCH twice.
- **An unidentified book is invisible to delivery** even when its row still carries a
  `readwise_id` from an earlier identification: 0 sittings in the deliverable set,
  0 requests, 0 ledger rows.
- **Switched off means no request**: 0 requests received, 0 sessions, 0 ledger rows.

The suite at this commit is **134 passed** (103 before this ticket), including the 12
deliberate breakages listed in the ticket's completion comment — every one of which the
suite caught.

## What this does NOT prove

**Nothing here has been sent to Readwise.** No token exists in the environment this was
built in, so every state change above happens against the double. The relay's half of every
criterion is pinned; Readwise's acceptance of it is not. Specifically still unproven until
`runbook.md` is run:

- that a real account accepts the session body (the double enforces the documented
  constraints, but a documented constraint is not the same as the deployed service);
- that a `session_id` issued by a real account is what `GET /sessions` returns for it
  (criterion 1);
- that a real account keeps `publish_to_feed: false` (criterion 3);
- that a real book moves to `finished` with the date on `PATCH /books` (criterion 5);
- that `GET /sessions?book_ids=…&started_after=…` returns sessions in the shape
  reconciliation parses — the crash-safety path depends on it.

Criterion 4's evidence is genuinely offline (the retry log *is* the relay's log), and
criterion 6's is a zero-request count, so both are closed by this directory. Criteria 1,
2's live half, 3 and 5 are not.
