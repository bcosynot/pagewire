# Spike evidence — raw captures

One file per claim in the PER-39 completion comment. Each holds the request as it was
issued and the response as it came back, so a later reader can re-run the same call and
compare rather than trust a summary. Captured 2026-11-01 against the Xteink X4
(`device 01`, PER-39) with the reader in **File Transfer mode**, device address
`192.168.0.217` (STA mode). See `../README.md` for the runbook these close out.

| File | Criterion | What it shows |
|---|---|---|
| `criterion-1-status.http` | 1 | `GET /api/status` → `version 2.0.0`, `firmwareFamily crosspoint`, `pluginApi 1`, `device X4`. The firmware build string the ticket was waiting on. |
| `criterion-4-files.http` | 4 (1/2) | `GET /api/files?path=/` → 200 JSON card listing, **no credentials sent**. |
| `criterion-4-download.http` | 4 (2/2) | `GET /download?path=…` → 200, `Content-Type: application/epub+zip`, 964,964 bytes, plus the sha256 of the body as received. |
| `criterion-3-hash-check.txt` | 3 | `spike/document_id.py` over the **freshly downloaded** file: `document` `8f49706e…` reproduced exactly by the content method. Re-proves the identity key against bytes fetched this session, not a cached copy. |

`serial` in the status response reads `"Not found"` — the device does not report one, so
identity keys on the content hash and nothing else. That agrees with the design, which
never keys on serial.

The EPUB body itself is **not** committed: `spike/downloads/` is gitignored, and only its
size and sha256 are recorded above as the reproducibility handle.

## Re-running any of these

```sh
DEV=192.168.0.217                     # the address on the device's screen
curl -i "http://$DEV/api/status"
curl -i "http://$DEV/api/files?path=/"
curl -D - -o /tmp/book.epub \
  "http://$DEV/download?path=%2FSamit%20Basu%20-%20The%20Jinn-Bot%20of%20Shantiport.epub"
python spike/document_id.py /tmp/book.epub --document 8f49706e45186c3185a9a7fcbcabe6f5
```

The `/api/files` and `/download` endpoints are up **only while the reader is in File
Transfer or Calibre Wireless mode**; during a normal reading session the web server is
not running and these calls fail to connect. That is why the sweep is a separate,
triggered path rather than part of ingest.
