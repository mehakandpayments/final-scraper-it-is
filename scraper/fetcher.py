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

from .antiblock import detect_block, detect_signals, is_soft_block
from .config import RenderMode, ScrapeConfig
from .models import FetchResult
from .utils import registered_domain

# Realistic, consistent browser-like headers reduce trivial UA/header blocking.
def _default_headers(user_agent: str) -> dict:
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                  "image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        # NB: deliberately no Accept-Encoding — httpx sets it to exactly the
        # codecs it can decode (gzip/deflate, + br/zstd only if those libs are
        # installed). Advertising "br" ourselves would yield undecodable bytes.
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


# Minimal "stealth" — hides the most trivial automation tells (a headless browser
# leaves obvious fingerprints like navigator.webdriver=true that naive bot checks
# read). This does not defeat real anti-bot systems; it just lets a legitimate
# browser look like a normal browser to basic checks.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""


def _pw_proxy(proxy: str | None) -> dict | None:
    """Convert a proxy URL into Playwright's proxy dict (splitting credentials)."""
    if not proxy:
        return None
    u = urlparse(proxy)
    server = f"{u.scheme or 'http'}://{u.hostname}" + (f":{u.port}" if u.port else "")
    out: dict = {"server": server}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out

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
            headers=_default_headers(config.user_agent),
            follow_redirects=True,
            timeout=config.request_timeout,
            proxy=config.proxy,  # route through the user's own proxy if set
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

        # AUTO: escalate to a real browser when static failed, looks like a JS
        # shell, or hit a *soft* block (e.g. a Cloudflare JS challenge / 403 that
        # a genuine browser can clear). Hard gates (CAPTCHA, login) are not retried.
        soft_blocked = is_soft_block(static.blocked_reason)
        if not static.ok or looks_js_rendered(static.html) or soft_blocked:
            rendered = self._fetch_browser(url)
            if rendered.ok:
                return rendered
            # Browser also failed – prefer whichever is more informative.
            return rendered if (not static.ok or soft_blocked) else static
        return static

    def fetch_browser(self, url: str) -> FetchResult:
        """Force a headless-browser fetch (used by content-aware escalation)."""
        return self._fetch_browser(url)

    def download_pdf(self, url: str, max_bytes: int | None = None):
        """Download + hash a single PDF (no bytes retained). Honours the host throttle."""
        from .pdfs import PdfInfo, download_pdf

        cap = max_bytes if max_bytes is not None else self.cfg.pdf_max_bytes
        self.throttle.wait(url)
        return download_pdf(self._client, url, max_bytes=cap)

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

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float:
        """Seconds to wait before a 429/503 retry, honouring Retry-After."""
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                return min(float(ra), self.cfg.max_retry_after)
            except ValueError:
                pass  # HTTP-date form; fall through to backoff
        return min(2.0 * (attempt + 1), self.cfg.max_retry_after)

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
                html = resp.text
                blocked = detect_block(resp.status_code, html, dict(resp.headers))
                # Back off & retry on rate-limit / transient server challenges.
                if resp.status_code in (429, 503) and attempt < self.cfg.max_retries:
                    last_err = blocked or f"HTTP {resp.status_code}"
                    time.sleep(self._retry_after(resp, attempt))
                    continue
                if resp.status_code >= 500 and attempt < self.cfg.max_retries:
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if len(html.encode("utf-8", "ignore")) > self.cfg.max_html_bytes:
                    html = html[: self.cfg.max_html_bytes]
                err = None
                if blocked:
                    err = blocked
                elif resp.status_code >= 400:
                    err = f"HTTP {resp.status_code}"
                return FetchResult(
                    url=url, final_url=str(resp.url), status_code=resp.status_code,
                    html=html, content_type=ctype, rendered=False, elapsed_ms=elapsed,
                    error=err, blocked_reason=blocked, signals=detect_signals(html),
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
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        if self.cfg.stealth:
            args.append("--disable-blink-features=AutomationControlled")
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=True, args=args, proxy=_pw_proxy(self.cfg.proxy),
                )
                try:
                    context = browser.new_context(
                        user_agent=self.cfg.user_agent,
                        viewport={"width": 1366, "height": 900},
                        locale="en-US",
                        java_script_enabled=True,
                        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    )
                    if self.cfg.stealth:
                        context.add_init_script(_STEALTH_JS)
                    page = context.new_page()
                    # Track the status of the *latest* main-frame navigation, so a
                    # challenge that redirects to the real page (or a 401/403 that
                    # never clears) is reported with the correct final status.
                    nav_status: dict[str, int | None] = {"code": None}

                    def _on_response(response) -> None:
                        try:
                            if response.frame == page.main_frame and response.request.is_navigation_request():
                                nav_status["code"] = response.status
                        except Exception:
                            pass

                    page.on("response", _on_response)
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

                    def _status() -> int | None:
                        return nav_status["code"] if nav_status["code"] is not None else (resp.status if resp else None)

                    html = page.content()
                    # Let a soft JS challenge (e.g. Cloudflare "Just a moment…")
                    # run to completion in the real browser, then re-read.
                    if is_soft_block(detect_block(_status(), html)):
                        deadline = time.monotonic() + self.cfg.challenge_max_wait
                        while time.monotonic() < deadline:
                            page.wait_for_timeout(2000)
                            try:
                                page.wait_for_load_state("networkidle", timeout=4000)
                            except PWError:
                                pass
                            html = page.content()
                            if detect_block(_status(), html) is None:  # now looks real
                                break
                    status = _status()
                    final_url = page.url
                finally:
                    browser.close()
            elapsed = int((time.monotonic() - start) * 1000)
            if len(html.encode("utf-8", "ignore")) > self.cfg.max_html_bytes:
                html = html[: self.cfg.max_html_bytes]
            blocked = detect_block(status, html)  # final verdict from real status + content
            err = blocked or (f"HTTP {status}" if (status or 200) >= 400 else None)
            return FetchResult(
                url=url, final_url=final_url, status_code=status, html=html,
                content_type="text/html", rendered=True, elapsed_ms=elapsed,
                error=err, blocked_reason=blocked, signals=detect_signals(html),
            )
        except Exception as exc:
            detail = str(exc).lower()
            if "executable doesn't exist" in detail or "playwright install" in detail:
                msg = "browser: Chromium not installed — run `.venv/bin/playwright install chromium`"
            else:
                msg = f"browser: {type(exc).__name__}: {exc}"
            return FetchResult(
                url=url, final_url=url, status_code=None, html="", rendered=True,
                elapsed_ms=int((time.monotonic() - start) * 1000), error=msg,
            )
