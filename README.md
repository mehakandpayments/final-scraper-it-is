# 🕸️ General Web Scraper

A scalable, general-purpose web scraper with a Streamlit UI. Give it any URL and it:

1. **Fetches** the page — a fast `httpx` request, with an automatic **Playwright
   (headless Chromium)** fallback for JavaScript-rendered sites.
2. **Converts** the HTML to **clean Markdown** (no tags) using **[docling]**.
   **Tables are preserved as Markdown tables** — a post-processing pass
   ([`scraper/tables.py`](scraper/tables.py)) drops phantom empty columns,
   unwraps layout tables back into prose, and strips HTML comments, so genuine
   tables render as proper tables in any viewer.
3. **Stores** the `.md` in a local `storage/` folder (with YAML front-matter).
4. **Extracts every link** and the **category it is listed under** (the section
   heading and structural region — nav, footer, sidebar, main, …), saved as
   JSON + CSV.

It runs single pages or recursively crawls a whole site, concurrently.

[docling]: https://github.com/docling-project/docling

---

## Quick start

```bash
./run.sh                       # first run creates the venv + installs everything
# or, if the venv already exists:
.venv/bin/streamlit run app.py
```

Then open the URL Streamlit prints (default <http://localhost:8501>), paste one
or more URLs, pick a scope, and hit **Scrape**.

> Requires **Python 3.12** (the docling/torch stack is most reliable there).

### Manual setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium      # one-time browser download
.venv/bin/streamlit run app.py
```

---

## What gets written

For every page, under `storage/<host>/`:

| File                | Contents                                                        |
| ------------------- | --------------------------------------------------------------- |
| `<slug>.md`         | Clean Markdown (tag-free) + YAML front-matter (url, title, …)   |
| `<slug>.links.json` | Full link inventory with `url`, `text`, `region`, `section`, `category`, internal/nofollow flags |
| `<slug>.links.csv`  | The same inventory, spreadsheet-friendly                        |

Plus a run-wide index at `storage/manifest.jsonl` (one JSON object per page).

The file stem is `<path-slug>-<8-char-hash>`, so distinct URLs never collide and
re-scraping a URL deterministically overwrites its own files.

### How "category" is decided

Each link is tagged on two axes and given a human label:

- **region** — structural area inferred from semantic tags / ARIA roles /
  class & id hints on its ancestors: `navigation`, `header`, `footer`,
  `sidebar`, `breadcrumb`, `main`, `content`.
- **section** — the nearest preceding heading (`h1`–`h6`) in the document.
- **category** — the section heading when present, otherwise a friendly region
  name (e.g. *Navigation*, *Footer*).

---

## Configuration (sidebar)

| Setting              | Notes                                                                 |
| -------------------- | --------------------------------------------------------------------- |
| **Scope**            | *Single page* or *Recursive crawl*                                    |
| **Fetch strategy**   | *Auto* (static + JS fallback) · *Static only* · *Always browser*      |
| **Max depth / pages**| Crawl bounds (`max_pages` is a hard ceiling)                          |
| **Same site / subdomains** | Confine a crawl to the seed's registered domain                 |
| **Concurrent workers** | Size of the thread pool                                             |
| **Min delay per host** | Politeness throttle between hits to the same host                   |
| **Respect robots.txt** | Honour `robots.txt` (fail-open if unreachable)                      |

---

## Use it as a library

```python
from scraper import Crawler, ScrapeConfig, ScrapeMode, RenderMode

cfg = ScrapeConfig(mode=ScrapeMode.CRAWL, max_depth=2, max_pages=100,
                   render_mode=RenderMode.AUTO, concurrency=8)
for page in Crawler(cfg).run(["https://example.com"]):
    print(page.final_url, page.num_links, page.markdown_path)
```

`Crawler.run()` is a generator that yields a `PageResult` as each page finishes,
so it streams progress for any front-end.

---

## Architecture

```
app.py                  Streamlit UI (input, progress, results, downloads)
scraper/
  config.py             ScrapeConfig + RenderMode / ScrapeMode enums
  models.py             FetchResult · LinkRecord · PageResult dataclasses
  fetcher.py            httpx static fetch + Playwright fallback, per-host throttle, retries
  converter.py          docling HTML -> Markdown (thread-local converter)
  links.py              link extraction + region/section categorisation (BeautifulSoup)
  storage.py            writes .md + links.json/csv + manifest.jsonl
  crawler.py            concurrent orchestration (single + recursive), yields results live
  utils.py              URL normalisation, slugs, domain logic, robots.txt gate
```

**Scaling further:** the pipeline is intentionally decoupled — `Crawler.process_url`
is a pure `url -> PageResult` unit of work. Swap the in-process `ThreadPoolExecutor`
for a distributed queue (e.g. Celery/RQ + Redis) and the same function becomes a
worker task, with `Storage` pointed at shared/object storage.
