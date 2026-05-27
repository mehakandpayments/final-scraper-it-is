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

    output_dir = st.sidebar.text_input("Output folder", value="storage")
    ephemeral = st.sidebar.checkbox(
        "Ephemeral storage", value=True,
        help="Overwrite the output folder before each scrape (no appending) and "
             "wipe it when the app stops. Uncheck to keep results across runs.",
    )

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
    )
    return cfg, ephemeral


# --------------------------------------------------------------------------- #
# Scrape run
# --------------------------------------------------------------------------- #
def run_scrape(urls: list[str], cfg: ScrapeConfig) -> list[dict]:
    crawler = Crawler(cfg)

    progress = st.progress(0.0, text="Starting…")
    status = st.empty()
    live_table = st.empty()

    collected: list[dict] = []
    ok = failed = 0
    for result in crawler.run(urls):
        if result.error:
            failed += 1
        else:
            ok += 1
        collected.append(
            {
                **result.summary(),
                "markdown": result.markdown,
                "links": [lk.to_dict() for lk in result.links],
            }
        )
        done = len(collected)
        denom = max(done, cfg.max_pages if cfg.mode is ScrapeMode.CRAWL else len(urls))
        progress.progress(min(done / max(denom, 1), 1.0),
                          text=f"Scraped {done} page(s) · {ok} ok · {failed} failed")
        status.caption(f"Last: {result.final_url}  →  {result.num_links} links"
                       + (f"  ·  ⚠️ {result.error}" if result.error else ""))
        live_table.dataframe(
            pd.DataFrame([{"url": r["final_url"], "status": r["status"],
                           "links": r["num_links"], "rendered": r["rendered"],
                           "error": r["error"]} for r in collected]),
            use_container_width=True, hide_index=True,
        )

    progress.progress(1.0, text=f"Done · {ok} ok · {failed} failed")
    return collected


# --------------------------------------------------------------------------- #
# Results rendering
# --------------------------------------------------------------------------- #
def zip_results(results: list[dict]) -> bytes:
    """Bundle every written .md / links file for the run into a zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            for key in ("markdown_path", "links_path"):
                p = Path(r.get(key) or "")
                if p.is_file():
                    zf.write(p, arcname=str(p))
                csv_path = p.with_suffix(".csv") if key == "links_path" else None
                if csv_path and csv_path.is_file():
                    zf.write(csv_path, arcname=str(csv_path))
    return buf.getvalue()


def render_results(results: list[dict], output_dir: str) -> None:
    total_links = sum(r["num_links"] for r in results)
    ok = sum(1 for r in results if not r["error"])
    rendered = sum(1 for r in results if r["rendered"])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pages", len(results))
    c2.metric("Succeeded", ok)
    c3.metric("Links found", total_links)
    c4.metric("Browser-rendered", rendered)

    st.caption(f"📁 Saved under `{Path(output_dir).resolve()}`")
    st.download_button("⬇️ Download all (.zip)", data=zip_results(results),
                       file_name="scrape_results.zip", mime="application/zip")

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

            tab_md, tab_links = st.tabs(["📄 Markdown", "🔗 Links by category"])
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
                    st.dataframe(
                        view[["category", "region", "text", "url", "is_internal", "is_nofollow"]],
                        use_container_width=True, hide_index=True,
                    )
                    st.download_button("⬇️ Download links (.csv)",
                                       data=df.to_csv(index=False).encode("utf-8"),
                                       file_name=Path(r["links_path"]).with_suffix(".csv").name,
                                       mime="text/csv", key=f"links-{i}")
                else:
                    st.info("No links found on this page.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    st.title("🕸️ General Web Scraper")
    st.write("Fetch any page → clean **Markdown** (via docling) + a categorised "
             "**link inventory**, saved locally. Static fetch with a headless-browser "
             "fallback for JavaScript-rendered sites.")

    cfg, ephemeral = sidebar_config()

    # Keep the process-level janitor in sync with the current settings.
    janitor = _storage_janitor()
    janitor["dir"] = str(Path(cfg.output_dir).resolve())
    janitor["ephemeral"] = ephemeral
    if ephemeral:
        st.caption("🧹 Ephemeral storage: each scrape overwrites the output folder; "
                   "it's wiped when you stop the app. Use **Download all (.zip)** to keep results.")

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
            if ephemeral:
                clear_dir(cfg.output_dir)  # overwrite: no leftover/appended files
            with st.spinner("Scraping…"):
                results = run_scrape(urls, cfg)
            st.session_state["results"] = results
            st.session_state["output_dir"] = str(cfg.output_dir)

    if st.session_state.get("results"):
        st.divider()
        render_results(st.session_state["results"], st.session_state.get("output_dir", "storage"))


if __name__ == "__main__":
    main()
