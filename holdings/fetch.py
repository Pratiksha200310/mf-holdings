"""Polite HTTP: at most one request per second per host, and an on-disk cache.

Every AMC module goes through `Fetcher`, so the rate limit and cache rules live in one place.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# A normal desktop browser user agent. We do not rotate it or try to look like many users.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MIN_INTERVAL_S = 1.0


class Fetcher:
    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.session = requests.Session()
        # Akamai-fronted sites (HDFC, NSE) refuse requests that do not look like a browser's;
        # sending the headers a browser sends is enough. Nothing here defeats a CAPTCHA or login.
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        self._last_hit: dict[str, float] = {}

    def _wait_for(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_hit.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < MIN_INTERVAL_S:
                time.sleep(MIN_INTERVAL_S - gap)
        self._last_hit[host] = time.monotonic()

    def _cache_path(self, method: str, url: str, body: str | None, suffix: str) -> Path:
        key = hashlib.sha256(f"{method} {url} {body or ''}".encode()).hexdigest()[:24]
        return self.cache_dir / urlparse(url).netloc / f"{key}{suffix}"

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        data: str | None = None,
        form: dict | None = None,
        headers: dict | None = None,
        cache: bool = True,
        suffix: str = "",
        timeout: int = 60,
    ) -> bytes:
        """Return the response body, from cache when possible.

        `cache=False` is for document *listings*, which change as new months are published;
        downloaded files themselves never change, so they are always cached.
        """
        if json_body is not None:
            body = json.dumps(json_body, sort_keys=True)
        elif form is not None:
            body = json.dumps(form, sort_keys=True)
        else:
            body = data
        path = self._cache_path(method, url, body, suffix)
        if path.exists() and (cache or self.offline):
            return path.read_bytes()
        if self.offline:
            raise FileNotFoundError(f"offline mode and not cached: {method} {url}")

        self._wait_for(url)
        log.info("%s %s", method, url)
        # `form` is sent as multipart/form-data, the way browsers send FormData objects.
        files = {k: (None, str(v)) for k, v in form.items()} if form is not None else None
        resp = self.session.request(method, url, json=json_body, data=data, files=files,
                                    headers=headers, timeout=timeout)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        return resp.content

    def forget(self, url: str, suffix: str = "") -> None:
        """Drop a cached GET response (e.g. an HTML error page served with status 200)."""
        self._cache_path("GET", url, None, suffix).unlink(missing_ok=True)

    def get(self, url: str, **kw) -> bytes:
        return self.request("GET", url, **kw)

    def post_json(self, url: str, payload: dict, **kw) -> bytes:
        return self.request("POST", url, json_body=payload, **kw)

    def download(self, url: str) -> Path:
        """Download a file (cached forever) and return its local path."""
        suffix = Path(urlparse(url).path).suffix
        self.get(url, suffix=suffix)
        return self._cache_path("GET", url, None, suffix)
