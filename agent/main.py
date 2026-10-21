"""RTI Pilot agent HTTP application and dashboard host.

Run: uvicorn agent.main:app --port 8000
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import db, supervisor
from common import config

app = FastAPI(title="RTI Pilot Agent", docs_url="/api/docs", redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    conn = db.init_db()
    conn.close()


class RunRequest(BaseModel):
    text: str = Field(min_length=2)
    language: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class AnswerRequest(BaseModel):
    text: str = Field(min_length=1)


class DeclineRequest(BaseModel):
    reason: str = ""


class TestResultIn(BaseModel):
    test_id: str
    run_id: str
    name: str
    input: str
    expected: str
    actual: str
    passed: bool
    evidence_ref: str = ""
    notes: str = ""


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "rti-pilot-agent"}


@app.get("/api/ready")
def ready() -> Any:
    """Readiness beyond liveness: the database opens and the portal answers.

    Kept separate from /api/health so the platform's health check cannot fail
    because a dependency is briefly unavailable, and so a deployment that points
    at a placeholder URL is visible instead of failing silently on first filing.
    """
    portal_ok = False
    detail = ""
    try:
        response = httpx.get(f"{config.PORTAL_URL}/healthz", timeout=3.0)
        portal_ok = response.status_code == 200
        detail = f"portal {response.status_code}"
    except Exception as exc:  # network, DNS, timeout
        detail = f"portal unreachable ({type(exc).__name__})"

    db_ok = True
    try:
        conn = db.init_db()
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        db_ok = False
        detail = f"{detail}; database unavailable".lstrip("; ")

    payload = {
        "ready": portal_ok and db_ok,
        "portal_url": config.PORTAL_URL,
        "detail": detail,
    }
    if not payload["ready"]:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/config")
def app_config() -> dict[str, str]:
    return {"portal_url": config.PORTAL_URL}


@app.get("/api/rules")
def rules_summary() -> dict[str, Any]:
    from agent import rules

    return {"version": rules.rules().get("version"), "disclaimer": rules.rules().get("disclaimer"),
            "sections": rules.rules().get("sections")}


@app.post("/api/runs")
def create_run(payload: RunRequest) -> dict[str, Any]:
    run_id = supervisor.start_run(payload.text, payload.language, payload.overrides)
    return supervisor.detail(run_id)


@app.get("/api/runs")
def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (max(1, min(200, limit)),)))
        result = []
        for row in rows:
            detail = db.run_detail(conn, row["run_id"])
            result.append({
                "run_id": row["run_id"],
                "status": row["status"],
                "language": row["language"],
                "raw_input": row["raw_input"],
                "created_at": row["created_at"],
                "reg_no": row["reg_no"],
                "triage": next((d["label"] for d in detail["decisions"] if d["agent"] == "triage"), None),
                "tracker": detail["tracker"],
            })
        return result
    finally:
        conn.close()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return supervisor.detail(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")


@app.post("/api/runs/{run_id}/answer")
def answer_run(run_id: str, payload: AnswerRequest) -> dict[str, Any]:
    try:
        return supervisor.answer(run_id, payload.text)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/runs/{run_id}/approve")
def approve_run(run_id: str) -> dict[str, Any]:
    try:
        return supervisor.approve(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/runs/{run_id}/decline")
def decline_run(run_id: str, payload: DeclineRequest) -> dict[str, Any]:
    try:
        return supervisor.decline(run_id, payload.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/runs/{run_id}/captcha-complete")
def captcha_complete(run_id: str) -> dict[str, Any]:
    try:
        return supervisor.complete_captcha(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")


@app.post("/api/runs/{run_id}/track")
def track_run(run_id: str) -> dict[str, Any]:
    try:
        return supervisor.track(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/runs/{run_id}/approve-appeal")
def approve_appeal(run_id: str) -> dict[str, Any]:
    try:
        return supervisor.approve_appeal(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")


@app.get("/api/review-queue")
def review_queue() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute(
            "SELECT run_id, raw_input, created_at FROM runs WHERE status='review_queue' ORDER BY created_at DESC"
        ))
        result = []
        for row in rows:
            detail = db.run_detail(conn, row["run_id"])
            exemption = next(
                (d for d in detail["decisions"] if d["agent"] == "exemption_screen"), None
            )
            result.append({
                **dict(row),
                "exemption": exemption,
                "fields": detail["fields"],
                "evidence": [ev for ev in detail["evidence"] if ev["kind"] == "exemption"],
            })
        return result
    finally:
        conn.close()


@app.post("/api/tests")
def save_test(payload: TestResultIn) -> dict[str, Any]:
    conn = db.connect()
    try:
        db.save_test_result(
            conn, test_id=payload.test_id, run_id=payload.run_id, name=payload.name,
            input_text=payload.input, expected=payload.expected, actual=payload.actual,
            passed=payload.passed, evidence_ref=payload.evidence_ref, notes=payload.notes,
        )
    finally:
        conn.close()
    return {"saved": True, "test_id": payload.test_id}


@app.get("/api/tests")
def test_results() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute("SELECT * FROM test_results ORDER BY created_at, test_id"))
        result = []
        for row in rows:
            item = dict(row)
            item["passed"] = bool(item["passed"])
            result.append(item)
        return result
    finally:
        conn.close()


@app.get("/api/portal-clock")
def portal_clock() -> dict[str, Any]:
    try:
        with httpx.Client(timeout=5) as client:
            clock = client.get(f"{config.PORTAL_URL}/api/clock").json()
            apps = client.get(f"{config.PORTAL_URL}/api/applications").json()
        return {"mock_now": clock.get("mock_now"), "application_count": len(apps)}
    except Exception as exc:
        return {"mock_now": None, "error": str(exc)}


@app.get("/api/tracker")
def tracker_view() -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = list(conn.execute(
            """SELECT t.*, r.raw_input, r.status FROM tracker t
               JOIN runs r ON r.run_id = t.run_id ORDER BY t.last_checked_at DESC"""
        ))
        result = []
        for row in rows:
            detail = db.run_detail(conn, row["run_id"])
            events: list[dict[str, str]] = []
            for action in detail["actions"]:
                if action["type"] == "submit":
                    events.append({"label": "Application submitted", "at": action["created_at"], "kind": "submit"})
                if action["type"] == "payment":
                    events.append({"label": "Fee paid, application filed", "at": action["created_at"], "kind": "payment"})
            verification = detail["verification"]
            if verification:
                events.append({
                    "label": "Verified independently" if verification["verification_status"] == "verified"
                    else f"Verification {verification['verification_status']}",
                    "at": verification["checked_at"], "kind": "verification",
                })
            for decision_item in detail["decisions"]:
                if decision_item["agent"] == "tracker" and decision_item["label"] == "transferred":
                    events.append({"label": "Transferred under Section 6(3)", "at": decision_item["created_at"], "kind": "transfer"})
                if decision_item["agent"] == "tracker" and decision_item["label"] == "appeal_drafted":
                    events.append({"label": "First appeal drafted", "at": decision_item["created_at"], "kind": "appeal"})
            reply_labels = {
                "no_reply": "No reply by the due date",
                "evasive": "Evasive reply received",
                "replied": "Reply received",
            }
            if row["reply_state"] in reply_labels:
                events.append({
                    "label": reply_labels[row["reply_state"]],
                    "at": row["last_checked_at"] or "", "kind": "reply",
                })
            events.sort(key=lambda item: item["at"] or "")
            result.append({**dict(row), "timeline": events})
        return result
    finally:
        conn.close()


STATIC = config.STATIC_DIR
STATIC.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    path = Path(STATIC) / "index.html"
    if not path.exists():
        raise HTTPException(status_code=500, detail="dashboard not built")
    return FileResponse(path)
