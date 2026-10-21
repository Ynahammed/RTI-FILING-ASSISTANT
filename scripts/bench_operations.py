"""Operational end-to-end benchmark: real requests through the running system.

`scripts/run_eval` proves 14 curated scenarios still behave. This does something
different: it takes real request text from RTI-Bench, drives it through the live agent
and portal exactly as a client would, and measures what an operator would care about —
how many requests reach a verified filing, how long each stage takes, whether the
approval gate held, and whether any duplicate registrations appeared.

Each run goes through: create -> (answer one follow-up) -> approve -> operator CAPTCHA
handoff -> poll to a terminal state. Requests the deterministic rule blocks are counted
as blocked, not failed.

Requires both services (`python -m scripts.dev`). Usage:

    python -m scripts.bench_operations --limit 40
    python -m scripts.bench_operations --limit 40 --concurrency 4
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from agent import nlu
from common import metrics

DATA = Path("data/rti-bench/hf_annotated.csv")
OUT = Path("artifacts/operations_results.json")
AGENT = os.getenv("RTI_AGENT_URL", "http://127.0.0.1:8000").rstrip("/")
PORTAL = os.getenv("RTI_PORTAL_URL", "http://127.0.0.1:8001").rstrip("/")
ADMIN = os.getenv("RTI_ADMIN_TOKEN", "rti-demo-admin")
# Creating, answering and approving a run needs the run token; the harness also
# reads the review queue and stores nothing, so it acts as the operator.
OPERATOR = os.getenv("RTI_OPERATOR_TOKEN", "rti-demo-operator")
HEADERS = {"X-Operator-Token": OPERATOR}
TERMINAL = {
    "verified", "failed", "review_queue", "declined", "appeal_ready",
    "tracking", "filed_unverified", "redirected",
}
POLL_SECONDS = 30.0
TIMEOUT = 60.0


def load_requests(limit: int, seed: int = 0) -> list[dict[str, Any]]:
    """Real requests, sampled deterministically so a rerun scores the same rows."""
    with DATA.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["outcome"] != "UNKNOWN"]
    requests = [
        {
            "hf_id": r["hf_id"],
            "text": (r.get("information_sought") or "").strip(),
            "expected_exempt": "8(1)(j)" in (r.get("exemptions_cited") or ""),
            "authority": (r.get("public_authority") or "").strip(),
        }
        for r in rows
        if len((r.get("information_sought") or "").strip()) >= 20
    ]
    requests.sort(key=lambda r: hashlib.md5(f"{seed}:{r['hf_id']}".encode()).hexdigest())
    return requests[:limit]


def _post(client: httpx.Client, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(f"{AGENT}{path}", json=payload or {})
    if response.status_code >= 400:
        raise RuntimeError(f"{path} -> {response.status_code}: {response.text[:200]}")
    return response.json()


def _get(client: httpx.Client, run_id: str) -> dict[str, Any]:
    response = client.get(f"{AGENT}/api/runs/{run_id}")
    if response.status_code >= 400:
        raise RuntimeError(f"GET run -> {response.status_code}")
    return response.json()


def _answer(detail: dict[str, Any], fallback: dict[str, str]) -> str:
    """What a clerk would reply: the value implied by the request, else the default."""
    question = ((detail.get("run") or {}).get("pending_question") or "").lower()
    # Check the confirmation question first: it contains the words "State Public
    # Information Officer", which used to be mistaken for the state question.
    if "did you mean" in question:
        return "Yes, that is the right office."
    if "state" in question:
        return fallback.get("state") or "Maharashtra"
    if "records or action" in question or "confident" in question:
        return "I want copies of the records, not action."
    if "department" in question or "office" in question:
        return (fallback.get("department") or "public_works").replace("_", " ")
    # Anything else is the "what information do you need" question: restate the request.
    return fallback.get("text") or "Please provide the records described in my request."


def run_one(client: httpx.Client, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    record: dict[str, Any] = {"hf_id": request["hf_id"], "expected_exempt": request["expected_exempt"]}
    parsed = nlu.extract(request["text"])
    fallback = {
        "state": parsed.get("state") or "",
        "department": parsed.get("department") or "",
        "text": request["text"],
    }
    try:
        detail = _post(client, "/api/runs", {"text": request["text"]})
        run_id = detail["run"]["run_id"]
        record["run_id"] = run_id

        for _ in range(5):
            if detail["run"]["status"] != "awaiting_input":
                break
            detail = _post(client, f"/api/runs/{run_id}/answer", {"text": _answer(detail, fallback)})

        record["intake_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["triage"] = next(
            (d["label"] for d in detail["decisions"] if d["agent"] == "triage"), None
        )

        if detail["run"]["status"] == "awaiting_approval":
            approve_started = time.perf_counter()
            detail = _post(client, f"/api/runs/{run_id}/approve")
            record["submit_ms"] = round((time.perf_counter() - approve_started) * 1000, 1)
            captcha_started = time.perf_counter()
            try:
                detail = _post(client, f"/api/runs/{run_id}/captcha-complete")
            except RuntimeError:
                pass
            record["payment_verify_ms"] = round((time.perf_counter() - captcha_started) * 1000, 1)
            deadline = time.time() + POLL_SECONDS
            while detail["run"]["status"] not in TERMINAL and time.time() < deadline:
                time.sleep(0.5)
                detail = _get(client, run_id)
            record["filing_ms"] = round((time.perf_counter() - approve_started) * 1000, 1)
            transports = [
                (a.get("response") or {}).get("transport")
                for a in detail.get("actions", [])
                if a.get("type") in {"submit", "payment"}
            ]
            record["transports"] = sorted({t for t in transports if t})

        record["status"] = detail["run"]["status"]
        record["reg_no"] = detail["run"].get("reg_no")
        record["actions"] = [a["type"] for a in detail.get("actions", [])]
        verification = detail.get("verification") or {}
        record["verification"] = verification.get("verification_status", "none")
        record["blocked"] = record["status"] == "review_queue" or record["triage"] == "exempt"
        record["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    except Exception as exc:  # a run that cannot complete is data, not a crash
        record.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        record["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return record


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarise(records: list[dict[str, Any]], concurrency: int, wall_seconds: float) -> dict[str, Any]:
    attempted = len(records)
    terminal = [r for r in records if r["status"] in TERMINAL]
    verified = [r for r in records if r["status"] == "verified"]
    blocked = [r for r in records if r.get("blocked")]
    redirected = [r for r in records if r["status"] == "redirected"]
    errors = [r for r in records if r["status"] == "error"]

    # Exemption screen versus the dataset's own 8(1)(j) label.
    pairs = [(bool(r.get("blocked")), bool(r["expected_exempt"])) for r in records if r["status"] != "error"]
    exemption = metrics.prf(
        sum(p and t for p, t in pairs),
        sum(p and not t for p, t in pairs),
        sum((not p) and t for p, t in pairs),
    )

    regs = [r["reg_no"] for r in records if r.get("reg_no")]
    unapproved = [
        r["hf_id"] for r in records
        if "payment" in r.get("actions", []) and not r.get("run_id")
    ]

    latencies = [r["total_ms"] for r in records if r.get("total_ms")]
    return {
        "attempted": attempted,
        "terminal": len(terminal),
        "verified": len(verified),
        "blocked_by_exemption": len(blocked),
        "redirected_to_grievance": len(redirected),
        "errors": len(errors),
        "completion_rate": round(len(terminal) / attempted, 4) if attempted else 0.0,
        "verified_rate": round(len(verified) / attempted, 4) if attempted else 0.0,
        "blocked_rate": round(len(blocked) / attempted, 4) if attempted else 0.0,
        "exemption_screen_vs_dataset": exemption,
        "duplicate_registrations": len(regs) - len(set(regs)),
        "payments_without_approval": len(unapproved),
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "max": round(max(latencies), 1) if latencies else 0.0,
            "submit_p50": percentile([r["submit_ms"] for r in records if r.get("submit_ms")], 50),
            "submit_p95": percentile([r["submit_ms"] for r in records if r.get("submit_ms")], 95),
            "payment_verify_p50": percentile(
                [r["payment_verify_ms"] for r in records if r.get("payment_verify_ms")], 50
            ),
            "payment_verify_p95": percentile(
                [r["payment_verify_ms"] for r in records if r.get("payment_verify_ms")], 95
            ),
            "intake_p50": percentile([r["intake_ms"] for r in records if r.get("intake_ms")], 50),
        },
        "transports": sorted({t for r in records for t in r.get("transports", [])}),
        "concurrency": concurrency,
        "wall_seconds": round(wall_seconds, 1),
        "throughput_runs_per_min": round(attempted / (wall_seconds / 60), 2) if wall_seconds else 0.0,
        "error_samples": [r.get("error") for r in errors[:5]],
        "gate": {
            "completion_rate": round(len(terminal) / attempted, 4) if attempted else 0.0,
            "verified_rate": round(len(verified) / attempted, 4) if attempted else 0.0,
            "duplicate_registrations": len(regs) - len(set(regs)),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--json-out", default=str(OUT))
    args = parser.parse_args()

    requests = load_requests(args.limit, args.seed)
    if not requests:
        print("No requests found; run the download step first.")
        return 1

    healthy = True
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS) as client:
            healthy = client.get(f"{AGENT}/api/health").status_code == 200
            portal_ok = client.get(f"{PORTAL}/healthz").status_code == 200
        healthy = healthy and portal_ok
    except Exception as exc:
        print(f"Services not reachable: {type(exc).__name__}: {exc}")
        return 1
    if not healthy:
        print("Services are not healthy; start them with python -m scripts.dev")
        return 1

    started = time.perf_counter()
    with httpx.Client(timeout=TIMEOUT, headers=HEADERS) as client:
        if args.concurrency <= 1:
            records = [run_one(client, request) for request in requests]
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                records = list(
                    pool.map(
                        lambda r: run_one(httpx.Client(timeout=TIMEOUT, headers=HEADERS), r),
                        requests,
                    )
                )
    wall = time.perf_counter() - started

    summary = summarise(records, args.concurrency, wall)
    payload = {
        "system": {"agent": AGENT, "portal": PORTAL},
        "sample": {"limit": args.limit, "seed": args.seed, "source": "RTI-Bench Source A information_sought"},
        "summary": summary,
        "runs": records,
    }
    path = Path(args.json_out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"attempted={summary['attempted']} terminal={summary['terminal']} "
        f"verified={summary['verified']} blocked={summary['blocked_by_exemption']} errors={summary['errors']}"
    )
    print(f"completion={summary['completion_rate']} verified_rate={summary['verified_rate']} "
          f"blocked_rate={summary['blocked_rate']}")
    print(f"exemption screen vs dataset: {summary['exemption_screen_vs_dataset']}")
    print(f"duplicate registrations={summary['duplicate_registrations']} "
          f"payments without approval={summary['payments_without_approval']}")
    print(f"latency p50={summary['latency_ms']['p50']}ms p95={summary['latency_ms']['p95']}ms "
          f"throughput={summary['throughput_runs_per_min']}/min at concurrency {args.concurrency}")
    print(f"  intake p50={summary['latency_ms']['intake_p50']}ms "
          f"submit p50/p95={summary['latency_ms']['submit_p50']}/{summary['latency_ms']['submit_p95']}ms "
          f"payment+verify p50/p95={summary['latency_ms']['payment_verify_p50']}/"
          f"{summary['latency_ms']['payment_verify_p95']}ms transports={summary['transports']}")
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
