"""Portal transports behind one interface.

Filing and verification used to call HTTP and Playwright helpers directly, so the
only portal the system could be tested against was the one bundled with it. A
`PortalAdapter` is now the seam: the HTTP adapter drives the live mock, the
Playwright adapter fills the real form, and `FixtureAdapter` replays recorded
pages so the verifier can be tested without a running portal at all.

Nothing here decides anything. The adapter is a transport: it submits an already
approved payload, pays with an already signed token, and returns exactly what the
portal said. The approval gate lives in SQLite, not in this module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from agent import browser
from common import config

CONFIRMATION_RE = re.compile(r"/confirmation/([^\s?#]+)")
APP_ID_RE = re.compile(r"/(?:captcha|pay|confirmation)/([A-Za-z0-9-]+)")


class PortalError(RuntimeError):
    """The portal rejected a request or could not be reached."""


class PortalAdapter(Protocol):
    name: str

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def pay(self, app_id: str, token: str, key: str, amount: int) -> dict[str, Any]: ...

    def confirmation(self, reg_no: str) -> tuple[int, str, str]: ...

    def status(self, app_id: str) -> dict[str, Any] | None: ...

    def complete_captcha(self, app_id: str) -> bool: ...

    def resolve_by_idempotency(self, key: str) -> dict[str, Any] | None: ...


class HttpAdapter:
    """The portal's API. This is what the hosted deployment uses."""

    name = "http"

    def __init__(self, base_url: str | None = None, admin_token: str | None = None) -> None:
        self.base_url = (base_url or config.PORTAL_URL).rstrip("/")
        self.admin_token = admin_token or config.ADMIN_TOKEN

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=20) as client:
            resp = client.post(f"{self.base_url}/api/apply", json=payload)
        if resp.status_code != 200:
            raise PortalError(f"portal rejected the application: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        return {
            "app_id": body["app_id"],
            "captcha_pending": True,
            "duplicate": body.get("duplicate", False),
            "transport": self.name,
        }

    def pay(self, app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                f"{self.base_url}/api/pay/{app_id}",
                json={"approval_token": token, "idempotency_key": key, "amount_inr": amount},
            )
        if resp.status_code == 504:
            return {"ok": True, "timeout": True, "app_id": app_id, "transport": self.name}
        if resp.status_code != 200:
            try:
                reason = resp.json().get("reason", resp.text[:180])
            except Exception:
                reason = resp.text[:180]
            return {"ok": False, "reason": reason, "app_id": app_id, "transport": self.name}
        body = resp.json()
        return {
            "ok": True,
            "timeout": False,
            "app_id": app_id,
            "reg_no": body.get("reg_no"),
            "transport": self.name,
            "duplicate": body.get("duplicate", False),
        }

    def confirmation(self, reg_no: str) -> tuple[int, str, str]:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(f"{self.base_url}/confirmation/{reg_no}")
        return resp.status_code, resp.text, self.name

    def status(self, app_id: str) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.base_url}/api/status/{app_id}")
            return resp.json() if resp.status_code == 200 else None
        except Exception:
            return None

    def complete_captcha(self, app_id: str) -> bool:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{self.base_url}/admin/captcha/{app_id}/complete",
                    data={"admin_token": self.admin_token},
                    follow_redirects=False,
                )
            return resp.status_code in {303, 200}
        except Exception:
            return False

    def resolve_by_idempotency(self, key: str) -> dict[str, Any] | None:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{self.base_url}/api/applications/by-idempotency/{key}")
            if resp.status_code != 200:
                return None
            body = resp.json()
            return {
                "app_id": body["app_id"],
                "captcha_pending": body["status"] == "captcha_pending",
                "reg_no": body.get("reg_no"),
                "recovered": True,
                "transport": self.name,
            }
        except Exception:
            return None


