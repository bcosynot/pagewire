# AGENTS.md

Guidance for AI agents working in this repository.

## What this repo is

The Pagewire relay: a standalone Python service that receives reading-session events
from an e-reader, turns them into reading sessions, and forwards them to Readwise
Books and/or Hardcover. There is also a throwaway `spike/` kit used to prove the
device path before any relay code is written.

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

## What is deliberately NOT part of this repo

- Pointing the device at a KOSync server. Pagewire does not implement KOSync and does
  not modify autoCalibre-Web.
- Any on-device code beyond a declarative `device.json` manifest.
- Two-way sync: reading positions are never pushed back to the device.

## Commands

`make help` lists them. `make test` runs the tests (using `.venv` when it exists),
`make spike` runs the throwaway capture listener and prints the LAN address the device
must reach, `make ip` prints just that address.

## The spike kit

`spike/` is throwaway. It exists to answer two questions with real hardware: whether
the firmware really emits a `reader.session` event and drains it at sleep entry, and
whether a locally computed document hash matches the one the device reports. It is not
a foundation to build on — the relay does not import it — but its `document_id.py` is
the reference implementation of the hash, and will be lifted into the relay once the
spike confirms which of the two hash methods the device is configured for.
