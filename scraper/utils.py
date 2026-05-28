"""URL handling, slugs, domain comparison and a cached robots.txt gate."""

from __future__ import annotations

import hashlib
import re
import threading
import urllib.request
import urllib.robotparser
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

import tldextract

# tldextract normally hits the network once to refresh the public-suffix list.
# Pin it to the bundled snapshot so scraping stays fully offline/deterministic.
_extract = tldextract.TLDExtract(suffix_list_urls=())

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_url(url: str, base: str | None = None) -> str:
    """Resolve against ``base`` (if given), drop the fragment, tidy the path."""
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    parts = urlparse(url)
    # Lower-case scheme/host; collapse a bare trailing slash difference is left as-is
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunparse((scheme, netloc, path, parts.params, parts.query, ""))


def is_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme in ("http", "https")
    except ValueError:
        return False


def registered_domain(url: str) -> str:
    """e.g. ``https://blog.example.co.uk/x`` -> ``example.co.uk``."""
    ext = _extract(url)
    return ext.registered_domain or ext.domain or urlparse(url).netloc


def hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def same_site(a: str, b: str, include_subdomains: bool = True) -> bool:
    """Whether two URLs belong to the same site for crawl-scoping purposes."""
    if include_subdomains:
        return registered_domain(a) == registered_domain(b)
    return hostname(a) == hostname(b)


def slugify(text: str, max_len: int = 80) -> str:
    text = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return text[:max_len].strip("-") or "index"


def path_slug(url: str) -> str:
    """A filesystem-safe, collision-resistant stem derived from a URL's path."""
    parts = urlparse(url)
    raw = (parts.path or "/").strip("/")
    base = slugify(raw.replace("/", "-")) if raw else "index"
    # Disambiguate query strings / near-identical paths with a short hash.
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


class RobotsGate:
    """Thread-safe, per-host robots.txt cache.

    On any error fetching/parsing robots we *allow* the URL (fail-open) — the
    same pragmatic stance most crawlers take for unreachable robots files.
    """

    def __init__(self, user_agent: str, timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _parser_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlparse(url)
        host_key = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if host_key in self._cache:
                return self._cache[host_key]
        parser: urllib.robotparser.RobotFileParser | None
        try:
            # Fetch with an explicit timeout — RobotFileParser.read() uses urlopen
            # with *no* timeout, which can hang a worker on a slow/unresponsive host.
            parser = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(host_key, "/robots.txt")
            req = urllib.request.Request(robots_url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            parser.parse(raw.splitlines())
        except Exception:
            parser = None  # unreachable/missing robots -> fail-open (allow)
        with self._lock:
            self._cache[host_key] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._parser_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True
