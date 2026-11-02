"""Plan uploads: match an uploaded document to the plan database, or extract it into a new unverified plan.

The agent decides what an upload is and which pages hold the plan's cost sharing (skills/documents); this module
holds the deterministic parts it calls:
- match_plan / db_catalog: a plan whose printed name and insurer appear in the document's text is the database's
  plan, priced from the database, never from the upload.
- extract_uploaded_plan: extract_sbc on the chosen pages into temporary plans.csv and benefits.csv, validated by
  build_seed.py, then persisted in the shared policy database with verified=0.
"""

import sys
from pathlib import Path


def match_plan(text, catalog):
    """The plan whose match phrases all appear in text, or None. Case-insensitive."""
    lower = text.lower()
    for plan in catalog:
        rule = plan["match"]
        if all(p.lower() in lower for p in rule.get("all", [])) and \
                any(p.lower() in lower for p in rule.get("any", [""])):
            return plan
    return None


def db_catalog(db=None):
    """The plan database (data/plans/policy.sqlite) as a match catalog: a plan matches when its name as printed
    (the part before " - ") appears, with its insurer, and "Los Angeles" when the name names the campus."""
    from plan_db import PlanDB

    catalog = []
    db = db or PlanDB()
    for p in db.plans():
        name = p["plan_name"].split(" - ")[0].strip()
        required = [name] + (["Los Angeles"] if "Los Angeles" in p["plan_name"] else [])
        catalog.append({"plan_id": p["plan_id"], "name": p["plan_name"], "insurer": p["insurer"],
                        "plan_year": p["plan_year"], "verified": bool(p["verified"]),
                        "uploaded": p["plan_id"].startswith("upload_"),
                        "match": {"all": required, "any": [p["insurer"].split()[0]]}})
    # Longer names first, so "Silver 70 Ambetter HMO" is tried before a shorter name it contains.
    return sorted(catalog, key=lambda plan: -len(plan["match"]["all"][0]))


def extract_uploaded_plan(client, model, pdf, name, identity, db, work_dir, plan_id, progress=None, *, pages=None,
                          summary_pages=None, max_benefit_pages=20):
    """Extract, validate, and persist an uploaded PDF in the shared policy database."""
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "policy"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import build_seed
    import extract_sbc

    if existing := db.plan(plan_id):
        benefit_rows = db.con.execute("SELECT COUNT(*) FROM benefits WHERE plan_id=?", (plan_id,)).fetchone()[0]
        calculable_rows = db.con.execute(
            "SELECT COUNT(*) FROM benefits WHERE plan_id=? AND calculable=1", (plan_id,)).fetchone()[0]
        return {"plan_id": plan_id, "name": existing["plan_name"], "insurer": existing["insurer"],
                "plan_year": existing["plan_year"], "uploaded": True, "newly_added": False,
                "benefit_rows": benefit_rows, "calculable_rows": calculable_rows,
                "summary_pages": [], "benefit_pages": []}

    out = Path(work_dir) / "extracted" / plan_id
    # Keep the content-derived ID for provenance without persisting a user-supplied filename in the shared database.
    source = f"uploaded-pdf://{plan_id}"
    extracted = extract_sbc.extract(
        client, model, pdf, plan_id, source_url=source, out=out, identity=identity,
        eoc=identity.get("document_type") in {"benefit booklet", "certificate"}, progress=progress,
        pages=pages, summary_pages=summary_pages, max_benefit_pages=max_benefit_pages)
    plan, benefits, problems = build_seed.build_plan(out, blanket_verified=False,
                                                     allow_unverified_calculation=True)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    if plan is None or not benefits:
        raise ValueError("The upload did not produce a usable plan and benefit table.")
    if len(benefits) > 500:
        raise ValueError(f"The upload produced {len(benefits)} benefit rows; the upload limit is 500.")
    newly_added = db.add_uploaded_plan(plan, benefits)
    loaded = db.plan(plan_id)
    return {"plan_id": plan_id, "name": loaded["plan_name"], "insurer": loaded["insurer"],
            "plan_year": loaded["plan_year"], "uploaded": True, "newly_added": newly_added,
            "benefit_rows": len(benefits), "calculable_rows": sum(bool(row["calculable"]) for row in benefits),
            "summary_pages": extracted["summary_pages"], "benefit_pages": extracted["benefit_pages"]}
