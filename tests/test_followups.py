"""A follow-up question must be answerable, including by correcting it.

The operational benchmark found a run that asked "Did you mean ...?" forever
because a reply naming a state was written to a field called `confirm_authority`
and the question was re-asked. These tests pin the three useful replies: yes, a
different jurisdiction, and something unrecognisable (which must not invent one).
"""

from __future__ import annotations

from agent import supervisor


def start_generic() -> str:
    run_id, _ = supervisor.start_run(
        "How much was spent on road repair work in Gujarat during 2025-26?"
    )
    return run_id


def test_a_generic_jurisdiction_asks_for_confirmation():
    detail = supervisor.detail(start_generic())
    assert detail["run"]["status"] == "awaiting_input"
    assert detail["run"]["missing_evidence"] == "confirm_authority"
    assert "Did you mean" in detail["run"]["pending_question"]


def test_yes_accepts_the_suggested_office():
    run_id = start_generic()
    detail = supervisor.answer(run_id, "Yes, that is the right office.")
    routing = [d for d in detail["decisions"] if d["agent"] == "routing"][-1]
    assert routing["label"] == "routed"
    assert routing["output"].get("confirmed") is True
    assert detail["run"]["status"] == "awaiting_approval"


def test_a_named_state_corrects_the_jurisdiction_instead_of_looping():
    run_id = start_generic()
    detail = supervisor.answer(run_id, "Maharashtra")
    routing = [d for d in detail["decisions"] if d["agent"] == "routing"][-1]
    assert routing["output"]["authority"] == "MH-PWD-01"
    assert detail["fields"]["state"]["value"] == "Maharashtra"
    assert detail["run"]["status"] == "awaiting_approval"


def test_an_unrecognised_reply_does_not_invent_a_route():
    run_id = start_generic()
    detail = supervisor.answer(run_id, "I am not sure what you mean.")
    assert detail["run"]["status"] == "awaiting_input"
    assert detail["run"]["missing_evidence"] == "confirm_authority"
    assert detail["approval"] is None
