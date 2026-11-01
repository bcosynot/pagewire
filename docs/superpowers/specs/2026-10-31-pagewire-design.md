# Pagewire — design

Date: 2026-10-31
Status: awaiting approval
Author: Garu (with Vivek Ranjan)
Owner: Vivek Ranjan

## Intent

Record what Vivek actually reads, without him doing anything. An Xteink X4 running
Crosspoint firmware reports each reading sitting to a standalone relay service,
which forwards it to Readwise Books and/or Hardcover.

The triggering complaint, in his words: "i need to remember to hit the sync progress
button" and "hardcover isn't as granular as i want in my reading."

## Why the current setup fails

Progress today reaches Hardcover only when the KOSync "sync progress" button is
pressed on the device. Forget the button and the sitting is gone, and even when it
lands, the record has no notion of a sitting — no start, no end, no duration.

Vivek described the earlier attempt as "a system to use the existing kosync system in
crosspoint by pointing it to something running on the autocaliweb lxc to then forward it
onto the hardcover api". **That path is not in the autoCalibre-Web tree**: `deployed/`
and `upstream/` `cps/kosync.py` are byte-identical, and the file contains no reference to
Hardcover. What does exist is a *Kobo* path (see "The existing path on the LXC" below).
If a forwarding component exists it lives elsewhere on that container and is not part of
`/opt/autocaliweb`. Open question for Vivek.

## Goals

1. Zero taps on the device. No button, no manual sync, no confirmation step.
2. Each sitting is recorded with an accurate start position, end position,
   duration, and pause.
3. Readwise and Hardcover are independently switchable destinations, each with its
   own credentials, so one can be backup for the other or either can be off.
4. Works with the device's WiFi off by default.
5. Books are identified by content, not by library membership — most of Vivek's
   books may never pass through the Calibre library.
6. The relay is a standalone service, deployable independently of autoCalibre-Web.

## Non-goals

- Paper and audiobook sessions. The X4 reads EPUBs; only ebook/percent sessions are in scope.
- Other reading devices. KOReader, Kobo, and phone reading are out of scope.
- Two-way sync. Reading positions are never pushed back to the device.
- Any UI or code on the device beyond a declarative manifest.
- Protected/loaned content and the DRM key path.
- Replacing or modifying autoCalibre-Web's own behaviour.
- Publishing anything publicly. Readwise feed posts stay off.

## Anti-goals

Things that would make this a failure even if it technically works:

- **Lossy.** A sitting that happened on the device but never reaches a destination is
  a failure, including when the device was offline for days.
- **Duplicate journal entries on Hardcover.** The Hardcover docs are explicit that the
  server writes start/progress/finish journal entries itself and that writing them
  from the outside makes them "appear twice".
- **Premature sessions.** A session pushed to Readwise before it is actually over
  cannot be corrected — Readwise sessions accept no position updates after the fact, only
  a caption edit. Pushing early would leave two records for one sitting.
- **Any tap required.** If Vivek has to remember anything, the project failed.
- **Silent mutation of his existing setup.** autoCalibre-Web's own KOSync progress
  handling must keep working exactly as it does now.

## Constraints (verified)

### The device (Crosspoint firmware, branch `develop`)

Source: `docs/plugin-events.md`, `docs/sd-plugins.md`, `lib/TrustedTime/TrustedTime.h`.

**Step zero: the X4 cannot do any of this on its current firmware.** The installed build
is **v1.5.0** (confirmed by Vivek; that release was published 2026-08-07). The plugin
system landed in PR #3114, merged **2026-09-30**, which is after v1.5.0 and after 1.6.5
(published 2026-09-27) — so, by release date versus merge date, the only published build
containing it is **v2.0.0rc**, published 2026-10-19, marked prerelease, target
`develop`. The first milestone of this project is therefore flashing a prerelease
firmware, and the entire device-side mechanism rests on that build. Everything quoted
below is from the `develop` branch.

- The firmware emits `reader.session`: "An active-reading summary is flushed before
  sleep/exit". Variables: `book` (SD path), `document` ("binary KOSync partial-MD5,
  exactly 32 lowercase hex characters"), `start_time` ("trusted Unix epoch at the start
  of the first counted dwell"), `end_time` ("compact active end, exactly
  `start_time + duration_seconds`"), `duration_seconds`, `start_progress_bp`,
  `end_progress_bp` (0..10000), `progress_scale` (`10000`).
