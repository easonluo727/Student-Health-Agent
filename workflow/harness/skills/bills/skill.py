"""Bills skill: read a bill the user uploaded, check it, and draft an appeal letter from the check.

  read_bill      bill_reader on a photo or one PDF page (the trained prompt; the model only copies the lines).
  audit_bill     bill_audit.audit (no LLM); the model picks the plan, the tier and passes only a user-stated
                 remaining deductible.
  draft_appeal   a letter written by Python from the last audit of that bill, so every number in it is checked.

Bills live on the harness (Harness.bills: bill_id -> {"bill", "name", "model", "audit"}), saved with the session.
"""

import uuid
from pathlib import Path

from bill_audit import audit
from bill_reader import read_bill_image
from plan_db import PlanDB
from provider_directory import ProviderDirectory

from .. import Skill, read_skill_md
from ...mcp import Tool, schema
from ..documents.skill import document


def _usd(amount) -> str:
    return f"${amount:,.2f}"


def _validate(db, bills, args):
    if args["bill_id"] not in bills:
        raise ValueError(f"Unknown bill_id {args['bill_id']!r}; read the bill with read_bill first.")
    if "plan_id" in args and args["plan_id"] not in {p["plan_id"] for p in db.plans()}:
        raise ValueError(f"Unknown plan_id {args['plan_id']!r}; use one from list_plans.")
    value = args.get("remaining_deductible")
    if value is not None and value < 0:
        raise ValueError("remaining_deductible must be a nonnegative number or null.")


def run_read(files, bills, args):
    doc = document(files, args["file_id"])
    page = args["page"] or 1
    image, content_type = doc.image(page)
    result = read_bill_image(image, content_type)
    if result["status"] != "read":
        return {**result, "_step": f"Couldn't read a bill on {doc.name}, page {page}"}
    bill_id = args["file_id"] if page == 1 else f"{args['file_id']}_p{page}"
    bills[bill_id] = {"bill": result["bill"], "name": doc.name, "file_id": args["file_id"], "page": page,
                      "model": result["model"], "audit": None}
    return {"status": "read", "bill_id": bill_id, "bill": result["bill"],
            "message": "Read from the image by a model; if the user says a line is wrong, believe the user.",
            "_step": f"Read {len(result['bill']['lines'])} bill lines from {doc.name}"
                     + (f", page {page}" if doc.is_pdf else "")}


def run_audit(db, directory, bills, args):
    entry = bills[args["bill_id"]]
    result = audit(db, directory, entry["bill"], args["plan_id"], args["network_tier"], args["remaining_deductible"])
    if result["status"] in ("audited", "unavailable"):
        entry["audit"] = result
    return {"bill_id": args["bill_id"], **result}


