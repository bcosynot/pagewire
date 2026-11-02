# PER-45 runbook — the steps that need a real Hardcover account

Each step says **which acceptance criterion it closes** and is marked **NOT YET RUN**.
Nothing below has been executed: this environment has no Hardcover token, so every state
change in `reproduce.py` happened against the double in `tests/service_fixtures.py`.

Run the steps in order. Steps 1–2 are the same token-gated opening as PER-44's runbook
(`docs/evidence/per-44-delivery/runbook.md`); if that one has already been run, the ids are
already on the `books` rows and step 2 is a read rather than a write.

The shapes below come from Hardcover's own guide,
<https://docs.hardcover.app/api/guides/updatingabooksprogress/> ("Updating a Book's Progress
and Dates Read", last updated 2026-10-23) and from the "Read in Progress" query in the same
guide. That page is where `action_at`, the read-in-progress lookup and the "don't write the
journal entries yourself" rule all come from.

---

## 1. Put a real token in place — NOT YET RUN

Closes: the precondition for criteria 1, 2, 3 and 5.

Mint an API token at <https://hardcover.app/account/api> (the page shows it once). It needs
**`write:library`** for anything below to land, and `read:library` to look the read up.

On the deployed box, the token belongs in the relay's state file, not in the flake:

```sh
install -m 600 /dev/null /var/app/data/pagewire/hardcover-token
printf '%s' 'TOKEN_HERE' > /var/app/data/pagewire/hardcover-token
# The unit reads that file into the environment the relay already understands:
#   EnvironmentFile=/var/app/data/pagewire/hardcover.env
# containing PAGEWIRE_HARDCOVER_ENABLED=1 and PAGEWIRE_HARDCOVER_TOKEN=… .
# Wiring the file into the unit is PER-46's job.
```

Locally, for a one-off run:

```sh
export PAGEWIRE_HARDCOVER_ENABLED=1
export PAGEWIRE_HARDCOVER_TOKEN='TOKEN_HERE'
```

Watch for: `Hardcover refused the call: HTTP 401 invalid_token` means the token is wrong;
`HTTP 403 insufficient_scope … [needs scope write:library]` means it cannot write. Either
surfaces on every row and aborts the run rather than sleeping once per book.

## 2. Identify one book for real — NOT YET RUN

Closes: the precondition for 3–10.

With the reader in File Transfer mode (address from its screen — re-read it, do not reuse
the last one):

```sh
python -m pagewire.sweep --address <device-address>
python -m pagewire.identity            # adds the book on both services, prints the ids
```

Watch for: `hardcover <book id> / <edition id>` in the printed line for that book. Both ids
live on the `books` row; nothing in the configuration ever needs to know them.

## 3. Check the two things the page mapping needs — NOT YET RUN

Closes: the precondition for criterion 1's live half. This is a read-only check, and it is
worth doing before writing anything, because both of these are `unmatched`-style refusals
rather than errors:

```sh
curl -s -X POST https://api.hardcover.app/v1/graphql \
  -H "Authorization: Bearer $PAGEWIRE_HARDCOVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { me { user_books(where: {book_id: {_eq: <book id>}}, limit: 1) { id status_id user_book_reads(where: {finished_at: {_is_null: true}}, order_by: {id: desc}, limit: 1) { id progress_pages edition { id pages } } } } }"}'
```

Watch for two things, and if either is missing the relay will say so before making a write:

- a `user_book_reads` entry — the **read in progress**. Without one, the relay reports
  "hardcover has no read in progress for this book" and writes nothing. Setting the entry's
  status to Currently Reading (2) on hardcover.app starts a read, which is the fix.
- a non-zero `edition.pages`. Without one, the relay reports "records no page count" and
  refuses the sitting rather than writing page 0, which would sit in the reading record
  forever. The fix is on Hardcover's side (the edition's page count is crowd-edited).

## 4. Inspect before writing anything — NOT YET RUN

Closes: nothing. This is the gate that makes step 5 safe.

```sh
python -m pagewire.hardcover_delivery --dry-run
```

Watch for: one line per pending sitting, each showing its `end=…/10000` and the
`hardcover book=` / `edition=` pair the write would be addressed to, plus a line for any book
whose Read status still needs writing. The page itself is not computed here — that needs the
edition's count, which only Hardcover has — so this shows the inputs, not the output.

## 5. Deliver — NOT YET RUN

Closes: nothing on its own; it produces the state steps 6–10 read back.

```sh
python -m pagewire.delivery --dry-run    # both destinations, bodies and queue
python -m pagewire.delivery              # both destinations
python -m pagewire.delivery --show-log
```

Watch for a `hardcover:` block reading `N write(s) for M sitting(s), K book(s) throttled,
J finished`, and for `--show-log` to show `written external_id=<read id>` per sitting under
destination `hardcover` and `written external_id=read` under `hardcover:finished`. A row
saying `claimed, outcome unknown` means the process died mid-call; a progress write is
idempotent, so the next run simply sends the same page again.

