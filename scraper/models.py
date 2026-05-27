"""Plain data containers passed between pipeline stages.

Kept dependency-free (stdlib only) so they're trivial to serialise to JSON for
storage and to hand to the Streamlit layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class FetchResult:
    """Raw outcome of fetching a single URL."""

    url: str                      # the URL we asked for
    final_url: str                # after redirects
    status_code: int | None
    html: str                     # empty on failure
    content_type: str = ""
    rendered: bool = False        # True if a headless browser produced this HTML
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.html) and (self.status_code or 0) < 400


@dataclass(slots=True)
class LinkRecord:
    """One extracted hyperlink plus where it lives in the page."""

    url: str                      # absolute, normalised
    text: str                     # visible anchor text
    region: str                   # structural area: nav / main / footer / sidebar / header / body
    section: str                  # nearest preceding heading text ("" if none)
    category: str                 # human label combining region + section
    rel: str = ""                 # rel attribute (nofollow, etc.)
    is_internal: bool = True      # same registered domain as the page
    is_nofollow: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PageResult:
    """Everything produced for one scraped page."""

    url: str
    final_url: str
    depth: int = 0
    status_code: int | None = None
    title: str = ""
    rendered: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    elapsed_ms: int = 0

    markdown: str = ""
    markdown_path: str = ""       # where the .md was written
    links_path: str = ""          # where the link inventory JSON was written
    links: list[LinkRecord] = field(default_factory=list)

    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def num_links(self) -> int:
        return len(self.links)

    def summary(self) -> dict[str, Any]:
        """Compact row for tables / manifest (omits the bulky markdown body)."""
        return {
            "url": self.url,
            "final_url": self.final_url,
            "depth": self.depth,
            "status": self.status_code,
            "title": self.title,
            "rendered": self.rendered,
            "num_links": self.num_links,
            "fetched_at": self.fetched_at,
            "elapsed_ms": self.elapsed_ms,
            "markdown_path": self.markdown_path,
            "links_path": self.links_path,
            "error": self.error,
        }
