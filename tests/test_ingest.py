"""Ingest tests, written against what the hardware actually sends.

The fixture payloads are copied from the spike's capture (`spike/events.jsonl`), not
invented: the session event is the one real reading session, and the placeholder case is a
real `sleep.enter` from outside a reader.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pagewire.app import create_app
from pagewire.config import Settings
from pagewire.storage import Storage

TOKEN = "test-token"

# Captured from a real device, 2026-10-31: a full reading session.
REAL_SESSION = {
    "v": "1",
    "event": "reader.session",
    "id": "f55fb3e8-1",
    "ts": "1793553019",
    "book": "/Samit Basu - The Jinn-Bot of Shantiport.epub",
    "document": "8f49706e45186c3185a9a7fcbcabe6f5",
    "start_time": "1793550349",
    "end_time": "1793552987",
    "duration_seconds": "2638",
    "start_progress_bp": "1799",
    "end_progress_bp": "2244",
    "progress_scale": "10000",
}

# Also captured: a sleep from outside a reader, where the firmware had no values and sent
# the template text through untouched.
SLEEP_OUTSIDE_READER = {
    "v": "1",
    "event": "sleep.enter",
    "id": "1400c035-1",
    "ts": "1793550315",
    "book": "{event.book}",
    "percent": "{event.percent}",
    "end_percent": "{event.end_percent}",
}


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"db_path": str(tmp_path / "pagewire.db"), "device_token": TOKEN}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))


def post(client: TestClient, payload: dict, token: str = TOKEN):
    return client.post(
        "/events/session",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_the_same_event_twice_leaves_one_row(settings, client):
    """Delivery is at-least-once, so the same id will arrive again. It must not duplicate."""
    first = post(client, REAL_SESSION)
    second = post(client, REAL_SESSION)

    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "stored": True, "id": "f55fb3e8-1"}
    # A repeat is acknowledged, not rejected: the device's outbox clears on a 2xx.
    assert second.status_code == 200, second.text
    assert second.json() == {"ok": True, "stored": False, "id": "f55fb3e8-1"}

    assert Storage(settings.db_path).count_events() == 1


def test_idempotence_survives_a_restart(tmp_path, settings, client):
    """A restart between two deliveries must not turn the second one into a new event."""
    assert post(client, REAL_SESSION).json()["stored"] is True

    # Same file, new process as far as the app is concerned.
    restarted = TestClient(create_app(make_settings(tmp_path)))
    again = post(restarted, REAL_SESSION)

    assert again.status_code == 200
    assert again.json()["stored"] is False
    assert Storage(settings.db_path).count_events() == 1


def test_an_event_without_an_id_is_refused(settings, client):
    payload = {k: v for k, v in REAL_SESSION.items() if k != "id"}

    response = post(client, payload)

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "id"
    assert Storage(settings.db_path).count_events() == 0


def test_an_event_without_a_name_is_refused(settings, client):
    payload = {k: v for k, v in REAL_SESSION.items() if k != "event"}

    response = post(client, payload)

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "event"
    assert Storage(settings.db_path).count_events() == 0


def test_a_bad_token_is_refused(settings, client):
    response = post(client, REAL_SESSION, token="not-the-token")

    assert response.status_code == 401
    assert Storage(settings.db_path).count_events() == 0


def test_an_unconfigured_token_refuses_everything(tmp_path):
    """No credential configured is a misconfiguration, and must not mean 'open house'."""
    client = TestClient(create_app(make_settings(tmp_path, device_token="")))

    response = post(client, REAL_SESSION, token="")

    assert response.status_code == 503
    assert "no device token configured" in response.json()["detail"]


def test_unsubstituted_placeholders_are_stored_as_absent(settings, client):
    """The hardware sends literal `{event.…}` text when it has no value for a variable.

    Storing that text as if it were a value is how a book ends up looking 0% read, so it
    is normalised to NULL on the way in.
    """
    assert post(client, SLEEP_OUTSIDE_READER).status_code == 200

    row = Storage(settings.db_path).events()[0]
    assert row["event"] == "sleep.enter"
    assert row["book"] is None
    # The raw payload is kept verbatim, placeholder text and all, for later inspection.
    assert "{event.book}" in row["payload"]


def test_values_and_payload_are_stored(settings, client):
    post(client, REAL_SESSION)

    row = Storage(settings.db_path).events()[0]
    assert row["document"] == "8f49706e45186c3185a9a7fcbcabe6f5"
    assert row["book"] == "/Samit Basu - The Jinn-Bot of Shantiport.epub"
    # ts is stored as text and never parsed: it is a per-boot counter, not a time.
    assert row["ts_raw"] == "1793553019"


def test_healthz_reports_destinations_off_by_default(client):
    body = client.get("/healthz").json()

    assert body["ok"] is True
    assert body["events"] == 0
    assert body["destinations"] == {"readwise": False, "hardcover": False}
