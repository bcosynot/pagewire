# PER-42 evidence — the metadata sweep, run against the card

Raw captures from a real sweep of the Xteink X4 on 2026-11-01, with the reader in File
Transfer mode at `192.168.0.217`. One file per claim, so a later reader can check the
completion comment rather than trust it.

| File | Criterion | What it shows |
|---|---|---|
| `run-1-first-sweep.log` | 1, 2, 5 | The full first run: 100 EPUBs found, 100 downloaded (409,226,134 bytes), 100 rows written. |
| `books-rows.tsv` | 1, 5 | The resulting rows: document, ISBN, hash method, status, title, author, path. |
| `card-vs-rows.txt` | 1 | The card listing walked fresh and compared to the rows — 100 on the card, 100 rows, nothing on either side without a match. |
| `criterion-2-device-hash.txt` | 2 | The sweep's hash for the one book the device has reported a `document` for, beside the device's own value. |
| `run-2-second-sweep.log` | 3 | A second run: 0 downloaded, 0 bytes, 100 already known, 0 written — in 20 seconds instead of 14 minutes. |
| `run-3-unreachable.log` | 4 | The web server not up, both ways it happens: nothing listening on the port, and no host on the LAN. Exit 1, clean message, row count unchanged. |

## The numbers

- 100 EPUBs, 409,226,134 bytes. The first sweep took about 14 minutes; the device serves
  at roughly 490 KB/s, which is the real cost of identifying a card and the reason
  criterion 3 exists.
- 15 books carry an ISBN and 85 do not, so 85 land in the confirmation queue. That ratio
  is the design's own premise — "without it, nearly every book would land in the
  confirmation queue" — and it holds.
- Every row records `hash_method = content`, matching what the device spike found.
- Every row is `status = unmatched`. The sweep never writes `auto` or `confirmed`:
  matching is the identity ticket's job, not the sweep's.

## Re-running

```sh
# reader in File Transfer mode, address from its screen
python -m pagewire.sweep --address 192.168.0.217 --db state/pagewire.db
```

The whole card is re-downloaded only for books with no row yet. `books-rows.tsv` and the
comparison in `card-vs-rows.txt` were generated from the SQLite file that run left
behind, which is not committed — the TSV is the durable record of it.
