"""The annotation UI must be usable by a non-engineer without a data incident.

These tests drive the delivered routes: consent is enforced, identifiers are
redacted on arrival, a label is stored against the annotator's name, skipping
stores nothing, and a disagreement can be adjudicated exactly once.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts import annotate_server as server

TEXT = "How much was spent on road repairs in Ward 12, Pune during 2025-26?"


@pytest.fixture()
def store(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "citizen-annotations.jsonl"
    monkeypatch.setenv("RTI_CITIZEN_ANNOTATIONS", str(path))
    return path


@pytest.fixture()
def client(store):
    with TestClient(server.app) as test_client:
        yield test_client


def rows(store: Path) -> list[dict]:
    if not store.exists():
        return []
    return [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines() if line.strip()]


def contribute(client, text: str = TEXT, consent: str = "yes") -> None:
    response = client.post(
        "/contribute",
        data={"text": text, "source": "test filing", "language": "en", "consent": consent},
        follow_redirects=False,
    )
    assert response.status_code == 200


def test_consent_is_required(client, store):
    contribute(client, consent="")
    assert rows(store) == []
    assert "not added" in client.post(
        "/contribute", data={"text": TEXT, "consent": ""}, follow_redirects=False
    ).text.lower()


def test_submission_is_redacted_and_stored_once(client, store):
    contribute(client, text="Call me on 9876543210 about road repairs in Ward 12, Pune.")
    stored = rows(store)
    assert len(stored) == 1
    assert stored[0]["redactions"] == 1
    assert "[PHONE]" in stored[0]["text"]
    assert "9876543210" not in stored[0]["text"]
    assert stored[0]["consent"] is True
    assert stored[0]["text_sha256"]


def test_duplicate_text_is_not_added_twice(client, store):
    contribute(client)
    contribute(client, text=TEXT.upper())
    assert len(rows(store)) == 1


def test_labelling_records_the_annotator_and_advances(client, store):
    contribute(client)
    contribute(client, text="Please share the home address and phone number of the ward officer.")
    assert "Ward 12" in client.get("/label?annotator=A").text
    client.post("/label/A/CIT-0001", data={"value": "yes"}, follow_redirects=False)

    stored = rows(store)
    assert stored[0]["labels"]["A"] is True
    progress = client.get("/api/progress").json()["progress"]
    assert progress["labelled_by_annotator"]["A"] == 1
    assert progress["pairs"] == 0

    # A labelled row is not shown to the same annotator again.
    page = client.get("/label?annotator=A").text
    assert "CIT-0001" not in page
    assert "CIT-0002" in page


def test_skipping_stores_nothing(client, store):
    contribute(client)
    client.post("/label/A/CIT-0001", data={"value": "skip"}, follow_redirects=False)
    assert rows(store)[0]["labels"] == {}


def test_two_annotators_can_disagree_then_adjudicate(client, store):
    contribute(client, text="Please share the home address and phone number of the ward officer.")
    client.post("/label/A/CIT-0001", data={"value": "yes"}, follow_redirects=False)
    client.post("/label/B/CIT-0001", data={"value": "no"}, follow_redirects=False)

    progress = client.get("/api/progress").json()
    assert progress["progress"]["disagreements_open"] == 1
    assert "A: yes" in client.get("/adjudicate?name=C").text

    client.post(
        "/adjudicate/C/CIT-0001",
        data={"value": "yes", "note": "Asks for a named officer's home address."},
        follow_redirects=False,
    )
    stored = rows(store)[0]
    assert stored["adjudicated"] is True
    assert stored["adjudicator"] == "C"
    assert "home address" in stored["adjudication_note"]
    assert "Nothing to adjudicate" in client.get("/adjudicate?name=C").text

    again = client.post(
        "/adjudicate/C/CIT-0001", data={"value": "no", "note": "changed"}, follow_redirects=False
    )
    assert "already adjudicated" in again.text


def test_the_status_page_states_the_question_and_the_target(client):
    page = html.unescape(client.get("/").text)
    assert "Citizen-request annotation" in page
    assert "another person's private information" in page
    assert "100" in page
    assert client.get("/healthz").json()["status"] == "ok"
