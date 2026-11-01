# PER-43 evidence — identity resolution and the review page

What was captured, and what it does and does not prove.

**The honest boundary first.** No Readwise Books token and no Hardcover token exist in the
environment this was built in, so nothing here touched either live service. Every service
interaction below is against a local HTTP server that speaks the documented shapes
(`tests/service_fixtures.py`, the same doubles the test suite uses). So this evidence
proves what the relay *does* — the calls it makes, the states it writes, the page it
serves — and proves nothing about either vendor's live behaviour. The parts of criteria 1,
2 and 5 that need a real token are listed as owner-gated at the bottom.

## Files

| File | Criterion | What it shows |
|---|---|---|
| `run-resolve.log` | 1, 2, 3 | `python -m pagewire.identity` over all 100 swept books: 15 matched (every book carrying an ISBN), 85 held for confirmation, 0 failed. |
| `criterion-1-and-2-readback.txt` | 1, 2 | One row read back out of the database with `readwise_id`, `hardcover_id` and `hardcover_edition_id` written, beside the HTTP calls that produced them — Readwise's `GET /books?isbns=`/`POST /books` and Hardcover's one `editions(where: {isbn_13: …})` query plus the library-entry lookup. |
| `criterion-3-before-after.txt` | 3 | A book with no ISBN, read back before and after one confirmation: `status` moves `unmatched` → `confirmed` and all three ids are written. |
| `review-queue.png` | 3, 5 | The page itself, against the real swept rows: paths, titles, statuses, the per-row confirm form, and the problem column. |
| `review-candidates.png` | 3 | Hardcover's exact-title match offered as a one-click confirmation, so confirming a book does not require a trip to hardcover.com. |
| `review-queue-after-confirm.png` | 3 | The same queue one book shorter. |
| `queue-after-confirm.txt` | 3, 4 | The counts either side: 84 left waiting, 15 `auto`, 1 `confirmed`, 0 with a recorded failure. |
| `criterion-5-refused-calls.log` | 5 | The whole run against a Hardcover token with no `write:library`: 0 matched, 15 rows each carrying `HTTP 403 insufficient_scope … [needs scope write:library]`, and every one of them still `unmatched`. Nothing is skipped silently and nothing is half matched. |
| `reproduce.py` | — | Regenerates every file above from the repository alone. |

## How to regenerate

```sh
.venv/bin/python docs/evidence/per-43-identity/reproduce.py
```

It rebuilds the books table from `docs/evidence/per-42-sweep/books-rows.tsv` — the
committed record of a real sweep of the card — so neither the device nor a scratch
directory is needed. `run-books.db` is that rebuilt database and is not committed.

## The numbers

- The card's 100 books split 15 with an ISBN and 85 without, exactly as PER-42 found. The
  ISBN-carrying 15 all resolve with no human involved; the other 85 are the queue.
- One confirmation moves one book: 85 waiting → 84, `confirmed` 0 → 1, and both services'
  ids written in a single press.
- With the scope removed from the Hardcover token, the same run records 15 failures and
  matches nothing — which is the intended shape. A refusal is never read as an absence,
  because reading "403" as "not in the catalog" would silently enqueue every book in the
  library and look exactly like a library where nothing matched.

## Owner-gated: what a real token would add

These are the claims this evidence cannot make, and they are why PER-43 goes to In Review
rather than Done:

1. **That Readwise's live `GET /books?isbns=` and `POST /books` behave as documented** for
   these particular ISBNs, and that the `book_id` returned is the one `POST /sessions` will
   later accept. The shapes are taken from the vendor's own Books API page and are exercised
   against a double; the live round trip has not run.
2. **That Hardcover's live `editions(where: {isbn_13: …})` returns `book_id`** for these
   editions, and that `insert_user_book` accepts the object this code sends — including
   `privacy_setting_id`, which is read from `me.account_privacy_setting_id` in the same
   request. The mutation text is modelled on autoCalibre-Web's working client
   (`cps/services/hardcover.py`) rather than on a live call from here.
3. **That a real under-scoped token produces the 403** the double produces. The shape and
   the scope name are from Hardcover's documented response codes.

Everything else — the state machine, the gate, the page, the per-row error handling — is
covered by the test suite and does not depend on the vendors.

## A note on what is deliberately not built

The design's matching order is "ISBN, then title+author search, then a human". The
title+author *search* is not implemented on either side, for reasons recorded in the
ticket's completion comment: Readwise's Books API has no title search at all (`GET /books`
filters by `ids`, `product_ids`, `isbns`, `asins`, `status` only), and Hardcover's
`search(query:, query_type: "Book")` answers in a Typesense document (`ids`, `results`)
whose shape cannot be verified without a token. The page therefore offers Hardcover's
documented exact-title match (`books(where: {title: {_eq: …}})`) as candidates, and an id
field for anything else.
