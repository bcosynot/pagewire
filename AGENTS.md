# AGENTS.md

Guidance for AI agents working in this repository.

## What this repo is

The Pagewire relay: a standalone Python service that receives reading-session events
from an e-reader, turns them into reading sessions, and forwards them to Readwise
Books and/or Hardcover. It also works out *which book* an event refers to, by reading
the card through the reader's own File Transfer web server (`src/pagewire/sweep.py`),
because the events carry a path and a hash but no title or author.

`spike/` is a throwaway kit whose work is finished: it proved the device path on real
hardware before the relay was written. Nothing imports it.

The design of record is `docs/superpowers/specs/2026-10-31-pagewire-design.md`. Read
it before changing behaviour; it records the constraints (firmware limits, service API
limits) that the code exists to satisfy, and the decisions already made with the owner.

## Conventions

- Python 3.12+, `src/` layout, pytest. Run `pytest` before claiming anything works.
- **Never commit credentials.** API tokens for Readwise, Hardcover and the device live
  in a state file on the deployed host, or in a `token.json` on the SD card. `*.json`
  files holding tokens are gitignored; commit `*.example` files instead. If you need a
  new secret, add a placeholder example file and name it in the ticket.
- Every tunable the design names (stitch window, destination switches, tokens, device
  address, throttle, retry policy) belongs in configuration, not in code.
- Commit as the owner, not as the agent: `Vivek Ranjan <vivek@vivekranjan.me>`.
- **Evidence for a finished ticket is committed in the repo**, under
  `docs/evidence/<ticket>/` (spike findings in `spike/evidence/`). Never cite a scratch
  or temp path in a completion comment: those are pruned, and a comment that points at
  a deleted file describes evidence nobody can re-check.

## What is deliberately NOT part of this repo

- Pointing the device at a KOSync server. Pagewire does not implement KOSync and does
  not modify autoCalibre-Web.
- Any on-device code beyond a declarative `device.json` manifest.
- Two-way sync: reading positions are never pushed back to the device.

## Commands

`make help` lists them. `make test` runs the tests (using `.venv` when it exists),
`make spike` runs the finished capture listener and prints the LAN address the device
must reach, `make ip` prints just that address.

The metadata sweep is a module rather than a make target, because it needs the reader
in File Transfer mode and is therefore run deliberately rather than as part of a build:

```sh
python -m pagewire.sweep --address <device-address>   # tunables: PAGEWIRE_DEVICE_ADDRESS,
                                                      # PAGEWIRE_SWEEP_TIMEOUT_SECONDS, PAGEWIRE_DB
```

## The device

The reader runs Crosspoint **2.0.0** and reports it at `GET /api/status`
(`version`, `firmwareFamily`, `pluginApi`, `device`). Notes in this repo and on the
Linear epic saying the installed build is v1.5.0 predate the flash and are **no longer
true** — v1.5.0 has no plugin system, and the device is not on it.

Its web server (`GET /api/files`, `GET /download`) answers only while the reader is in
File Transfer or Calibre Wireless mode, and needs no credentials. During a normal
reading session there is no server at all, which is why the sweep is a separate,
triggered path rather than part of ingest.

## The spike kit

`spike/document_id.py` was the reference implementation of the KOSync document hash. It
has been lifted into the relay as `src/pagewire/document_id.py`, now that hardware has
confirmed which of the device's two methods is in use. **The relay's copy is
authoritative — do not keep the spike's in step, and do not re-derive the hash from the
spike.** The spike's runbook, findings and raw captures live in `spike/README.md` and
`spike/evidence/`, kept for the reasoning rather than the code.