- Counting rules: only successful ordinary forward page turns count; a page contributes
  once it has been visible ≥2s; one dwell is capped at 1800s; reverse turns, chapter
  skips, failed turns and shorter displays are excluded and re-anchor the timer; ≥10
  active seconds and a trustworthy positive start time are required. Idle gaps are
  removed from the duration.
- `end_time` is deliberately *not* the wall-clock time of the last interaction. The
  envelope's `{event.ts}` is the real emission time.
- `reader.exit` carries `book`, `percent`, `end_percent`; a book is finished when
  `end_percent` == `100`.
- Events are appended to a per-plugin outbox on SD (`<plugin dir>/events.jsonl`) and
  drained "whenever the device is already online… or on the way into sleep". A handler
  with `"connect": true` opts into WiFi bring-up **at sleep entry only** — popup, join
  the last-connected saved network with a **10-second deadline**, drain, sleep. Bring-up
  needs battery ≥20%. Sleep is never blocked on the network; failures retry at the next
  drain.
- Timestamps survive late delivery: `lib/TrustedTime` keeps a monotonic floor in NVS,
  restores it at boot, seeds from RTC, and SNTPs on every WiFi join; time never moves
  backwards, and `trustedNow()` returns 0 rather than a wrong time.
- **Observed on hardware (v2.0.0rc, 2026-10-31): no session summary was emitted at all.**
  Two real `reader.exit` events were delivered minutes apart, both carrying `percent`
  (16 and 2) and a `book` SD path, and no `reader.session` event followed either of them.
  Both carried a literal, unsubstituted `{event.end_percent}`, and `ts` values of 61 and
  139 — not Unix epochs, plausibly boot-relative. The docs' own emit condition for
  `reader.session` requires "a trustworthy positive start time", and `TrustedTime` gets
  one only from a battery-backed RTC or an SNTP sync on a WiFi join, which had never
  happened on this device. Working hypothesis: **sessions are suppressed until the clock
  has been set at least once**. UNVERIFIED — the next run joins WiFi once, then reads and
  sleeps, and either a full summary appears with real epoch times or the hypothesis is
  wrong. This matters beyond the spike: a `started_at` cannot be manufactured for a
  sitting the device refuses to date.
- `end_percent` did **not** substitute on `reader.exit` in this build, while `percent`
  did — consistently, across four events and three boots. The develop docs list both as
  `reader.exit` variables, so this is a divergence between the documentation and the rc
  build (or a variable populated only in some states). Finish detection must not be built
  on `end_percent` until that is resolved; `reader.session`'s `end_progress_bp` is the
  alternative once sessions flow.
- **Observed: queued events drain on a WiFi connection, not only at sleep.** Owner's
  note on the second run: "the sync happened after connecting to wifi, not on sleep".
  This matches the documented drain points (any WiFi connect, the File Transfer server
  coming up, or the way into sleep), and it is good news for the design: delivery is
  prompt and does not depend on the 10-second sleep-entry window in the common case. It
  also means **device delivery timing is decoupled from session finalisation** — the
  device may hand over a session the moment it is online, and the relay still holds it
  until the stitch window elapses, so prompt delivery cannot create a premature sitting.
  The sleep-entry drain remains the fallback for a reader that never otherwise joins.
- **`ts` is a per-boot counter, not a wall clock, and the docs are wrong about the value
  when the clock is unset.** Four captures across three boots: 61 and 139 on one boot,
  1268 on the next, 42 on the third. It rises within a boot and resets at reboot, which
  is seconds-since-boot. The docs promise `0` ("0 if the clock was never set"), and a
  small plausible-looking number is worse than a zero here, because treating `ts` as an
  epoch would write a 1970 date rather than fail loudly. **The relay must never derive a
  time from `ts`**; the only usable timestamp is `reader.session`'s `start_time`, which
  the same docs describe as requiring a trustworthy clock.
- Delivery is at-least-once, in order per plugin, stopping at the first failure. Each
  queued line has a unique `id` (`{event.id}`) for dedupe.
- Limits: outbox 4KB per plugin (dropped wholesale when full), one event line 512B,
  8 subscribed plugins, 4 handlers per drain, plugin folder name ≤23 bytes.
- A plugin is a folder under `/.crosspoint/plugins/<name>/`, `/plugins/<name>/`, or
  `/.plugins/<name>/` containing any of `manifest.json`, `plugin.js`, `device.json`,
  `README.md`. "A `device.json` may declare only `events` and omit `browse.url`; that is
  a valid background/events plugin." Discovery is gated by Settings → Connectivity →
  Plugins, **Disabled by default on new installs**.
