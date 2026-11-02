"""Documents skill: read any upload like a coding agent reads a repository (overview, search, read, look), and turn an
insurance plan into rows Python can price.

  document_overview  page count and a table of contents (documents.Document.overview)          no LLM
  search_document    pages with any of the |-separated terms, with a snippet                    no LLM
  read_pages         text of up to 5 pages                                                      no LLM
  view_page          the model looks at one page image and answers a narrow question             LLM (vision)
  find_plan_match    the plan database's plan for this document, if any                         no LLM
  extract_plan       extract_sbc on the pages the agent chose; build_seed validates; saved      LLM per page

Uploads are on the harness (Harness.files: file_id -> {"path", "name", "content_type", "sha256"}). Each result may
carry "_step", the line the chat shows for this step; long steps report progress through the harness queue.
"""

import base64
import hashlib
from functools import lru_cache
from pathlib import Path

from documents import Document
from plan_db import PlanDB

from .. import Skill, read_skill_md
from ...mcp import Tool, schema
from ...reader import db_catalog, extract_uploaded_plan, match_plan

MAX_EXTRACT_PAGES = 50
VIEW_PROMPT = ("You are looking at page {page} of {name}. Answer only from what is visible on this page. Copy "
               "names, codes, dates and amounts exactly as printed. If the page doesn't show it, say so.\n\n"
               "Question: {question}")


@lru_cache(maxsize=1)
def _model():
    """(client, model) for page images; made on first use, so building a harness needs no LLM settings."""
    from llm_client import llm_model, make_client

    return make_client(timeout=180.0), llm_model()


def document(files: dict, file_id: str) -> Document:
    upload = files.get(file_id)
    if upload is None:
        raise ValueError(f"Unknown file_id {file_id!r}; use a file_id from an upload note.")
    if not Path(upload["path"]).exists():
        raise ValueError(f"{upload['name']} has expired; ask the user to upload it again.")
    return Document(upload["path"], upload["name"], upload.get("content_type", "application/pdf"))


def _pages_label(pages):
    pages = sorted(pages)
    return f"page {pages[0]}" if len(pages) == 1 else "pages " + ", ".join(map(str, pages))


def overview(files, args):
    doc = document(files, args["file_id"])
    result = doc.overview()
    kind = "photo" if result["kind"] == "photo" else f"{result['pages']} pages"
    return {**result, "_step": f"Opened {doc.name} ({kind})"}


def search(files, args):
    doc = document(files, args["file_id"])
    result = doc.search(args["query"])
    found = [r["page"] for r in result["results"]]
    shown = ", ".join(map(str, found[:8])) + ("…" if len(found) > 8 else "")
    return {**result, "_step": f"Searched {doc.name} for “{args['query']}”: "
                               + (f"pages {shown}" if found else "no matches")}


def read(files, args):
    doc = document(files, args["file_id"])
    result = doc.read(args["pages"])
    return {**result, "_step": f"Read {doc.name}, {_pages_label(args['pages'])}"}


def view(files, args):
    doc = document(files, args["file_id"])
    image, content_type = doc.image(args["page"])
    client, model = _model()
    from llm_client import fast_extra_body

    response = client.chat.completions.create(
        model=model, temperature=0, max_tokens=1500, extra_body=fast_extra_body(),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": VIEW_PROMPT.format(page=args["page"], name=doc.name, question=args["question"])},
            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,"
                                                      + base64.b64encode(image).decode("ascii")}}]}])
    answer = (response.choices[0].message.content or "").strip()
    return {"page": args["page"], "answer": answer[:4000],
            "_step": f"Looked at {doc.name}, page {args['page']}: {args['question'][:80]}"}


def find_match(db, files, args):
    doc = document(files, args["file_id"])
    texts = doc.texts()
    sample = "\n".join(texts[:3])
    plan = match_plan(sample, db_catalog(db)) if sample.strip() else None
    if plan is None:
        return {"status": "no_match", "message": "Not in the plan database. If the user wants costs from this plan, "
                                                 "find its cost-sharing pages and use extract_plan.",
                "_step": f"{doc.name} isn't in the plan database yet"}
    return {"status": "matched", "plan_id": plan["plan_id"], "name": plan["name"], "verified": plan["verified"],
            "message": "Use this plan_id; its benefits come from the plan database, not from the upload.",
            "_step": f"Matched {plan['name']} in the plan database"}


