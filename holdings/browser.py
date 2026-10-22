"""Headless Chromium, for the one site (Bandhan) where plain HTTP was shown not to work.

Only `holdings.amcs.bandhan` uses this, and only to obtain the list of files; the files themselves
are downloaded with plain HTTP through `Fetcher`. Playwright is imported lazily so the other AMCs
run without it installed.

Environment:
  HOLDINGS_CHROMIUM   path to a Chromium/headless-shell binary (default: Playwright's own)
  HTTPS_PROXY         passed to the browser if set
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlparse

from .model import HoldingsError

# Third-party trackers and media the page loads; skipping them keeps the visit to the AMC's own
# hosts and cuts the number of requests.
BLOCK_TYPES = {"image", "media", "font"}
ALLOW_HOST_SUFFIXES = ("bandhanmutual.com",)


def collect(url: str, init_script: str, result_js: str, *, timeout_s: int = 60) -> list:
    """Open `url` with `init_script` installed, then poll `result_js` (an arrow function returning
    a list) until it is non-empty. Returns that list."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise HoldingsError("this AMC needs Playwright: pip install playwright") from None

    launch: dict = {"headless": True}
    if os.environ.get("HOLDINGS_CHROMIUM"):
        launch["executable_path"] = os.environ["HOLDINGS_CHROMIUM"]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        launch["proxy"] = {"server": proxy}

    def route(r):
        host = urlparse(r.request.url).hostname or ""
        if r.request.resource_type in BLOCK_TYPES or not host.endswith(ALLOW_HOST_SUFFIXES):
            return r.abort()
        return r.continue_()

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            ctx = browser.new_context()
            ctx.add_init_script(init_script)
            ctx.route("**/*", route)
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                got = page.evaluate(result_js)
                if got:
                    return got
                page.wait_for_timeout(1000)
            raise HoldingsError(f"{url}: page did not produce the expected data within {timeout_s}s")
        finally:
            browser.close()
