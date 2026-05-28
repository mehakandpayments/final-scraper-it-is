"""Streamlit front-end for the scraper.

Paste one or more URLs, choose single-page or recursive-crawl, and the app
fetches each page (httpx with a Playwright fallback), converts it to clean
Markdown with docling, extracts every link and the category it sits under, and
writes everything to the local ``storage/`` folder.
"""

from __future__ import annotations

import atexit
import io
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from scraper import Crawler, RenderMode, ScrapeConfig, ScrapeMode
from scraper.storage import clear_dir, wipe_dir
from scraper.mongo_store import MongoStore, check_connection, DEFAULT_URI, DEFAULT_DB, DEFAULT_COLLECTION
from scraper.antiblock import HARD_REASONS

st.set_page_config(page_title="General Web Scraper", page_icon="🕸️", layout="wide")


@st.cache_resource
def _storage_janitor() -> dict:
    """Process-level storage lifecycle (runs once per server process).

    Holds the current output dir + ephemeral flag (refreshed each rerun) and
    registers an atexit hook so that, on Ctrl+C / shutdown, an ephemeral
    storage folder is wiped — nothing is left behind.
    """
    state = {"dir": "storage", "ephemeral": True}
    atexit.register(lambda: wipe_dir(state["dir"]) if state["ephemeral"] else None)
    return state


# --------------------------------------------------------------------------- #
# Sidebar: run configuration
# --------------------------------------------------------------------------- #
def sidebar_config() -> ScrapeConfig:
    st.sidebar.header("⚙️ Configuration")

    mode_label = st.sidebar.radio(
        "Scope",
        ["Single page", "Recursive crawl"],
        help="Single page scrapes exactly the URLs you submit. "
             "Recursive crawl also follows in-scope links.",
    )
    mode = ScrapeMode.CRAWL if mode_label == "Recursive crawl" else ScrapeMode.SINGLE

    render_label = st.sidebar.selectbox(
        "Fetch strategy",
        ["Auto (static + JS fallback)", "Static only (fast)", "Always headless browser"],
        help="Auto uses a fast HTTP request first and only spins up a headless "
             "browser when a page looks JavaScript-rendered.",
    )
    render_mode = {
        "Auto (static + JS fallback)": RenderMode.AUTO,
        "Static only (fast)": RenderMode.STATIC,
        "Always headless browser": RenderMode.BROWSER,
    }[render_label]

    with st.sidebar.expander("Crawl scope", expanded=(mode is ScrapeMode.CRAWL)):
        max_depth = st.slider("Max depth", 0, 5, 1,
                              help="0 = seeds only · 1 = seeds + their links · …",
                              disabled=mode is ScrapeMode.SINGLE)
        max_pages = st.number_input("Max pages (hard cap)", 1, 5000, 50, step=10)
        same_domain_only = st.checkbox("Stay on the same site", value=True,
                                       disabled=mode is ScrapeMode.SINGLE)
        include_subdomains = st.checkbox("Include subdomains", value=True,
                                         disabled=mode is ScrapeMode.SINGLE)

    with st.sidebar.expander("Performance & politeness", expanded=False):
        concurrency = st.slider("Concurrent workers", 1, 32, 8)
        per_domain_delay = st.slider("Min delay per host (s)", 0.0, 3.0, 0.5, 0.1)
        respect_robots = st.checkbox("Respect robots.txt", value=True)
        request_timeout = st.slider("Request timeout (s)", 5, 120, 30, 5)

    with st.sidebar.expander("🔄 Change monitoring", expanded=False):
        monitor_pdfs = st.checkbox(
            "Track PDF changes", value=True,
            help="Download every PDF link and hash it, so re-scrapes can flag "
                 "PDFs that were added, removed, or updated in place (same URL, "
                 "new content). Requires MongoDB to compare across runs.",
        )
        pdf_max_mb = st.slider("Max PDF size (MB)", 1, 200, 50, 1,
                               help="PDFs larger than this are skipped, not downloaded.")
        st.caption("Page-content + link-set diffing run automatically whenever "
                   "Mongo is enabled.")

    with st.sidebar.expander("🛡️ Anti-blocking", expanded=False):
        stealth = st.checkbox(
            "Browser stealth", value=True,
            help="Make the headless browser look like a normal browser to basic "
                 "bot checks (hides navigator.webdriver, etc.). Used in browser mode.",
        )
        proxy = st.text_input(
            "Proxy URL (optional)", value="", placeholder="http://user:pass@host:port",
            help="Route requests through your own proxy to get past IP-based blocks.",
        )
        st.caption("Tip: for sites behind a JS firewall, set **Fetch strategy → "
                   "Always headless browser**. CAPTCHAs / login walls can't be bypassed.")

    output_dir = st.sidebar.text_input("Output folder", value="storage")
    ephemeral = st.sidebar.checkbox(
        "Ephemeral storage", value=False,
        help="When OFF (default): each URL gets its own folder and storage "
             "accumulates across runs (re-scraping a URL refreshes its folder). "
             "When ON: the whole output folder is wiped before each scrape and "
             "on app exit.",
    )

    with st.sidebar.expander("🗄️ MongoDB (optional)", expanded=False):
        mongo_enabled = st.checkbox(
            "Save to MongoDB", value=False,
            help="Upsert each page as a document (keyed by URL) into MongoDB. "
                 "Persistent — not affected by 'Ephemeral storage'.",
        )
        mongo_uri = st.text_input("Connection URI", value=DEFAULT_URI)
        mongo_db = st.text_input("Database", value=DEFAULT_DB)
        mongo_collection = st.text_input("Collection", value=DEFAULT_COLLECTION)
        if mongo_enabled:
            ok, msg = check_connection(mongo_uri)
            (st.success if ok else st.error)(f"{'✅' if ok else '❌'} {msg}")
    mongo_cfg = {"enabled": mongo_enabled, "uri": mongo_uri,
                 "db": mongo_db, "collection": mongo_collection}

    cfg = ScrapeConfig(
        output_dir=output_dir,
        render_mode=render_mode,
        request_timeout=float(request_timeout),
        concurrency=int(concurrency),
        per_domain_delay=float(per_domain_delay),
        respect_robots=respect_robots,
        mode=mode,
        max_depth=int(max_depth),
        max_pages=int(max_pages),
        same_domain_only=same_domain_only,
        include_subdomains=include_subdomains,
        stealth=stealth,
        proxy=proxy.strip() or None,
        monitor_pdfs=monitor_pdfs,
        pdf_max_bytes=int(pdf_max_mb) * 1024 * 1024,
    )
    return cfg, ephemeral, mongo_cfg


