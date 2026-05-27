"""A scalable, general-purpose web scraper.

Pipeline per URL:
  fetch (httpx, with Playwright fallback for JS pages)
    -> convert HTML to clean Markdown (docling)
    -> extract every link and the category/section it sits under
    -> persist Markdown + link inventory to local storage.

The :class:`~scraper.crawler.Crawler` orchestrates this concurrently and
supports both single-page and recursive same-site crawling.
"""

from .config import ScrapeConfig, RenderMode, ScrapeMode
from .crawler import Crawler
from .models import PageResult, LinkRecord, FetchResult

__all__ = [
    "ScrapeConfig",
    "RenderMode",
    "ScrapeMode",
    "Crawler",
    "PageResult",
    "LinkRecord",
    "FetchResult",
]
