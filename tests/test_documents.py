"""Reading uploads like a coding agent: overview, search, read (poppler on a PDF built here), and the documents and
bills tools around them (model calls patched out). No LLM, no network."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflow"))

from documents import MAX_READ_PAGES, Document  # noqa: E402
from harness import Harness  # noqa: E402
from harness.skills.bills import skill as bills_skill  # noqa: E402
from harness.skills.documents import skill as documents_skill  # noqa: E402


def make_pdf(path: Path, pages: list[list[str]]) -> None:
    """A minimal PDF, one Helvetica text line per item; an empty list is a page with no text (like a scan)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        text = "".join(f"BT /F1 11 Tf 50 {750 - 16 * i} Td ({line}) Tj ET\n" for i, line in enumerate(lines))
        objects.append(f"<< /Length {len(text)} >>\nstream\n{text}endstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


PAGES = [
    ["L.A. Care Covered Silver 70 HMO", "Evidence of Coverage 2026"],
    ["TABLE OF CONTENTS 2", "Schedule of Benefits ........ 3"],
    ["SCHEDULE OF COST SHARE AND BENEFITS 3", "Primary care visit   $40 copay", "Deductible   $5,400"],
    ["SCHEDULE OF COST SHARE AND BENEFITS 4", "Specialist visit   $80 copay", "Lab tests   $50 copay"],
    [],
    ["DEFINITIONS 6", "Coinsurance means the percent you pay."],
]


class DocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.path = cls.tmp / "plan.pdf"
        make_pdf(cls.path, PAGES)
        cls.doc = Document(cls.path, "plan.pdf", "application/pdf")

    def test_overview_merges_running_headers_and_flags_pages_without_text(self):
        overview = self.doc.overview()
        self.assertEqual(overview["pages"], 6)
        contents = {c["pages"]: c["heading"] for c in overview["contents"]}
        self.assertEqual(contents["3-4"], "SCHEDULE OF COST SHARE AND BENEFITS")
        self.assertIn("no text", contents["5"])
        self.assertEqual(overview["pages_without_text"], 1)

    def test_search_any_term_with_snippets(self):
        result = self.doc.search("copay | coinsurance")
        self.assertEqual([r["page"] for r in result["results"]], [3, 4, 6])
        self.assertIn("$40 copay", result["results"][0]["snippet"])

    def test_read_is_bounded(self):
        self.assertIn("$80 copay", self.doc.read([4])["pages"][0]["text"])
        with self.assertRaises(ValueError):
            self.doc.read(list(range(1, MAX_READ_PAGES + 2)))
        with self.assertRaises(ValueError):
            self.doc.read([99])
        self.assertIn("view_page", self.doc.read([5])["pages"][0]["note"])

    def test_contains_ignores_spacing_and_case(self):
        self.assertTrue(self.doc.contains("primary CARE visit $40 copay"))
        self.assertFalse(self.doc.contains("primary care visit $45 copay"))

    def test_a_photo_is_one_page_without_text(self):
        photo = self.tmp / "card.png"
        photo.write_bytes(b"\x89PNG\r\n\x1a\n")
        doc = Document(photo, "card.png", "image/png")
        self.assertEqual(doc.overview()["kind"], "photo")
        self.assertEqual(doc.image(1), (b"\x89PNG\r\n\x1a\n", "image/png"))


class DocumentToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        make_pdf(cls.tmp / "plan.pdf", PAGES)

    def setUp(self):
        self.harness = Harness()
        self.harness.files["f1"] = {"path": self.tmp / "plan.pdf", "name": "plan.pdf",
                                    "content_type": "application/pdf", "sha256": "ab" * 32}

    def run_tool(self, name, **args):
        parsed, error = self.harness.tools.parse(name, __import__("json").dumps(args))
        self.assertIsNone(error)
        return self.harness.tools[name].run(parsed)

    def test_each_step_says_what_it_did(self):
        self.assertEqual(self.run_tool("document_overview", file_id="f1")["_step"], "Opened plan.pdf (6 pages)")
        self.assertIn("pages 3, 4", self.run_tool("search_document", file_id="f1", query="copay")["_step"])

    def test_known_plan_is_matched_to_the_database(self):
        result = self.run_tool("find_plan_match", file_id="f1")
        self.assertEqual((result["status"], result["plan_id"]), ("matched", "lacare_silver70_hmo_2026"))

    def test_unknown_file_is_an_error_the_model_can_read(self):
        with self.assertRaises(ValueError):
            self.run_tool("read_pages", file_id="nope", pages=[1])

    def test_extract_uses_the_pages_the_agent_chose(self):
        extracted = {"plan_id": "upload_x", "name": "Plan", "insurer": "L.A. Care", "plan_year": "2026",
                     "uploaded": True, "newly_added": True, "benefit_rows": 3, "calculable_rows": 3,
                     "summary_pages": [3], "benefit_pages": [3, 4]}
        with patch.object(documents_skill, "extract_uploaded_plan", return_value=extracted) as extract, \
                patch.object(documents_skill, "_model", return_value=(object(), "model")):
            result = self.run_tool("extract_plan", file_id="f1", document_kind="booklet", benefit_pages=[4, 3],
                                   summary_pages=[3], plan_name=None, insurer="L.A. Care", plan_year="2026")
        self.assertEqual(result["status"], "extracted")
        kwargs = extract.call_args.kwargs
        self.assertEqual((kwargs["pages"], kwargs["summary_pages"]), ([3, 4], [3]))
        self.assertEqual(extract.call_args.args[4]["document_type"], "benefit booklet")
        self.assertEqual(extract.call_args.args[7], "upload_" + "ab" * 12)

    def test_extract_rejects_pages_outside_the_file(self):
        with self.assertRaises(ValueError):
            self.run_tool("extract_plan", file_id="f1", document_kind="sbc", benefit_pages=[40], summary_pages=[1],
                          plan_name=None, insurer=None, plan_year=None)

    def test_read_bill_from_a_pdf_page_keeps_the_page(self):
        bill = {"provider_name": "A", "provider_npi": None, "statement_date": None, "total_billed": None,
                "lines": [{"service_date": None, "code": "99213", "description": None, "units": None, "billed": 90.0}]}
        with patch.object(bills_skill, "read_bill_image", return_value={"status": "read", "bill": bill,
                                                                        "model": "m"}) as read:
            result = self.run_tool("read_bill", file_id="f1", page=3)
        self.assertEqual(read.call_args.args[1], "image/png")  # the page was rendered for the model
        self.assertEqual(result["bill_id"], "f1_p3")
        self.assertEqual(self.harness.bills["f1_p3"]["page"], 3)


if __name__ == "__main__":
    unittest.main()