# --------------------------------------------------------------------------- #
# Scrape run
# --------------------------------------------------------------------------- #
def run_scrape(urls: list[str], cfg: ScrapeConfig, mongo_store: MongoStore | None = None) -> list[dict]:
    crawler = Crawler(cfg, mongo_store=mongo_store)

    progress = st.progress(0.0, text="Starting…")
    status = st.empty()
    live_table = st.empty()

    collected: list[dict] = []
    ok = failed = saved = 0
    try:
        for result in crawler.run(urls):
            if result.error:
                failed += 1
            else:
                ok += 1
            if result.mongo_saved:
                saved += 1
            collected.append(
                {
                    **result.summary(),
                    "markdown": result.markdown,
                    "links": [lk.to_dict() for lk in result.links],
                    "pdf_files": [p.to_dict() for p in result.pdf_files],
                    "mongo_error": result.mongo_error,
                }
            )
            done = len(collected)
            denom = max(done, cfg.max_pages if cfg.mode is ScrapeMode.CRAWL else len(urls))
            db_note = f" · 🗄️ {saved} in DB" if mongo_store is not None else ""
            progress.progress(min(done / max(denom, 1), 1.0),
                              text=f"Scraped {done} page(s) · {ok} ok · {failed} failed{db_note}")
            status.caption(f"Last: {result.final_url}  →  {result.num_links} links"
                           + (f"  ·  ⚠️ {result.error}" if result.error else ""))
            live_table.dataframe(
                pd.DataFrame([{"url": r["final_url"], "status": r["status"],
                               "links": r["num_links"], "rendered": r["rendered"],
                               "in_db": r["mongo_saved"], "error": r["error"]} for r in collected]),
                use_container_width=True, hide_index=True,
            )
    finally:
        if mongo_store is not None:
            mongo_store.close()

    progress.progress(1.0, text=f"Done · {ok} ok · {failed} failed")
    return collected


