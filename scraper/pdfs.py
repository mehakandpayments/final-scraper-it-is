"""PDF link tracking — download every PDF referenced by a page and hash it.

This is what makes change-monitoring useful for documents: the scraper notices
when the *same* PDF URL serves a *different* file (e.g. a regulator publishes
an updated version of a master direction at the same path) — not just when a
URL appears or disappears from the page.

``download_pdf`` streams the response so very large files don't blow up memory,
caps by a configurable byte limit, and reports a structured result (sha1, size,
content-type, HTTP status) per link. Failures are recorded, never raised — one
unreachable PDF can't abort a scrape of dozens.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass(slots=True)
class PdfInfo:
    url: str
    sha1: str = ""                  # hex sha1 of the file contents; "" on error
    size: int = 0
    content_type: str = ""
    http_status: int | None = None
    downloaded_at: str = ""         # ISO timestamp
    error: str | None = None
    change: str = ""                # set later by the diff: "new"/"changed"/"unchanged"/"error"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def download_pdf(client: httpx.Client, url: str, max_bytes: int = 50_000_000) -> PdfInfo:
    """Fetch a PDF, hash its bytes, return a :class:`PdfInfo`.

    Streams the response and aborts cleanly if the file exceeds ``max_bytes``
    (either declared via Content-Length or while reading). The PDF *bytes* are
    not kept — only the hash + metadata.
    """
    started = datetime.now(timezone.utc).isoformat()
    try:
        with client.stream("GET", url) as resp:
            ct = resp.headers.get("content-type", "")
            if resp.status_code >= 400:
                return PdfInfo(url=url, content_type=ct, http_status=resp.status_code,
                               downloaded_at=started, error=f"HTTP {resp.status_code}")
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                return PdfInfo(url=url, content_type=ct, http_status=resp.status_code,
                               size=int(cl), downloaded_at=started,
                               error=f"too large ({cl} bytes > cap {max_bytes})")
            hasher = hashlib.sha1()
            size = 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return PdfInfo(url=url, content_type=ct, http_status=resp.status_code,
                                   size=size, downloaded_at=started,
                                   error=f"too large (>{max_bytes} bytes)")
                hasher.update(chunk)
            return PdfInfo(url=url, sha1=hasher.hexdigest(), size=size, content_type=ct,
                           http_status=resp.status_code, downloaded_at=started)
    except Exception as exc:  # noqa: BLE001 - one bad PDF must not kill the scrape
        return PdfInfo(url=url, downloaded_at=started,
                       error=f"{type(exc).__name__}: {exc}")
