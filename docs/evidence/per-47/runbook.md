# PER-47 runbook — end-to-end on the device

**Owner input required — every step below needs Vivek's hands on the reader.**

**Status at handover: NOT YET RUN — every numbered step (0–8) and every gate step (A1,
A2, B) carries that mark. Nothing that touches the reader or either destination has been
executed.** The four *readback* commands are tools rather than steps, and each one *was*
executed on 2026-11-02 while this file was written — they are marked as verified so that
the steps that depend on them are not hypotheses.

**Status update, later on 2026-11-02 — read this before running.** A1 and A2 have since been
done: both tokens are installed and both destinations report `true`. And criteria **2 and 3
are already met**, by a real sitting that drained from the device on its own — see
`docs/evidence/per-47/README.md` and the `criterion-2`/`criterion-3` files beside it. That
does **not** close the ticket: criteria **4, 5 and 6 are unrun** and still need the reader in
hand, so steps 6, 7 and 8 remain the work. The original handover status above is kept as
written.

This is the only proof that matters for this project: a real sitting, read on the X4 with
nothing pressed, landing in Readwise, and progress landing in Hardcover. It closes the
epic's original complaint. It is written as a runbook for the owner to execute, not as
agent work.

| criterion | what it proves | step | evidence file to write |
|---|---|---|---|
| 1 | this runbook exists, every step unrun | — (this file) | `docs/evidence/per-47/runbook.md` |
| 2 | one sitting in Readwise, with the device's own positions and duration | 3, 4 | `criterion-2-readwise-session.txt` |
| 3 | Hardcover's progress matches the device's percent through the edition | 5 | `criterion-3-hardcover-readback.txt` |
| 4 | the offline case still lands | 6 | `criterion-4-offline.txt` |
| 5 | the book-switch case gives two sittings in A | 7 | `criterion-5-book-switch.txt` |
| 6 | nothing was pressed except sleep | 8 | `criterion-6-nothing-pressed.txt` |

---

## Gate A — the two destinations are OFF, and criteria 2 and 3 cannot close without them

**This is the correction to PER-46's closing note.** That note said the one thing left
unproven was "that the **device** itself can reach the listener". Read against the live
box, the device is not the only gap: **nothing can reach Readwise or Hardcover at all
while both destinations are off**, and both are off.

Measured on the box, 2026-11-02:

```
$ curl -s http://192.168.0.112:8799/healthz
{"ok":true,"version":"0.0.1","events":2,"destinations":{"readwise":false,"hardcover":false}}
$ grep -E '^(PAGEWIRE_READWISE_ENABLED|PAGEWIRE_HARDCOVER_ENABLED)=' \
    /var/app/data/pagewire/pagewire.env
PAGEWIRE_READWISE_ENABLED=false
PAGEWIRE_HARDCOVER_ENABLED=false
```

So a device run started today would prove criterion 4's queueing and criterion 5's
stitching, and would fail criteria 2 and 3 for a reason that has nothing to do with the
reader. Do A1 and A2 first.

**Resolved later on 2026-11-02 — A1 and A2 are DONE; do not re-run them.** Both tokens were
validated against their own service, written into `/var/app/data/pagewire/pagewire.env`
(mode 600, `pagewire:pagewire`; the previous files are kept as
`pagewire.env.pre-per47-readwise` and `pagewire.env.pre-per47-hardcover`), and both switches
are `true`:

```
$ curl -s http://192.168.0.112:8799/healthz
{"ok":true,"version":"0.0.1","events":6,"destinations":{"readwise":true,"hardcover":true}}
```

The measurement above is kept as what the box showed before them. Skip to Gate B and step 0.
These two steps also closed more than they were written for: PER-44's criterion 1 and PER-45's
criterion 1 have since been exercised for real — see `docs/evidence/per-44-delivery/` and
`docs/evidence/per-45-delivery/`.

### A1. Obtain the two tokens — NOT YET RUN

*Closes: the precondition for criteria 2 and 3 — and, as a side effect, most of PER-44
and PER-45.*

