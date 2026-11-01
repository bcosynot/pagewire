# Spike: prove the device path before building the relay

Throwaway. Nothing in `spike/` is imported by the relay and none of it is built to last.
It exists to answer two questions with real hardware that the design currently assumes:

1. **Does the firmware really emit a reading-session event, and does it deliver it at
   sleep entry with WiFi otherwise off?** Everything device-side rests on this.
2. **Can we reproduce the content hash the device reports as `document`?** If not, the
   identity table has no key and the design changes.

Everything here is written from Crosspoint's `develop`-branch documentation
(`docs/plugin-events.md`, `docs/sd-plugins.md`, `docs/webserver-endpoints.md`) and from
its `lib/KOReaderSync/KOReaderDocumentId.cpp`. The installed firmware is v1.5.0, which
has none of it, so the plugin system is step one.

## Kit

| File | What it is |
|---|---|
| `plugin/` | the manifest to copy onto the SD card — `device.json` plus a config and a token |
| `listener.py` | the throwaway HTTP listener that captures events and checks the hash |
| `document_id.py` | the reference implementation of both document-id methods |

## Runbook

Every step is marked **NOT YET RUN**. Tick each one off and record the observed value;
a step filled in from expectation rather than observation is worse than a blank one.

- [ ] **NOT YET RUN — 0. Preconditions.** Firmware v2.0.0rc (or newer) is flashed. This
      machine's LAN address is known. Record the exact firmware build string:

      build: ____________________

- [ ] **NOT YET RUN — 1. Enable plugins on the device.** Settings → Connectivity →
      Plugins → Enabled. It is **Disabled by default** on new installs, and without it
      the folder is invisible no matter how correct the manifest is.

- [ ] **NOT YET RUN — 2. Install the plugin.** Mount the card, then from the repo root:

      ```sh
      make sd DEST=/path/to/card     # or wherever the card mounted
      ```

      That writes `plugins/pagewire/` on the card with the manifest, the three files the
      device reads, and a `config.json` containing this host's LAN address. The firmware
      scans `/plugins`, `/.plugins` and `/.crosspoint/plugins`; `/plugins` is used here
      because it needs no dotfile handling on a desktop. The folder name must stay
      `pagewire` — the firmware caps plugin folder names at 23 bytes.

      server it wrote: ______________________

- [ ] **NOT YET RUN — 3. Start the listener.** From the repo root:

      ```sh
      make spike
      ```

      It creates `spike/plugin/token.json` if there isn't one, prints the LAN address the
      device has to reach, prints the exact `config.json` and `token.json` contents for
      the SD card, and then runs. `make spike BOOK=/path/to/book.epub` additionally
      checks the document hash against that file. Leave it running.

      address it printed: ______________________

- [ ] **NOT YET RUN — 4. See it on the device.** Home → Plugins lists "Pagewire Spike".
      If it does not, stop here: the manifest is wrong, and the device will tell you
      nothing else until it is right.

- [ ] **NOT YET RUN — 5. Read.** With the device's WiFi off, read a book for a few
      minutes and turn pages forward. Note the time you started:

      started reading at: __________

- [ ] **NOT YET RUN — 6. Press sleep.** Watch for the WiFi popup, then the
      "Pagewire spike: session sent" toast. Record what actually happened, including how
      long delivery took and whether the device slept regardless:

      popup appeared: ______   delivery took: ______s   slept anyway: ______

- [ ] **NOT YET RUN — 7. Confirm the capture.** A line appeared in `spike/events.jsonl`
      and in the listener's output. Record the key fields:

      document: ______________________________
      start_time / end_time: ____________________
      duration_seconds: ______   bp start/end: ______ / ______

- [ ] **NOT YET RUN — 8. Check the hash.** Enter File Transfer mode on the device
      (this is also where its web server lives), fetch the same book, and compare:

      ```sh
      curl "http://<device-ip>/api/files?path=/"                 # the listing
      curl -OJ "http://<device-ip>/download?path=<the book>"      # the file
      python spike/document_id.py ./<the book> --document <the captured document>
      ```

      Record the verdict. **A mismatch is a result, not a failure** — it means the hash
      cannot be reproduced yet and the identity design needs revisiting before anything
      is built on it.

      verdict: ______________________

- [ ] **NOT YET RUN — 9. Record which document-id method is in play.** The firmware has
      two (a content hash and a plain hash of the filename) and picks one per device
      setting. If the setting is visible in the device's sync settings, record it — a
      filename-method device means renaming a book on the card changes its identity.

      method: ______________________

- [ ] **NOT YET RUN — 10. Note anything surprising.** Anything the documentation did not
      predict: a field that was empty, a value that was `0`, a delivery that happened at
      a moment other than sleep, an event that never arrived at all.

## Findings so far (2026-10-31, firmware v2.0.0rc)

Recorded here because they are the point of the spike, and two of them are surprises.

**Events do arrive, and the ids are the documented ones.** Four events so far, across
three boots — ids `09588be3-1`, `09588be3-2`, `e500489e-1`, `98fd9b01-1`. The nonce
changes per boot and the counter restarts, which is exactly the dedupe key the design
relies on, and it means the relay must treat a repeated `id` after a reboot as a new
event only if the nonce differs.