- The firmware itself never learns what any service is: a handler is a declarative HTTP
  request template (`url`, `method`, `headers`, `body`) with `{token}`, `{cfg.KEY}`,
  `{event.NAME}`, `{meta.KEY}` substitution.
- The `.meta.json` sidecar (flat JSON next to a book, 2KB cap) is merged into the
  `metadata` object of KOSync progress uploads, with reserved keys `filename`, `title`,
  `authors` that cannot be overridden. **Those three come from the firmware**, so a
  KOSync upload carries title and author; a `reader.session` event does not.
- UNVERIFIED: whether Crosspoint uploads KOSync progress automatically (e.g. on exit)
  or only when the sync button is pressed. The design must not depend on it.

### Readwise Books API

Source: https://readwise.io/books_api.

- `POST /books/api/v1/sessions` records up to 10 finished sessions: `book_id`,
  `started_at`, `ended_at`, `format`, plus the position pair for that format —
  `percent_at_start`/`percent_at_end` (0–1) for `ebook` — plus `paused_seconds`,
  `timezone`, `device` (`model`/`vendor`/`type`, type may be `E-Reader`), and
  `publish_to_feed` (default false).
- No in-progress session exists. Sessions are retroactive and immutable except for a
  caption. `ended_at` may be at most 24 hours after `started_at`.
- Books are matched/added via `product` identity (`id`, `isbn`, `asin`, or
  `title`+`author`); `GET /books` filters by `ids`, `product_ids`, `isbns`, `asins`.
- `POST /books` adds a book with a `status` shelf; `PATCH /books` updates shelf, dates,
  rating, review, `manual_total_pages`.
- Readwise's own "progress" on a book is a single fraction; for `paper` sessions
  progress is page ÷ `manual_total_pages`.

### Hardcover API

Source: https://docs.hardcover.app/ (site reorganized; `/api/` and `/sitemap.xml` 404).
Endpoint `https://api.hardcover.app/v1/graphql`. Docs marked beta, "Last Updated
Oct 22, 2026".

- Progress lives on a *read*: `user_book_reads` (a per-read-through row under a
  `user_books` library entry), fields `id`, `started_at`, `finished_at`,
  `finished_at_precision`, `progress_pages`, `progress_seconds`, `edition_id`.
- Progress mutation: `update_user_book_read(id: 123, object: {progress_pages: 142,
  action_at: "..."})`. Audiobooks use `progress_seconds`. There is **no stored percent**
  — "To show a percentage, work it out from the edition's `pages` or `audio_seconds`".
- Status mutation: `update_user_book(id, object: {status_id})`; 1=Want to Read,
  2=Currently Reading, 3=Read, 4=Paused, 5=Did Not Finish, 6=Ignored. Setting 2 starts
  a read (a new one on a re-read); 3 finishes it. "Progress that reaches the end of the
  edition finishes the read and marks the book Read".
- The server writes the start/progress/finish journal entries itself — "Don't write
  those entries yourself, or they'll appear twice." The only journal entries a client
  may write are notes and quotes.
- **There is no per-sitting session concept on Hardcover.** Verified as an absence
  across the documented schemas; introspection would need a token.
- ISBN lookup: `editions(where: {isbn_13: {_eq: "..."}})`; editions carry `pages`,
  `audio_seconds`, `isbn_10/13`, `edition_format`, `reading_format_id`. Library entries
  key on `book_id` + `edition_id`. Catalog search: `search(query:, query_type: "Book")`.
- Scopes: progress and journal writes need `write:library`; reads need `read:library`
  / `read:journal`. Rate limits per plan: Free 5,000/day, 60/min.
