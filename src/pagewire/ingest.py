"""Reading what the device sends: normalising it, then refusing what cannot be trusted.

Two rules live here, both from things the hardware actually did during the spike rather
than from the documentation.

**Placeholders are absent.** When the firmware has no value for a variable it substitutes
nothing, so the literal text `{event.book}` arrives over the wire — a `sleep.enter` from
outside a reader sends three of them. Reading `{event.end_percent}` as 0 would make a book
look unread and fire a finish test wrongly, so every placeholder becomes `None` here,
once, rather than being guarded against at each use.

**Duplicates are normal.** Delivery is at-least-once, and the spike caught the device
sending the same event twice with an identical id. So a repeat is not an error and must
not be rejected: it is the contract.
"""

from __future__ import annotations

from typing import Any

PLACEHOLDER_PREFIX = "{event."


class IngestError(ValueError):
    """A payload we will not store, with the reason a caller can act on."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def is_placeholder(value: Any) -> bool:
    """True for the literal text the firmware leaves when it has no value."""
    return isinstance(value, str) and value.startswith(PLACEHOLDER_PREFIX)


def normalise(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the payload with every unsubstituted placeholder replaced by None."""
    return {key: (None if is_placeholder(value) else value) for key, value in payload.items()}


def as_int(value: Any) -> int | None:
    """Coerce a device-supplied numeric string, treating anything unusable as absent.

    Every value arrives as a string. `ts` is the trap that made this necessary: it is a
    per-boot counter rather than a wall clock, so it stays a string and is never parsed
    into a time here.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    """Check the two fields the relay cannot work without, and return the payload.

    Deliberately narrow. The relay stores raw events verbatim, so anything it does not
    understand is kept for later rather than dropped — but an event with no id cannot be
    deduplicated and one with no name cannot be routed, so those two are required and
    everything else is the device's business.
    """
    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise IngestError("missing or empty event id", field="id")

    event = payload.get("event")
    if not isinstance(event, str) or not event.strip():
        raise IngestError("missing or empty event name", field="event")

    return payload