class PlaywrightAdapter(HttpAdapter):
    """Fills the portal's actual form. Browser per thread, fresh context per call."""

    name = "playwright"

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        with browser.page() as page:
            page.set_default_timeout(config.PLAYWRIGHT_TIMEOUT_MS)
            page.goto(f"{self.base_url}/apply", wait_until="domcontentloaded")
            page.fill("#applicant_name", payload["applicant_name"])
            page.fill("#applicant_address", payload["applicant_address"])
            page.select_option("#state", payload["state"])
            page.select_option("#department", payload["department"])
            page.select_option("#authority_code", payload["authority_code"])
            page.fill("#information_sought", payload["information_sought"])
            if payload["bpl"]:
                page.check("input[name=bpl]")
            if payload["urgent"]:
                page.check("input[name=urgent]")
            page.evaluate(
                "(value) => { document.querySelector('input[name=idempotency_key]').value = value; }",
                payload["idempotency_key"],
            )
            page.click("button[type=submit]")
            page.wait_for_url(
                re.compile(r"/(captcha|pay|confirmation)/"),
                timeout=config.PLAYWRIGHT_TIMEOUT_MS,
            )
            url = page.url
            alert = ""
            try:
                if page.locator(".alert").count():
                    alert = page.locator(".alert").inner_text()
            except Exception:
                alert = ""
            if alert:
                raise PortalError(f"portal form validation failed: {alert}")
            match = APP_ID_RE.search(url)
            return {
                "app_id": match.group(1) if match else "",
                "captcha_pending": "/captcha/" in url,
                "url": url,
                "transport": self.name,
            }

    def pay(self, app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        with browser.page() as page:
            page.set_default_timeout(config.PLAYWRIGHT_TIMEOUT_MS)
            page.goto(f"{self.base_url}/pay/{app_id}", wait_until="domcontentloaded")
            page.fill("#approval_token", token)
            page.fill("#idempotency_key", key)
            page.fill("#amount", str(amount))
            page.click("button[type=submit]")
            try:
                page.wait_for_url(
                    re.compile(r"/confirmation/"), timeout=config.PLAYWRIGHT_TIMEOUT_MS
                )
            except PlaywrightTimeout:
                body = page.inner_text("body")
                lowered = body.lower()
                if "timeout" in lowered or "timed out" in lowered:
                    return {"ok": True, "timeout": True, "app_id": app_id, "transport": self.name}
                if page.locator(".alert").count():
                    return {
                        "ok": False,
                        "reason": page.locator(".alert").inner_text(),
                        "app_id": app_id,
                        "transport": self.name,
                    }
                return {
                    "ok": False,
                    "reason": "payment page did not reach a confirmation",
                    "app_id": app_id,
                    "transport": self.name,
                }
            match = CONFIRMATION_RE.search(page.url)
            return {
                "ok": True,
                "timeout": False,
                "app_id": app_id,
                "reg_no": match.group(1) if match else None,
                "transport": self.name,
            }

    def confirmation(self, reg_no: str) -> tuple[int, str, str]:
        with browser.page() as page:
            response = page.goto(
                f"{self.base_url}/confirmation/{reg_no}", wait_until="domcontentloaded"
            )
            status = response.status if response else 0
            return status, page.content(), self.name


class AutoAdapter:
    """Try the browser, fall back to the API, and never mask a validation refusal."""

    name = "auto"

    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode or config.FILING_MODE).lower()
        self.http = HttpAdapter()
        self.playwright = PlaywrightAdapter() if self.mode in {"playwright", "auto"} else None

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.playwright is None:
            return self.http.submit(payload)
        try:
            return self.playwright.submit(payload)
        except PortalError:
            raise
        except Exception as exc:
            if self.mode == "playwright":
                raise PortalError(f"Playwright filing failed: {exc}") from exc
            response = self.http.submit(payload)
            response["fallback_reason"] = str(exc)[:200]
            return response

    def pay(self, app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
        if self.playwright is None:
            return self.http.pay(app_id, token, key, amount)
        try:
            return self.playwright.pay(app_id, token, key, amount)
        except Exception as exc:
            if self.mode == "playwright":
                raise PortalError(f"Playwright payment failed: {exc}") from exc
            response = self.http.pay(app_id, token, key, amount)
            response["fallback_reason"] = str(exc)[:200]
            return response

    def confirmation(self, reg_no: str) -> tuple[int, str, str]:
        if self.playwright is None:
            return self.http.confirmation(reg_no)
        try:
            return self.playwright.confirmation(reg_no)
        except Exception:
            if self.mode == "playwright":
                raise
            return self.http.confirmation(reg_no)

    def status(self, app_id: str) -> dict[str, Any] | None:
        return self.http.status(app_id)

    def complete_captcha(self, app_id: str) -> bool:
        return self.http.complete_captcha(app_id)

    def resolve_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return self.http.resolve_by_idempotency(key)


class FixtureAdapter:
    """Replay a recorded portal from a directory.

    Expected files (all optional except a confirmation page):
      status.json                     {"APP-1": {"reg_no": "...", "transferred_to": null}}
      submit.json                     {"app_id": "APP-1", "captcha_pending": true}
      pay.json                        {"ok": true, "timeout": false, "reg_no": "..."}
      by_key.json                     {"<idempotency key>": {...}}
      confirmation_<reg_no with / as _>.html
    """

    name = "fixture"

    def __init__(self, root: str | Path, app_id: str = "APP-FIXTURE") -> None:
        self.root = Path(root)
        self.app_id = app_id

    def _json(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._json("submit.json", {"app_id": self.app_id, "captcha_pending": True})
        return {**body, "transport": self.name}

    def pay(self, app_id: str, token: str, key: str, amount: int) -> dict[str, Any]:
        body = self._json("pay.json", {"ok": True, "timeout": False, "reg_no": None})
        return {**body, "app_id": app_id, "transport": self.name}

    def confirmation(self, reg_no: str) -> tuple[int, str, str]:
        path = self.root / f"confirmation_{reg_no.replace('/', '_')}.html"
        if not path.exists():
            return 404, "", self.name
        return 200, path.read_text(encoding="utf-8"), self.name

    def status(self, app_id: str) -> dict[str, Any] | None:
        return self._json("status.json", {}).get(app_id)

    def complete_captcha(self, app_id: str) -> bool:
        return True

    def resolve_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return self._json("by_key.json", {}).get(key)


def get_adapter(mode: str | None = None) -> AutoAdapter | HttpAdapter:
    if (mode or config.FILING_MODE).lower() == "http":
        return HttpAdapter()
    return AutoAdapter(mode)
