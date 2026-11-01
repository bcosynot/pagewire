"""Session derivation and stitching — Vivek's rules written down as tests.

The design section "Derive and deliver sessions" is the spec; every test names the
criterion it is the evidence for. Times are epoch seconds. The fixtures are synthetic
except the one marked *captured*, which is the real `reader.session` from the spike
(`spike/events.jsonl` line 10) and is used where the criterion quotes its numbers.

Two conventions the fixtures follow deliberately:

* `ts` defaults to `end_time`, i.e. a session with no trailing idle. Where the trailing
  gap matters the test sets `ts` explicitly — otherwise a stray 32 seconds would land in
  `paused_seconds` and every "paused == N" assertion would be off by it.
* `received_at` defaults to `end_time`. Finalisation is measured from *delivery*, not from
  the reading, so the tests that care set it.

Run with `-s` to see the derived rows the criteria ask for as evidence.
"""

from __future__ import annotations

import json

from pagewire.sessions import derive, derive_and_store
from pagewire.storage import Storage

WINDOW = 30 * 60
CAP = 24 * 60 * 60
NOW = 2_000_000_000  # far past every fixture, so nothing finalises by accident

A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

# The real captured session. start_time/end_time/duration agree exactly, and `ts` is 32
# seconds after `end_time` — the session's own idle time, visible nowhere else.
CAPTURED_PAYLOAD = {
    "book": "/Samit Basu - The Jinn-Bot of Shantiport.epub",
    "document": "8f49706e45186c3185a9a7fcbcabe6f5",
    "duration_seconds": "2638",
    "start_time": "1793550349",
    "end_time": "1793552987",
    "start_progress_bp": "1799",
    "end_progress_bp": "2244",
    "progress_scale": "10000",
    "event": "reader.session",
    "id": "f55fb3e8-1",
    "ts": "1793553019",
    "v": "1",
}


def session(
    document,
    start_time,
    end_time,
    *,
    duration=None,
    ts=None,
    event_id=None,
    received_at=None,
    progress=(None, None),
    scale=10000,
    name="reader.session",
):
    """One raw event as the relay stores it: verbatim JSON body plus its arrival time."""
    duration = end_time - start_time if duration is None else duration
    ts = end_time if ts is None else ts
    payload = {
        "id": event_id or f"{document[:4]}-{start_time}",
        "event": name,
        "document": document,
        "book": f"/{document}.epub",
        "start_time": str(start_time),
        "end_time": str(end_time),
        "duration_seconds": str(duration),
        "ts": str(ts),
        "progress_scale": str(scale),
    }
    if progress[0] is not None:
        payload["start_progress_bp"] = str(progress[0])
    if progress[1] is not None:
        payload["end_progress_bp"] = str(progress[1])
    return {
        "id": payload["id"],
        "event": name,
        "received_at": end_time if received_at is None else received_at,
        "payload": json.dumps(payload),
    }


def captured(received_at=None):
    return {
        "id": CAPTURED_PAYLOAD["id"],
        "event": "reader.session",
        "received_at": 1793553019 if received_at is None else received_at,
        "payload": json.dumps(CAPTURED_PAYLOAD),
    }


def show(label, sittings):
    """Print the derived rows — several criteria name these as their evidence."""
    print(f"\n{label}")
    print(
        f"  {'document':10} {'started_at':>12} {'ended_at':>12} "
        f"{'active':>7} {'paused':>7} {'fin':>3}"
    )
    for s in sittings:
        print(
            f"  {s.document[:8]:10} {s.started_at:>12} {s.ended_at:>12} "
            f"{s.active_seconds:>7} {s.paused_seconds:>7} {int(s.finalised):>3}"
        )
    return sittings


def assert_ends_and_pause_pair(sitting):
    """The invariant: span minus pause is the device's own active seconds, never less.

    A naive pairing (`ended_at` = `end_time` with a non-zero pause) fails this by
    subtracting a pause from a span that already excludes it.
    """
    span = sitting.ended_at - sitting.started_at
    assert span - sitting.paused_seconds == sitting.active_seconds, (
        f"{sitting.document[:8]} {sitting.started_at}: span {span} - paused "
        f"{sitting.paused_seconds} != active {sitting.active_seconds}"
    )


