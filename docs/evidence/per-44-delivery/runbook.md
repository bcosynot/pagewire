# PER-44 runbook — the steps that need a real Readwise account

Each step says **which acceptance criterion it closes** and is marked **NOT YET RUN**.
Nothing below has been executed: this environment has no Readwise token, so every state
change in `reproduce.py` happened against the double in `tests/service_fixtures.py`.

Run the steps in order. Steps 1–2 are also the outstanding token-gated steps from PER-43's
runbook (`docs/evidence/per-43-identity/runbook.md`), so running them closes those too.

---

## 1. Put a real token in place — NOT YET RUN

Closes: the precondition for criteria 1, 2, 3 and 5. Closes PER-43 criterion 1's live half.

Mint a personal access token at <https://readwise.io/books_api> (the page shows the token
once). It must be a **personal access token**, sent as `Authorization: Token …`; an OAuth
token would need `write` and would use `Bearer`.

On the deployed box, the token belongs in the relay's state file, not in the flake:

```sh
install -m 600 /dev/null /var/app/data/pagewire/readwise-token
printf '%s' 'TOKEN_HERE' > /var/app/data/pagewire/readwise-token
# The unit reads that file into the environment the relay already understands:
#   EnvironmentFile=/var/app/data/pagewire/readwise.env
# containing PAGEWIRE_READWISE_ENABLED=1 and PAGEWIRE_READWISE_TOKEN=… .
# The relay itself takes PAGEWIRE_READWISE_TOKEN and nothing else; wiring the file into
# the unit is PER-46's job.
```

Locally, for a one-off run:

```sh
export PAGEWIRE_READWISE_ENABLED=1
export PAGEWIRE_READWISE_TOKEN='TOKEN_HERE'
```

Watch for: a 401 (`unauthenticated`) means the token is wrong; a 403
(`insufficient_scope`) means it cannot write. Both surface per row and per sitting, and
neither is retried past the auth abort.

## 2. Identify one book for real — NOT YET RUN

Closes: the precondition for 3–8. Closes PER-43 criteria 1 and 2's live halves.

With the reader in File Transfer mode (address from its screen — re-read it, do not reuse
the last one):

```sh
python -m pagewire.sweep --address <device-address>
python -m pagewire.identity            # adds the book and prints the ids it got
```

Watch for: the printed `readwise <id>` for that book. Confirm it is a number; that number
is what `PAGEWIRE_…` never has to know, because it lives on the `books` row.

## 3. Inspect the bodies before writing anything — NOT YET RUN

Closes: nothing. This is the gate that makes step 4 safe.

```sh
python -m pagewire.delivery --dry-run
```

Watch for: `items` batches of at most 10; `format: "ebook"`; a `percent_at_start` /
`percent_at_end` pair between 0 and 1; `publish_to_feed: false`; and the device object
`{"type": "E-Reader", "vendor": "Xteink", "model": "X4"}`. Any book refused for having no
positions is named there, before any request is made.

## 4. Deliver — NOT YET RUN

Closes: nothing on its own; it produces the state steps 5–8 read back.

```sh
python -m pagewire.delivery
python -m pagewire.delivery --show-log
```

Watch for: `N sent, 0 already present, 0 failed, M finished`. The exit code is 1 if
anything failed. `--show-log` must show `delivered external_id=<session id>` for each
sitting; a row saying `claimed, outcome unknown` means the process died mid-call and the
next run will reconcile it (step 6 exercises that path indirectly).

## 5. Read the sessions back — NOT YET RUN

Closes: **criterion 1** (start, end, positions, pause) and **criterion 3**
(`publish_to_feed` false).

```sh
curl -s -H "Authorization: Token $PAGEWIRE_READWISE_TOKEN" \
  "https://readwise.io/books/api/v1/sessions?book_ids=<readwise book id>&limit=10"
```

Watch for, per session: `started_at` and `ended_at` equal to the ledger's sitting,
`paused_seconds` equal to what `derived_sessions` holds, a `format` of `ebook`, and the
percent pair matching the device's basis points over 10000. Record the `id`s.

For criterion 3: every one of those sessions must be absent from the feed
(<https://readwise.io/books>), and no `publish_to_feed: true` may appear in step 3's bodies.

Paste the readback into `criterion-1-and-3-readback.txt` beside this file and commit it.

## 6. Deliver the same sittings twice — NOT YET RUN

Closes: **criterion 2's live half** (the offline half is `idempotence.txt`).

```sh
python -m pagewire.delivery      # again, immediately
python -m pagewire.delivery --show-log
```

Watch for: `0 sent`, and the session count from step 5 unchanged. Exactly one session per
sitting, which is the whole point of the ledger: the API has no idempotency key and a
repeat records a second session.

To exercise the crash path deliberately, kill the process with `SIGKILL` during step 4,
then re-run: the next run must report `already present` (reconciled) and must not add a
session. Append both counts to `idempotence-live.txt`.

## 7. Finish a book, and leave one unfinished — NOT YET RUN

Closes: **criterion 5**.

Read the two books back:

```sh
curl -s -H "Authorization: Token $PAGEWIRE_READWISE_TOKEN" \
  "https://readwise.io/books/api/v1/books?ids=<book id>"
```

Watch for: the book whose sitting reached `end_percent` 100 has `status: "finished"` and
`finished_reading_at` equal to that sitting's `ended_at`; the book that only reached 99 is
still `currently_reading` with `finished_reading_at: null`. Paste both readbacks into
`criterion-5-book-readbacks.txt`.

## 8. Switch the destination off — NOT YET RUN

Closes: **criterion 6's live half** (the offline half is `switched-off.txt`).

```sh
PAGEWIRE_READWISE_ENABLED=0 python -m pagewire.delivery
```

Watch for: `readwise is switched off — nothing is delivered`, no outbound request, and no
new ledger rows. With the service's own request log to hand, confirm zero entries for the
window.

## 9. Watch a bad token be refused — NOT YET RUN

Closes: **criterion 4's live half**. The retry sequence itself is proven offline in
`retry-log.txt` (500 → 503 → 200, three attempts, two sessions).

```sh
PAGEWIRE_READWISE_TOKEN='deliberately-wrong' python -m pagewire.delivery
python -m pagewire.delivery --show-log
```

Watch for: one attempt, `stopped: Readwise refused the call: HTTP 401 unauthenticated`, and
every ledger row still `pending` with the 401 recorded. Nothing delivered, nothing lost.
Restore the real token afterwards and re-run: the sittings go out then.

---

## What remains unverifiable even after all nine steps

The 24-hour cap and the "no in-progress session" rule are enforced by Readwise, not by the
relay; the relay sidesteps both (it splits at the cap and only sends finalised sittings), so
a refusal there would show up as a per-sitting error rather than a silent loss. The
`paused_seconds` arithmetic assumes Readwise reads active time as
`ended_at − started_at − paused_seconds` — the API documents exactly that sentence, which
upgrades the design's UNVERIFIED flag to documented, but no live session has yet been
checked against it.