- Prior art: the Hardcover KOReader plugin (Billiam/hardcoverapp.koplugin, in
  Hardcover's own showcase) maps device pages onto edition pages with
  `round((document_page ÷ document_total_pages) × edition_pages)` and throttles pushes
  to once a minute. It links books by ISBN, by Calibre identifiers `hardcover` /
  `hardcover-edition`, or by title+author search.
- No Crosspoint/Xteink → Hardcover plugin exists (GitHub repo search, 2026-10-31).

### The existing path on the LXC

Source: `autocaliweb/upstream/cps/kosync.py`, `cps/services/hardcover.py`.

- autoCalibre-Web already implements a KOSync *server*: `PUT /kosync/syncs/progress`
  reading `document`, `progress`, `percentage`, `device`, `device_id`; it ignores any
  other field, including `metadata`.
- It also already contains a Hardcover GraphQL client: ISBN-13 → `book_id` +
  `edition_id`, `insert_user_book`, `insert_user_book_read(progress_pages)`,
  `update_reading_progress(identifiers, progress_percent)`, and journal entry
  read/write, driven per-user from a `hardcover_token`.
- **But nothing wires KOSync to that client.** Verified 2026-10-31: `deployed/cps/kosync.py`
  is identical to `upstream/cps/kosync.py`, and `grep -c hardcover` on it returns 0.
  The only progress writer to Hardcover in the tree is the Kobo path:
  `push_reading_state_to_hardcover(book, request_bookmark)` in `cps/kobo.py`, called from
  `handle_reading_state`, gated on `config_hardcover_sync` and using
  `hardcoverClient.update_reading_progress(book.identifiers, request_bookmark["ProgressPercent"])`.
  Annotations sync separately under `config_hardcover_annosync` in `cps/readingservices.py`.
  Deployed release: `ACW_RELEASE` v0.12.12.
- Consequence: **any writer that stays enabled will fight the relay** — last-write-wins
  on `progress_pages`, repeated Read / Currently-Reading status flips, and the duplicate
  journal entries this design exists to avoid. **Asked and answered by Vivek 2026-10-31:**
  pressing sync is the only thing sending progress to Hardcover today, and no separate
  forwarding script exists on the container — the earlier attempt never landed as running
  code. So there is nothing to disable or coordinate with, provided the Kobo path
  (`config_hardcover_sync`) and annotation sync (`config_hardcover_annosync`) stay off.

### Prior art and neighbouring work (verified 2026-10-31)

Two upstream projects were found while writing this, and both were read before the plan
was committed to. Pagewire stays separate from either — that is Vivek's call, made
explicitly — but what overlaps is recorded here so nobody rebuilds it by accident.

**`crosspoint-reader/crosspoint-sync`** — a KOSync-compatible server plus a
service-connector hub, Node + SQLite, one Docker image, from the same org as the
firmware. Relevant parts:

- A KOSync server the device points at by changing its sync URL, and an extended
  `/api/v1` it says is "designed for the ESP32", carrying the full CrossPoint position
  (spine, paragraph, anchor, page hints) rather than a lossy percentage.
- `PUT /api/v1/stats/sessions` accepts measured reading-session checkpoints —
  `session_id`, `document`, `started_at`, `ended_at`, `seconds`, `utc_offset_minutes` —
  with stable ids so later checkpoints update the same session, active seconds ≥10, and
  a span cap of seven days. That is close to this design's derived sitting, without the
  pause semantics or the book-switch rule.
- A **connector hub** already containing a `hardcover` connector, tier 1,
  `carries: ["progress", "finished"]`, write-capable, credentials encrypted at rest,
  with a background worker doing retry/backoff. Its `readwise` connector is
  `carries: ["highlight"]` — highlights, not progress and not sessions.
- **Server-side matching**, from the document's title and author as sent by the firmware
  (so its "Send Metadata" device setting must be on), with confidence, `auto|manual|none`
  provenance, sticky manual overrides, rematch, lookup, a library refresh, and a review
  endpoint that even reports a per-book `push_note` explaining why a push cannot proceed.
  That is this design's identity resolution and review page, already built.
- Per-document status (`reading|paused|finished|dnf`), dates, info and cover endpoints;
  clippings and bookmark delta sync; reading stats with streaks; auto-pause after 30 days.

**What this changes here:** nothing structurally, because Pagewire is deliberately
separate. Three things were still worth taking. Its document-identity note confirms the
device has **two** hash methods and that a book synced under both appears twice (see the
identity section). Its matcher shows a workable alternative to the metadata sweep —
title and author sent by the device — which is only available to a KOSync server, which
this design is not, so the sweep stays. And its connector contract is worth reading
before writing the Readwise client, because the Readwise Books session API this design
targets is the one gap no upstream connector fills.

**`bartholomej/almanac.crosspoint-plugin`** — a published SD-card plugin whose
`device.json` binds `reader.session` to a POST with `"connect": true` and the same
`{event.*}` substitutions this design's manifest uses. Useful as a known-good manifest
shape: the token is declared `{"file": "token.json", "path": "token"}` and a flat config
file is declared `{"config": {"file": "config.json"}}`, with `{cfg.KEY}` reading its
keys. The spike manifest follows that shape rather than guessing from the prose docs.

## The design

Three pieces.

### 1. The device plugin — declarative JSON, no code

A folder on the SD card:

```
/.crosspoint/plugins/pagewire/
    device.json      event handlers
    config.json      {server} base URL
    token            shared secret for the relay
    README.md        setup notes, shown on-device
```

`device.json` binds one request template to `reader.session` (carrying `{event.book}`,
`{event.document}`, `{event.start_time}`, `{event.end_time}`, `{event.duration_seconds}`,
`{event.start_progress_bp}`, `{event.end_progress_bp}`, `{event.id}`, `{event.ts}`) and
one to `reader.exit` (carrying `{event.percent}` and `{event.end_percent}`, which is how
a finished book is detected). The handler declares `"connect": true` so the drain
happens at sleep entry.

No `plugin.js`. Nothing executes on the device.

### 2. The relay — standalone Python/FastAPI service

Deployed on the NixOS LXC. Storage is SQLite. It has three jobs:

**Ingest.** Authenticated endpoints for the device's events. Each event is stored
verbatim, idempotently keyed on `event.id` (delivery is at-least-once, so replay must be
a no-op). Raw events are kept, not just the derived sessions, so the derivation can be
changed later without losing history.

**Resolve identity.** A `books` table, primary keyed on the content hash (`document`,
the KOSync partial-MD5), because it is content-derived and survives renames, moves, and
reorganizing the card. Columns: first-seen SD path, title, author, `isbn`,
`readwise_book_id`, `hardcover_book_id`, `hardcover_edition_id`, plus a status
(`unmatched` / `auto` / `confirmed`). Rows are created on first sight. Matching is
attempted in this order: an ISBN if one is known, then title+author search against each
service, then manual confirmation in the relay's web UI. A row that cannot be matched
appears in a review list showing its SD path, and Vivek confirms it once.

**Auto-add rule (answered by Vivek 2026-10-31):** when the match is confident — an ISBN
match — the relay creates the book in Readwise and Hardcover by itself with no gate.
Anything ambiguous (title+author search, or a filename-derived guess) is held: the
sitting is recorded locally and delivery to the services waits for him to confirm the
match once in the review UI. A wrong match is the failure mode being avoided here, since
Readwise sittings cannot be moved to a different book afterwards.

The label problem, stated honestly: `reader.session` carries no title or author, only the
SD path. So the design treats the SD path as the initial label, parses title/author out
of it when the filename is conventional, takes real metadata from the **metadata sweep**
(see the section on it below) once the device has been in File Transfer mode, and always
lets a human fix it. Identity never depends on the Calibre library.

Two known wrinkles. The hash is per-*file*, so replacing a book with a different edition
produces a new row for the same book — rows must be mergeable or reassignable by hand.
And one service book can be pointed at by two different hashes (two copies of the same
file) — allowed, but the relay must not be surprised when both report progress.

**Derive and deliver sessions.** A firmware session ends at sleep/exit, which is not the
same as Vivek's rule. His rule: "when i started reading and when it ended. if i don't
pick it up again in 30 minutes, then when i turned the page last should be the end
session", and "the time between page turns should be recorded as a pause".

So: consecutive firmware sessions for the same book whose gap is **less than the stitch
window (default 30 minutes, a tunable)** become one delivered session —
`started_at` = first `start_time`, `ended_at` = last `end_time`, duration = the sum of
the constituent `duration_seconds`, and `paused_seconds` = the sum of the intervening
gaps. A gap at or above the window is a boundary, not a pause. Pauses are only visible
at stitch points; a single firmware session arrives already compacted, with sub-2-second
gaps and reverse turns removed by the firmware and unrecoverable.

**Book switches end a sitting (answered by Vivek 2026-10-31).** Offered "one stitched
sitting with the five minutes counted as pause" versus "two sittings", he chose: read
book A, put it down, read B, come back to A — that is **two sittings in A**, and pause
"only counts time when no other book was open". So the stitcher additionally requires
that **no session for any other book falls inside the gap**. A gap containing another
book's reading is a boundary, not a pause, no matter how short it is.

**Open decision — does the pre-flush lump count as pause?** The firmware compacts idle
gaps out of `duration_seconds`, so the gaps inside a sitting are unrecoverable. The only
trace is the gap between `end_time` and the event's `ts` (its emission time), which on the
spike's real session was 32 seconds. Awaiting Vivek's ruling; my proposal is that
`ended_at` = `end_time` (the last counted page turn, which is what he said the end should
be) and `paused_seconds` = the pre-flush lump **plus** the stitched inter-session gaps,
with the 30-minute window deciding each — a gap at or over the window is a boundary, not a
pause. That makes the window do double duty and keeps a sitting always ending on a page
turn. One case it mishandles: a book left open with the device sleeping hours later
produces an hours-long lump, which the window catches by demoting it to a boundary.

Because Readwise cannot amend a session after the fact, nothing is delivered while it is
still open: a derived session is only eligible once no new constituent event has arrived
for the stitch window, and a periodic sweep finalises and pushes it.

Only two things finalise a derived session: the stitch window elapsing with no new
constituent event, or the device reporting the book finished (`end_percent` == 100). A
plain `reader.exit` does **not** finalise one — treating it as final would directly
contradict Vivek's rule, since leaving the reader and resuming four minutes later is the
common case and must stay one sitting. This is my call, correcting an earlier draft that
listed exit as a finaliser. This is the piece that most needs care, because a mistake here
is visible forever.

If a stitched span would exceed Readwise's 24-hour `started_at`→`ended_at` cap, it is
split into multiple sessions at the boundary.

**Delivery** is per destination, each independently enabled with its own credentials:

- Readwise: `POST /sessions` with `format: ebook`, `percent_at_start`/`percent_at_end`
  (the device's basis points ÷ 10000), the summed `paused_seconds`, a `timezone`, and
  `device: {type: "E-Reader", vendor: "Xteink", model: "X4"}`. `publish_to_feed` stays
  false. A book is marked finished (PATCH status `finished`, `finished_reading_at`) when
  the device reports `end_percent` == 100.
- Hardcover: `update_user_book_read(progress_pages, action_at)` with the page computed
  from the edition — the same page mapping the KOReader plugin uses — and
  `update_user_book(status_id: 3)` when finished. Never a journal entry, since the
  server writes those.

Idempotence is by `(book, started_at)` against a local send log, so a retry after a
crash does not create a second session.

### 3. Deployment — NixOS, on the NixOS LXC

A NixOS module plus the service definition, alongside the existing ones on that box, with
the SQLite file and the token in a state directory. Independently deployable; no
dependency on autoCalibre-Web.

The configuration lives in `github.com/bcosynot/base-server` (private; read on
2026-10-31 via the `gh` CLI with `GH_CONFIG_DIR` pointed at the profile's gh config —
without that env var the CLI reports no login). Branch `unit` was 2 ahead / 1 behind
`main` at read time; `main` is the default. Verified layout:

- Pure infrastructure-as-code. **No application source lives in this repo** (AGENTS.md is
  explicit about this): `flake.nix` is the entire configuration, plus `AGENTS.md` and
  `docs/jhop-ouch-build-issue.md` on the `unit` branch. `configuration.nix` is imported by
  `nixosConfigurations.base-server` but **does not exist** in the repo.
- Apps are consumed as **flake inputs from their own repositories** — `jhop` and `ouch`
  come from `github:bcosynot/jhop` / `github:bcosynot/ouch` and are built with
  poetry2nix; `prodlog.xyz` from `github:bcosynot/prodlog`. So the relay's source wants
  its own repo, not a directory in `base-server`. Locally that repo is
  `/root/projects/pagewire` today, unpublished.
- The `webServer` module exposes a `sites` attrset; each site is `static` (served
  straight by nginx) or dynamic (an **NGINX Unit** python application with `module`,
  `callable`, `port`, `home`, `environmentVariables`). Unit's listeners are generated as
  `127.0.0.1:<port>`, and nginx fronts each dynamic site with `forceSSL = true` and
  certs at `/etc/ssl/<domain>.pem`. `networking.firewall.allowedTCPPorts = [ 80 443 ]`.
- The real deployment target is `nixosConfigurations.base-server-lxc` (x86_64, Proxmox
  NixOS LXC) at **192.168.0.112**, reachable on ports 22/80/443. Per AGENTS.md the
  running system was last built from an **older revision** than the current `flake.nix`
  (no tailscale binary/unit, no `/dev/net/tun`), so a redeploy is needed before any new
  module takes effect. AGENTS.md also warns: **do not introduce new plaintext secrets —
  flag them instead** (the existing `OUCH_OW_API_KEY` inline in `flake.nix` is the
  anti-pattern to avoid). Tokens therefore belong in a state file with restrictive
  permissions, referenced by path, not inline in the flake.
- Data lives under `/var/app/data/<app>` with `systemd.tmpfiles` rules and a shared
  `app-writers` group; apps get their own uid/gid. A relay should follow that convention
  (`/var/app/data/pagewire`).
- Validation per AGENTS.md: `nix flake check`, or
  `nix build .#nixosConfigurations.base-server-lxc.config.system.build.toplevel`. Full
  deployment generally isn't possible from a dev box.

**Two conflicts this creates, both mine to call.** First, the repo's dynamic-app pattern
is Unit on loopback behind an HTTPS-only nginx vhost, but the device's sleep-entry drain
needs a plain-HTTP answer it can reach on the LAN inside 10 seconds — it cannot present a
trusted certificate and should not need a VPN. So the relay does **not** follow the
`webServer` site pattern: it runs as a plain systemd service (uvicorn) with a separate
small nginx listener on the LAN (or an allowed firewall port) speaking HTTP, in front of
the app on loopback. Second, the finalisation sweep needs one long-lived process with a
scheduler, which an ASGI app under Unit's process model does not give cleanly; the
systemd service gives it directly. Data still goes in `/var/app/data/pagewire` with the
tmpfiles convention, so it fits the box even while the exposure differs.

**Deployment access is not yet available from this environment**: AGENTS.md says to SSH
as root using `~/.ssh/base_server_lxc`, and that key is not present in this profile's
`~/.ssh` (only `id_aayavyay`, `id_haos`, `id_pve`), so the relay cannot be installed from
here until the key is available. Verified 2026-10-31.

### The relay does NOT take over KOSync — revised 2026-10-31

An earlier draft had the relay serve Crosspoint's KOSync endpoint and forward those
uploads to the LXC unchanged, purely to harvest `filename` / `title` / `authors` for
labels. Vivek asked why that was in the plan, and the answer was not good enough:

- KOSync progress uploads happen **only when the sync button is pressed** — the exact
  action this project exists to remove. The label supply would depend on him continuing
  the habit we are eliminating.
- It cost a full KOSync server implementation plus an upstream forwarding path, and it
  dragged his existing setup into the blast radius, contradicting the anti-goal "silent
  mutation of his existing setup".

So the relay does not implement KOSync and does not forward anything to the LXC.
autoCalibre-Web is untouched.

**Replacement — the metadata sweep.** Source: `docs/webserver-endpoints.md`. The device
runs an HTTP server on port 80 with `GET /api/files?path=…` (JSON listing:
`name`/`size`/`isDirectory`/`isEpub`) and `GET /download?path=…` (EPUB served as
`application/epub+zip`) — available "while CrossPoint Reader is in File Transfer or
Calibre Wireless mode", in STA mode, advertised as `crosspoint.local` via mDNS. These
endpoints are reachable without credentials in the documented examples (UNVERIFIED: the
doc never states auth explicitly). Protected paths (`.koreader.json`, `opds.json`,
`wifi.json`, `System Volume Information`, `XTCache`) cannot be downloaded, which does not
matter here.

When the device is in File Transfer mode, the relay lists the card, downloads any EPUB
whose content hash it has not yet identified, and reads the OPF for title, author and the
ISBN identifier. That ISBN is what makes "auto-add on a confident match" real — without
it, nearly every book would land in the confirmation queue. File Transfer mode is a mode
Vivek already enters to load books, so this adds no new habit.

Consequences to get right:

- The sweep is a separate, manually triggered (or periodically probed) path from ingest.
  It cannot run during a normal reading session, because the web server is not up then.
- The sweep must map a downloaded file back to a hash. `GET /api/files` gives names and
  sizes only, no hash, so the relay computes the same KOSync partial-MD5 the device does
  over the file it downloaded, and keys the row on that. **VERIFIED on hardware
  2026-10-31** — the spike computed it locally for a real book and got the device's exact
  value: device `document` `8f49706e45186c3185a9a7fcbcabe6f5`, locally computed
  `8f49706e45186c3185a9a7fcbcabe6f5`, over a 964,964-byte EPUB. The algorithm is
  `spike/document_id.py` (content method), which lifts into the relay unchanged. This was
  the single highest-risk assumption in the design, because the whole identity table hangs
  on it, and it now holds.
- The relay needs the device's address: `crosspoint.local` (mDNS, may not resolve inside
  the LXC) or the IP shown on the device screen. A tunable.
- **There are two document-id methods, not one — and this device is on the content one.**
  Confirmed by reading the firmware (`lib/KOReaderSync/KOReaderDocumentId.cpp`) and
  independently by crosspoint-sync's protocol doc: the device computes either a partial
  MD5 over content-sampled bytes, or a plain MD5 of the file's basename, "per device
  setting". They are different keys for the same book, and crosspoint-sync's own note is
  that "the server never tries to unify the two hash methods; a book synced under both
  appears as two documents". **Verified on hardware:** for one book the device's
  `document` matched the content method and not the filename method (`191d894e…`), so a
  rename on the card is currently harmless. The relay still stores which method keyed a
  row, because the setting can change and a device reset would silently re-key everything.

## Tunables (exposed, not baked in)

Stitch window (30 min), per-destination enable switches, Readwise token, Hardcover token,
Hardcover page-mapping on/off, device address for the metadata sweep, sweep trigger
(manual / periodic probe), delivery throttle, retry/backoff, timezone, device label,
finalisation sweep interval.

## Decisions I made (check these)

- Python + FastAPI — chosen by Vivek.
- SQLite for storage; raw events retained alongside derived sessions.
- The relay does **not** touch KOSync and does not forward anything to the LXC; a
  metadata sweep against the device's own File Transfer web server replaces the KOSync
  label harvest. (Revised 2026-10-31 after Vivek challenged the original call.)
- No `plugin.js`; no on-device UI. The matching/review UI is a page on the relay.
- Identity is keyed on the content hash, never on Calibre library membership.
- Readwise is the only destination that stores sittings; Hardcover gets current progress
  and finished status, because that is all it can store. **Confirmed by Vivek
  2026-10-31** — asked whether Hardcover sittings were needed via the only writable
  journal entry type, he answered "readwise session details are enough". So no note-type
  journal entries are written to Hardcover; its feed stays clean.
- Sessions are delivered only after they are final, never speculatively.

## Answered by Vivek (2026-10-31)

Recorded here so nothing depends on remembering the conversation. Each is reflected in
the section referenced.

1. **Book switches end a sitting.** A, then B, then A within 30 minutes = two sittings in
   A, and pause counts only time when no other book was open. (See "Derive and deliver
   sessions".)
2. **Installed firmware is v1.5.0 — it has no plugin system.** A firmware flash is step
   zero. (See "The device".)
3. **Nothing else writes to Hardcover today** except the sync button; the earlier
   forwarding attempt never landed as running code. (See "The existing path on the LXC".)
4. **Auto-add on confident matches only** (ISBN); hold ambiguous ones for one
   confirmation. (See "Resolve identity".)
5. **Config repo: `github.com/bcosynot/base-server`, branch `unit`** — private, so its
   layout is still unknown to this document. (See "Deployment".)
6. **Readwise's session detail is enough** for the granularity complaint; Hardcover needs
   only progress and finished. (See "Decisions I made".)

## Left to the builder

Everything below is deliberately open. HTTP framework layout, module boundaries,
schema details beyond the columns named, the exact page-mapping maths, scheduler
implementation, configuration file format, logging, packaging layout, tests.

## Open risks

- **The device side depends on a prerelease firmware.** v2.0.0rc is the only published
  build with the plugin system. The event names are documented as a compatibility promise
  ("semantic, never internal activity names"), which limits the blast radius, but the
  installed firmware is still a release candidate.
- The box cannot be deployed to from this environment yet: `~/.ssh/base_server_lxc` (the
  key AGENTS.md names) is absent here, and the running LXC is on an older revision than
  `flake.nix`, so a redeploy is needed before any relay module takes effect.
- Whether Crosspoint uploads KOSync progress on its own (UNVERIFIED). If it does, labels
  improve for free; if not, the SD path carries the load.
- Hardcover's schema beyond the documented pages (introspection needs a token). A
  per-sitting structure may exist undocumented; the design does not rely on one.
- The 10-second WiFi join deadline at sleep entry, and the 4KB outbox, are hard firmware
  limits: a device offline for a long stretch drops oldest events wholesale rather than
  losing everything, so long outages lose the earliest sittings and keep the latest.