def test_an_a_b_a_series_yields_two_sittings_in_a_and_the_pause_excludes_b():
    """Criterion 1: reading A, then B, then A again is two sittings in A, not one."""
    events = [
        session(A, 1000, 1100, duration=100),
        session(B, 1200, 1300, duration=100),
        session(A, 1400, 1500, duration=100),
    ]
    sittings = show("criterion 1: A -> B -> A", derive(events, stitch_window_seconds=WINDOW, now=NOW))

    in_a = [s for s in sittings if s.document == A]
    in_b = [s for s in sittings if s.document == B]

    assert len(in_a) == 2, "the gap contained another book, so it is a boundary, not a stitch"
    assert len(in_b) == 1
    assert [s.started_at for s in in_a] == [1000, 1400]

    # B's reading is not A's pause: A's pause only counts time when no other book was open.
    assert sum(s.paused_seconds for s in in_a) == 0
    for s in sittings:
        assert_ends_and_pause_pair(s)


def test_two_sessions_in_a_25_minutes_apart_stitch_into_one_sitting():
    """Criterion 2: a short gap with no other book in it is one sitting, and it is pause."""
    events = [
        session(A, 1000, 1300, duration=300),
        session(A, 2800, 3100, duration=300),  # 1500s == 25 min after the first ended
    ]
    sittings = show("criterion 2: 25-minute gap", derive(events, stitch_window_seconds=WINDOW, now=NOW))

    assert len(sittings) == 1
    (sitting,) = sittings
    assert sitting.started_at == 1000  # from the first
    assert sitting.ended_at == 3100  # from the last
    assert sitting.paused_seconds == 1500
    assert sitting.active_seconds == 600
    assert_ends_and_pause_pair(sitting)


def test_a_29_minute_gap_stitches_but_a_30_minute_gap_does_not():
    """Criterion 3: the boundary. At the window it is a boundary, below it a pause."""
    under = derive(
        [session(A, 1000, 1300), session(A, 3040, 3340)],  # 1740s == 29 min
        stitch_window_seconds=WINDOW,
        now=NOW,
    )
    at = derive(
        [session(A, 1000, 1300), session(A, 3100, 3400)],  # 1800s == 30 min
        stitch_window_seconds=WINDOW,
        now=NOW,
    )
    show("criterion 3: 29-minute gap (one sitting)", under)
    show("criterion 3: 30-minute gap (two sittings)", at)

    assert len(under) == 1
    assert under[0].paused_seconds == 1740

    assert len(at) == 2
    assert [s.started_at for s in at] == [1000, 3100]
    assert sum(s.paused_seconds for s in at) == 0


def test_a_sitting_is_finalised_when_the_stitch_window_elapses():
    """Criterion 4, case 1: quiet for the window is what makes a sitting deliverable."""
    events = [session(A, 1000, 1300, received_at=10_000)]

    early = derive(events, stitch_window_seconds=WINDOW, now=10_000 + WINDOW - 1)
    assert early[0].finalised is False

    elapsed = derive(events, stitch_window_seconds=WINDOW, now=10_000 + WINDOW)
    assert elapsed[0].finalised is True


def test_a_sitting_is_finalised_immediately_when_the_device_reports_the_book_finished():
    """Criterion 4, case 2: finishing the book finalises now, not after the window."""
    events = [
        session(A, 1000, 1300, received_at=10_000, progress=(9000, 10000), scale=10000),
    ]
    sittings = derive(events, stitch_window_seconds=WINDOW, now=10_000)

    assert sittings[0].finalised is True


def test_a_plain_reader_exit_does_not_finalise_a_sitting():
    """Criterion 4, case 3: leaving the reader is the common case and must stay open.

    Treating an exit as final would contradict the rule this whole ticket encodes —
    A, put it down, come back four minutes later is still one sitting.
    """
    events = [
        session(A, 1000, 1300, received_at=10_000),
        session(A, 1000, 1300, event_id="exit-1", received_at=10_010, name="reader.exit"),
    ]
    sittings = derive(events, stitch_window_seconds=WINDOW, now=10_010)

    assert len(sittings) == 1
    assert sittings[0].finalised is False


def test_a_stitched_span_over_24_hours_splits_into_sittings_within_the_cap():
    """Criterion 5: Readwise caps a session at 24 hours, so a long span is split."""
    # Reading roughly every 20 minutes for 30 hours: one continuous stitched span.
    events = [
        session(A, 1000 + 1200 * i, 1000 + 1200 * i + 600, duration=600)
        for i in range(91)  # 90 gaps of 600s idle, spanning 30 hours
    ]
    sittings = show("criterion 5: 30-hour span", derive(events, stitch_window_seconds=WINDOW, now=NOW))

    assert len(sittings) > 1
    for s in sittings:
        assert s.ended_at - s.started_at <= CAP
        assert_ends_and_pause_pair(s)

    # Nothing is lost in the split: every session is a constituent of exactly one sitting.
    assert sum(len(s.event_ids) for s in sittings) == len(events)


