"""Turning raw firmware events into the sittings Vivek described.

The device ends a session at sleep or exit, which is not the same thing as a sitting. This
module holds the reconciliation, and it is arithmetic on the device's own numbers rather
than a guess at them: nothing here invents a time.

The rules, all from the design section "Derive and deliver sessions":

* Consecutive firmware sessions for the same book whose gap is **less than the stitch
  window** become one sitting. `started_at` is the first `start_time`, `ended_at` is the
  last `end_time`, `active_seconds` is the sum of the constituent `duration_seconds`, and
  the intervening gaps are `paused_seconds`. A gap at or above the window is a boundary.
* **A different book read inside the gap is a boundary too**, however short it was
  (Vivek, 2026-10-31): reading A, putting it down, reading B and coming back to A is
  *two* sittings in A, and pause "only counts time when no other book was open".
* **The trailing gap gets the same window.** The firmware compacts idle gaps out of
  `duration_seconds`, so the gap between the last counted page turn (`end_time`) and the
  event's own emission (`ts`) is the only surviving evidence of idle time. Under the
  window it is a pause, so `ended_at` = `ts`; at or over it, the sitting ended at the last
  page turn and the gap is discarded. "Short gaps are pauses, long pauses end sittings."
* A span over Readwise's 24-hour cap is split, because Readwise would refuse it.

The ends and the pause are always paired so that `(ended_at - started_at) -
paused_seconds` equals the sum of the constituent `duration_seconds` — exactly, never
less. That identity is what makes the two readings of the trailing gap distinguishable:
`end_time - start_time` already equals the active seconds, so pairing `ended_at` =
`end_time` with a non-zero pause would report less reading than the device measured.

**`ts` is never read as a time.** It is a per-boot counter when the device's clock was
never set (61, 139, 1268, 42 across the spike's three boots), and only a real epoch once
SNTP has run. It is used here as a *difference* from `end_time`, and only when it lands
after it — on an unset clock the subtraction yields a huge negative number, which reads
as "no trailing gap" rather than dating a sitting to 1970.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .ingest import as_int, is_placeholder

DEFAULT_STITCH_WINDOW_SECONDS = 30 * 60

# Readwise refuses a session whose `ended_at` is more than 24 hours after its `started_at`.
READWISE_MAX_SPAN_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class Sitting:
    """One derived reading sitting, ready to be stored and later delivered."""

    document: str
    started_at: int
    ended_at: int
    active_seconds: int
    paused_seconds: int
    start_progress_bp: int | None
    end_progress_bp: int | None
    finalised: bool
    event_ids: tuple[str, ...]
    # The device's own `end_percent == 100`: the book was finished. Carried through the
    # derivation rather than re-derived at delivery time, because the flag belongs to the
    # firmware session that saw it and the raw events are not re-read on the delivery path.
    finished: bool = False


@dataclass(frozen=True)
class _Session:
    """One `reader.session` event, parsed. Not every raw event becomes one of these."""

    event_id: str
    document: str
    start_time: int
    end_time: int
    duration: int
    trailing: int | None
    start_progress_bp: int | None
    end_progress_bp: int | None
    finished: bool
    received_at: int


def parse_session(raw: Any) -> _Session | None:
    """Read one stored raw event as a session, or return None if it is not one.

    None is the answer for anything that is not a `reader.session`, and for a session
    missing the fields that cannot be invented: the document it belongs to, and when it
    started and ended. Everything else is optional because the device has been observed
    to leave fields unsubstituted, and a placeholder must read as absent rather than as 0.
    """
    payload = _payload(raw)
    if payload is None or payload.get("event") != "reader.session":
        return None

    document = _text(payload.get("document"))
    start_time = as_int(payload.get("start_time"))
    end_time = as_int(payload.get("end_time"))
    if document is None or start_time is None or end_time is None:
        return None

    duration = as_int(payload.get("duration_seconds"))
    if duration is None:
        duration = max(end_time - start_time, 0)

    # Only meaningful as a difference, and only when it lands after the reading ended.
    ts = as_int(payload.get("ts"))
    trailing = ts - end_time if ts is not None and ts >= end_time else None

    scale = as_int(payload.get("progress_scale"))
    end_progress_bp = as_int(payload.get("end_progress_bp"))
    finished = as_int(payload.get("end_percent")) == 100 or (
        scale is not None and end_progress_bp is not None and end_progress_bp >= scale
    )

    event_id = payload.get("id")
    return _Session(
        event_id=event_id if isinstance(event_id, str) and event_id else str(_field(raw, "id", "")),
        document=document,
        start_time=start_time,
        end_time=end_time,
        duration=duration,
        trailing=trailing,
        start_progress_bp=as_int(payload.get("start_progress_bp")),
        end_progress_bp=end_progress_bp,
        finished=finished,
        received_at=as_int(_field(raw, "received_at")) or 0,
    )


def derive(
    events: Iterable[Any],
    *,
    now: int,
    stitch_window_seconds: int = DEFAULT_STITCH_WINDOW_SECONDS,
    max_span_seconds: int = READWISE_MAX_SPAN_SECONDS,
) -> list[Sitting]:
    """Derive every sitting implied by `events`.

    Pure: the same events and the same `now` give the same sittings, in the same order.
    Nothing is written, so a derivation can be replayed over the whole history whenever a
    rule changes.

    `now` is a parameter rather than a call to the clock because finalisation is a
    statement about the present — passing it in keeps derivation reproducible, which is
    what makes a replay safe.
    """
    sessions = [s for s in (parse_session(event) for event in events) if s is not None]
    sessions.sort(key=lambda s: (s.start_time, s.end_time, s.event_id))

    by_document: dict[str, list[_Session]] = {}
    for session in sessions:
        by_document.setdefault(session.document, []).append(session)

    sittings: list[Sitting] = []
    for document in sorted(by_document):
        grouped = _stitch(document, by_document[document], sessions, stitch_window_seconds)
        for chunk in _split_to_cap(grouped, stitch_window_seconds, max_span_seconds):
            sittings.append(_build(document, chunk, stitch_window_seconds, now))
    return sittings


def derive_and_store(
    storage: Any,
    *,
    now: int | None = None,
    stitch_window_seconds: int = DEFAULT_STITCH_WINDOW_SECONDS,
    max_span_seconds: int = READWISE_MAX_SPAN_SECONDS,
) -> list[Sitting]:
    """Re-derive from every stored raw event and replace the derived set.

    Derivation is a function of the raw events, so the derived table is a cache and is
    replaced wholesale rather than appended to. That is what makes a replay idempotent:
    running it twice cannot produce a second sitting for the same reading.
    """
    sittings = derive(
        storage.events(),
        now=int(time.time()) if now is None else now,
        stitch_window_seconds=stitch_window_seconds,
        max_span_seconds=max_span_seconds,
    )
    storage.replace_sittings(sittings)
    return sittings


def _stitch(
    document: str,
    group: Sequence[_Session],
    all_sessions: Sequence[_Session],
    window: int,
) -> list[list[tuple[_Session, int]]]:
    """Group one document's sessions into sittings, keeping each gap that became a pause."""
    sittings: list[list[tuple[_Session, int]]] = []
    current: list[tuple[_Session, int]] = []

    for session in group:
        if not current:
            current = [(session, 0)]
            continue

        previous = current[-1][0]
        gap = session.start_time - previous.end_time
        if gap < window and not _another_book_inside(
            document, previous.end_time, session.start_time, all_sessions
        ):
            current.append((session, gap))
        else:
            sittings.append(current)
            current = [(session, 0)]

    if current:
        sittings.append(current)
    return sittings


