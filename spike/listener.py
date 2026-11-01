#!/usr/bin/env python3
"""Throwaway HTTP listener for the spike: receive one real reading-session event.

It does three things and nothing else:

  1. Authenticates the device with a bearer token, so a stray POST from anything else
     on the LAN does not land in the capture.
  2. Appends every accepted event as one raw JSON line to `spike/events.jsonl`, so the
     capture is a file that outlives the session rather than a scrollback.
  3. Prints a readable summary, and — given `--book` — compares the event's `document`
     against the hash computed locally from the same file, which is the one thing the
     whole identity design rests on.

Usage:

    python spike/listener.py --token "$(cat spike/plugin/token.json)"
    python spike/listener.py --book /tmp/book.epub        # also verify the hash

Run it on a machine the device can reach on the LAN over plain HTTP. The device's
sleep-entry drain gives the request a 10-second window, so keep this process up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from document_id import content_document_id, filename_document_id  # noqa: E402

CAPTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")


PLACEHOLDER = "{event."


def describe(payload: dict) -> str:
    """Print the payload as it arrived, and flag anything that arrived unsubstituted.

    Deliberately does NOT print a fixed list of keys: the first real capture was read
    through an earlier version of this function that only showed the keys it expected,
    and an `event` key of `reader.exit` went unseen because of it.
    """
    lines = ["", "event received -------------------------------------------------"]
    for key in sorted(payload):
        value = payload[key]
        flag = "  <-- NOT SUBSTITUTED (the firmware had no value for this)" \
            if isinstance(value, str) and PLACEHOLDER in value else ""
        lines.append(f"  {key:20} {value}{flag}")

    missing = [k for k, v in payload.items()
               if isinstance(v, str) and PLACEHOLDER in v]
    if missing:
        lines.append(f"  note: {len(missing)} variable(s) came through as literal placeholders: "
                     + ", ".join(sorted(missing)))

    ts = payload.get("ts")
    try:
        if ts is not None and int(ts) < 1_000_000_000:
            lines.append(f"  note: ts={ts} is not a wall clock — the device's clock is "
                         f"probably still unset (no SNTP sync yet)")
    except (TypeError, ValueError):
        pass

    if payload.get("event") == "reader.session":
        try:
            start = int(payload["start_time"])
            end = int(payload["end_time"])
            duration = int(payload["duration_seconds"])
            span = end - start
            lines.append(f"  active seconds     {duration} (span {span}s, "
                         f"{'consistent' if span == duration else 'MISMATCH vs span'})")
        except (KeyError, ValueError, TypeError):
            lines.append("  active seconds     (start/end/duration not all present)")
    return "\n".join(lines)


def verify_hash(payload: dict, book_path: str) -> str:
    document = str(payload.get("document", "")).strip().lower()
    content = content_document_id(book_path)
    filename = filename_document_id(book_path)
    if document == content:
        return f"  hash: MATCH (content method) — {document}"
    if document == filename:
        return (f"  hash: MATCH (filename method) — the device is set to filename sync, "
                f"so the content hash is not the key in play")
    return (f"  hash: NO MATCH\n"
            f"    device   {document}\n"
            f"    content  {content}\n"
            f"    filename {filename}\n"
            f"    ^ report this: it changes the identity design")


def make_handler(token: str, book_path: str | None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz"):
                self._reply(200, b'{"ok":true}')
            else:
                self._reply(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            if not self.path.startswith("/events/session"):
                self._reply(404, b'{"error":"not found"}')
                return
            if token and self.headers.get("Authorization", "") != f"Bearer {token}":
                print("rejected: bad or missing bearer token", file=sys.stderr)
                self._reply(401, b'{"error":"unauthorized"}')
                return

            raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                print(f"rejected: body was not JSON ({raw[:200]!r})", file=sys.stderr)
                self._reply(400, b'{"error":"bad json"}')
                return

            with open(CAPTURE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")

            print(describe(payload))
            if book_path:
                print(verify_hash(payload, book_path))
            print(f"  appended to {CAPTURE}")
            self._reply(200, b'{"ok":true}')

        def log_message(self, *args):  # silence the default per-request noise
            pass

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8799, help="bind port (default 8799)")
    parser.add_argument("--token", default=os.environ.get("PAGEWIRE_TOKEN", ""),
                        help="bearer token the device will send (or set PAGEWIRE_TOKEN)")
    parser.add_argument("--book", help="a downloaded copy of the book, to verify the hash")
    args = parser.parse_args(argv)

    if not args.token:
        print("warning: no --token given, so anything on the LAN can post here",
              file=sys.stderr)
    if args.book and not os.path.isfile(args.book):
        print(f"no such file: {args.book}", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.token, args.book))
    print(f"listening on http://{args.host}:{args.port}")
    print(f"device should POST to  http://<this-host-lan-ip>:{args.port}/events/session")
    print(f"capture file           {CAPTURE}")
    if args.book:
        print(f"verifying hashes against {args.book}")
    print("waiting for the first event. leave this running, then read and press sleep.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
