"""HTML -> clean Markdown via docling.

docling parses the HTML into a structured ``DoclingDocument`` (headings, lists,
tables, links) and serialises it to Markdown, so the output carries no HTML
tags while keeping document structure intact.

A :class:`DocumentConverter` is created lazily *per worker thread* (docling's
pipeline isn't guaranteed thread-safe), which lets conversions run in parallel
without a global lock.
"""

from __future__ import annotations

import io
import threading

from .models import FetchResult

_thread_local = threading.local()


def _converter():
    conv = getattr(_thread_local, "converter", None)
    if conv is None:
        # Imported lazily so the module loads fast and failures surface clearly.
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat

        conv = DocumentConverter(allowed_formats=[InputFormat.HTML])
        _thread_local.converter = conv
    return conv


def html_to_markdown(fetch: FetchResult) -> tuple[str, str | None]:
    """Convert fetched HTML to Markdown.

    Returns ``(markdown, error)``. On failure ``markdown`` is ``""`` and
    ``error`` describes what went wrong (so one bad page can't abort a batch).
    """
    if not fetch.html.strip():
        return "", "empty HTML"
    try:
        from docling.datamodel.base_models import DocumentStream

        buf = io.BytesIO(fetch.html.encode("utf-8", "ignore"))
        # docling infers format from the name extension.
        source = DocumentStream(name="page.html", stream=buf)
        result = _converter().convert(source)
        markdown = result.document.export_to_markdown()
        # Tidy docling's tables (drop empty columns, unwrap layout tables, strip
        # HTML comments) so genuine tables render as clean Markdown tables.
        from .tables import postprocess_markdown

        return postprocess_markdown(markdown), None
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return "", f"docling: {type(exc).__name__}: {exc}"
