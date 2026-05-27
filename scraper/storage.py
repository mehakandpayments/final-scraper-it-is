"""Persist results to the local ``storage/`` folder.

For each page:

    storage/<host>/<slug>.md          # clean Markdown + YAML front-matter
    storage/<host>/<slug>.links.json  # full link inventory with categories
    storage/<host>/<slug>.links.csv   # same, spreadsheet-friendly

and one appended line per page in ``storage/manifest.jsonl`` (a run-wide index).
File stems embed a short URL hash, so distinct URLs never clobber each other and
re-scraping a URL deterministically overwrites its own files.
"""

from __future__ import annotations

import csv
import json
import shutil
import threading
from pathlib import Path

from .config import ScrapeConfig
from .models import PageResult
from .utils import hostname, path_slug

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

    def _page_dir(self, url: str) -> Path:
        host = hostname(url) or "unknown-host"
        d = self.root / host
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, result: PageResult) -> PageResult:
        """Write all artefacts for ``result`` and record the paths on it."""
        page_dir = self._page_dir(result.final_url or result.url)
        stem = path_slug(result.final_url or result.url)

        md_path = page_dir / f"{stem}.md"
        body = result.markdown or ""
        if result.error and not body:
            body = f"> Scrape note: {result.error}\n"
        # docling already emits the page's own headings, so we don't re-add the
        # title here (it lives in the front-matter); just guard an empty body.
        md_path.write_text(_front_matter(result) + body, encoding="utf-8")
        result.markdown_path = str(md_path)

        # Link inventory (JSON + CSV).
        links_json = page_dir / f"{stem}.links.json"
        link_dicts = [lr.to_dict() for lr in result.links]
        links_json.write_text(
            json.dumps(
                {"source_url": result.url, "final_url": result.final_url,
                 "fetched_at": result.fetched_at, "count": len(link_dicts), "links": link_dicts},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result.links_path = str(links_json)

        links_csv = page_dir / f"{stem}.links.csv"
        with links_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_LINK_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for d in link_dicts:
                writer.writerow(d)

        self._append_manifest(result)
        return result

    def _append_manifest(self, result: PageResult) -> None:
        line = json.dumps(result.summary(), ensure_ascii=False)
        with self._manifest_lock:
            with self._manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
