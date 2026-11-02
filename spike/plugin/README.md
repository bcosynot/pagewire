Pagewire spike

This plugin sends reading-session events to a throwaway listener on your network. It
is a test fixture, not the real thing.

Setup

1. Put this folder on the SD card at `/.crosspoint/plugins/pagewire/`. The folder name
   must stay `pagewire` (the firmware caps plugin folder names at 23 bytes).
2. Copy `token.json.example` to `token.json` and replace the value with a secret of
   your own. The listener needs the same value.
3. Edit `config.json` so `server` points at whatever will receive the events, for example
   `http://192.168.1.50:8799`. For the **deployed relay** that is the box's LAN address,
   `http://192.168.0.112:8799` — staging with
   `make sd DEST=<card> SERVER=http://192.168.0.112:8799` writes it for you, instead of
   this machine's address. `make sd` also prints the staged token's sha256, which must
   match the relay's `PAGEWIRE_TOKEN`.
4. On the device: Settings → Connectivity → Plugins → Enabled. This is off by default.
5. Home → Plugins should now list "Pagewire Spike".

What it does

Binds two events to HTTP requests:

- `reader.session` — the summary the firmware writes when a reading session ends,
  carrying the book path, the document hash, the start and end times, the active
  seconds, and the start and end progress in basis points.
- `reader.exit` — carries `percent` and `end_percent`; `end_percent` of 100 means the
  book was finished.

Both declare `"connect": true`, so the device brings WiFi up briefly at sleep entry to
deliver them, even though WiFi is otherwise off. Delivery is queued on the SD card
first, so nothing is lost if the network is unavailable.

Troubleshooting

- Nothing arrives: check the listener is reachable from the device's network, and that
  `Plugins` is Enabled rather than just installed.
- A 401 in the listener output: the two `token.json` values differ.
- The URL in the request is malformed: `config.json` must hold the base URL only, with
  no trailing slash, and the listener path is added by the handler.
