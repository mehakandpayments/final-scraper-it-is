"""Fetching layer: fast static HTTP with a headless-browser fallback.

Strategy by :class:`RenderMode`:

* ``STATIC``  – httpx only.
* ``BROWSER`` – Playwright/Chromium only.
* ``AUTO``    – httpx first; if the response failed or *looks* like an empty
  JS shell (an SPA that renders client-side), re-fetch with the browser.

All fetching is designed to run inside worker threads (never the Streamlit /
asyncio main thread), so the Playwright **sync** API is safe to use here.
"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

import httpx

from .config import RenderMode, ScrapeConfig
from .models import FetchResult
from .utils import registered_domain

# --- heuristics for detecting client-rendered shells -------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_SPA_MARKERS = (
    'id="root"', "id='root'",
    'id="app"', "id='app'",
    'id="__next"', "__NEXT_DATA__",
    'data-reactroot', 'ng-app', 'id="svelte"',
)
# Below this many characters of visible text we assume the static HTML is a shell.
_MIN_TEXT_CHARS = 250


def visible_text_length(html: str) -> int:
    """Rough count of human-visible characters (tags + scripts stripped)."""
    no_scripts = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", no_scripts)
    return len(re.sub(r"\s+", " ", text).strip())


def looks_js_rendered(html: str) -> bool:
    """True when the static HTML appears to need a browser to populate content."""
    if not html:
        return True
    has_spa_marker = any(m in html for m in _SPA_MARKERS)
    sparse = visible_text_length(html) < _MIN_TEXT_CHARS
    needs_js = "enable javascript" in html.lower() or "<noscript" in html.lower()
    # A real page can be sparse but content-rich isn't a shell; require either an
    # SPA marker, or (sparse + an explicit "needs JS" hint), or extreme sparsity.
    return (has_spa_marker and sparse) or (sparse and needs_js) or visible_text_length(html) < 60


class _DomainThrottle:
    """Enforces a minimum delay between consecutive hits to the same host."""

    def __init__(self, min_delay: float) -> None:
        self.min_delay = max(0.0, min_delay)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.min_delay <= 0:
            return
        key = registered_domain(url)
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._last.get(key, 0.0)
                ready_at = last + self.min_delay
                if now >= ready_at:
                    self._last[key] = now
                    return
                sleep_for = ready_at - now
            time.sleep(sleep_for)


class Fetcher:
    """Thread-safe fetcher. Share one instance across the worker pool."""

    def __init__(self, config: ScrapeConfig) -> None:
        self.cfg = config
        self.throttle = _DomainThrottle(config.per_domain_delay)
        # httpx.Client is safe to reuse across threads.
        self._client = httpx.Client(
            headers={"User-Agent": config.user_agent, "Accept-Language": "en-US,en;q=0.9"},
            follow_redirects=True,
            timeout=config.request_timeout,
            limits=httpx.Limits(
                max_connections=max(10, config.concurrency * 2),
                max_keepalive_connections=config.concurrency,
            ),
        )

    # -- public API -----------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        mode = self.cfg.render_mode
        if mode is RenderMode.BROWSER:
            return self._fetch_browser(url)

        static = self._fetch_static(url)
        if mode is RenderMode.STATIC:
            return static

        # AUTO: fall back to the browser when static failed or looks like a shell.
        if not static.ok or looks_js_rendered(static.html):
            rendered = self._fetch_browser(url)
            if rendered.ok:
                return rendered
            # Browser also failed – prefer whichever has usable HTML.
            return rendered if not static.ok else static
        return static

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- static ---------------------------------------------------------------

    def _fetch_static(self, url: str) -> FetchResult:
        last_err: str | None = None
        start = time.monotonic()
        for attempt in range(self.cfg.max_retries + 1):
            self.throttle.wait(url)
            try:
                resp = self._client.get(url)
                elapsed = int((time.monotonic() - start) * 1000)
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype and ctype:
                    # Not a web page (PDF, image, JSON, ...). Return as-is; the
                    # converter/extractor will simply produce little.
                    return FetchResult(
                        url=url, final_url=str(resp.url), status_code=resp.status_code,
                        html=resp.text if "text" in ctype else "", content_type=ctype,
                        rendered=False, elapsed_ms=elapsed,
                        error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
                    )
                if resp.status_code >= 500 and attempt < self.cfg.max_retries:
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(0.5 * (attempt + 1))
                    continue
                html = resp.text
                if len(html.encode("utf-8", "ignore")) > self.cfg.max_html_bytes:
                    html = html[: self.cfg.max_html_bytes]
                return FetchResult(
                    url=url, final_url=str(resp.url), status_code=resp.status_code,
                    html=html, content_type=ctype, rendered=False, elapsed_ms=elapsed,
                    error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                if attempt < self.cfg.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
            except Exception as exc:  # malformed URL, decoding, etc.
                last_err = f"{type(exc).__name__}: {exc}"
                break
        return FetchResult(
            url=url, final_url=url, status_code=None, html="", rendered=False,
            elapsed_ms=int((time.monotonic() - start) * 1000), error=last_err or "fetch failed",
        )

    # -- browser --------------------------------------------------------------

    def _fetch_browser(self, url: str) -> FetchResult:
        """Render with Chromium. Launches per call to keep Playwright objects on
        a single thread (correct + leak-free); content is fetched after the
        network settles so SPA pages are fully populated."""
        from playwright.sync_api import sync_playwright, Error as PWError

        self.throttle.wait(url)
        start = time.monotonic()
        timeout_ms = int(self.cfg.request_timeout * 1000)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                try:
                    context = browser.new_context(
                        user_agent=self.cfg.user_agent,
                        viewport={"width": 1366, "height": 900},
                        java_script_enabled=True,
                    )
                    page = context.new_page()
                    # Don't waste time/bandwidth on images & media.
                    page.route(
                        re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|mp4|webm|woff2?)(\?.*)?$", re.I),
                        lambda route: route.abort(),
                    )
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
                    except PWError:
                        pass  # networkidle can time out on long-polling pages; proceed anyway
                    html = page.content()
                    status = resp.status if resp else None
                    final_url = page.url
                finally:
                    browser.close()
            elapsed = int((time.monotonic() - start) * 1000)
            if len(html.encode("utf-8", "ignore")) > self.cfg.max_html_bytes:
                html = html[: self.cfg.max_html_bytes]
            return FetchResult(
                url=url, final_url=final_url, status_code=status, html=html,
                content_type="text/html", rendered=True, elapsed_ms=elapsed,
                error=None if (status or 200) < 400 else f"HTTP {status}",
            )
        except Exception as exc:
            return FetchResult(
                url=url, final_url=url, status_code=None, html="", rendered=True,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                error=f"browser: {type(exc).__name__}: {exc}",
            )
