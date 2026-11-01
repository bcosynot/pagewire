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

**Events do arrive, and the ids are the documented ones.** Two events landed minutes
after the plugin was installed, with ids `09588be3-1` and `09588be3-2` — the per-boot
nonce plus a counter, which is exactly the dedupe key the design relies on.

**Both were `reader.exit`, and neither was followed by a `reader.session`.** That second
event is the one carrying the start position, end position, duration and progress — the
only reason to bind the first. Nothing needed to be read into it: an earlier version of
`listener.py` printed only the keys it expected, which hid the `event` field, so the
`reader.exit` lines looked like malformed sessions. The listener now prints every key it
receives and marks unsubstituted placeholders inline.

**`end_percent` arrived unsubstituted** — the literal text `{event.end_percent}` — while
`percent` on the same event substituted fine (16, then 2).

**`ts` was 61 and 139.** Not Unix epochs. Probably seconds since boot.

Working hypothesis for the missing session summary: the docs require "at least 10 active
seconds **and a trustworthy positive start time**", and the device had never had a WiFi
join, so SNTP had never run and there was no trustworthy time to stamp a sitting with.
If that is right, sessions are suppressed until the clock has been set at least once —
which would mean the design cannot place a sitting in time on a device whose clock has
never been set, and Readwise wants `started_at`.

## Next run (the one that settles it)

1. Let the reader join WiFi **once**, so SNTP can set the clock. Any sync or File
   Transfer session does it.
2. Read a book for a couple of minutes with real forward page turns, WiFi back off.
3. Press **sleep** while still inside the book. Not "back to home": sleep is the path the
   docs say flushes the summary through the pre-sleep hook.
4. `cat spike/events.jsonl` and compare with the transcripts above. A `reader.session`
   with a real epoch `start_time` means the hypothesis holds and the design stands. No
   session, with a set clock, means the summary has a different trigger on this build and
   the device-side design needs revisiting.

Expect the first sleep after the WiFi join to deliver a session stamped with the *old*
untrusted time, since the queued event was written before the clock moved. The second
reading is the clean one.

## What the results change

- **Event arrives, hash matches** — the design stands as written.
- **Event arrives, hash does not match** — the metadata sweep's key is wrong. Either the
  hash algorithm differs (fix `document_id.py`) or the key has to become the SD path
  plus the title, which weakens automatic matching.
- **No event, but the device queues one** — check the outbox behaviour and the 10-second
  deadline; the design's delivery model assumes late delivery works.
- **No event at all** — the plugin path is not usable on this build, and the whole
  device-side half of the design needs rethinking.
