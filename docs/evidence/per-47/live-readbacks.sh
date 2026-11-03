#!/bin/sh
# Live readbacks for PER-47 criteria 2 and 3 (and the PER-44/PER-45 live halves).
#
# Run it ON THE PAGE WIRE BOX, where the tokens live in the mode-600 env file:
#
#   ssh -F ~/.ssh/config pve exec 99999 -- /bin/sh < docs/evidence/per-47/live-readbacks.sh
#
# `pct exec` has no PATH, and quoting does not survive the ssh -> pct hop, so this file is
# piped to /bin/sh on stdin. It prints secrets-free output: the tokens are sourced, never
# echoed.
#
# Values as of 2026-11-02: document 8f49706e45186c3185a9a7fcbcabe6f5,
# readwise book 6057408, hardcover book 770867 / edition 30772692.

export PATH=/run/current-system/sw/bin:/run/wrappers/bin:$PATH
set -a; . /var/app/data/pagewire/pagewire.env; set +a

DOC="${DOC:-8f49706e45186c3185a9a7fcbcabe6f5}"
RW_BOOK="${RW_BOOK:-6057408}"
HC_BOOK="${HC_BOOK:-770867}"
HC_EDITION="${HC_EDITION:-30772692}"

echo "=== R1  the relay answers ==="
curl -s -m 5 http://127.0.0.1:8799/healthz; echo

echo
echo "=== R2  the relay's own rows ==="
PY=$(ls -d /nix/store/*-python3-3.1[34]*/bin/python3 2>/dev/null | head -1)
"$PY" - "$DOC" <<'PYEOF'
import sqlite3, sys
doc = sys.argv[1]
c = sqlite3.connect('file:/var/app/data/pagewire/pagewire.db?mode=ro', uri=True)
print("--- derived_sessions (cite document+started_at, NOT id: ids are re-derived) ---")
for r in c.execute("""select document,started_at,ended_at,active_seconds,paused_seconds,
                      start_progress_bp,end_progress_bp,finalised,finished,event_ids
                      from derived_sessions where document=?""", (doc,)):
    print(r)
print("--- books row for this document ---")
for r in c.execute("""select title,status,readwise_id,hardcover_id,hardcover_edition_id,
                      error,matched_by from books where document=?""", (doc,)):
    print(r)
print("--- send_log (all rows) ---")
for r in c.execute("select id,destination,external_id,sent_at,error,error_status from send_log"):
    print(r)
PYEOF

echo
echo "=== R3  Readwise: the book, then its sessions ==="
curl -s -m 20 -H "Authorization: Token $PAGEWIRE_READWISE_TOKEN" \
  "https://readwise.io/books/api/v1/books?ids=$RW_BOOK"; echo
curl -s -m 20 -H "Authorization: Token $PAGEWIRE_READWISE_TOKEN" \
  "https://readwise.io/books/api/v1/sessions?book_ids=$RW_BOOK"; echo

echo
echo "=== R4  Hardcover: the entry and its in-progress read ==="
curl -s -m 25 -X POST https://api.hardcover.app/v1/graphql \
  -H "Content-Type: application/json" -H "Authorization: Bearer $PAGEWIRE_HARDCOVER_TOKEN" \
  -d "{\"query\":\"query { user_books(where: {book_id: {_eq: $HC_BOOK}, edition_id: {_eq: $HC_EDITION}}, limit: 1) { id status_id user_book_reads(where: {finished_at: {_is_null: true}}, order_by: {id: desc}, limit: 1) { id progress_pages started_at edition { pages } } } }\"}"; echo

echo
echo "NOTE: action_at is NOT readable — user_book_reads exposes only started_at, finished_at"
echo "      and paused_at. And reading_journals needs a read:journal scope this token lacks."
