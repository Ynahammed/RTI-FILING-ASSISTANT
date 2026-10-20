"""Start both services (mock portal + agent) for local development.

    python -m scripts.dev            # keep existing demo data
    python -m scripts.dev --reset    # wipe data/ first
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
AGENT_PORT = int(os.getenv("RTI_AGENT_PORT", "8000"))
PORTAL_PORT = int(os.getenv("RTI_PORTAL_PORT", "8001"))


def wait_for(url: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete data/ before starting")
    args = parser.parse_args()

    if args.reset and DATA.exists():
        shutil.rmtree(DATA)
        print("Removed existing demo data.")

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT))

    portal = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "portal.main:app", "--host", "127.0.0.1", "--port", str(PORTAL_PORT)],
        cwd=ROOT, env=env,
    )
    agent = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent.main:app", "--host", "127.0.0.1", "--port", str(AGENT_PORT)],
        cwd=ROOT, env=env,
    )

    ok_portal = wait_for(f"http://127.0.0.1:{PORTAL_PORT}/healthz")
    ok_agent = wait_for(f"http://127.0.0.1:{AGENT_PORT}/api/health")
    if not (ok_portal and ok_agent):
        print("One of the services did not start.", file=sys.stderr)
        for proc in (portal, agent):
            proc.terminate()
        return 1

    print()
    print(f"  Mock portal : http://127.0.0.1:{PORTAL_PORT}")
    print(f"  Agent + UI  : http://127.0.0.1:{AGENT_PORT}")
    print(f"  Portal demo : http://127.0.0.1:{PORTAL_PORT}/admin/clock")
    print()
    print("Press Ctrl+C to stop both.")
    try:
        while True:
            if portal.poll() is not None or agent.poll() is not None:
                print("A service exited; stopping the other.", file=sys.stderr)
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (portal, agent):
            if proc.poll() is None:
                proc.terminate()
        for proc in (portal, agent):
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