def test_re_running_derivation_over_the_same_events_produces_identical_sittings():
    """Criterion 6: derivation is idempotent, so a replay cannot create a second sitting."""
    storage = Storage(":memory:")
    events = [
        session(A, 1000, 1300, received_at=10_000),
        session(A, 2800, 3100, received_at=10_000),
        session(B, 4000, 4300, received_at=10_000),
    ]
    for event in events:
        storage.store_event(json.loads(event["payload"]), received_at=event["received_at"])

    first = derive_and_store(storage, stitch_window_seconds=WINDOW, now=NOW)
    second = derive_and_store(storage, stitch_window_seconds=WINDOW, now=NOW)

    assert first == second
    rows = storage.sittings()
    assert len(rows) == len(first) == 2
    assert len(storage.sittings()) == 2, "a replay must replace, not append"


def test_the_real_captured_trailing_gap_is_a_pause():
    """Criterion 7, case 1: the 32 seconds between the last page turn and the flush.

    This is the captured session (start 1793550349, end 1793552987, ts 1793553019), so the
    numbers are the device's, not a fixture's.
    """
    sittings = show("criterion 7: the captured session", derive([captured()], stitch_window_seconds=WINDOW, now=NOW))

    assert len(sittings) == 1
    (sitting,) = sittings
    assert sitting.started_at == 1793550349
    assert sitting.ended_at == 1793553019, "a short trailing gap is a pause, so the end is the flush"
    assert sitting.paused_seconds == 32
    assert sitting.active_seconds == 2638
    assert_ends_and_pause_pair(sitting)


def test_a_trailing_gap_of_thirty_minutes_or_more_is_discarded():
    """Criterion 7, case 2: the same window applied to the trailing gap. Long ends it."""
    events = [session(A, 1000, 1300, duration=300, ts=1300 + WINDOW)]
    sittings = show("criterion 7: a 30-minute trailing gap", derive(events, stitch_window_seconds=WINDOW, now=NOW))

    assert len(sittings) == 1
    (sitting,) = sittings
    assert sitting.ended_at == 1300, "a long trailing gap is a boundary: end at the last page turn"
    assert sitting.paused_seconds == 0
    assert_ends_and_pause_pair(sitting)


def test_the_ends_and_the_pause_always_pair():
    """Criterion 8: the invariant, over one of each shape the stitcher produces.

    `(ended_at - started_at) - paused_seconds` must equal the device's own active seconds.
    A naive pairing reports less reading than the device measured.
    """
    events = [
        captured(),
        session(A, 1000, 1300, duration=300, ts=1310),  # a short trailing gap
        session(A, 2800, 3100, duration=300),  # stitched on
        session(B, 4000, 4300, duration=300),  # a different book, its own sitting
        session(A, 6000, 6300, duration=300, ts=6300 + WINDOW),  # a long trailing gap
    ]
    sittings = show("criterion 8: mixed", derive(events, stitch_window_seconds=WINDOW, now=NOW))

    for sitting in sittings:
        assert_ends_and_pause_pair(sitting)


def test_a_sitting_carries_the_devices_finished_flag():
    """Delivery's criterion 5 needs this fact, and it is the device's to state.

    `end_percent == 100` (or a position at the whole scale) is the firmware saying the book
    was finished. The derivation carries it onto the sitting so the delivery step does not
    have to re-read raw events — and so 100 % and 99 % are distinguishable at all.
    """
    at_end = derive(
        [session(A, 1000, 1300, duration=300, progress=(0, 10_000), scale=10_000)],
        stitch_window_seconds=WINDOW,
        now=NOW,
    )
    nearly = derive(
        [session(A, 1000, 1300, duration=300, progress=(0, 9_900), scale=10_000)],
        stitch_window_seconds=WINDOW,
        now=NOW,
    )

    assert [sitting.finished for sitting in at_end] == [True]
    assert [sitting.finished for sitting in nearly] == [False]


def test_finishing_a_book_survives_stitching():
    """A sitting that stitched several firmware sessions is finished if any of them was."""
    events = [
        session(A, 1000, 1300, duration=300),
        session(A, 1500, 1800, duration=300, progress=(0, 10_000), scale=10_000),
    ]
    sittings = derive(events, stitch_window_seconds=WINDOW, now=NOW)

    assert len(sittings) == 1
    assert sittings[0].finished is True