# --------------------------------------------------------------------------- #
# Results rendering
# --------------------------------------------------------------------------- #
def zip_results(results: list[dict]) -> bytes:
    """Bundle every per-URL folder for the run into a single zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            folder = Path(r.get("folder") or "")
            if folder.is_dir():
                for f in folder.iterdir():
                    if f.is_file():
                        zf.write(f, arcname=str(f))
            else:
                # Legacy fallback (single-file layout).
                for key in ("markdown_path", "links_path"):
                    p = Path(r.get(key) or "")
                    if p.is_file():
                        zf.write(p, arcname=str(p))
    return buf.getvalue()


def render_results(results: list[dict], output_dir: str, mongo: dict | None = None) -> None:
    total_links = sum(r["num_links"] for r in results)
    ok = sum(1 for r in results if not r["error"])
    rendered = sum(1 for r in results if r["rendered"])
    in_db = sum(1 for r in results if r.get("mongo_saved"))

    cols = st.columns(5 if mongo else 4)
    cols[0].metric("Pages", len(results))
    cols[1].metric("Succeeded", ok)
    cols[2].metric("Links found", total_links)
    cols[3].metric("Browser-rendered", rendered)
    if mongo:
        cols[4].metric("Saved to DB", in_db)

    st.caption(f"📁 Saved under `{Path(output_dir).resolve()}`")
    st.download_button("⬇️ Download all (.zip)", data=zip_results(results),
                       file_name="scrape_results.zip", mime="application/zip")

    if mongo:
        st.success(f"🗄️ Upserted {in_db} document(s) into MongoDB "
                   f"`{mongo['db']}.{mongo['collection']}`. View them with:")
        st.code(f"mongosh \"{mongo['uri']}\"\n"
                f"use {mongo['db']}\n"
                f"db.{mongo['collection']}.findOne()                 // one full document\n"
                f"db.{mongo['collection']}.find({{}}, {{title:1, last_updated_at:1, num_pdf_links:1}})\n"
                f"db.{mongo['collection']}.find({{num_pdf_links: {{$gt: 0}}}})   // pages with PDF links",
                language="javascript")

    blocked = [r for r in results if r.get("blocked_reason")]
    if blocked:
        reasons = sorted({r["blocked_reason"] for r in blocked})
        st.warning(f"🛡️ {len(blocked)} page(s) hit a firewall / bot-wall — detected: "
                   + ", ".join(reasons))
        if any(r["blocked_reason"] in HARD_REASONS for r in blocked):
            st.caption("• Human-only gates (CAPTCHA / login) can't be bypassed — those pages need a human.")
        if any(r["blocked_reason"] not in HARD_REASONS for r in blocked):
            st.caption("• For the rest, try **Fetch strategy → Always headless browser**, raise "
                       "**Min delay per host**, or set a **Proxy URL** under 🛡️ Anti-blocking.")

    st.subheader("Pages")
    st.dataframe(
        pd.DataFrame([{k: r[k] for k in
                       ("final_url", "status", "num_links", "rendered", "depth", "elapsed_ms", "error")}
                      for r in results]),
        use_container_width=True, hide_index=True,
    )

    st.subheader("Page details")
    for i, r in enumerate(results):
        label = f"{'⚠️ ' if r['error'] else ''}{r['title'] or r['final_url']}  ·  {r['num_links']} links"
        with st.expander(label, expanded=(len(results) == 1)):
            st.write(f"**URL:** {r['final_url']}")
            meta = f"status `{r['status']}` · depth `{r['depth']}` · {'rendered' if r['rendered'] else 'static'}"
            if r["error"]:
                meta += f" · ⚠️ `{r['error']}`"
            st.caption(meta)
            if r.get("signals"):
                st.caption("🔎 " + " · ".join(r["signals"]))

            # Change badges (only set when Mongo is enabled and computed a diff)
            ch = r.get("changes")
            if ch:
                badges: list[str] = []
                if ch.get("is_first_scrape"):
                    badges.append("🆕 first scrape")
                if ch.get("content_changed"):
                    badges.append("🔄 content changed")
                la, lr_ = len(ch.get("links_added", [])), len(ch.get("links_removed", []))
                pa, pr_, pc = (len(ch.get("pdfs_added", [])), len(ch.get("pdfs_removed", [])),
                               len(ch.get("pdfs_changed", [])))
                if la or lr_:
                    badges.append(f"🔗 links +{la} -{lr_}")
                if pa or pr_ or pc:
                    badges.append(f"📑 PDFs +{pa} -{pr_} ~{pc}")
                if badges:
                    st.info(" · ".join(badges))

            tabs_def = ["📄 Markdown", "🔗 Links by category"]
            if r.get("pdf_files"):
                tabs_def.append("📥 PDFs")
            tab_objs = st.tabs(tabs_def)
            tab_md, tab_links = tab_objs[0], tab_objs[1]
            tab_pdfs = tab_objs[2] if len(tab_objs) > 2 else None
            with tab_md:
                if r["markdown"]:
                    st.download_button("⬇️ Download .md", data=r["markdown"],
                                       file_name=Path(r["markdown_path"]).name,
                                       mime="text/markdown", key=f"md-{i}")
                    st.markdown(r["markdown"])
                else:
                    st.info("No Markdown produced for this page.")
            with tab_links:
                if r["links"]:
                    df = pd.DataFrame(r["links"])
                    cats = ["(all)"] + sorted(df["category"].unique().tolist())
                    chosen = st.selectbox("Filter by category", cats, key=f"cat-{i}")
                    view = df if chosen == "(all)" else df[df["category"] == chosen]
                    st.caption(f"{len(view)} of {len(df)} links")
                    cols_show = [c for c in ["category", "region", "text", "url",
                                             "is_internal", "is_nofollow", "is_pdf"] if c in view.columns]
                    st.dataframe(view[cols_show], use_container_width=True, hide_index=True)
                    st.download_button("⬇️ Download links (.csv)",
                                       data=df.to_csv(index=False).encode("utf-8"),
                                       file_name=Path(r["links_path"]).with_suffix(".csv").name,
                                       mime="text/csv", key=f"links-{i}")
                else:
                    st.info("No links found on this page.")

            if tab_pdfs is not None:
                with tab_pdfs:
                    pdfs = r["pdf_files"]
                    by_change: dict[str, int] = {}
                    for p in pdfs:
                        by_change[p.get("change", "")] = by_change.get(p.get("change", ""), 0) + 1
                    summary_bits = [f"{n} {k or 'unknown'}" for k, n in sorted(by_change.items())]
                    st.caption(f"{len(pdfs)} PDF link(s) · " + " · ".join(summary_bits))
                    pdf_df = pd.DataFrame([{
                        "change": p.get("change") or "",
                        "url": p.get("url"),
                        "sha1": (p.get("sha1") or "")[:12],
                        "size_kb": round((p.get("size") or 0) / 1024, 1),
                        "http_status": p.get("http_status"),
                        "content_type": p.get("content_type") or "",
                        "downloaded_at": p.get("downloaded_at") or "",
                        "error": p.get("error") or "",
                    } for p in pdfs])
                    st.dataframe(pdf_df, use_container_width=True, hide_index=True)
                    st.download_button("⬇️ Download PDF inventory (.csv)",
                                       data=pdf_df.to_csv(index=False).encode("utf-8"),
                                       file_name="pdf_files.csv", mime="text/csv",
                                       key=f"pdfs-{i}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    st.title("🕸️ General Web Scraper")
    st.write("Fetch any page → clean **Markdown** (via docling) + a categorised "
             "**link inventory**, saved locally. Static fetch with a headless-browser "
             "fallback for JavaScript-rendered sites.")

    cfg, ephemeral, mongo_cfg = sidebar_config()

    # Keep the process-level janitor in sync with the current settings.
    janitor = _storage_janitor()
    janitor["dir"] = str(Path(cfg.output_dir).resolve())
    janitor["ephemeral"] = ephemeral
    if ephemeral:
        st.caption("🧹 Ephemeral storage: each scrape overwrites the output folder; "
                   "it's wiped when you stop the app. Use **Download all (.zip)** to keep results.")
    if mongo_cfg["enabled"]:
        st.caption(f"🗄️ MongoDB: pages upserted into `{mongo_cfg['db']}.{mongo_cfg['collection']}` "
                   "(persistent; one document per URL).")

    urls_text = st.text_area(
        "URLs to scrape (one per line)",
        placeholder="https://example.com\nhttps://docs.python.org/3/",
        height=120,
    )
    run = st.button("🚀 Scrape", type="primary")

    if run:
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Please enter at least one URL.")
        else:
            mongo_store = None
            if mongo_cfg["enabled"]:
                try:
                    mongo_store = MongoStore(mongo_cfg["uri"], mongo_cfg["db"], mongo_cfg["collection"])
                    mongo_store.ping()
                except Exception as exc:  # connect failed -> scrape to files only
                    st.error(f"MongoDB connection failed, saving to files only: {exc}")
                    mongo_store = None
            if ephemeral:
                clear_dir(cfg.output_dir)  # overwrite: no leftover/appended files
            with st.spinner("Scraping…"):
                results = run_scrape(urls, cfg, mongo_store)
            st.session_state["results"] = results
            st.session_state["output_dir"] = str(cfg.output_dir)
            st.session_state["mongo"] = mongo_cfg if (mongo_store is not None) else None

    if st.session_state.get("results"):
        st.divider()
        render_results(st.session_state["results"],
                       st.session_state.get("output_dir", "storage"),
                       st.session_state.get("mongo"))


if __name__ == "__main__":
    main()
