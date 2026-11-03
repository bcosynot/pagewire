# PER-47 — end-to-end on the device: evidence

`runbook.md` is the numbered runbook the owner executes. This README records what happened
when it was first run against a real reading, and what is still open.

Two readings of the runbook's status exist, and they disagree; this file is the later one.

## State at 2026-11-02 (this file)

**Criteria 2 and 3 are met, on real sittings, from the real device.** Every sitting of
The Jinn-Bot of Shantiport the reader has drained over the relay's listener — beginning
2026-11-01 22:02Z, and continuing — is present in both destinations. The list lives in
`criterion-2-readwise-session.txt`; **the count is deliberately not restated as prose in these
files**, because it grows every time he reads:

| criterion | met? | file |
|---|---|---|
| 1 — a numbered runbook, every step unrun at handover | yes | `runbook.md` (commit `cc37ee7`) |
| 2 — one sitting in Readwise, with the device's positions and duration | **yes** | `criterion-2-readwise-session.txt` |
| 3 — Hardcover's progress matches the device's percent through the edition | **yes** | `criterion-3-hardcover-readback.txt` |
| 4 — the offline case still lands | no — needs the reader | runbook step 6 |
| 5 — the book-switch case gives two sittings in A | no — needs the reader | runbook step 7 |
| 6 — nothing was pressed except sleep | no — needs the owner's statement | runbook step 8 |

Criteria 2 and 3 were closed by evidence that arrived **outside** a runbook run: the device
drained the sitting on its own, the relay derived, delivered and logged it, and both
services read back matching the device's own numbers. That is what the two criteria ask for.
It is **not** a substitute for criteria 4–6, which need the reader in hand, and it does not
satisfy criterion 6, which is explicitly a statement from the person who did the pressing.

## Regenerating these files

Both readbacks are live service reads, so they can be re-run against the deployed box (which
holds the tokens in its mode-600 env file):

```sh
ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < docs/evidence/per-47/live-readbacks.sh
```

That prints the relay's derived row, the Readwise session and the Hardcover read in one go.
It needs no argument and reads the tokens from `/var/app/data/pagewire/pagewire.env`, so it
never carries a secret in itself.

## What this does NOT prove

* **Criteria 4, 5 and 6 are unrun.** The offline case and the book-switch case need new
  readings on the device; criterion 6 needs the owner to state that sleep was the only press.
* **The reading was not performed to the runbook's script.** The sitting that landed was an
  ordinary reading, not step 2 of the runbook, so there is no claim here that the runbook's
  own sequence was followed — only that its criteria 2 and 3 describe what is observed.
* **`runbook.md`'s step 0 baseline and Gate A are stale** and are corrected in place in the
  file, with the original wording kept visible. Read those two sections before running.