- **Readwise** personal access token, with the `write:library` scope. A missing scope
  comes back as a 403 `insufficient_scope` and is reported in the book's `error` column,
  not swallowed.
- **Hardcover** token, with `read:library` **and** `write:library`.

Watch for: neither token ever being written into `flake.nix` or any committed file. They
go only into the mode-600 env file below. The config repo's `AGENTS.md` forbids new
plaintext secrets in the flake.

### A2. Flip the two switches and restart — NOT YET RUN

*Closes: the live half of PER-44's criterion 1, and PER-45's criterion 1.*

```sh
# on the box, via: ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < script.sh
export PATH=/run/current-system/sw/bin:/run/wrappers/bin:$PATH
cp /var/app/data/pagewire/pagewire.env /var/app/data/pagewire/pagewire.env.pre-per47
sed -i \
  -e 's|^PAGEWIRE_READWISE_ENABLED=.*|PAGEWIRE_READWISE_ENABLED=true|' \
  -e 's|^PAGEWIRE_READWISE_TOKEN=.*|PAGEWIRE_READWISE_TOKEN=<the Readwise token>|' \
  -e 's|^PAGEWIRE_HARDCOVER_ENABLED=.*|PAGEWIRE_HARDCOVER_ENABLED=true|' \
  -e 's|^PAGEWIRE_HARDCOVER_TOKEN=.*|PAGEWIRE_HARDCOVER_TOKEN=<the Hardcover token>|' \
  /var/app/data/pagewire/pagewire.env
ls -l /var/app/data/pagewire/pagewire.env      # want -rw------- pagewire pagewire
systemctl restart pagewire.service
```

Watch for: `/healthz` reporting `"readwise":true,"hardcover":true`. If it still reports
false, the restart did not pick the file up — check with
`systemctl show pagewire.service -p EnvironmentFiles`.

**Note what else these two steps close.** PER-44 and PER-45 are both parked `In Review`
precisely because no real account has ever been spoken to:

- PER-44 criteria 1, 2's live half, 3 and 5.
- PER-45 criteria 1, 2, 3 and 5.

Each is one request away once A2 is done. Re-read both tickets after the run and close
the criteria that were actually exercised, with the readbacks pasted in — do not close
them on the strength of "the token is now set".

---

## Gate B — the reader and the card

*Preconditions for every reading step below.* NOT YET RUN.

- **Charge the reader to ≥20%.** The sleep-entry WiFi bring-up requires it; below that the
  device will not join, will not drain, and the sitting waits for the next drain.
- **The plugin must be on the card** at `/plugins/pagewire/`, containing `manifest.json`,
  `device.json`, `README.md`, `token.json` and `config.json`.
  - `make sd DEST=<card root>` writes `config.json` from **the machine running it**, which
    is not the relay. Its `server` value must be the relay:
    `{"server": "http://192.168.0.112:8799"}`. Fix it by hand after staging — a card
    pointed at a laptop address is the likeliest way this run fails for a non-device
    reason.
  - `token.json` must match `PAGEWIRE_TOKEN` on the box. They agree today: the sha256 of
    the value inside `spike/plugin/token.json` and the sha256 of `PAGEWIRE_TOKEN` in the
    box's env file are both `86eeb5b1fb4eeb7ddf9c2703616d7ec1e6624ab7650bb62d7acede59510bf3c5`.
    **Re-check this if either side is ever re-minted** — a mismatch is a 401 at the relay,
    which fails criteria 2 and 3 silently as far as the reader is concerned.
- **Settings → Connectivity → Plugins must be enabled.** It is disabled by default on new
  installs, and with it off no handler fires at all.

---

## The readbacks — verified working on 2026-11-02

These are tools, not steps. Every one of them was executed while writing this file.

**Box access.** `pct exec` has no PATH of its own, and quoting does not survive the
ssh → `pct` hop, so pipe a script to `/bin/sh` on stdin — that is the only form that
works:

```sh
ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < script.sh
# first line of any script:
#   export PATH=/run/current-system/sw/bin:/run/wrappers/bin:$PATH
```

**R1 — the relay answers at all**, from any LAN machine, plain HTTP:

