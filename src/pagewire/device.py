"""Talking to the reader's own File Transfer web server.

Crosspoint publishes the card over HTTP while the reader is in File Transfer or Calibre
Wireless mode: `GET /api/files?path=…` for the listing and `GET /download?path=…` for a
file. Both answer without credentials (confirmed on hardware — see the device spike's
evidence), and both are **unavailable during a normal reading session**, because the
server is not running then.

That mode-dependence is the whole reason this is a separate, triggered path rather than
part of ingest: a sweep can only run while the owner has the reader in a mode he already
uses to load books.

Every failure here is a `DeviceError` with the address in the message. "The reader is in
reading mode" is the ordinary case, not an exceptional one, and it should read like a
state the caller can act on rather than a traceback.
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DeviceError(RuntimeError):
    """The reader's web server could not be reached, or refused the request."""


@dataclass(frozen=True)
class CardEntry:
    """One entry in the device's card listing."""

    path: str
    name: str
    size: int
    is_directory: bool
    is_epub: bool


class DeviceClient:
    """The device's File Transfer server, as seen from the relay."""

    def __init__(self, address: str, timeout: float = 30.0) -> None:
        self.address = normalise_address(address)
        self.timeout = timeout

    def list_files(self, path: str = "/") -> list[CardEntry]:
        """List one directory. Directories are not walked here — see `sweep.list_epubs`."""
        body = self._get_json(f"/api/files?path={urllib.parse.quote(path)}")
        if not isinstance(body, list):
            raise DeviceError(f"{self.address} returned a listing that is not a list")
        return [self._entry(path, item) for item in body if isinstance(item, dict)]

    def download(self, path: str, destination: str | Path) -> int:
        """Fetch one file to `destination` and return the number of bytes written.

        Streamed in chunks rather than read into memory: the card carries EPUBs over
        40 MB, and the relay has no reason to hold one whole.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.address}/download?path={urllib.parse.quote(path)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                with destination.open("wb") as handle:
                    shutil.copyfileobj(response, handle, length=64 * 1024)
        except urllib.error.HTTPError as exc:
            raise DeviceError(
                f"{self.address} refused {path!r}: HTTP {exc.code} {exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DeviceError(f"could not reach {self.address}: {exc}") from exc
        return destination.stat().st_size

    # -- internals ---------------------------------------------------------

    def _get_json(self, path: str) -> Any:
        url = f"{self.address}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise DeviceError(
                f"{self.address} refused {path!r}: HTTP {exc.code} {exc.reason}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise DeviceError(f"could not reach {self.address}: {exc}") from exc

    def _entry(self, parent: str, item: dict[str, Any]) -> CardEntry:
        name = str(item.get("name", ""))
        return CardEntry(
            path=join_card_path(parent, name),
            name=name,
            size=int(item.get("size") or 0),
            is_directory=bool(item.get("isDirectory")),
            is_epub=bool(item.get("isEpub")),
        )


def normalise_address(address: str) -> str:
    """Accept `192.168.0.217`, `crosspoint.local` or a full URL; store a base URL."""
    address = address.strip().rstrip("/")
    if not address:
        raise DeviceError("no device address configured")
    if not address.startswith(("http://", "https://")):
        address = f"http://{address}"
    return address


def join_card_path(parent: str, name: str) -> str:
    """`("/Kuang, R. F_", "Dragon.epub")` -> `/Kuang, R. F_/Dragon.epub`."""
    if parent in ("", "/"):
        return f"/{name}"
    return f"{parent.rstrip('/')}/{name}"