**They drained on a WiFi connection, not at sleep.** Owner's observation: "the sync
happened after connecting to wifi, not on sleep". That matches the documented drain
points and is good news — delivery does not depend on the 10-second sleep-entry window
in the common case, and the relay's own stitch window is what decides when a sitting is
final, so prompt delivery cannot create a premature one.

**All four were `reader.exit`, and none was followed by a `reader.session`.** That second
event is the one carrying the start position, end position, duration and progress — the
only reason to bind the first. Nothing needed to be read into it: an earlier version of
`listener.py` printed only the keys it expected, which hid the `event` field, so the
`reader.exit` lines looked like malformed sessions. The listener now prints every key it
receives and marks unsubstituted placeholders inline.

**`end_percent` arrived unsubstituted** — the literal text `{event.end_percent}` — while
`percent` on the same event substituted fine (16, then 2).

**`ts` is a per-boot counter.** 61 and 139 on the first boot, 1268 on the second, 42 on
the third: it rises within a boot and resets at reboot, so it is seconds since boot. The
docs promise `0` when the clock was never set, so this value is a doc divergence and a
trap — it looks like a plausible timestamp, and reading it as an epoch would date a
sitting to 1970 rather than failing. The relay must never derive a time from `ts`.

**No `reader.session` in any of the four captures.** Every event has been `reader.exit`,
and `sleep.enter` has never appeared — so either the card still carries the pre-`sleep.enter`
manifest, or sleep has not been used as the way out of the book. Both are worth ruling
out rather than assuming.

**CONFIRMED, third run: sessions are suppressed until the clock has been set.** Once the
device had joined WiFi (SNTP sets the clock), a full `reader.session` arrived
(`spike/events.jsonl` line 10):

```json
{"book": "/Samit Basu - The Jinn-Bot of Shantiport.epub",
 "document": "8f49706e45186c3185a9a7fcbcabe6f5",
 "duration_seconds": "2638", "start_time": "1793550349", "end_time": "1793552987",
 "start_progress_bp": "1799", "end_progress_bp": "2244", "progress_scale": "10000",
 "event": "reader.session", "id": "f55fb3e8-1", "ts": "1793553019", "v": "1"}
```

Everything the design needs, and it agrees with the docs on the arithmetic: `end_time -
start_time` = 2638 = `duration_seconds`, exactly. `ts` (emission) is 32 seconds after
`end_time`, and a `sleep.enter` for the same book and boot (id `f55fb3e8-2`) shares that
second — so the pre-sleep hook is what flushed it. That 32 seconds is the session's own
idle time, and it is only visible as the gap between `end_time` and `ts`; the firmware
compacts idle gaps out of `duration_seconds`, so individual gaps inside a sitting cannot
be recovered.

Four more things this run established:

- **Duplicate delivery is real.** Two byte-identical `sleep.enter` lines, same id
  `1400c035-1`, both appended. Nothing was wrong with the device: this is the documented
  at-least-once contract, and it means the relay's dedupe on `id` is load-bearing, not
  defensive padding.
- **A `sleep.enter` from outside a reader sends literal placeholders** — `{event.book}`,
  `{event.percent}`, `{event.end_percent}` — because sleeping outside a book carries none
  of those variables. The relay must treat a value shaped like `{event.…}` as absent.
- **`end_percent` substituted fine in this run** (18 and 22) after failing to in the three
  earlier ones. Cause not established. Finish detection can use it, but only once it is
  known to be populated; a placeholder must read as "unknown", never as 0.
- **The id nonce is not reliable as a boot marker.** Two `reader.exit` events one second
  apart (`1793549736`, `1793549735`) carried different nonces. Whatever the nonce is
  scoped to, the relay must key dedupe on the whole id and never infer boots from it.

## Next run (the one that settles it)

The WiFi join has already happened — that is what triggered the last sync — so SNTP has
had its chance and the clock should now be set. That makes the next capture the real
test, and it needs no special setup:

1. Read a book for a couple of minutes, with real forward page turns.
2. Leave the book by pressing **sleep while still inside it**, not Back/Home. Sleep is
   the path the docs say flushes the summary through the pre-sleep hook; the teardown
   fallback has not been observed to fire on this build.
3. `cat spike/events.jsonl` and compare. Three outcomes, each meaning something different:
   * **a `reader.session` with a `ts` that is a real epoch** — the hypothesis holds, the
     clock is now the only prerequisite, and the design stands as written;
   * **`reader.session` with a small `ts` again** — sessions flow but the clock is still
     untrusted, so the times inside it need checking against `start_time`, not `ts`;
   * **no `reader.session` at all, from a sleep** — the trigger on this build differs from
     the docs, and the device half of the design needs revisiting before any relay code.

Also confirm `sleep.enter` appears. It is bound in the current `device.json`, so its
absence means the card still has the older manifest.

## What the results change

- **Event arrives, hash matches** — the design stands as written.
- **Event arrives, hash does not match** — the metadata sweep's key is wrong. Either the
  hash algorithm differs (fix `document_id.py`) or the key has to become the SD path
  plus the title, which weakens automatic matching.
- **No event, but the device queues one** — check the outbox behaviour and the 10-second
  deadline; the design's delivery model assumes late delivery works.
- **No event at all** — the plugin path is not usable on this build, and the whole
  device-side half of the design needs rethinking.
