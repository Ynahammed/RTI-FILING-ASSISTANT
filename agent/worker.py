"""Durable deadline checks.

Before this existed a filed request only moved forward when a human clicked
"Check now". A 30-day deadline, a Section 6(3) transfer or a Section 19 appeal was
therefore invisible until someone remembered to look.

The queue is a SQLite table, not an in-memory timer, so a restart does not lose a
check. A job is claimed with a lease: exactly one worker owns it until the lease
expires, and an expired lease is reclaimable, so a crashed worker cannot strand a
filing. Failures back off and eventually stop rather than retrying forever.

The same loop runs in-process (`RTI_WORKER_ENABLED=1`, the default, because the
hosted deployment keeps SQLite on the instance) or standalone:

    python -m agent.worker --once
    python -m agent.worker
"""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent import db
from common import config, database

KIND = "deadline_check"
LEASE_SECONDS = 120
MAX_ATTEMPTS = 5
BACKOFF_CAP_MINUTES = 60
JOB_STATUSES = {"pending", "done", "failed", "cancelled"}
TERMINAL_RUN_STATUSES = {"appeal_ready", "closed"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def enqueue_check(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    due_at: datetime | None = None,
    kind: str = KIND,
) -> bool:
    """Schedule a check unless one is already pending for this run and kind."""
    when = _iso(due_at or (now_utc() + timedelta(hours=config.CHECK_INTERVAL_HOURS)))
    cursor = conn.execute(
        """INSERT INTO jobs(job_id, run_id, kind, due_at, status, attempts, created_at, updated_at)
           SELECT ?, ?, ?, ?, 'pending', 0, ?, ?
           WHERE NOT EXISTS (
               SELECT 1 FROM jobs WHERE run_id=? AND kind=? AND status='pending'
           )""",
        (db.new_id("JOB"), run_id, kind, when, db.now_iso(), db.now_iso(), run_id, kind),
    )
    return cursor.rowcount > 0


def claim(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 5,
    lease_seconds: int = LEASE_SECONDS,
) -> list[sqlite3.Row]:
    """Take ownership of due jobs. Two workers never receive the same row."""
    now = now or now_utc()
    lease_until = _iso(now + timedelta(seconds=lease_seconds))
    # The write lock makes select-then-update atomic; without it two workers can
    # both read the same pending row before either writes the lease.
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = list(
            conn.execute(
                """SELECT * FROM jobs
                   WHERE status='pending' AND due_at <= ?
                     AND (lease_until IS NULL OR lease_until < ?)
                   ORDER BY due_at LIMIT ?"""
                + database.for_update_skip_locked(conn),
                (_iso(now), _iso(now), limit),
            )
        )
        for row in rows:
            conn.execute(
                "UPDATE jobs SET lease_until=?, worker_id=?, updated_at=? WHERE job_id=?",
                (lease_until, worker_id, db.now_iso(), row["job_id"]),
            )
        columns = [row["job_id"] for row in rows]
        claimed = (
            list(
                conn.execute(
                    "SELECT * FROM jobs WHERE job_id IN ({})".format(
                        ",".join("?" for _ in columns)
                    ),
                    columns,
                )
            )
            if columns
            else []
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return claimed


def _finish(conn: sqlite3.Connection, job_id: str, status: str, error: str = "") -> None:
    conn.execute(
        """UPDATE jobs SET status=?, last_error=?, lease_until=NULL, worker_id=NULL,
               updated_at=? WHERE job_id=?""",
        (status, error[:500], db.now_iso(), job_id),
    )


def _reschedule(
    conn: sqlite3.Connection, job_id: str, *, due_at: datetime, attempts: int, error: str = ""
) -> None:
    conn.execute(
        """UPDATE jobs SET status='pending', attempts=?, due_at=?, last_error=?,
               lease_until=NULL, worker_id=NULL, updated_at=? WHERE job_id=?""",
        (attempts, _iso(due_at), error[:500], db.now_iso(), job_id),
    )


def run_once(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    worker_id: str | None = None,
    limit: int = 5,
) -> dict[str, int]:
    """Claim and run due checks once. Returns counts for the caller's log."""
    from agent import supervisor  # local import keeps the module graph acyclic

    now = now or now_utc()
    worker_id = worker_id or f"worker-{db.new_id('W')}"
    jobs = claim(conn, worker_id=worker_id, now=now, limit=limit)
    stats = {"claimed": len(jobs), "checked": 0, "finished": 0, "retried": 0, "failed": 0}

    for job in jobs:
        run_id = job["run_id"]
        attempts = int(job["attempts"])
        try:
            detail = supervisor.track(run_id)
        except ValueError as exc:
            # Not filed, or no portal application: nothing to track, ever.
            _finish(conn, job["job_id"], "cancelled", str(exc))
            stats["finished"] += 1
            continue
        except Exception as exc:  # portal timeout, transport error, bad response
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                _finish(conn, job["job_id"], "failed", f"{type(exc).__name__}: {exc}")
                stats["failed"] += 1
            else:
                delay = min(BACKOFF_CAP_MINUTES, 2 ** attempts)
                _reschedule(
                    conn, job["job_id"], due_at=now + timedelta(minutes=delay),
                    attempts=attempts, error=f"{type(exc).__name__}: {exc}",
                )
                stats["retried"] += 1
            continue

        status = (detail.get("run") or {}).get("status", "")
        stats["checked"] += 1
        if status in TERMINAL_RUN_STATUSES:
            _finish(conn, job["job_id"], "done")
            stats["finished"] += 1
        else:
            _reschedule(
                conn, job["job_id"],
                due_at=now + timedelta(hours=config.CHECK_INTERVAL_HOURS),
                attempts=attempts,
            )
    return stats


def pending_jobs(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='pending'").fetchone()["n"]
    )


_stop = threading.Event()
_thread: threading.Thread | None = None
_guard = threading.Lock()


def start_background_worker(interval_seconds: float | None = None) -> bool:
    """Start the in-process loop once. Returns False when already running."""
    global _thread
    with _guard:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        interval = interval_seconds or config.WORKER_INTERVAL_SECONDS

        def loop() -> None:
            while not _stop.wait(interval):
                conn = db.connect()
                try:
                    db.init_db(conn)
                    stats = run_once(conn)
                    if stats["claimed"]:
                        print(f"worker: {stats}", flush=True)
                except Exception as exc:  # the loop must survive one bad tick
                    print(f"worker tick failed: {type(exc).__name__}: {exc}", flush=True)
                finally:
                    conn.close()

        _thread = threading.Thread(target=loop, name="rti-deadline-worker", daemon=True)
        _thread.start()
        return True


def stop_background_worker(timeout: float = 5.0) -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
        _thread = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one sweep and exit")
    parser.add_argument("--interval", type=float, default=config.WORKER_INTERVAL_SECONDS)
    args = parser.parse_args()

    conn = db.connect()
    try:
        db.init_db(conn)
        if args.once:
            print(run_once(conn))
            return 0
        while True:
            print(run_once(conn), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
