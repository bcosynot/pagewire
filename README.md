# Pagewire

Record what Vivek actually reads, with zero taps on the device.

An Xteink X4 running Crosspoint firmware reports each reading sitting — start
position, end position, active seconds — to a standalone relay. The relay stitches
consecutive sittings for the same book into one reading session with the gaps
counted as pause, identifies the book by its content hash, and forwards the result
to Readwise Books and/or Hardcover, each independently switchable.

The two complaints that started this:

> i need to remember to hit the sync progress button

> hardcover isn't as granular as i want in my reading

## Status

Building. Six of the epic's ten children are merged on `main` — the relay's ingest,
derivation, identification and identity layers — and the device-side path is verified on
real hardware. **Nothing is delivered to either service yet**, so the two complaints above
still stand.

Merged so far:

- **The relay skeleton** (`PER-40`) — token-authenticated ingest, idempotent on the
  device's own event id, with the SQLite schema and the design's tunables in config.
- **Session derivation and stitching** (`PER-41`) — consecutive sittings joined across
  the 30-minute window, with pauses and the book-switch rule applied.
- **The metadata sweep** (`PER-42`) — the card read through the reader's own File
  Transfer web server, giving every book its title, author and ISBN keyed on the
  content hash. Run against the real card: 100 books, 100 rows.
- **Book identity and the review page** (`PER-43`) — an ISBN match is added to Readwise
  and Hardcover with no gate; anything ambiguous waits in the page for one confirmation.
  Of the card's 100 books, 15 carry an ISBN and resolve themselves; 85 need a decision.
- **The device spike** (`PER-39`) — the event path, the drain behaviour and the
  document hash all confirmed against hardware.

Next: delivery to Readwise (`PER-44`) and Hardcover (`PER-45`), then deploy (`PER-46`)
and the end-to-end check on the device (`PER-47`).

The design of record is
[`docs/superpowers/specs/2026-10-31-pagewire-design.md`](docs/superpowers/specs/2026-10-31-pagewire-design.md),
and work is tracked as a Linear epic (PER-38) with one child per workstream.

The device runs Crosspoint **2.0.0**, which carries the SD-card plugin system this
project needs. (v1.5.0, the build the design was originally written against, has no
plugin system at all.)

## Layout

```
docs/superpowers/specs/   the design of record
docs/evidence/            raw captures behind each completed ticket
spike/                    throwaway kit that proved the device path — finished, nothing imports it
src/pagewire/             the relay
tests/                    tests for the relay
```

## Related reading

- Crosspoint firmware docs: `docs/plugin-events.md` and `docs/sd-plugins.md` in
  [crosspoint-reader/crosspoint-reader](https://github.com/crosspoint-reader/crosspoint-reader)
- [crosspoint-reader/crosspoint-sync](https://github.com/crosspoint-reader/crosspoint-sync)
  solves a neighbouring problem (KOSync server plus a service-connector hub). Pagewire
  is deliberately separate, and notes in the design record what was borrowed as
  reference and what was rejected.

## Development

```sh
make test     # runs the tests, using .venv when it exists
make venv     # creates .venv and installs the package with its dev extras
make help     # the rest
```

Equivalent by hand: `python -m venv .venv && . .venv/bin/activate`, then
`pip install -e '.[dev]'`, then `pytest`.

## Running the metadata sweep

Reading-session events carry an SD path and a content hash, but no title, author or
ISBN. The sweep reads those out of the books themselves, over the reader's own web
server, and keys each one on the hash the device reports:

```sh
python -m pagewire.sweep --address 192.168.0.217    # the address on the device's screen
```

The device's web server runs **only** while the reader is in File Transfer or Calibre
Wireless mode, so a sweep cannot run during a normal reading session — that is also
why it is a deliberate command rather than part of ingest.

A first sweep downloads every EPUB with no row yet: 100 books and ~409 MB on the
current card, about 14 minutes at the device's ~490 KB/s. A later sweep skips anything
already identified and finishes in seconds, because the identity is keyed on the SD
path. Tunables: `PAGEWIRE_DEVICE_ADDRESS`, `PAGEWIRE_SWEEP_TIMEOUT_SECONDS`,
`PAGEWIRE_DB`.

## Resolving identity

A sweep records what the card knows; this decides which book each row *is*, as each
service knows it, and writes both services' ids onto the row:

```sh
python -m pagewire.identity          # match everything an ISBN can match
python -m pagewire.identity --list   # show the queue and the reasons, offline
```

An ISBN match is confident, so it is added to Readwise and Hardcover with no gate. A book
without one is left `unmatched` and appears in the review page, where one confirmation
writes its ids and ends the ambiguity — a wrong match sends immutable Readwise sittings to
the wrong book, so the relay never guesses. On the current card that is 15 books resolving
themselves and 85 waiting.

The page is served by the relay at `/review`, gated on the same shared device token
(`?token=…`), with a form per book and a one-click confirmation from Hardcover's
exact-title match. A refused call — an under-scoped token, most often — is recorded on the
row and shown there, and **Re-run automatic matching** retries the queue once the cause is
fixed.

Tunables: `PAGEWIRE_READWISE_ENABLED`, `PAGEWIRE_READWISE_TOKEN`,
`PAGEWIRE_READWISE_BASE_URL`, `PAGEWIRE_HARDCOVER_ENABLED`, `PAGEWIRE_HARDCOVER_TOKEN`,
`PAGEWIRE_HARDCOVER_ENDPOINT`, `PAGEWIRE_SERVICE_TIMEOUT_SECONDS`, `PAGEWIRE_DB`.

Evidence for this, including the page itself and a run over all 100 swept books, is in
[`docs/evidence/per-43-identity/`](docs/evidence/per-43-identity/) — with the boundary
stated plainly: no live service token existed in the build environment, so the vendor
round trips are exercised against servers speaking the documented shapes and remain
owner-verified.

## Running the spike listener

The spike is finished — `PER-39` closed it, and its findings are in
[`spike/README.md`](spike/README.md) and [`spike/evidence/`](spike/evidence/). These
targets still work, and are here for completeness:

```sh
make spike                      # capture listener, prints the LAN address to use
make spike BOOK=/tmp/book.epub  # ...and also check the document hash against a file
make sd DEST=/media/card        # stage the plugin folder onto a mounted SD card
make ip                         # just the LAN address
```

`make sd` writes `DEST/plugins/pagewire/` with `manifest.json`, `device.json`,
`README.md`, `token.json`, and a `config.json` holding this host's LAN address — so the
card is ready to put back in the device. Then enable the plugin subsystem on the device
(Settings → Connectivity → Plugins → Enabled, **off by default**) and reboot it.

`make spike` creates a local `spike/plugin/token.json` if there isn't one, then prints
the exact `config.json` and `token.json` contents to put on the SD card, and the URL the
device should post to. The device reaches this host over the LAN, so a `0.0.0.0` bind is
not enough on its own — that's why the address is printed rather than the bind.

See [AGENTS.md](AGENTS.md) for the conventions that apply to automated work in this repo.
