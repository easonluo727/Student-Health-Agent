"""PDF text: glyph-id text from fonts without a Unicode map is shifted back only when that reads as English."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "policy"))

from pdf_text import repair  # noqa: E402


def glyphs(text):
    """What pypdf returns for such a font: each character's code minus 29; its own spaces and line breaks kept."""
    return "".join(c if c in " \n" else chr(ord(c) - 29) for c in text)


class RepairTest(unittest.TestCase):
    def test_glyph_ids_become_text(self):
        text = "Table of Contents\nWritten notice of claim must be given to the Company within 90 days."
        self.assertEqual(repair(glyphs(text)), text)

    def test_normal_text_is_left_alone(self):
        for text in ["The deductible is $500 per policy year for in-network care.", "", "Page 3"]:
            self.assertEqual(repair(text), text)


if __name__ == "__main__":
    unittest.main()