def letter(entry: dict, insurer: str | None) -> dict:
    """The appeal email for the card: only the audit's findings and numbers, in plain text."""
    result = entry["audit"]
    provider = result["provider"]
    name = provider["name_on_bill"] or provider["name_on_record"] or "the provider"
    by_line = {line["line"]: line for line in result["lines"]}
    points = []
    for finding in result["findings"]:
        line = by_line.get(finding["line"]) or {}
        what = " ".join(filter(None, [f"CPT {line.get('code')}" if line.get("code") else None,
                                      f"({line['description']})" if line.get("description") else None,
                                      f"on {line['service_date']}" if line.get("service_date") else None]))
        if finding["kind"] == "billed_above_your_share":
            points.append(f"- Line {finding['line']}, {what}: billed {_usd(line['billed'])}. The negotiated rate is "
                          f"{_usd(line['negotiated_rate'])}, and my share under the plan is {_usd(line['you_owe'])}, "
                          f"a difference of {_usd(finding['amount'])}.")
        elif finding["kind"] == "possible_duplicate":
            points.append(f"- Line {finding['line']}, {what}: billed {_usd(line['billed'])} a second time for the "
                          "same service on the same day. Please confirm it is not a duplicate charge.")
        elif finding["kind"] == "total_mismatch":
            points.append(f"- {finding['message']}")
    unchecked = [f"CPT {line['code']}" for line in result["lines"]
                 if line["check"] in ("no_rate", "no_data", "no_benefit") and line.get("code")]
    statement = f"the statement dated {result['statement_date']}" if result.get("statement_date") else "my statement"
    payer = insurer or "my insurer"
    missing = list(dict.fromkeys(unchecked))
    paragraphs = [
        f"To the billing department of {name},",
        f"I am writing about {statement}" + (f" (NPI {provider['npi']})." if provider.get("npi") else ".")
        + f" I am covered by {result['plan_name']}. I compared each charge with the negotiated rates {payer} "
          f"publishes for its {result['network']} network under the federal Transparency in Coverage rule, and with "
          "my plan's cost sharing:\n\n" + "\n".join(points),
        f"In total, the bill asks for {_usd(result['totals']['possible_overcharge'])} more than I should owe for these "
        "lines." if result["totals"]["possible_overcharge"] else None,
        f"Please confirm that this claim was submitted to {payer} and processed at the in-network rate, correct any "
        "error, and send me an itemized statement. Please do not send this balance to collections while it is "
        "under review.",
        (f"I could not find a published rate for {', '.join(missing)}; please include the allowed amount for "
         f"{'it' if len(missing) == 1 else 'these'} in the itemized statement.") if missing else None,
        "Thank you,\n[Your name]\n[Member ID / claim number]",
    ]
    body = "\n\n".join(p for p in paragraphs if p)
    return {
        "request_id": f"email_{uuid.uuid4().hex[:8]}",
        "title": f"Appeal to {name}",
        "from": None,
        "to": [],
        "cc": [],
        "subject": f"Billing review request: {statement}" + (f", NPI {provider['npi']}" if provider.get("npi") else ""),
        "body": body,
        "attachments": [{"file_id": entry["file_id"], "name": entry["name"]}] if entry.get("file_id") else [],
        "sources": [{"kind": "user_message", "label": "Your bill photo"},
                    {"kind": "mrf", "label": f"{result['network']} negotiated rates"},
                    {"kind": "policy", "label": result["plan_name"]}],
    }


def run_draft(db, bills, args):
    entry = bills[args["bill_id"]]
    if not entry.get("audit"):
        return {"status": "needs_audit", "message": "Run audit_bill on this bill first."}
    if not entry["audit"]["findings"]:
        return {"status": "nothing_to_appeal", "message": "The audit found nothing to appeal."}
    plan = db.plan(entry["audit"]["plan_id"]) or {}
    return {"status": "drafted", "draft": letter(entry, plan.get("insurer")),
            "message": "The letter is shown to the user in an editable card. Nothing is sent: the user copies it, "
                       "adds their name and member ID, and sends it to the billing office."}


def build(db: PlanDB, directory: ProviderDirectory, bills: dict, files: dict):
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"))
    read_tool = Tool(
        name="read_bill",
        description=("Read a medical bill or EOB from an uploaded photo, or from one page of an uploaded PDF, into "
                     "its lines (provider, NPI, codes, amounts). Returns the bill_id for audit_bill."),
        parameters=schema(file_id={"type": "string"}, page={"type": ["integer", "null"]}),
        run=lambda args: run_read(files, bills, args),
    )
    audit_tool = Tool(
        name="audit_bill",
        description=("Check an uploaded bill line by line against this provider's own negotiated rates and the "
                     "user's plan: what the user should owe, possible overcharges and duplicates."),
        parameters=schema(
            bill_id={"type": "string"},
            plan_id={"type": "string"},
            network_tier={"type": ["string", "null"]},
            remaining_deductible={"type": ["number", "null"]},
        ),
        run=lambda args: run_audit(db, directory, bills, args),
        validate=lambda args: _validate(db, bills, args),
    )
    draft_tool = Tool(
        name="draft_appeal",
        description="Draft an appeal letter to the provider's billing office from the last audit_bill of this bill.",
        parameters=schema(bill_id={"type": "string"}),
        run=lambda args: run_draft(db, bills, args),
        validate=lambda args: _validate(db, bills, args),
    )
    return Skill(name=meta["name"], description=meta["description"], instructions=body,
                 tools=[read_tool, audit_tool, draft_tool])
