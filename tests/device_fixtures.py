"""Fixtures for the sweep: real EPUBs, and a real device web server to serve them.

Two doubles, both deliberately as close to the hardware as they can be.

`make_epub_bytes` writes a genuine EPUB — a zip with `META-INF/container.xml`
pointing at an OPF package document carrying Dublin Core metadata — rather than a
hand-shaped blob, so the parser is exercised on the same structure it will meet on
the card.

`DeviceServer` is a real HTTP server speaking the device's own shapes: the JSON
listing from `GET /api/files?path=…` and a body from `GET /download?path=…`. Tests
that need a device use it instead of a stub object, so the sweep's HTTP client is
under test rather than bypassed.
"""

from __future__ import annotations

import io
import json
import threading
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
{identifiers}
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{author}</dc:creator>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""


def _zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    """A zip member with a fixed timestamp, so two builds of the same book match."""
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    return info, data


def make_epub_bytes(
    title: str = "Untitled",
    author: str = "Anonymous",
    *,
    identifiers: list[str] | None = None,
    filler: bytes = b"",
) -> bytes:
    """Build a real EPUB as bytes.

    `identifiers` are written verbatim as `dc:identifier` text, so a test can hand
    the parser `urn:isbn:9780441013593`, a bare ISBN, a hyphenated one, or an
    unrelated scheme — which is the whole point, since cards carry all four.
    """
    identifiers = [] if identifiers is None else identifiers
    blocks = "\n".join(f"    <dc:identifier>{value}</dc:identifier>" for value in identifiers)
    opf = _OPF.format(identifiers=blocks, title=title, author=author)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(*_zip_entry("mimetype", b"application/epub+zip"))
        archive.writestr(*_zip_entry("META-INF/container.xml", CONTAINER_XML.encode()))
        archive.writestr(*_zip_entry("OEBPS/content.opf", opf.encode()))
        archive.writestr(*_zip_entry("OEBPS/chapter.xhtml", b"<html><body>hi</body></html>"))
        if filler:
            archive.writestr(*_zip_entry("OEBPS/filler.bin", filler))
    return buffer.getvalue()


@dataclass
class DeviceServer:
    """A device File Transfer server, in-process.

    `files` maps a card path (e.g. `/Author/Book.epub`) to its bytes. Directories are
    inferred from the paths, which is how the real listing behaves: the card has no
    empty directories that matter here.
    """

    files: dict[str, bytes]
    requests: list[str] = field(default_factory=list)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "DeviceServer":
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def address(self) -> str:
        assert self._server is not None, "DeviceServer must be used as a context manager"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def downloads(self) -> list[str]:
        """Every `/download` this server was asked for, in order."""
        return [r for r in self.requests if r.startswith("/download")]

    # -- the device's API --------------------------------------------------

    def _listing(self, path: str) -> list[dict[str, object]]:
        prefix = path.rstrip("/") + "/"
        seen: dict[str, dict[str, object]] = {}
        for full, body in self.files.items():
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            head, _, tail = rest.partition("/")
            if tail:
                seen.setdefault(head, _dir_entry(head))
            else:
                seen[head] = {
                    "name": head,
                    "size": len(body),
                    "isDirectory": False,
                    "isEpub": head.lower().endswith(".epub"),
                }
        return sorted(seen.values(), key=lambda e: str(e["name"]))

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:  # type: ignore[override]
                pass

            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
                parsed = urllib.parse.urlparse(self.path)
                outer.requests.append(self.path)
                if parsed.path == "/api/files":
                    params = urllib.parse.parse_qs(parsed.query)
                    listing = outer._listing(params.get("path", ["/"])[0])
                    body = json.dumps(listing).encode()
                    self._send(200, "application/json", body)
                elif parsed.path == "/download":
                    params = urllib.parse.parse_qs(parsed.query)
                    card_path = params.get("path", [""])[0]
                    if card_path in outer.files:
                        self._send(
                            200, "application/epub+zip", outer.files[card_path]
                        )
                    else:
                        self._send(404, "text/plain", b"Not Found")
                else:
                    self._send(404, "text/plain", b"Not Found")

            def _send(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def _dir_entry(name: str) -> dict[str, object]:
    return {"name": name, "size": 0, "isDirectory": True, "isEpub": False}
