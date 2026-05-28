"""Concurrent orchestration of the fetch -> convert -> extract -> store pipeline.

The :meth:`Crawler.run` method is a generator: it dispatches per-URL work to a
thread pool and ``yield``\\s each :class:`PageResult` as it completes, so the
Streamlit layer can render live progress. All pool work happens in worker
threads; the orchestration loop (and therefore every ``yield``) runs on the
caller's thread, keeping it safe to drive UI updates from.

* **SINGLE** mode scrapes exactly the submitted URLs.
* **CRAWL** mode additionally follows in-scope links breadth-first up to
  ``max_depth`` / ``max_pages``.
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Callable, Iterator

from .config import RenderMode, ScrapeConfig, ScrapeMode
from .converter import html_to_markdown
from .fetcher import Fetcher
from .links import extract_links_and_title
from .models import PageResult
from .storage import Storage
from .utils import RobotsGate, hostname, is_http_url, normalize_url, registered_domain


class Crawler:
    def __init__(self, config: ScrapeConfig, mongo_store=None) -> None:
        self.cfg = config
        self.fetcher = Fetcher(config)
        self.storage = Storage(config)
        self.mongo = mongo_store  # optional MongoStore; None disables DB writes
        self.robots = RobotsGate(config.user_agent) if config.respect_robots else None
        self.stop_event = threading.Event()
        # Crawl scope, filled in by run() from the seed URLs.
        self._allowed_domains: set[str] = set()
        self._allowed_hosts: set[str] = set()

    # -- per-URL work (runs in a worker thread) -------------------------------

    def process_url(self, url: str, depth: int) -> PageResult:
        result = PageResult(url=url, final_url=url, depth=depth)
        if self.robots is not None and not self.robots.allowed(url):
            result.error = "blocked by robots.txt"
        else:
            fetch = self.fetcher.fetch(url)
            result.final_url = fetch.final_url or url
            result.status_code = fetch.status_code
            result.rendered = fetch.rendered
            result.elapsed_ms = fetch.elapsed_ms
            result.blocked_reason = fetch.blocked_reason
            result.signals = list(fetch.signals)
            if not fetch.ok:
                result.error = fetch.error or "fetch failed"
            else:
                markdown, md_err = html_to_markdown(fetch)
                links, title = extract_links_and_title(fetch.html, result.final_url)

                # Content-aware JS escalation: in AUTO mode a *static* fetch may
                # return HTTP 200 yet yield almost no content because the page
                # builds itself with JavaScript (the static text heuristic can
                # miss this when there's server-rendered nav/sidebar). If a
                # sizable page produced near-empty Markdown, render it for real
                # and keep whichever result has more content.
                if (self.cfg.render_mode is RenderMode.AUTO and not fetch.rendered
                        and len(markdown.strip()) < 200 and len(fetch.html) > 2000):
                    rfetch = self.fetcher.fetch_browser(url)
                    if rfetch.ok:
                        rmd, rmd_err = html_to_markdown(rfetch)
                        rlinks, rtitle = extract_links_and_title(rfetch.html, rfetch.final_url)
                        if len(rmd.strip()) > len(markdown.strip()):
                            fetch, markdown, md_err, links, title = rfetch, rmd, rmd_err, rlinks, rtitle
                            result.final_url = rfetch.final_url or url
                            result.status_code = rfetch.status_code
                            result.rendered = True
                            result.elapsed_ms += rfetch.elapsed_ms
                            result.blocked_reason = rfetch.blocked_reason
                            result.signals = list(rfetch.signals)

                result.markdown = markdown
                result.title = title
                result.links = links
                if md_err:
                    result.error = md_err  # soft: links may still be present

                # PDF change tracking: download + hash every direct .pdf link.
                if self.cfg.monitor_pdfs and result.links:
                    pdf_urls = sorted({lk.url for lk in result.links if lk.is_pdf})
                    if pdf_urls:
                        import time as _time
                        t0 = _time.monotonic()
                        result.pdf_files = [self.fetcher.download_pdf(u) for u in pdf_urls]
                        result.elapsed_ms += int((_time.monotonic() - t0) * 1000)

        self.storage.save(result)
        self._save_mongo(result)
        return result

    def _save_mongo(self, result: PageResult) -> None:
        if self.mongo is None:
            return
        try:
            self.mongo.save(result)
            result.mongo_saved = True
        except Exception as exc:  # DB hiccup must not abort the scrape
            result.mongo_error = f"{type(exc).__name__}: {exc}"

    # -- in-scope link filtering ---------------------------------------------

    def _in_scope(self, url: str) -> bool:
        if not is_http_url(url):
            return False
        if not self.cfg.same_domain_only:
            return True
        if self.cfg.include_subdomains:
            return registered_domain(url) in self._allowed_domains
        return hostname(url) in self._allowed_hosts

    # -- main loop (runs on the caller's thread) ------------------------------

    def run(
        self,
        seed_urls: list[str],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Iterator[PageResult]:
        """Yield a :class:`PageResult` per page as it completes.

        ``on_progress(done, total_known)`` is invoked on the caller's thread
        after each completion, where ``total_known`` is the number of URLs
        enqueued so far (it grows during a crawl).
        """
        seeds: list[str] = []
        seen_seed: set[str] = set()
        for raw in seed_urls:
            raw = raw.strip()
            if not raw:
                continue
            # Accept bare domains ("example.com") by assuming https://.
            if "://" not in raw and not raw.startswith("//"):
                raw = "https://" + raw
            u = normalize_url(raw)
            if u and is_http_url(u) and u not in seen_seed:
                seeds.append(u)
                seen_seed.add(u)
        self._allowed_domains = {registered_domain(u) for u in seeds}
        self._allowed_hosts = {hostname(u) for u in seeds}

        visited: set[str] = set()
        depth_of: dict[Future, int] = {}
        enqueued = 0
        done_count = 0

        try:
            with ThreadPoolExecutor(max_workers=self.cfg.concurrency) as pool:
                pending: set[Future] = set()

                def submit(url: str, depth: int) -> bool:
                    nonlocal enqueued
                    if url in visited or enqueued >= self.cfg.max_pages:
                        return False
                    visited.add(url)
                    enqueued += 1
                    fut = pool.submit(self.process_url, url, depth)
                    depth_of[fut] = depth
                    pending.add(fut)
                    return True

                for s in seeds:
                    submit(s, 0)

                while pending:
                    if self.stop_event.is_set():
                        for fut in pending:
                            fut.cancel()
                        break
                    finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        pending.discard(fut)
                        depth = depth_of.pop(fut, 0)
                        try:
                            result = fut.result()
                        except Exception as exc:  # defensive: never lose the loop
                            result = PageResult(url="<unknown>", final_url="<unknown>",
                                                depth=depth, error=f"worker crashed: {exc}")
                        done_count += 1
                        if on_progress is not None:
                            on_progress(done_count, enqueued)
                        yield result

                        # Expand the frontier (CRAWL mode only).
                        if (
                            self.cfg.mode is ScrapeMode.CRAWL
                            and depth < self.cfg.max_depth
                            and not self.stop_event.is_set()
                        ):
                            for link in result.links:
                                if enqueued >= self.cfg.max_pages:
                                    break
                                nu = link.url
                                if nu in visited or not self._in_scope(nu):
                                    continue
                                if link.is_nofollow and self.cfg.respect_robots:
                                    continue
                                submit(nu, depth + 1)
        finally:
            self.fetcher.close()