def extract(db, files, progress, args):
    doc = document(files, args["file_id"])
    if not doc.is_pdf:
        raise ValueError("extract_plan reads PDFs; a photo of a plan can only be read with view_page.")
    pages = sorted(dict.fromkeys(args["benefit_pages"]))
    summary = sorted(dict.fromkeys(args["summary_pages"]))
    doc.check_pages(pages + summary)
    if not pages or not summary:
        raise ValueError("Give the benefit-table pages and the page(s) with the deductible and out-of-pocket limit.")
    if len(pages) > MAX_EXTRACT_PAGES:
        raise ValueError(f"At most {MAX_EXTRACT_PAGES} benefit pages; choose the cost-sharing table only.")
    upload = files[args["file_id"]]
    sha256 = upload.get("sha256") or hashlib.sha256(Path(upload["path"]).read_bytes()).hexdigest()
    plan_id = "upload_" + sha256[:24]
    client, model = _model()
    identity = {"plan_name": args["plan_name"], "insurer": args["insurer"], "plan_year": args["plan_year"],
                "document_type": "benefit booklet" if args["document_kind"] == "booklet" else
                "summary of benefits and coverage"}
    progress(f"Extracting {doc.name}: {_pages_label(pages)}")
    try:
        plan = extract_uploaded_plan(client, model, doc.path, doc.name, identity, db, Path(upload["path"]).parent,
                                     plan_id, progress, pages=pages, summary_pages=summary,
                                     max_benefit_pages=MAX_EXTRACT_PAGES)
    except ValueError as error:
        return {"status": "failed", "message": str(error)[:500], "_step": f"Couldn't structure {doc.name}"}
    return {"status": "extracted", **plan,
            "message": "Saved in the shared plan database as unverified (not checked by a person); disclose that "
                       "before every result. Benefits needing negotiated rates stay unavailable until a confirmed "
                       "price network is linked.",
            "_step": f"Structured {plan['benefit_rows']} benefit rows from {doc.name}"}


FILE_ID = {"type": "string"}


def build(db: PlanDB, files: dict, progress):
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"))
    tools = [
        Tool("document_overview", "Page count and table of contents of an uploaded file. Start here.",
             schema(file_id=FILE_ID), lambda a: overview(files, a)),
        Tool("search_document", "Find the pages of an uploaded file that contain any of the |-separated terms "
                                "(case-insensitive), with a snippet of each.",
             schema(file_id=FILE_ID, query={"type": "string"}), lambda a: search(files, a)),
        Tool("read_pages", "The text of up to 5 pages of an uploaded PDF.",
             schema(file_id=FILE_ID, pages={"type": "array", "items": {"type": "integer"}}),
             lambda a: read(files, a)),
        Tool("view_page", "Look at one page (or a photo) as an image and answer a narrow question about it. For "
                          "scans, photos, tables whose text came out jumbled, and logos.",
             schema(file_id=FILE_ID, page={"type": "integer"}, question={"type": "string"}),
             lambda a: view(files, a)),
        Tool("find_plan_match", "Whether an uploaded insurance document is a plan already in the plan database.",
             schema(file_id=FILE_ID), lambda a: find_match(db, files, a)),
        Tool("extract_plan", "Turn an uploaded plan's cost-sharing pages into benefit rows Python can price, saved "
                             "as an unverified plan. Slow: about 10 seconds a page.",
             schema(file_id=FILE_ID, document_kind={"type": "string", "enum": ["sbc", "booklet"]},
                    benefit_pages={"type": "array", "items": {"type": "integer"}},
                    summary_pages={"type": "array", "items": {"type": "integer"}},
                    plan_name={"type": ["string", "null"]}, insurer={"type": ["string", "null"]},
                    plan_year={"type": ["string", "null"]}),
             lambda a: extract(db, files, progress, a)),
    ]
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=tools)
