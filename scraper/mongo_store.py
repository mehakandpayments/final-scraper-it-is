"""Persist scraped pages into MongoDB (optional, persistent layer).

Each page becomes **one document**, keyed by its original (requested) URL, so
re-scraping a URL *updates the same document in place* — overwrite, never
append. ``fetched_at`` is stamped once on first insert; ``last_updated_at`` is
refreshed every scrape, and ``scrape_count`` is incremented.

Document schema (collection defaults to ``scraper.pages``):

    {
      _id:               <original_url>,          # natural key -> dedupes by URL
      original_url:      str,                      # the URL you submitted
      final_url:         str,                      # after redirects
      host:              str,   domain: str,
      title:             str,
      status_code:       int|null,
      rendered:          bool,                     # was a headless browser used
      depth:             int,                      # crawl distance from a seed
      elapsed_ms:        int,
      fetched_at:        datetime,                 # first time we scraped it
      last_updated_at:   datetime,                 # most recent scrape
      scrape_count:      int,                      # how many times scraped
      content_hash:      str,                      # sha1(markdown) -> detect changes
      markdown:          str,                      # clean, tag-free Markdown
      num_links:         int,
      links: [ { url, text, region, section, category, rel,
                 is_internal, is_nofollow, is_pdf } ],
      pdf_links: [ { url, text, category } ],      # convenience subset of links
      num_pdf_links:     int,
      error:             str|null,
      local_markdown_path: str,  local_links_path: str,   # pointers to the files
    }
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .models import ChangeSummary, PageResult
from .utils import hostname, registered_domain

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "scraper"
DEFAULT_COLLECTION = "pages"


def check_connection(uri: str = DEFAULT_URI, timeout_ms: int = 1500) -> tuple[bool, str]:
    """Quick reachability probe for the UI. Returns ``(ok, message)``."""
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, appname="general-scraper")
        try:
            info = client.server_info()
            return True, f"connected (MongoDB {info.get('version', '?')})"
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


class MongoStore:
    """Thread-safe MongoDB writer (the underlying client pools connections)."""

    def __init__(self, uri: str = DEFAULT_URI, db_name: str = DEFAULT_DB,
                 collection: str = DEFAULT_COLLECTION, timeout_ms: int = 3000) -> None:
        from pymongo import MongoClient

        self.uri, self.db_name, self.collection_name = uri, db_name, collection
        self._client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, appname="general-scraper")
        self._col = self._client[db_name][collection]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        from pymongo import ASCENDING, DESCENDING, TEXT

        # _id (original_url) is already a unique index. Add query-friendly ones.
        self._col.create_index([("domain", ASCENDING)])
        self._col.create_index([("host", ASCENDING)])
        self._col.create_index([("last_updated_at", DESCENDING)])
        self._col.create_index([("links.category", ASCENDING)])
        self._col.create_index([("num_pdf_links", DESCENDING)])
        self._col.create_index([("content_changed_at", DESCENDING)])
        self._col.create_index([("content_changed", ASCENDING)])
        self._col.create_index([("pdf_files.url", ASCENDING)])
        try:  # full-text search over title + body (best-effort)
            self._col.create_index([("title", TEXT), ("markdown", TEXT)], name="text_search")
        except Exception:
            pass

    def ping(self) -> bool:
        self._client.admin.command("ping")
        return True

    @staticmethod
    def _to_dt(iso: str) -> datetime:
        try:
            return datetime.fromisoformat(iso)
        except Exception:
            return datetime.now(timezone.utc)

    def _document(self, result: PageResult, content_hash: str) -> dict:
        links = [lk.to_dict() for lk in result.links]
        pdf_links = [{"url": l["url"], "text": l["text"], "category": l["category"]}
                     for l in links if l.get("is_pdf")]
        pdf_files = [p.to_dict() for p in result.pdf_files]
        ref = result.final_url or result.url
        return {
            "original_url": result.url,
            "final_url": result.final_url,
            "host": hostname(ref),
            "domain": registered_domain(ref),
            "title": result.title,
            "status_code": result.status_code,
            "rendered": result.rendered,
            "depth": result.depth,
            "elapsed_ms": result.elapsed_ms,
            "content_hash": content_hash,
            "markdown": result.markdown,
            "num_links": result.num_links,
            "links": links,
            "pdf_links": pdf_links,
            "num_pdf_links": len(pdf_links),
            "pdf_files": pdf_files,
            "num_pdfs_downloaded": sum(1 for p in pdf_files if p.get("sha1")),
            "num_pdfs_failed": sum(1 for p in pdf_files if p.get("error")),
            "error": result.error,
            "blocked_reason": result.blocked_reason,
            "signals": list(result.signals),
            "local_folder": result.folder,
            "local_markdown_path": result.markdown_path,
            "local_links_path": result.links_path,
        }

    @staticmethod
    def _compute_changes(prev: dict | None, new_hash: str, result: PageResult,
                         scraped_at: datetime) -> ChangeSummary:
        """Build a ChangeSummary by diffing the previous Mongo doc against this scrape."""
        summary = ChangeSummary(is_first_scrape=prev is None)
        new_link_urls = {lk.url for lk in result.links}
        new_pdf_urls = {p.url for p in result.pdf_files}

        if prev is None:
            summary.content_changed = False
            summary.content_changed_at = scraped_at.isoformat()
            return summary

        old_hash = prev.get("content_hash", "") or ""
        if old_hash and old_hash != new_hash:
            summary.content_changed = True
            summary.content_changed_at = scraped_at.isoformat()
        else:
            prev_changed_at = prev.get("content_changed_at")
            summary.content_changed_at = (
                prev_changed_at.isoformat() if isinstance(prev_changed_at, datetime)
                else (prev_changed_at or scraped_at.isoformat())
            )

        old_link_urls = {l.get("url") for l in (prev.get("links") or []) if l.get("url")}
        summary.links_added = sorted(new_link_urls - old_link_urls)
        summary.links_removed = sorted(old_link_urls - new_link_urls)

        old_pdf_map = {p.get("url"): p.get("sha1")
                       for p in (prev.get("pdf_files") or []) if p.get("url")}
        old_pdf_urls = set(old_pdf_map)
        summary.pdfs_added = sorted(new_pdf_urls - old_pdf_urls)
        summary.pdfs_removed = sorted(old_pdf_urls - new_pdf_urls)
        summary.pdfs_changed = sorted(
            p.url for p in result.pdf_files
            if p.url in old_pdf_urls and p.sha1 and old_pdf_map.get(p.url)
            and p.sha1 != old_pdf_map[p.url]
        )
        return summary

    @staticmethod
    def _annotate_pdf_changes(result: PageResult, summary: ChangeSummary) -> None:
        added, changed = set(summary.pdfs_added), set(summary.pdfs_changed)
        for p in result.pdf_files:
            if p.error:
                p.change = "error"
            elif summary.is_first_scrape:
                p.change = "new"
            elif p.url in added:
                p.change = "new"
            elif p.url in changed:
                p.change = "changed"
            else:
                p.change = "unchanged"

    def save(self, result: PageResult) -> str:
        """Upsert one page; compute change diff vs previous run; push capped history."""
        scraped_at = self._to_dt(result.fetched_at)
        new_hash = hashlib.sha1((result.markdown or "").encode("utf-8")).hexdigest()

        # Read previous state for diffing (projected: only what we need).
        prev = self._col.find_one(
            {"_id": result.url},
            {"content_hash": 1, "content_changed_at": 1,
             "links.url": 1, "pdf_files.url": 1, "pdf_files.sha1": 1},
        )

        summary = self._compute_changes(prev, new_hash, result, scraped_at)
        self._annotate_pdf_changes(result, summary)
        result.changes = summary

        doc = self._document(result, content_hash=new_hash)
        doc["last_updated_at"] = scraped_at
        doc["content_changed"] = summary.content_changed
        doc["content_changed_at"] = self._to_dt(summary.content_changed_at) \
            if summary.content_changed_at else scraped_at
        doc["links_added_last"] = summary.links_added
        doc["links_removed_last"] = summary.links_removed
        doc["pdfs_added_last"] = summary.pdfs_added
        doc["pdfs_removed_last"] = summary.pdfs_removed
        doc["pdfs_changed_last"] = summary.pdfs_changed

        history_entry = {
            "scraped_at": scraped_at,
            "content_hash": new_hash,
            "content_changed": summary.content_changed,
            "status_code": result.status_code,
            "num_links": result.num_links,
            "num_pdf_links": doc["num_pdf_links"],
            "links_added": len(summary.links_added),
            "links_removed": len(summary.links_removed),
            "pdfs_added": len(summary.pdfs_added),
            "pdfs_removed": len(summary.pdfs_removed),
            "pdfs_changed": len(summary.pdfs_changed),
        }

        self._col.update_one(
            {"_id": result.url},
            {
                "$set": doc,
                "$setOnInsert": {"fetched_at": scraped_at},
                "$inc": {"scrape_count": 1},
                "$push": {"change_history": {"$each": [history_entry], "$slice": -50}},
            },
            upsert=True,
        )
        return result.url

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