Run the same thing again immediately: it must make **no** Hardcover request (the throttle
window, and the ledger — criterion 4's live half).

## 6. Read the progress back — NOT YET RUN

Closes: **criterion 1**.

```sh
curl -s -X POST https://api.hardcover.app/v1/graphql \
  -H "Authorization: Bearer $PAGEWIRE_HARDCOVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { me { user_books(where: {book_id: {_eq: <book id>}}, limit: 1) { id status_id user_book_reads(order_by: {id: desc}, limit: 1) { id progress_pages started_at finished_at edition { pages } } } } }"}'
```

Watch for, on the read the relay wrote to:

- `progress_pages` equal to `round(edition.pages × end_progress_bp ÷ 10000)` for the sitting
  the relay reported — **and equal to the page number in step 4's dry run of a real run**, not
  just to what the relay printed. Compute the expected page by hand from those two numbers
  and compare; that is the whole of the mapping.
- the read's `edition.pages` matching the edition id on the `books` row.
- the journal entry Hardcover wrote for it (see step 8): its date should be the sitting's own
  end, in the reader's zone, not the moment the relay ran — that is what `action_at` is for.

**`action_at` is the one shape here with no independent confirmation.** The guide passes it
as `action_at: "2026-09-24T21:00:00-06:00"`, and the relay sends the same form as a `String`
variable (`UPDATE_READ_PROGRESS` in `src/pagewire/hardcover.py`). If the service rejects the
variable's type, the failure is a GraphQL `errors` array on the write and the relay reports it
per sitting; the fix is the scalar name in that one document, and nothing else changes.

Paste the readback, with the page and its arithmetic shown together, into
`criterion-1-readback-live.txt` beside this file and commit it.

## 7. Finish a book, and leave one unfinished — NOT YET RUN

Closes: **criterion 2**.

Read the same query from step 6 for both books.

Watch for: the book whose last sitting reached `end_percent` 100 has an entry `status_id` of
3 (Read) and its read carries a `finished_at`; the book that only reached 99 is still on
Currently Reading (2) with `finished_at: null`, and no `update_user_book` naming its entry
appears in the run's output. Note that the guide says the server finishes the read itself when
progress reaches the edition's last page — so if `status_id` is already 3 without the relay's
second write having happened, that is the documented behaviour and not a missing write; the
request log in step 5's output is what says which of the two did it.

Paste both readbacks into `criterion-2-book-readbacks.txt`.

## 8. Count the journal rows — NOT YET RUN

Closes: **criterion 3**.

The relay must never write one of these; the server writes the start/progress/finish entries
itself, and a client that writes one makes it appear twice. Count them for the book before and
after a delivery:

```sh
curl -s -X POST https://api.hardcover.app/v1/graphql \
  -H "Authorization: Bearer $PAGEWIRE_HARDCOVER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { reading_journals(where: {book_id: {_eq: <book id>}}) { id event action_at } }"}'
```

Watch for: the count rising by exactly the number of **server-written** entries for the
progress the relay sent — typically one per progress write and one for the finish — and no
duplicate pairs at the same `action_at` with the same `event`. A doubling for one delivery is
this criterion failing. `grep -c insert_reading_journal` over the relay's own source must be
0, which is the offline half.

Paste both counts into `criterion-3-journal-counts.txt`.

## 9. Watch the throttle on real sittings — NOT YET RUN

Closes: **criterion 4's live half**. The offline half is `criterion-4-throttle.txt`.

Do this on a day with two real sittings in the same book inside a minute (or by reading,
sleeping, and reading again within the minute). Then:

```sh
python -m pagewire.hardcover_delivery
python -m pagewire.delivery --show-log
```

Watch for: one `update_user_book_read` per book per run, carrying the **further** sitting's
page; the earlier sitting recorded against the same write rather than sent separately; and
`K book(s) throttled` naming the book when a run lands inside the window. Both sittings must
still be present in the derived table:

```sh
python -m pagewire.delivery --show-log       # both rows, whatever the destination got
sqlite3 state/pagewire.db 'select started_at, end_progress_bp from derived_sessions order by started_at'
```

The throttle reads the ledger's record of when a write actually landed, so a run that failed
to write must not hold the next one back — verify by pointing the relay at a wrong token once
(step 10), then restoring it: the very next run must write rather than report the book as
throttled.

## 10. Watch a scope refusal be surfaced — NOT YET RUN

Closes: **criterion 5**.

Use a token minted without `write:library` (Hardcover lets you choose scopes at mint time):

```sh
PAGEWIRE_HARDCOVER_TOKEN='<read-only token>' python -m pagewire.hardcover_delivery
python -m pagewire.delivery --show-log
```

Watch for: one attempt, `stopped: Hardcover refused the call: HTTP 403 insufficient_scope
(token is missing a scope this request needs) [needs scope write:library]`, and every ledger
row still pending with the 403 recorded — nothing written, nothing lost, and no repeated
attempt for each remaining book. Restore the real token and re-run: the sittings go out then.

## 11. Switch the destination off — NOT YET RUN

Closes: **criterion 6's live half** (the offline half is `criterion-6-switched-off.txt`).

```sh
PAGEWIRE_HARDCOVER_ENABLED=0 python -m pagewire.delivery --dry-run
PAGEWIRE_HARDCOVER_ENABLED=0 python -m pagewire.delivery
```

Watch for: `hardcover: off`, no line claiming a write, and no new `hardcover*` ledger rows.
With Readwise still enabled in the same run, Readwise must still be delivered — the two
destinations are independent, and switching one off must not stop the other.

---

## What remains unverifiable even after all eleven steps

Hardcover's Free-plan rate limits (5 000/day, 60/minute) and the 10-top-level-field burst cap
are enforced by Hardcover, not by the relay. The relay spends one top-level field per book per
run and throttles progress writes to one a minute per book, so a breach would show up as a 403
`insufficient_scope`-shaped refusal on a burst — which is why step 10's wording matters when
reading a failure there.

Two things about the read's own dates are the server's business and this ticket deliberately
does not touch them: the read's `started_at` (set when the status first went to Currently
Reading) and its `finished_at` (set by the server when progress reaches the end, or by the
status change). The relay sends neither, so neither can be checked from the relay's side —
only observed.