```sh
curl -s http://192.168.0.112:8799/healthz
# {"ok":true,"version":"0.0.1","events":N,"destinations":{"readwise":...,"hardcover":...}}
```

This is the same listener the device posts to. It is not TLS and must not be: the
reader's web client cannot present a trusted certificate and must answer inside the
10-second sleep-entry window.

**R2 — the relay's own rows.** `sqlite3` is not installed on the box, but the store's
Python is, and `python3` is not on the exec PATH. Both are handled here:

```sh
export PATH=/run/current-system/sw/bin:/run/wrappers/bin:$PATH
PY=$(ls -d /nix/store/*-python3-3.13*/bin/python3 | head -1)
"$PY" - <<'PY'
import sqlite3
c = sqlite3.connect('file:/var/app/data/pagewire/pagewire.db?mode=ro', uri=True)
print("--- raw_events ---")
for r in c.execute("select id,event,book,document,ts_raw from raw_events order by received_at"):
    print(r)
print("--- derived_sessions ---")
for r in c.execute("""select id,document,started_at,ended_at,active_seconds,paused_seconds,
                      start_progress_bp,end_progress_bp,finalised,finished
                      from derived_sessions order by started_at"""):
    print(r)
print("--- books ---")
for r in c.execute("select document,title,author,isbn,readwise_id,hardcover_id,status,error from books"):
    print(r)
print("--- send_log ---")
for r in c.execute("select * from send_log"):
    print(r)
PY
```

Read the raw event to get the device's own numbers, and the derived row to see what the
relay made of them. `started_at`/`ended_at` are Unix epochs. `start_progress_bp` and
`end_progress_bp` are basis points, so divide by 10000 for a fraction.

**R3 — Readwise's sessions for one book.** The relay's own reconciliation read, so it is
the readable half of `POST /sessions`:

```sh
curl -s -H "Authorization: Token <the Readwise token>" \
  "https://readwise.io/books/api/v1/sessions?book_ids=<readwise_id>&started_after=<iso-8601>"
```

`readwise_id` for the book comes from the `books` table in R2. `started_after` keeps the
answer to just this run. Readwise exposes no per-book web URL on this API, so the session
object itself is the evidence — the app's book page is a useful second view.

**R4 — Hardcover's read in progress.** The same query the relay uses; run it against
`https://api.hardcover.app/v1/graphql` with `Authorization: Bearer <the Hardcover token>`:

```graphql
query ReadInProgress($bookId: Int!, $editionId: Int!) {
  user_books(where: {book_id: {_eq: $bookId}, edition_id: {_eq: $editionId}}, limit: 1) {
    id
    user_book_reads(where: {finished_at: {_is_null: true}}, order_by: {id: desc}, limit: 1) {
      id
      progress_pages
      edition { pages }
    }
  }
}
```

Hardcover stores **no percent** — the percentage is `progress_pages ÷ edition.pages`.
`bookId` and `editionId` are the `hardcover_id` and `hardcover_edition_id` columns.

---

## 0. Baseline — before touching the reader — NOT YET RUN

*Closes: nothing. It is what makes a new sitting identifiable.*

There are already two sittings in the database, and they are **not readings**: they are
the synthetic events PER-46's deploy check posted (`deploy-check-1`, `deploy-check-2`).

```
raw_events: 6   (deploy-check-1, deploy-check-2 — synthetic; 823b7018-1 and
                 fdfeeadb-1..3 — the device's own Jinn-Bot drain on 2026-11-01)
derived_sessions: 3   (the two synthetic ones, plus the Jinn-Bot sitting)
books: 100   (the metadata sweep has run; ~30 auto-matched, the rest held for confirmation)
```

**Updated 2026-11-02.** The baseline originally read `raw_events: 2`, `derived_sessions: 2`
(ids 62, 63), `books: 0`. All three have moved: the device has since drained a real sitting and
the sweep has keyed 100 books, so **this is no longer a clean baseline** — do not mistake the
Jinn-Bot rows for new evidence of your run. Record the counts you see and diff against them
after your reading.

