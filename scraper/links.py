"""Extract every link on a page and the *category* it is listed under.

"Category" is resolved on two axes and combined into a human label:

* **region**  – the structural area the link lives in (navigation, header,
  footer, sidebar, breadcrumb, main, content), inferred from semantic tags,
  ARIA roles and class/id hints on the link's ancestors.
* **section** – the nearest preceding heading (``h1``-``h6``) in document order.

So a link gets a category like ``"Navigation"`` or ``"Pricing"`` (its section),
which is exactly the kind of grouping a human would read off the page.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import LinkRecord
from .utils import is_http_url, normalize_url, registered_domain

_WS_RE = re.compile(r"\s+")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "data:", "#", "sms:", "file:")

# region -> friendly label used when a link has no nearer heading.
_REGION_LABEL = {
    "navigation": "Navigation",
    "header": "Header",
    "footer": "Footer",
    "sidebar": "Sidebar",
    "breadcrumb": "Breadcrumb",
    "main": "Main content",
    "content": "Content",
}

# ARIA role -> region.
_ROLE_REGION = {
    "navigation": "navigation",
    "banner": "header",
    "contentinfo": "footer",
    "complementary": "sidebar",
    "main": "main",
}

# Tag name -> region.
_TAG_REGION = {
    "nav": "navigation",
    "header": "header",
    "footer": "footer",
    "aside": "sidebar",
    "main": "main",
}

# Substrings in class/id -> region (checked closest-ancestor first).
_HINT_REGION = [
    ("breadcrumb", "breadcrumb"),
    ("navbar", "navigation"),
    ("nav", "navigation"),
    ("menu", "navigation"),
    ("masthead", "header"),
    ("header", "header"),
    ("footer", "footer"),
    ("sidebar", "sidebar"),
    ("widget", "sidebar"),
    ("aside", "sidebar"),
    ("article", "main"),
    ("content", "content"),
    ("main", "main"),
]


def _clean(text: str, limit: int = 300) -> str:
    return _WS_RE.sub(" ", (text or "").strip())[:limit]


def _region_for(anchor) -> str:
    """Walk ancestors (closest first) and classify the link's structural area."""
    for parent in anchor.parents:
        name = getattr(parent, "name", None)
        if not name:
            continue
        if name in _TAG_REGION:
            return _TAG_REGION[name]
        role = (parent.get("role") or "").strip().lower() if parent.has_attr("role") else ""
        if role in _ROLE_REGION:
            return _ROLE_REGION[role]
        ident = " ".join(parent.get("class", [])) + " " + (parent.get("id") or "")
        ident = ident.lower()
        if ident.strip():
            for hint, region in _HINT_REGION:
                if hint in ident:
                    return region
        if name == "body":
            break
    return "content"


def _anchor_text(anchor) -> str:
    text = _clean(anchor.get_text(" ", strip=True))
    if text:
        return text
    # Fall back to accessible labels / image alt for icon-only links.
    for attr in ("aria-label", "title"):
        if anchor.has_attr(attr) and anchor.get(attr):
            return _clean(anchor.get(attr))
    img = anchor.find("img")
    if img is not None and img.get("alt"):
        return _clean(img.get("alt"))
    return ""


def extract_title(soup: BeautifulSoup) -> str:
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _clean(og["content"], 200)
    if soup.title and soup.title.string:
        return _clean(soup.title.string, 200)
    h1 = soup.find("h1")
    if h1:
        return _clean(h1.get_text(" ", strip=True), 200)
    return ""


def extract_links_and_title(html: str, base_url: str) -> tuple[list[LinkRecord], str]:
    """Parse ``html`` and return ``(links, page_title)``.

    Links are absolute + normalised, de-duplicated on (url, category, text),
    and tagged with their region/section/category.
    """
    soup = BeautifulSoup(html or "", "lxml")
    title = extract_title(soup)
    page_domain = registered_domain(base_url)

    # One document-order pass tracks the "current" heading so each anchor can be
    # attributed to the section it appears under. (Parents precede children in
    # find_all order, so an <a> inside an <h2> still maps to that heading.)
    current_section = ""
    records: list[LinkRecord] = []
    seen: set[tuple[str, str, str]] = set()

    for el in soup.find_all([*_HEADINGS, "a"]):
        if el.name in _HEADINGS:
            current_section = _clean(el.get_text(" ", strip=True), 160)
            continue

        href = (el.get("href") or "").strip()
        if not href or href.lower().startswith(_SKIP_PREFIXES):
            continue
        abs_url = normalize_url(urljoin(base_url, href))
        if not is_http_url(abs_url):
            continue

        region = _region_for(el)
        section = current_section
        category = section or _REGION_LABEL.get(region, region.title())
        rel = " ".join(el.get("rel", [])) if el.has_attr("rel") else ""

        key = (abs_url, category, _anchor_text(el))
        if key in seen:
            continue
        seen.add(key)

        records.append(
            LinkRecord(
                url=abs_url,
                text=_anchor_text(el),
                region=region,
                section=section,
                category=category,
                rel=rel,
                is_internal=registered_domain(abs_url) == page_domain,
                is_nofollow="nofollow" in rel.lower(),
            )
        )

    return records, title
