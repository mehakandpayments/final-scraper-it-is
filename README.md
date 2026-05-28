# 🕸️ General Web Scraper

A scalable, general-purpose web scraper with a Streamlit UI. Give it any URL and it:

1. **Fetches** the page — a fast `httpx` request, with an automatic **Playwright
   (headless Chromium)** fallback for JavaScript-rendered sites. *Auto* mode
   escalates to the browser on JS-shell heuristics **and** on a content-yield
   check (if a sizable page converts to near-empty Markdown it's re-rendered),
   so SPA pages that slip past the heuristics are still captured. Bare domains
   like `example.com` are accepted (assumed `https://`).
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

Each URL gets **its own self-contained folder** at `storage/<host>/<slug>/`:

| File          | Contents                                                                  |
| ------------- | ------------------------------------------------------------------------- |
| `page.md`     | Clean Markdown (tag-free) + YAML front-matter (url, title, …)            |
| `links.json`  | Full link inventory with `url`, `text`, `region`, `section`, `category`, `is_pdf`, internal/nofollow flags |
| `links.csv`   | The same inventory, spreadsheet-friendly                                  |
| `meta.json`   | Every other field as a clean JSON (status, timestamps, signals, …)        |

Plus a run-wide index at `storage/manifest.jsonl` (one JSON object per page).

The slug is `<path-slug>-<8-char-hash>` derived from the URL — deterministic, so
**re-scraping the same URL refreshes its own folder in place** (no half-stale
files). Different URLs land in different folders, so `storage/` accumulates
across runs. Toggle **Ephemeral storage** in the sidebar if you want the whole
folder wiped on each scrape and on exit instead.

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
| **Ephemeral storage**  | Overwrite the folder each run & wipe it on exit                     |
| **MongoDB**            | Optionally upsert each page into MongoDB (see below)               |

---

## Storing in MongoDB

The local files are great for browsing/downloading, but for **querying, dedup,
and scale** you can also persist every page into MongoDB (sidebar → *🗄️ MongoDB*
→ **Save to MongoDB**). It runs *alongside* the files and is unaffected by
*Ephemeral storage*.

### Why MongoDB?

- **The data is already document-shaped.** A scraped page *is* a JSON document —
  metadata + Markdown text + a nested array of links. Mongo stores that as-is;
  a relational DB would need a `pages` table plus a joined `links` table.
- **Flexible schema.** Pages differ (some error, some have PDFs, some are
  browser-rendered). Mongo doesn't force a rigid column layout.
- **Upsert = overwrite, no append.** One document per URL, keyed by the original
  URL. Re-scraping *updates that document in place* — `fetched_at` is set once,
  `last_updated_at` and `scrape_count` track revisits.
- **Queryable & indexed.** Find pages by domain, by category, full-text search
  the Markdown, or list every page that contains PDF links — all indexed.
- **Scales horizontally** (sharding/replication), matching the scraper's
  concurrent design.

### Document schema (`scraper.pages`)

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `_id` / `original_url` | string | The URL you submitted (natural key → dedupes) |
| `final_url` | string | URL after redirects |
| `host`, `domain` | string | `www.rbi.org.in`, `rbi.org.in` |
| `title` | string | Page title |
| `status_code` | int | HTTP status |
| `rendered` | bool | Was a headless browser used |
| `depth` | int | Crawl distance from a seed (0 = seed) |
| `elapsed_ms` | int | Fetch time |
| **`fetched_at`** | datetime | **First** time scraped (set once) |
| **`last_updated_at`** | datetime | **Most recent** scrape |
| `scrape_count` | int | How many times this URL was scraped |
| `content_hash` | string | `sha1(markdown)` — detect content changes |
| `markdown` | string | Clean, tag-free Markdown |
| `num_links` | int | Link count |
| `links[]` | array | `{url, text, region, section, category, rel, is_internal, is_nofollow, is_pdf}` |
| **`pdf_links[]`** | array | Convenience subset: links to `.pdf` files `{url, text, category}` |
| `num_pdf_links` | int | Count of PDF links |
| `error` | string\|null | Scrape error/warning |
| `local_markdown_path`, `local_links_path`, `local_folder` | string | Pointers to the on-disk artifacts |
| **`content_changed`** | bool | `true` iff `content_hash` differs from the previous scrape |
| **`content_changed_at`** | datetime | Last time the page's content actually changed |
| **`pdf_files[]`** | array | One entry per `.pdf` link with `sha1`, `size`, `content_type`, `http_status`, `change` (`new`/`changed`/`unchanged`/`error`) |
| **`pdfs_added_last` / `pdfs_removed_last` / `pdfs_changed_last`** | array | URLs added / removed / re-hashed since last scrape |
| **`links_added_last` / `links_removed_last`** | array | Link URLs added / removed since last scrape |
| **`change_history[]`** | array (capped 50) | One entry per scrape: `scraped_at`, `content_hash`, `content_changed`, link & pdf delta counts |

### Install & run MongoDB (macOS)

Already installed and running on this machine. From scratch:

```bash
brew tap mongodb/brew
brew install mongodb-community mongosh
brew services start mongodb-community      # starts mongod on localhost:27017
```

Alternatives: **Docker** — `docker run -d -p 27017:27017 --name mongo mongo:7`;
or a free cloud cluster on **MongoDB Atlas** (paste its URI into the sidebar).

### Create it

Nothing to create manually — MongoDB makes the **database and collection on the
first insert**. Just enable *Save to MongoDB* and scrape; `scraper.pages` (plus
its indexes) appears automatically.

### View it

**CLI (`mongosh`):**

```bash
mongosh                                  # connects to localhost:27017
use scraper
db.pages.countDocuments()                # how many pages stored
db.pages.findOne()                       # one full document
db.pages.find({}, {title:1, last_updated_at:1, num_pdf_links:1})
db.pages.find({ num_pdf_links: { $gt: 0 } })          // pages that have PDF links
db.pages.find({ domain: "rbi.org.in" })               // by site
db.pages.find({ $text: { $search: "master direction" } })   // full-text search

// --- change monitoring queries ---
db.pages.find({ content_changed: true })                          // pages whose content changed on the last scrape
db.pages.find({ pdfs_changed_last: { $ne: [] } })                 // pages with at least one PDF updated in place
db.pages.find({ pdfs_added_last:   { $ne: [] } })                 // new PDFs since last scrape
db.pages.find({}, { _id:1, content_changed_at:1, scrape_count:1 }) // when each URL last actually changed
```

**GUI:** install **MongoDB Compass** (`brew install --cask mongodb-compass`),
connect to `mongodb://localhost:27017`, open the `scraper` → `pages` collection
and browse/filter visually.

After a scrape, the app also prints the exact `mongosh` commands to view what it
just wrote.

---

## Getting past firewalls / bot protection

The scraper is built to reliably read **public pages** that sit behind common
bot-detection, and to **clearly report** when it hits a wall it can't (or
shouldn't) pass — rather than failing silently.

**What it handles (sidebar → 🛡️ Anti-blocking, and Fetch strategy):**

| Obstacle | How it's handled |
| -------- | ---------------- |
| User-Agent / header blocking | Realistic browser UA + headers (`Accept`, `Sec-Fetch-*`, …) |
| JavaScript-rendered pages / basic JS checks | Headless Chromium via Playwright |
| `navigator.webdriver` & trivial automation tells | **Browser stealth** (on by default) |
| Cloudflare "Just a moment…" JS challenge | Real browser + waits up to `challenge_max_wait` for it to clear |
| Rate limiting (HTTP 429 / 503) | Backoff that honours the `Retry-After` header + per-host throttle |
| IP-based blocking | Optional **Proxy URL** (route through your own proxy) |
| 403 / soft blocks in *Auto* mode | Auto-escalates from a static fetch to a real browser |

**What it does _not_ do (by design):**

- **Solve CAPTCHAs** (reCAPTCHA / hCaptcha / Cloudflare Turnstile) — these exist
  to require a human. The scraper detects them and reports `CAPTCHA challenge`.
- **Get past login walls / authentication / paywalls** — that's an access
  control, not an obstacle to route around (reported `Login/authentication required`).
- **Defeat hardened anti-bot** (DataDome, PerimeterX, Akamai) — usually needs
  dedicated services; out of scope. They're detected and named.

When pages are blocked, the results panel shows the detected reason and suggests
next steps (try *Always headless browser*, raise the per-host delay, or set a
proxy). Detection is tuned to avoid false positives — a normal page that merely
*mentions* "captcha" isn't flagged; only sparse interstitial/challenge pages are.

> Be a good citizen: scrape **public** data, honour `robots.txt` and rate limits,
> and respect each site's Terms of Service and applicable law.

---

## Use it as a library

```python
from scraper import Crawler, ScrapeConfig, ScrapeMode, RenderMode
from scraper.mongo_store import MongoStore

cfg = ScrapeConfig(mode=ScrapeMode.CRAWL, max_depth=2, max_pages=100,
                   render_mode=RenderMode.AUTO, concurrency=8)

mongo = MongoStore("mongodb://localhost:27017", "scraper", "pages")  # optional
for page in Crawler(cfg, mongo_store=mongo).run(["https://example.com"]):
    print(page.final_url, page.num_links, page.markdown_path, "in_db:", page.mongo_saved)
mongo.close()
```

`Crawler.run()` is a generator that yields a `PageResult` as each page finishes,
so it streams progress for any front-end. Pass `mongo_store=None` to skip the DB.

---

## Architecture

```
app.py                  Streamlit UI (input, progress, results, downloads)
scraper/
  config.py             ScrapeConfig + RenderMode / ScrapeMode enums
  models.py             FetchResult · LinkRecord · PageResult dataclasses
  fetcher.py            httpx static fetch + Playwright fallback, per-host throttle, retries
  converter.py          docling HTML -> Markdown (thread-local converter)
  tables.py             tidies docling tables (drop empty cols, unwrap layout tables)
  links.py              link extraction + region/section categorisation + PDF flag
  storage.py            writes .md + links.json/csv + manifest.jsonl (+ dir reset/wipe)
  mongo_store.py        optional MongoDB upsert (one document per URL)
  crawler.py            concurrent orchestration (single + recursive), yields results live
  utils.py              URL normalisation, slugs, domain logic, robots.txt gate
```

**Scaling further:** the pipeline is intentionally decoupled — `Crawler.process_url`
is a pure `url -> PageResult` unit of work. Swap the in-process `ThreadPoolExecutor`
for a distributed queue (e.g. Celery/RQ + Redis) and the same function becomes a
worker task, with `Storage` pointed at shared/object storage.
