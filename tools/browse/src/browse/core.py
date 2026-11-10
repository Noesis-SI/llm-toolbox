"""Render pages in headless Chromium. One navigation per call, no retries.

The tool never tries to get past a CAPTCHA, bot check or access wall. When one is
detected it raises BrowseError(code="blocked") and stops.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

WAIT_UNTIL = ("load", "domcontentloaded", "networkidle")
ACTIONS = ("fill", "click", "wait_for", "press", "extract")
BLOCK_STATUS = (401, 403, 429)
BLOCK_TITLE = re.compile(
    r"captcha|are you a robot|access denied|attention required|just a moment|"
    r"verify you are human|unusual traffic|security check|request blocked|pardon our interruption",
    re.I,
)
BLOCK_WIDGET = re.compile(
    r"g-recaptcha|h-captcha|cf-turnstile|challenges\.cloudflare\.com|geetest|px-captcha|press & hold",
    re.I,
)


class BrowseError(Exception):
    def __init__(self, code: str, message: str, hint: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details


def detect_block(status: int | None, title: str, html: str) -> str | None:
    """Return a reason if the page looks like a CAPTCHA, bot check or access wall, else None."""
    if status in BLOCK_STATUS:
        return f"HTTP {status}"
    if BLOCK_TITLE.search(title or ""):
        return f"page title looks like a block page: {title!r}"
    if BLOCK_WIDGET.search(html or ""):
        return "page contains a CAPTCHA or bot-check widget"
    return None


def _check_block(page: Page, status: int | None) -> None:
    reason = detect_block(status, page.title(), page.content())
    if reason:
        raise BrowseError(
            "blocked",
            f"Stopped: {reason} at {page.url}. This tool does not solve CAPTCHAs or bypass access controls.",
            "Do not retry in a loop. Ask Ben how to proceed (use the provider's official route, an API, "
            "or a phone or in-person check).",
            {"url": page.url, "status": status},
        )


def _launch_error(error: PlaywrightError) -> BrowseError:
    text = str(error)
    if "Executable doesn't exist" in text or "playwright install" in text:
        return BrowseError(
            "browser_missing",
            "Chromium is not installed for Playwright.",
            "Run: uv run --project /home/noa/repos/llm-toolbox/tools/browse playwright install chromium",
        )
    if "error while loading shared libraries" in text:
        libs = sorted(set(re.findall(r"(lib[\w.+-]+\.so[\w.]*)", text)))
        return BrowseError(
            "browser_missing_libs",
            "Chromium cannot load shared libraries: " + (", ".join(libs) or "unknown"),
            "Ask the system operator to install the Ubuntu packages that provide these libraries "
            "(apt install), then retry.",
            {"libraries": libs},
        )
    return BrowseError("browser_error", f"Chromium failed to start: {text.splitlines()[0]}",
                       "Check the message; if it persists, report it as a bug in the browse tool.")


@contextmanager
def open_page(
    url: str,
    *,
    timeout: float = 30,
    user_agent: str | None = None,
    wait_until: str = "load",
    settle: float = 1.0,
) -> Iterator[tuple[Page, int | None]]:
    """Open url in a fresh headless Chromium and yield (page, http_status).

    After load, waits `settle` seconds so page scripts can render, then checks for blocks.
    """
    if wait_until not in WAIT_UNTIL:
        raise BrowseError("usage_error", f"wait_until must be one of {WAIT_UNTIL}", "Pass --wait-until load.")
    if not re.match(r"^https?://", url):
        raise BrowseError("usage_error", "url must start with http:// or https://", "Pass a full URL.")
    ms = int(timeout * 1000)
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except PlaywrightError as error:
                raise _launch_error(error) from error
            try:
                context = browser.new_context(user_agent=user_agent) if user_agent else browser.new_context()
                page = context.new_page()
                page.set_default_timeout(ms)
                try:
                    response = page.goto(url, wait_until=wait_until, timeout=ms)
                except PlaywrightTimeout as error:
                    raise BrowseError("timeout", f"Page did not finish loading within {timeout}s: {url}",
                                      "Retry once with a larger --timeout, or check the URL.") from error
                except PlaywrightError as error:
                    raise BrowseError("navigation_failed", str(error).splitlines()[0],
                                      "Check the URL and that the site is reachable.") from error
                status = response.status if response else None
                if settle > 0:
                    page.wait_for_timeout(settle * 1000)  # let page scripts render content
                _check_block(page, status)
                yield page, status
            finally:
                browser.close()
    except PlaywrightError as error:
        raise BrowseError("browser_error", str(error).splitlines()[0], "Retry; report if it persists.") from error


def get_text(url: str, *, max_chars: int = 20000, **opts: Any) -> dict[str, Any]:
    with open_page(url, **opts) as (page, status):
        text = page.inner_text("body")
        return {
            "url": url,
            "final_url": page.url,
            "status": status,
            "title": page.title(),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }


def screenshot(url: str, out_path: str, *, full_page: bool = True, **opts: Any) -> dict[str, Any]:
    with open_page(url, **opts) as (page, status):
        page.screenshot(path=out_path, full_page=full_page)
        return {"url": url, "final_url": page.url, "status": status, "path": out_path}


def validate_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise BrowseError("bad_steps", "steps must be a JSON list of action objects",
                          'Use e.g. [{"action":"fill","selector":"#q","value":"x"},{"action":"click","selector":"button"}].')
    for i, step in enumerate(steps):
        where = f"step {i}"
        if not isinstance(step, dict) or step.get("action") not in ACTIONS:
            raise BrowseError("bad_steps", f"{where}: action must be one of {ACTIONS}",
                              "Each step is an object with an 'action' field.")
        needs = {"fill": ("selector", "value"), "click": ("selector",), "wait_for": ("selector",),
                 "press": ("key",), "extract": ("selector",)}[step["action"]]
        missing = [k for k in needs if not isinstance(step.get(k), str)]
        if missing:
            raise BrowseError("bad_steps", f"{where} ({step['action']}): missing string field(s) {missing}",
                              "Add the field(s) as strings.")
    return steps


def run_steps(url: str, steps: Any, *, max_chars: int = 20000, **opts: Any) -> dict[str, Any]:
    steps = validate_steps(steps)
    with open_page(url, **opts) as (page, status):
        timeout = int(opts.get("timeout", 30) * 1000)
        extracted: list[dict[str, Any]] = []
        for i, step in enumerate(steps):
            action = step["action"]
            try:
                if action == "fill":
                    page.fill(step["selector"], step["value"], timeout=timeout)
                elif action == "click":
                    page.click(step["selector"], timeout=timeout)
                    page.wait_for_load_state("load", timeout=timeout)
                elif action == "wait_for":
                    page.wait_for_selector(step["selector"], state="visible", timeout=timeout)
                elif action == "press":
                    if step.get("selector"):
                        page.press(step["selector"], step["key"], timeout=timeout)
                    else:
                        page.keyboard.press(step["key"])
                    page.wait_for_load_state("load", timeout=timeout)
                elif action == "extract":
                    texts = page.locator(step["selector"]).all_inner_texts()[:50]
                    extracted.append({"selector": step["selector"], "texts": [t[:2000] for t in texts]})
            except PlaywrightTimeout as error:
                raise BrowseError(
                    "step_timeout",
                    f"step {i} ({action} {step.get('selector', step.get('key'))!r}) timed out",
                    "Check the selector against the page (use extract or screenshot to look), or raise --timeout.",
                    {"step": i, "page_url": page.url},
                ) from error
            _check_block(page, status)
        text = page.inner_text("body")
        return {
            "url": url,
            "final_url": page.url,
            "status": status,
            "steps_done": len(steps),
            "extracted": extracted,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }
