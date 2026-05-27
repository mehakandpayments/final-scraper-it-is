"""Post-process docling's Markdown so real tables render as clean tables.

Real-world (especially legacy) pages abuse ``<table>`` for layout and stuff
content into colspan'd cells, which docling faithfully reproduces as Markdown
tables riddled with empty columns or single-cell "tables" wrapping prose. This
module tidies that up:

* strip HTML comments (e.g. docling's ``<!-- image -->`` placeholders);
* drop columns that are empty in *every* row;
* unwrap 1-column tables (layout tables) back into normal paragraphs;
* re-emit valid GitHub-flavoured Markdown tables.

Tables that are genuinely tabular are preserved and look like proper tables in
any Markdown viewer.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")
_PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")  # split on unescaped pipes


def _split_row(line: str) -> list[str]:
    parts = _PIPE_SPLIT_RE.split(line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _is_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.count("|") >= 2


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(_SEP_CELL_RE.match(c) for c in cells)


def _clean_table(block: list[str]) -> list[str]:
    sep_idx = next((k for k, ln in enumerate(block) if _is_separator(ln)), None)
    if sep_idx is None:
        return block  # not a real table; leave untouched

    header_rows = [_split_row(b) for b in block[:sep_idx]]
    body_rows = [_split_row(b) for b in block[sep_idx + 1:]]
    all_rows = header_rows + body_rows
    if not all_rows:
        return block

    ncols = max(len(r) for r in all_rows)
    for r in all_rows:  # pad in place (header/body share these list objects)
        r.extend([""] * (ncols - len(r)))

    keep = [c for c in range(ncols) if any(row[c].strip() for row in all_rows)]
    if not keep:
        return []  # wholly empty table -> drop

    if len(keep) == 1:
        # A single-column "table" is almost always a layout wrapper: unwrap it.
        col = keep[0]
        out: list[str] = []
        for row in all_rows:
            val = row[col].strip()
            if val:
                out.extend([val, ""])
        return out

    def fmt(row: list[str]) -> str:
        return "| " + " | ".join(row[c] for c in keep) + " |"

    header = header_rows[0] if header_rows else [""] * ncols
    rebuilt = [fmt(header), "| " + " | ".join(["---"] * len(keep)) + " |"]
    rebuilt.extend(fmt(r) for r in header_rows[1:] + body_rows)
    return rebuilt


def tidy_tables(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, n, in_fence = 0, len(lines), False
    while i < n:
        line = lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        # A table starts with a row immediately followed by a separator row.
        if (not in_fence and _is_row(line) and i + 1 < n
                and _is_row(lines[i + 1]) and _is_separator(lines[i + 1])):
            j = i
            block: list[str] = []
            while j < n and _is_row(lines[j]):
                block.append(lines[j])
                j += 1
            out.extend(_clean_table(block))
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def postprocess_markdown(md: str) -> str:
    """Strip HTML comments, tidy tables, and normalise blank lines."""
    md = _HTML_COMMENT_RE.sub("", md)
    md = tidy_tables(md)
    md = re.sub(r"[ \t]+\n", "\n", md)       # trailing whitespace
    md = re.sub(r"\n{3,}", "\n\n", md)       # collapse blank runs
    return md.strip() + "\n"
