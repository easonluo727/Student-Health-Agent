"""Read any uploaded file the way a coding agent reads a repository: overview first, then search, then read only the
pages that matter. No LLM here; the documents skill puts a model in front of these functions.

A document is a PDF (text from pdftotext, page images from pdftoppm) or a photo (one page, no text layer). Everything
returned is bounded, because each tool result stays in the conversation history and is resent on every later call.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

POPPLER_TIMEOUT = 60
SCANNED_CHARS = 30         # a page with less text than this is an image (scan, photo, chart)
MAX_READ_PAGES = 5
MAX_READ_CHARS = 12_000
MAX_SEARCH_PAGES = 15
SNIPPET = 160
HEADING_CHARS = 80


@lru_cache(maxsize=16)
def _page_texts(path: str, mtime: float) -> tuple[str, ...]:
    out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True,
                         timeout=POPPLER_TIMEOUT).stdout
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]  # pdftotext ends with a form feed
    return tuple(pages)


def _squeeze(text: str) -> str:
    """Layout text with long runs of spaces shortened (table columns stay apart, tokens drop by about half)."""
    text = re.sub(r"[ \t]{3,}", "   ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _heading(text: str) -> str:
    for line in text.splitlines():
        line = " ".join(line.split())
        # Skip bare page numbers and running dates.
        if len(line) > 3 and not re.fullmatch(r"[\d\s./-]+|page \d+( of \d+)?", line, re.I):
            return line[:HEADING_CHARS]
    return ""


class Document:
    def __init__(self, path: Path, name: str, content_type: str):
        self.path, self.name, self.content_type = Path(path), name, content_type
        self.is_pdf = content_type == "application/pdf"

    # ------------------------------------------------------------------ text
    def texts(self) -> tuple[str, ...]:
        if not self.is_pdf:
            return ("",)
        return _page_texts(str(self.path), self.path.stat().st_mtime)

    @property
    def pages(self) -> int:
        return len(self.texts())

    def has_text(self, page: int) -> bool:
        return len(self.texts()[page - 1].strip()) >= SCANNED_CHARS

    def check_pages(self, pages: list[int]) -> None:
        bad = [p for p in pages if not 1 <= p <= self.pages]
        if bad:
            raise ValueError(f"{self.name} has pages 1–{self.pages}; there is no page {bad[0]}.")

    def overview(self) -> dict:
        """Page count and a table of contents built from each page's first line, runs of the same heading merged."""
        if not self.is_pdf:
            return {"name": self.name, "kind": "photo", "pages": 1, "text_layer": False,
                    "contents": [], "hint": "A photo has no text layer: look at it with view_page."}
        texts = self.texts()
        contents = []
        for page in range(1, len(texts) + 1):
            heading = _heading(texts[page - 1]) if self.has_text(page) else "[no text: scan or image]"
            key = re.sub(r"^\d+\s+|\s+\d+$", "", heading).lower()  # running headers carry the page number
            if contents and contents[-1]["key"] == key:
                contents[-1]["last"] = page
            else:
                contents.append({"key": key, "first": page, "last": page, "heading": re.sub(r"\s+\d+$", "", heading)})
        contents = [{"pages": str(c["first"]) if c["first"] == c["last"] else f"{c['first']}-{c['last']}",
                     "heading": c["heading"]} for c in contents]
        scanned = sum(not self.has_text(p) for p in range(1, len(texts) + 1))
        return {"name": self.name, "kind": "pdf", "pages": len(texts), "text_layer": scanned < len(texts),
                "pages_without_text": scanned, "contents": contents[:150],
                "contents_truncated": len(contents) > 150}

    def search(self, query: str) -> dict:
        """Pages containing any of the |-separated terms (case-insensitive, any spacing), most hits first."""
        terms = [t.strip() for t in query.split("|") if t.strip()]
        if not terms:
            raise ValueError("query must contain at least one search term.")
        pattern = re.compile("|".join(r"\s+".join(map(re.escape, t.split())) for t in terms), re.I)
        hits = []
        for page, text in enumerate(self.texts(), start=1):
            found = list(pattern.finditer(text))
            if found:
                m = found[0]
                snippet = " ".join(text[max(0, m.start() - SNIPPET // 2):m.end() + SNIPPET // 2].split())
                hits.append({"page": page, "hits": len(found), "snippet": snippet})
        top = sorted(sorted(hits, key=lambda h: -h["hits"])[:MAX_SEARCH_PAGES], key=lambda h: h["page"])
        return {"query": query, "pages_with_hits": len(hits), "results": top,
                "note": None if self.is_pdf else "A photo has no text to search: use view_page."}

    def read(self, pages: list[int]) -> dict:
        """The text of up to MAX_READ_PAGES pages, cut at MAX_READ_CHARS in total."""
        pages = sorted(dict.fromkeys(pages))
        if len(pages) > MAX_READ_PAGES:
            raise ValueError(f"Read at most {MAX_READ_PAGES} pages at a time.")
        self.check_pages(pages)
        out, used, cut = [], 0, False
        for page in pages:
            text = _squeeze(self.texts()[page - 1]) if self.has_text(page) else ""
            room = MAX_READ_CHARS - used
            if len(text) > room:
                text, cut = text[:max(room, 0)], True
            used += len(text)
            out.append({"page": page, "text": text or None,
                        **({} if self.has_text(page) else {"note": "No text layer: use view_page."})})
        return {"pages": out, "truncated": cut}

    def contains(self, quote: str) -> bool:
        """Whether this exact wording (any spacing, any case) is in the document's text."""
        words = quote.split()
        if not words:
            return False
        pattern = re.compile(r"\s+".join(map(re.escape, words)), re.I)
        return any(pattern.search(text) for text in self.texts())

    # ------------------------------------------------------------------ images
    def page_png(self, page: int, dpi: int = 150) -> bytes:
        self.check_pages([page])
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "page"
            subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-png", "-singlefile",
                            str(self.path), str(prefix)], check=True, capture_output=True, timeout=POPPLER_TIMEOUT)
            return prefix.with_suffix(".png").read_bytes()

    def image(self, page: int) -> tuple[bytes, str]:
        """(image bytes, content type) of one page: the photo itself, or the PDF page rendered."""
        if not self.is_pdf:
            self.check_pages([page])
            return self.path.read_bytes(), self.content_type
        return self.page_png(page), "image/png"
