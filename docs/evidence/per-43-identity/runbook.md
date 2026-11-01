# PER-43 runbook — the parts that need real credentials

Everything the relay can prove on its own is proven and in this directory. What is left
needs a Readwise Books API token and a Hardcover personal access token, which existed
nowhere in the build environment. Five steps, each read-only or reversible, none of which
touch a book you are still reading.

The order matters: confirm the tokens can *read* before letting anything *write*, because
the first failure you want is a refusal rather than a half-written library.

## 1. Mint the two tokens — NOT YET RUN

- **Readwise Books.** Readwise Books must be on the account, then mint a personal access
  token at <https://readwise.io/access_token>. The relay sends it as
  `Authorization: Token …` — a *personal access token*, not the OAuth token the MCP uses.
- **Hardcover.** <https://hardcover.app/account/api> → New API Key. Scopes matter: reading
  needs `read:catalog` and `read:library`; creating a library entry needs `write:library`.
  A token minted before August 2026 has `all`, which is more than this needs.

Watch for: a token with the wrong scopes is the expected first mistake, and step 3 is where
you will see it — as a per-row `403 insufficient_scope`, which is the point.

## 2. Point the relay at them — NOT YET RUN

```sh
export PAGEWIRE_READWISE_ENABLED=true
export PAGEWIRE_READWISE_TOKEN=...
export PAGEWIRE_HARDCOVER_ENABLED=true
export PAGEWIRE_HARDCOVER_TOKEN=...
export PAGEWIRE_DB=/path/to/state/pagewire.db     # the database the sweep wrote
```

Nothing is written by this step.

## 3. Resolve, and read what happened — NOT YET RUN

```sh
python -m pagewire.identity --list     # the queue as it stands, offline
python -m pagewire.identity            # match every book that carries an ISBN
```

Expected on the current card: **15 matched, 85 left for confirmation, 0 failed.** This is
criterion 1 and 2's readback — it is the live confirmation the evidence directory cannot
make.

Watch for, and what each means:

- `insufficient_scope` on every row → the Hardcover token's scopes. Re-mint with
  `write:library` (step 1) and re-run. **Nothing was half written**: a row that failed
  carries its error and stays `unmatched`.
- `Hardcover has no edition with ISBN …` on a specific row → Hardcover's catalog has no
  edition carrying the ISBN the book's OPF declares. That book belongs in the review page,
  not on the retry pile.

```sh
python -m pagewire.identity --list     # confirm the 15 moved and nothing else did
```

Read back the decisive one:

```sh
sqlite3 "$PAGEWIRE_DB" \
  "SELECT book_path, status, readwise_id, hardcover_id, hardcover_edition_id
     FROM books WHERE status IN ('auto','confirmed') LIMIT 5;"
```

Then check one of those against each service directly — the readwise id at
`https://readwise.io/books/api/v1/books?ids=<id>` (the Books API exposes no per-book web
URL), and the hardcover pair in the app. **This is the step that closes criteria 1 and 2**,
and it is deliberately a read: it cannot change anything.

## 4. Confirm one row in the page — NOT YET RUN

```sh
uvicorn pagewire.app:create_app --factory --host 0.0.0.0 --port 8799
```

Open `http://<this-host>:8799/review?token=<PAGEWIRE_TOKEN>` — the same shared token the
device sends. Expect 85 rows. On one with no ISBN: press **look up the title on Hardcover**,
then the candidate's button.

Expected: the row moves from `unmatched` to `confirmed` with both ids written, and **the
queue drops to 84.** This is criterion 3's live confirmation.

If the candidate lookup finds nothing: paste the ids from hardcover.app into the row's
form instead. A confirming press is refused, with the reason shown, until every enabled
destination has an id — that refusal is the "no half match" rule, not a bug.

## 5. The end-to-end check belongs to PER-47 — NOT YET RUN

Once PER-44 and PER-45 exist, a confirmed book should show deliveries and an unmatched one
should show none. That is criterion 4's send-log half, and it needs the delivery tickets;
`deliverable_books()` is the gate this ticket contributes to it.

## What would prove this wrong

- A row that resolves here but whose `readwise_id` is rejected by `POST /sessions` in
  PER-44 — the id would not be the one sessions address.
- `insert_user_book` refusing the object (for example over `privacy_setting_id`, read from
  `me.account_privacy_setting_id` in the same request rather than a call of its own).
  Both are modelled on autoCalibre-Web's working client rather than a live call from here.
- The ISBNs in the swept rows not matching real editions, which would put real books in the
  queue — visible in step 3's counts, and the reason those counts are the acceptance test.
