"""The text of each page of a PDF: pdftotext, and for pages it can't read, pypdf with a fix for glyph-id text.

Some PDFs embed TrueType subsets with an Identity encoding and no ToUnicode map (seen in PDFTron output: poppler says
"Unknown character collection 'PDFTron-Identity'" and returns nothing). pypdf returns the glyph ids instead, and for a
font in the standard Macintosh glyph order a glyph id is the character code minus 29 ("7DEOH\\x03RI" is "Table of").
repair() shifts the text back when that makes it read as English, and leaves it alone otherwise.
"""

from __future__ import annotations

import logging
import re
import subprocess

POPPLER_TIMEOUT = 60
SCANNED_CHARS = 30  # a page with less text than this has no usable text layer
GLYPH_SHIFT = 29
COMMON_WORDS = {"the", "of", "and", "to", "in", "or", "for", "is", "a", "be", "by", "you", "your", "are", "not", "on",
                "with", "this", "that", "will", "may", "any", "as", "at"}


def _english_words(text: str) -> int:
    return sum(1 for w in re.findall(r"[A-Za-z]+", text) if w.lower() in COMMON_WORDS)


def repair(text: str) -> str:
    """Glyph-id text shifted back to characters, if that reads as English; otherwise the text unchanged. pypdf's own
    line breaks and spaces stay as they are, so an apostrophe (glyph 10) or "=" (glyph 32) is lost: the price of the
    heuristic."""
    shifted = "".join(c if c in "\n " or ord(c) + GLYPH_SHIFT > 0x7E else chr(ord(c) + GLYPH_SHIFT) for c in text)
    words = _english_words(shifted)
    return shifted if words >= 3 and words > 2 * _english_words(text) else text


def _pypdf_texts(pdf: str, pages: list[int]) -> dict[int, str]:
    """{page index: text} from pypdf for these 0-based pages; {} if pypdf isn't installed or can't open the file."""
    try:
        import pypdf
    except ImportError:
        return {}
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-xref warnings are common and harmless
    try:
        reader = pypdf.PdfReader(pdf)
        return {i: repair(reader.pages[i].extract_text() or "") for i in pages if i < len(reader.pages)}
    except Exception:
        return {}


def page_texts(pdf) -> list[str]:
    """One string per page (layout text from pdftotext; pypdf for the pages pdftotext can't read)."""
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True,
                         timeout=POPPLER_TIMEOUT).stdout
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]  # pdftotext ends with a form feed
    empty = [i for i, t in enumerate(pages) if len(t.strip()) < SCANNED_CHARS]
    for i, text in _pypdf_texts(str(pdf), empty).items():
        if len(text.strip()) >= SCANNED_CHARS:
            pages[i] = text
    return pages