def _another_book_inside(
    document: str,
    start: int,
    end: int,
    all_sessions: Sequence[_Session],
) -> bool:
    """True when some other book was being read during (start, end).

    A gap with another book in it is a boundary however short it is, because the time in
    it was spent reading something else and so is not a pause in this book.
    """
    return any(
        other.document != document and other.start_time < end and other.end_time > start
        for other in all_sessions
    )


def _split_to_cap(
    grouped: Sequence[Sequence[tuple[_Session, int]]],
    window: int,
    max_span: int,
) -> list[list[tuple[_Session, int]]]:
    """Split any sitting longer than Readwise's cap, at a session boundary.

    Readwise refuses a session whose span exceeds 24 hours, and the device can easily
    produce a longer continuous one. Splitting at a session boundary keeps every
    constituent session whole, so the arithmetic still telescopes within each part.
    """
    chunks: list[list[tuple[_Session, int]]] = []

    for group in grouped:
        current: list[tuple[_Session, int]] = []
        for session, gap in group:
            if current and (_end_of(session, window) - current[0][0].start_time) > max_span:
                chunks.append(current)
                current = []
                gap = 0  # the boundary between chunks is not a pause in either
            current.append((session, gap))
        if current:
            chunks.append(current)

    return chunks


def _end_of(session: _Session, window: int) -> int:
    """The sitting's `ended_at` if it ended at this session — trailing gap included."""
    if session.trailing is not None and session.trailing < window:
        return session.end_time + session.trailing
    return session.end_time