Note also that `derived_sessions.id` is **not stable**: every sweep re-derives, and the same
sitting has been observed as id 973 and, a minute later, id 976. Identify a sitting by its
`document` and `started_at`, never by row id.

Run R1 and R2 and write down the counts and the ids. After the run, the new
`raw_events` row and the new `derived_sessions` row are the sitting. Do not treat the
two existing rows as evidence of anything.

## 1. Ask the device for its build — NOT YET RUN

*Closes: nothing directly. It establishes the provenance criterion 2's session claims.*

With the reader in **File Transfer or Calibre Wireless mode** — entered by hand, and the
only mode in which its web server runs — read its address off the screen (**it is DHCP;
re-read it, never reuse the last one**) and:

```sh
curl -s http://<device-ip>/api/status
```

Watch for: `version 2.0.0`, `firmwareFamily crosspoint`, `pluginApi 1`, device `X4`. The
spike found the installed build already reports `2.0.0` with the plugin system present, so
this should agree. **Ask the device rather than trusting a version recorded in prose.**

## 2. The sitting — NOT YET RUN

*Produces the raw material for criteria 2 and 3.*

1. Read a book on the X4 for at least a couple of minutes, turning pages forward.
2. Put it down.
3. **Press sleep.** This is the only press in the whole run.
4. Wait out the stitch window plus one sweep: **the window is 1800s and the sweep runs
   every 60s, so wait ~31 minutes** before expecting anything final. The device may drain
   earlier — it also drains whenever it joins WiFi — but the relay will not finalise a
   sitting until no new constituent event has arrived for 1800s. This is deliberate:
   Readwise sessions cannot be amended after the fact, so nothing is sent while it may
   still be open.

Watch for: a `reader.session` row appearing in R2 within a minute or two of the drain,
long before the 31 minutes. That is expected and is the point — prompt *delivery* is
decoupled from *finalisation*.

## 3. Read the relay's derived row — NOT YET RUN

*Closes: criterion 2's relay half.*

Run R2. The derived row for this sitting should read:

| field | expected |
|---|---|
| `started_at` | the sitting's first `start_time` — a real Unix epoch, not 0 |
| `ended_at` | the last event's `ts` **if** the trailing gap (`ts − end_time`) is under 1800s; otherwise the last `end_time` |
| `active_seconds` | the sum of the constituent events' `duration_seconds` |
| `paused_seconds` | the trailing gap (when it is under the window) **plus** any gaps between stitched events |
| `start_progress_bp` | the first event's `start_progress_bp`, 0..10000 |

**If `started_at` is 0 or absent, stop and read this.** The spike's working hypothesis,
UNVERIFIED, is that the firmware suppresses `reader.session` entirely until its clock has
been trusted — which needs the RTC or one SNTP sync on a WiFi join, and had never happened
on that device. If a session arrives with no usable `start_time`, the fix is to join WiFi
once and re-run step 2 rather than to invent a `started_at`. A sitting the device refuses
to date cannot be given a date by the relay.

Also confirm the arithmetic is self-consistent: `ended_at − started_at − paused_seconds`
should equal `active_seconds` for a single un-stitched sitting (the trailing-gap rule is
what makes this hold). A single sitting with a trailing gap over the window has
`paused_seconds` = 0 and `ended_at` = the last page turn, by design.

## 4. Read the Readwise session back — NOT YET RUN

*Closes: criterion 2.*

Run R3 with the book's `readwise_id`. Watch for a session whose:

- `started_at` / `ended_at` match the relay's derived row in step 3,
- `percent_at_start` / `percent_at_end` equal the derived row's basis points ÷ 10000,
- `paused_seconds` matches,
- `device` is `{type: "E-Reader", vendor: "Xteink", model: "X4"}`.

Paste the session object and the derived row **side by side** into
`criterion-2-readwise-session.txt` — the criterion asks for exactly that comparison.

**If nothing is there after 31 minutes**, check the `error` column for the book in R2
first: a refused session records the service's own words rather than disappearing. A 403
`insufficient_scope` is a token to re-mint (A1). A book still `unmatched` is held
deliberately — confirm it in the relay's review page and it will go out on the next sweep.

