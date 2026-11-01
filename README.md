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

Design phase. Nothing is built yet. The design of record is
[`docs/superpowers/specs/2026-10-31-pagewire-design.md`](docs/superpowers/specs/2026-10-31-pagewire-design.md),
and work is tracked as a Linear epic (PER-38) with one child per workstream.

The device side cannot run on the installed firmware (v1.5.0): the SD-card plugin
system landed after that release and currently ships only in v2.0.0rc.

## Layout

```
docs/superpowers/specs/   the design of record
spike/                    throwaway kit to prove the device path before building
src/pagewire/             the relay (empty skeleton for now)
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

## Running the spike listener

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
