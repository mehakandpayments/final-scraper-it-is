"""Configuration for a scrape run.

A single immutable :class:`ScrapeConfig` is threaded through the fetcher,
converter, link extractor, storage layer and crawler so behaviour is tuned in
exactly one place (and surfaced as one Streamlit sidebar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class RenderMode(str, Enum):
    """How a page's HTML is obtained."""

    AUTO = "auto"        # static fetch first, fall back to a browser if it looks JS-rendered
    STATIC = "static"    # httpx only (fastest, lightest)
    BROWSER = "browser"  # always render with Playwright/Chromium (most robust)


class ScrapeMode(str, Enum):
    """How far a submitted URL is followed."""

    SINGLE = "single"  # scrape exactly the submitted page(s)
    CRAWL = "crawl"    # follow same-site links recursively up to a depth/page budget


# A desktop Chrome UA keeps the most sites happy without pretending to be a bot.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class ScrapeConfig:
    # --- output ---
    output_dir: Path = Path("storage")

    # --- fetching ---
    render_mode: RenderMode = RenderMode.AUTO
    request_timeout: float = 30.0          # seconds per request
    user_agent: str = DEFAULT_USER_AGENT
    max_html_bytes: int = 8 * 1024 * 1024  # skip absurdly large responses

    # --- concurrency / politeness ---
    concurrency: int = 8                   # worker threads
    per_domain_delay: float = 0.5          # min seconds between hits to one host
    respect_robots: bool = True
    max_retries: int = 2

    # --- anti-blocking (for public pages behind bot detection) ---
    stealth: bool = True                   # reduce trivial automation fingerprints in the browser
    proxy: str | None = None               # e.g. "http://user:pass@host:port" (your own proxy)
    challenge_max_wait: float = 12.0       # seconds to let a JS (e.g. Cloudflare) challenge clear
    max_retry_after: float = 30.0          # cap on honouring a 429 Retry-After header

    # --- change monitoring ---
    monitor_pdfs: bool = True              # download each PDF link and hash it to detect changes
    pdf_max_bytes: int = 50 * 1024 * 1024  # skip any single PDF larger than this

    # --- crawl scope (ScrapeMode.CRAWL only) ---
    mode: ScrapeMode = ScrapeMode.SINGLE
    max_depth: int = 1                     # 0 = seeds only; 1 = seeds + their links; ...
    max_pages: int = 50                    # hard ceiling on pages per run
    same_domain_only: bool = True          # confine the crawl to the seed's registered domain
    include_subdomains: bool = True        # treat blog.x.com / x.com as the same site

    # --- conversion ---
    converter_backend: str = "docling"     # currently only docling is wired in

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        # Coerce strings (e.g. from the Streamlit UI) into the enums.
        if not isinstance(self.render_mode, RenderMode):
            self.render_mode = RenderMode(self.render_mode)
        if not isinstance(self.mode, ScrapeMode):
            self.mode = ScrapeMode(self.mode)
        self.concurrency = max(1, int(self.concurrency))
        self.max_pages = max(1, int(self.max_pages))
        self.max_depth = max(0, int(self.max_depth))
        self.proxy = self.proxy or None  # normalise "" -> None