## 5. Read Hardcover back — NOT YET RUN

*Closes: criterion 3.*

Run R4. Watch for `progress_pages ÷ edition.pages` matching the device's reported percent
for that book, within one page of rounding. Hardcover stores pages, never a percent, and
the relay maps them the way the KOReader plugin does.

While here, confirm **no journal entry was written**: Hardcover's server writes
start/progress/finish entries itself, and writing them from outside makes them "appear
twice". That is an anti-goal of the design, so the absence is part of the criterion.

Paste the GraphQL response into `criterion-3-hardcover-readback.txt`.

## 6. The offline case — NOT YET RUN

*Closes: criterion 4.*

1. Turn the reader's WiFi off.
2. Read for a couple of minutes.
3. **Press sleep with WiFi restored** — or, per the owner's own observation from the
   spike, let it connect to WiFi afterwards; both drain points are documented and either
   satisfies "the device queues it and drains".
4. Wait out the window again.

Watch for: the `reader.session` row in R2 and the session in R3, arriving from a sitting
that was read with no network at all. This is the anti-goal the design names — "a sitting
that happened on the device but never reaches a destination is a failure, including when
the device was offline for days".

Paste the captured request (the raw event from R2) and the resulting Readwise session into
`criterion-4-offline.txt`.

## 7. The book-switch case — NOT YET RUN

*Closes: criterion 5.*

1. Read book A.
2. Put it down, and **within 30 minutes** open book B and read it.
3. Come back to A and read again, still inside 30 minutes of leaving A.
4. Sleep, and wait out the window.

Watch for **two sittings in A and one in B**, with A's `paused_seconds` excluding the
interval spent in B. That is Vivek's own ruling, made 2026-10-31: a gap containing another
book's reading is a boundary, not a pause, no matter how short it is.

Paste the three session objects into `criterion-5-book-switch.txt`.

## 8. State that nothing was pressed — NOT YET RUN

*Closes: criterion 6.*

Write, in your own words, that the only press in this entire run was sleep for each of
steps 2, 6 and 7 — no sync button, no manual upload, no confirmation on the device. This
cannot be evidenced by a machine: the criterion asks for the statement of the person who
did it. Put it in `criterion-6-nothing-pressed.txt`.

---

## What this run still does not prove, even when all six pass

1. **The metadata sweep has never run against the real device.** `PAGEWIRE_DEVICE_ADDRESS`
   is empty on the box, so the sweep has no target. Every book keyed so far would have to
   arrive through `reader.session` alone, which carries no title or author — only the SD
   path. The sweep is what makes "auto-add on a confident match" real, because the ISBN
   comes from the EPUB's OPF. A book read in this run will land in the review page as
   `unmatched` unless a sweep has identified it first. Entering File Transfer mode with
   the address set is PER-42's deliverable; re-run it if the review queue is larger than
   expected. This is not a defect in this ticket, but it is the likeliest reason a step 4
   readback comes back empty.
2. **Neither destination has ever spoken to a real account before this run.** Steps 4 and
   5 are the first real Readwise and Hardcover calls the relay has ever made. A
   schema-level surprise in either — a changed field, a scope the token lacks — shows up
   here for the first time, and belongs to PER-44 or PER-45 rather than to PER-47.
   Report it against those tickets rather than working around it here.
3. **Readwise's arithmetic is assumed.** The design assumes a consumer reads active time
   as `ended_at − started_at − paused_seconds`. That is the most plausible reading of the
   field and it is *not documented*. If Readwise instead sums sessions without subtracting
   pauses, the pause field is decorative and the step-3 arithmetic check proves less than
   it looks like it does.

## If something fails

The relay's own record is the `error` column in R2's `books` table and `send_log`. The
device's side is its own outbox at `<plugin dir>/events.jsonl` on the SD card; delivery is
at-least-once and idempotent at the relay (keyed on `event.id`), so a re-drain cannot
double-record a raw event — but `POST /sessions` is *not* idempotent, and that is why the
`send_log` ledger exists and why nothing is sent before the window closes.
