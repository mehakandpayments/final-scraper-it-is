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
    blocked_reason: str | None = None  # set when a firewall/bot-wall was detected
    signals: list[str] = field(default_factory=list)  # informational (e.g. reCAPTCHA v3)

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
    is_pdf: bool = False          # link points directly at a .pdf resource

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChangeSummary:
    """Per-page change report computed by MongoStore when an existing doc exists."""

    is_first_scrape: bool = True
    content_changed: bool = False              # markdown sha1 differs from previous run
    content_changed_at: str = ""               # ISO timestamp of the last actual content change
    links_added: list[str] = field(default_factory=list)
    links_removed: list[str] = field(default_factory=list)
    pdfs_added: list[str] = field(default_factory=list)
    pdfs_removed: list[str] = field(default_factory=list)
    pdfs_changed: list[str] = field(default_factory=list)  # same URL, new hash

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_changes(self) -> bool:
        return (self.content_changed or
                bool(self.links_added or self.links_removed
                     or self.pdfs_added or self.pdfs_removed or self.pdfs_changed))


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
    folder: str = ""              # per-URL output folder
    markdown_path: str = ""       # path to page.md inside that folder
    links_path: str = ""          # path to links.json inside that folder
    links: list[LinkRecord] = field(default_factory=list)

    error: str | None = None
    blocked_reason: str | None = None  # firewall/bot-wall detected (subset of error)
    signals: list[str] = field(default_factory=list)  # informational findings (non-blocking)
    # PDF link tracking & change monitoring
    pdf_files: list[Any] = field(default_factory=list)  # list[PdfInfo]; Any to avoid circular import
    changes: Any = None  # ChangeSummary | None — set by MongoStore after diff
    mongo_saved: bool = False      # upserted into MongoDB this run
    mongo_error: str | None = None # DB write failure (kept separate from scrape error)

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
            "folder": self.folder,
            "markdown_path": self.markdown_path,
            "links_path": self.links_path,
            "error": self.error,
            "blocked_reason": self.blocked_reason,
            "signals": list(self.signals),
            "num_pdf_files": len(self.pdf_files),
            "changes": self.changes.to_dict() if self.changes is not None else None,
            "mongo_saved": self.mongo_saved,
        }
