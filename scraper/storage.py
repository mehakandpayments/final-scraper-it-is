"""Persist results to the local ``storage/`` folder.

Each scraped URL gets its own self-contained folder:

    storage/<host>/<slug>/
        page.md        # clean Markdown + YAML front-matter
        links.json     # full link inventory with categories
        links.csv      # same, spreadsheet-friendly
        meta.json      # every other field (status, timestamps, signals, ...)

plus one appended line per page in ``storage/manifest.jsonl`` (a run-wide index).
The slug is a deterministic path-derived stem + short URL hash, so distinct URLs
never clobber each other and **re-scraping the same URL refreshes its own folder
in place** (no half-stale files). Different URLs sit in different folders, so the
overall ``storage/`` accumulates across runs.
"""

from __future__ import annotations

import csv
import json
import shutil
import threading
from pathlib import Path

from .config import ScrapeConfig
from .models import PageResult
from .utils import hostname, path_slug, registered_domain

_LINK_CSV_FIELDS = ["url", "text", "category", "region", "section", "is_internal", "is_nofollow", "rel"]


def _is_safe_target(p: Path) -> bool:
    """Guard against wiping the filesystem root or the user's home directory."""
    p = p.resolve()
    return p != Path(p.anchor) and p != Path.home().resolve() and str(p) not in ("", "/")


def clear_dir(path) -> None:
    """Empty ``path`` (overwrite semantics) but keep the directory itself."""
    p = Path(path).resolve()
    if not _is_safe_target(p):
        return
    if p.exists():
        for child in p.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    p.mkdir(parents=True, exist_ok=True)


def wipe_dir(path) -> None:
    """Remove ``path`` entirely (used to clean up on app exit)."""
    p = Path(path).resolve()
    if _is_safe_target(p):
        shutil.rmtree(p, ignore_errors=True)


def _yaml_escape(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _front_matter(result: PageResult) -> str:
    fields = {
        "url": result.url,
        "final_url": result.final_url,
        "title": result.title,
        "fetched_at": result.fetched_at,
        "status_code": result.status_code,
        "rendered": result.rendered,
        "depth": result.depth,
        "num_links": result.num_links,
    }
    lines = ["---"]
    for key, val in fields.items():
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)) or val is None:
            lines.append(f"{key}: {val if val is not None else 'null'}")
        else:
            lines.append(f"{key}: {_yaml_escape(val)}")
    lines.append("---\n")
    return "\n".join(lines)


class Storage:
    """Thread-safe writer shared across the worker pool."""

    def __init__(self, config: ScrapeConfig) -> None:
        self.root = Path(config.output_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_lock = threading.Lock()
        self._manifest_path = self.root / "manifest.jsonl"

    def _page_folder(self, url: str) -> Path:
        """``storage/<host>/<slug>/`` — one folder per URL, created on demand."""
        host = hostname(url) or "unknown-host"
        slug = path_slug(url)
        folder = self.root / host / slug
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _meta(self, result: PageResult) -> dict:
        """All scalar metadata for the page (everything except markdown + links)."""
        ref = result.final_url or result.url
        return {
            "url": result.url,
            "final_url": result.final_url,
            "title": result.title,
            "host": hostname(ref),
            "domain": registered_domain(ref),
            "fetched_at": result.fetched_at,
            "status_code": result.status_code,
            "rendered": result.rendered,
            "depth": result.depth,
            "elapsed_ms": result.elapsed_ms,
            "num_links": result.num_links,
            "num_pdf_links": sum(1 for lk in result.links if getattr(lk, "is_pdf", False)),
            "error": result.error,
            "blocked_reason": result.blocked_reason,
            "signals": list(result.signals),
            "pdf_files": [p.to_dict() for p in result.pdf_files],
            "changes": result.changes.to_dict() if result.changes is not None else None,
        }

    def save(self, result: PageResult) -> PageResult:
        """Write all artefacts for ``result`` into its own per-URL folder."""
        folder = self._page_folder(result.final_url or result.url)
        result.folder = str(folder)

        # page.md  — docling already emits the page's own headings, so we don't
        # re-add the title (it's in the front-matter); guard an empty body.
        md_path = folder / "page.md"
        body = result.markdown or ""
        if result.error and not body:
            body = f"> Scrape note: {result.error}\n"
        md_path.write_text(_front_matter(result) + body, encoding="utf-8")
        result.markdown_path = str(md_path)

        # links.json — full inventory with category/region/is_pdf etc.
        link_dicts = [lr.to_dict() for lr in result.links]
        links_json = folder / "links.json"
        links_json.write_text(
            json.dumps(
                {"source_url": result.url, "final_url": result.final_url,
                 "fetched_at": result.fetched_at, "count": len(link_dicts), "links": link_dicts},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result.links_path = str(links_json)

        # links.csv — same data, spreadsheet-friendly.
        links_csv = folder / "links.csv"
        with links_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_LINK_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for d in link_dicts:
                writer.writerow(d)

        # meta.json — everything else, as a clean JSON.
        (folder / "meta.json").write_text(
            json.dumps(self._meta(result), indent=2, ensure_ascii=False), encoding="utf-8",
        )

        self._append_manifest(result)
        return result

    def _append_manifest(self, result: PageResult) -> None:
        line = json.dumps(result.summary(), ensure_ascii=False)
        with self._manifest_lock:
            with self._manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