def _build(
    document: str,
    chunk: Sequence[tuple[_Session, int]],
    window: int,
    now: int,
) -> Sitting:
    """Turn one group of sessions into a `Sitting`, applying the trailing-gap rule."""
    first = chunk[0][0]
    last = chunk[-1][0]

    stitched_pause = sum(gap for _, gap in chunk[1:])
    if last.trailing is not None and last.trailing < window:
        ended_at = last.end_time + last.trailing
        paused_seconds = stitched_pause + last.trailing
    else:
        ended_at = last.end_time
        paused_seconds = stitched_pause

    # Finalisation is a statement about delivery, not about reading: a sitting is eligible
    # once no new constituent event has arrived for the window. A plain `reader.exit` is
    # deliberately not a finaliser — A, put down, resumed four minutes later is one
    # sitting, and the device reports an exit every time it leaves the reader.
    last_received_at = max(session.received_at for session, _ in chunk)
    finalised = last.finished or (now - last_received_at) >= window

    return Sitting(
        document=document,
        started_at=first.start_time,
        ended_at=ended_at,
        active_seconds=sum(session.duration for session, _ in chunk),
        paused_seconds=paused_seconds,
        start_progress_bp=first.start_progress_bp,
        end_progress_bp=last.end_progress_bp,
        finalised=finalised,
        event_ids=tuple(session.event_id for session, _ in chunk),
        # Any constituent reporting 100 % finished the book — the device says this on the
        # session that reached the end, and a stitched sitting keeps the fact.
        finished=any(session.finished for session, _ in chunk),
    )


def _payload(raw: Any) -> Mapping[str, Any] | None:
    """The verbatim body of a stored raw event, as a mapping."""
    body: Any = _field(raw, "payload")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return None
    return body if isinstance(body, Mapping) else None


def _field(raw: Any, name: str, default: Any = None) -> Any:
    """One column of a stored event, whether it is a dict or a `sqlite3.Row`.

    The row type is deliberately not narrowed to a mapping: `Storage.events()` hands back
    `sqlite3.Row`, which supports `row[name]` but neither `.get()` nor a `Mapping`
    subclass check, so both shapes are read through this one accessor.
    """
    try:
        return raw[name]
    except (KeyError, IndexError, TypeError):
        return default


def _text(value: Any) -> str | None:
    """A device string, or None when it sent nothing usable.

    Placeholders read as absent: the firmware writes the literal `{event.book}` when it
    has no value, and a document id of `{event.document}` would otherwise key a real row.
    """
    if not isinstance(value, str) or is_placeholder(value):
        return None
    return value or None
