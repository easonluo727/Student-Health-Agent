"""Policy PDF import, split into two outputs (overlay of workflow/import_policy.py).

    python policy_import_v2.py --policy-plan-id uc_ship_2026 \
        --metadata-pdf plan.pdf --metadata-pages 1-3 \
        --benefits-pdf plan.pdf --benefit-pages 12-15 --out-dir out/

Outputs (all validated against overlay/schemas):
  policy_metadata.json      issuer, plan name/IDs, region, coverage dates, network names as printed,
                            each with its source page; extraction + review status
  benefit_rules.json        Schedule of Benefits rules (benefit_rule.v2), one per service x network
                            category, with source file/page and review status; no rates
  policy_mrf_map.pending.json  one pending_review mapping per policy network category; the model never
                            fills mrf_plan_key / mrf_network (a human does, in overlay/data/policy_mrf_map.v1.json)

LLM calls go only to the AMD OpenAI-compatible endpoint (AMD_BASE_URL, AMD_API_KEY, AMD_MODEL).
There is no OpenAI default and no fallback. Validation, IDs, page checks and the mapping skeleton are
deterministic Python. The extractor is injectable so tests run without any LLM.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

import overlay_config
from schema_check import validate

FORBIDDEN_HOSTS = ("api.openai.com",)

METADATA_LLM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["issuer", "plan_name", "product_ids", "region", "coverage_year", "effective_start",
                 "effective_end", "networks"],
    "properties": {
        "issuer": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "plan_name": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "product_ids": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                        "required": ["id_type", "value", "source_page"],
                        "properties": {"id_type": {"enum": ["hios", "ein", "group_number", "policy_number", "other"]},
                                       "value": {"type": "string"}, "source_page": {"type": "integer"}}}},
        "region": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "coverage_year": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "effective_start": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "effective_end": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/t"}]},
        "networks": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["name_in_pdf", "policy_network_category", "source_page"],
                     "properties": {"name_in_pdf": {"type": "string"},
                                    "policy_network_category": {"enum": ["in-network", "out-of-network", "unspecified"]},
                                    "source_page": {"type": "integer"}}}},
    },
    "$defs": {"t": {"type": "object", "additionalProperties": False, "required": ["value", "source_page"],
                    "properties": {"value": {"type": "string"}, "source_page": {"type": "integer"}}}},
}

METADATA_INSTRUCTIONS = """
Extract plan metadata printed on these policy pages. Return JSON only, matching the schema.
Each page image is preceded by its PDF page number; every value must carry the page it is printed on.
- issuer: the insurance company / issuer name as printed.
- plan_name: the plan or product name as printed. product_ids: HIOS IDs, EINs, group or policy numbers.
- region, coverage_year, effective_start/effective_end (YYYY-MM-DD) only if printed.
- networks: every provider network NAME printed (e.g. a branded PPO network), verbatim, with the
  benefit column it belongs to (in-network / out-of-network) or "unspecified".
Use null or [] when something is not printed. Never infer, translate, or map to other systems.
"""

ROW_INSTRUCTIONS = """
Extract the visible benefit table into one JSON object {"rows": [...]}. Return JSON only.
A physical row is one service cell and the benefit cells aligned with it on the same horizontal band.
Create exactly one object per physical row:
{"setting": "inpatient" | "outpatient" | "other" | null, "service": string,
 "preferred": string | null, "out_of_network": string | null}
Copy each cell completely and verbatim. Use the nearest section heading above the row as setting.
Never merge, split, infer, duplicate, or recreate rows. Never shift text between columns.
Do not output headings, column labels, blank rows, or footers. Use null only for absent cells.
"""

ROW_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rows"],
              "properties": {"rows": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["setting", "service", "preferred", "out_of_network"],
                  "properties": {"setting": {"enum": ["inpatient", "outpatient", "other", None]},
                                 "service": {"type": "string"},
                                 "preferred": {"type": ["string", "null"]},
                                 "out_of_network": {"type": ["string", "null"]}}}}}}

NORMALIZE_INSTRUCTIONS = """
Normalize each input entry. Return {"rules": [...]} with exactly one object per input, same order,
copying entry_id. Fields: entry_id, copay_usd, plan_pays_percentage, patient_pays_percentage,
deductible_applies, prior_authorization, coverage_status, needs_review.
Interpret payment fields only from benefit_text. "$X Copay" -> copay_usd = X. "X% of Allowed Amount" ->
plan_pays_percentage = X, patient_pays_percentage = 100-X. "X% Coinsurance" -> patient_pays_percentage = X.
"after Deductible" -> deductible_applies = true; "not subject to Deductible" -> false.
"No Benefits" -> coverage_status = "not_covered"; explicit payment -> "covered"; otherwise "unknown".
prior_authorization: required | not_required | may_be_required | not_stated. Percentages are whole numbers.
needs_review = false only for one clear copay or percentage rule; true for tiers, limits, references,
conditions, contradictions or ambiguity. Use null for values that are not stated.
"""

NORMALIZE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rules"],
                    "properties": {"rules": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["entry_id", "copay_usd", "plan_pays_percentage", "patient_pays_percentage",
                                     "deductible_applies", "prior_authorization", "coverage_status", "needs_review"],
                        "properties": {
                            "entry_id": {"type": "integer"},
                            "copay_usd": {"type": ["number", "null"]},
                            "plan_pays_percentage": {"type": ["number", "null"]},
                            "patient_pays_percentage": {"type": ["number", "null"]},
                            "deductible_applies": {"type": ["boolean", "null"]},
                            "prior_authorization": {"enum": ["required", "not_required", "may_be_required", "not_stated"]},
                            "coverage_status": {"enum": ["covered", "not_covered", "unknown"]},
                            "needs_review": {"type": "boolean"}}}}}}


class Extractor(Protocol):
    def extract_metadata(self, pages: dict[int, bytes]) -> dict: ...
    def extract_rows(self, page: int, png: bytes) -> list[dict]: ...
    def normalize(self, entries: list[dict]) -> list[dict]: ...


class AMDExtractor:
    """OpenAI-compatible client pointed only at the AMD endpoint (vLLM / Token Factory)."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        base_url = base_url or os.environ.get("AMD_BASE_URL")
        api_key = api_key or os.environ.get("AMD_API_KEY")
        model = model or os.environ.get("AMD_MODEL")
        if not (base_url and api_key and model):
            raise RuntimeError("Set AMD_BASE_URL, AMD_API_KEY and AMD_MODEL. No other provider is used.")
        if urlparse(base_url).hostname in FORBIDDEN_HOSTS:
            raise RuntimeError("AMD-only: OpenAI endpoints are not allowed.")
        from openai import OpenAI  # client library only; the endpoint is AMD's
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=120.0, max_retries=1)
        self.model = model

    def _json(self, instructions: str, content, schema: dict, name: str) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model, temperature=0,
            messages=[{"role": "system", "content": instructions}, {"role": "user", "content": content}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        )
        return json.loads(resp.choices[0].message.content)

    @staticmethod
    def _image(png: bytes) -> dict:
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}

    def extract_metadata(self, pages: dict[int, bytes]) -> dict:
        content = []
        for page, png in sorted(pages.items()):
            content += [{"type": "text", "text": f"PDF page {page}:"}, self._image(png)]
        return self._json(METADATA_INSTRUCTIONS, content, METADATA_LLM_SCHEMA, "policy_metadata")

    def extract_rows(self, page: int, png: bytes) -> list[dict]:
        return self._json(ROW_INSTRUCTIONS, [self._image(png)], ROW_SCHEMA, "benefit_rows")["rows"]

    def normalize(self, entries: list[dict]) -> list[dict]:
        return self._json(NORMALIZE_INSTRUCTIONS, json.dumps(entries, ensure_ascii=False), NORMALIZE_SCHEMA,
                          "normalized_rules")["rules"]


# ---------------------------------------------------------------- deterministic helpers

def parse_pages(spec: str) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo < 1 or hi < lo:
            raise ValueError(f"bad page range {part!r}")
        pages += range(lo, hi + 1)
    if not pages:
        raise ValueError("no pages given")
    return sorted(set(pages))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render_pages(pdf: Path, pages: list[int], workdir: Path) -> dict[int, bytes]:
    """pdftoppm at 300 dpi; returns PNG bytes per requested page."""
    out: dict[int, bytes] = {}
    for page in pages:
        prefix = workdir / f"p{page}"
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-png", str(pdf), str(prefix)],
                       check=True)
        produced = sorted(workdir.glob(f"p{page}-*.png"))
        if not produced:
            raise RuntimeError(f"pdftoppm produced no image for page {page}")
        out[page] = produced[0].read_bytes()
    return out


def _sourced(value, pages: set[int], problems: list[str], field: str):
    if value is None:
        return None
    if value.get("source_page") not in pages or not str(value.get("value", "")).strip():
        problems.append(f"{field}: source page {value.get('source_page')} is outside the metadata pages or empty")
        return None
    return {"value": value["value"].strip(), "source_page": value["source_page"]}


def build_metadata(raw: dict, policy_plan_id: str, pdf: Path, pages: list[int], pdf_sha256: str) -> dict:
    page_set, problems = set(pages), []
    issuer = _sourced(raw.get("issuer"), page_set, problems, "issuer")
    plan_name = _sourced(raw.get("plan_name"), page_set, problems, "plan_name")
    dates = {}
    for key in ("effective_start", "effective_end"):
        v = _sourced(raw.get(key), page_set, problems, key)
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v["value"]):
            problems.append(f"{key}: {v['value']!r} is not YYYY-MM-DD")
            v = None
        dates[key] = v
    networks = []
    for n in raw.get("networks") or []:
        if n.get("source_page") in page_set and str(n.get("name_in_pdf", "")).strip():
            networks.append({"name_in_pdf": n["name_in_pdf"].strip(),
                             "policy_network_category": n.get("policy_network_category") or "unspecified",
                             "source_page": n["source_page"]})
        else:
            problems.append(f"network {n.get('name_in_pdf')!r}: missing name or page outside metadata pages")
    ids = [i for i in raw.get("product_ids") or [] if i.get("source_page") in page_set and str(i.get("value", "")).strip()]
    if issuer is None or plan_name is None:
        problems.append("issuer and plan name are required")
    doc = {
        "schema_version": "policy_metadata.v1",
        "policy_plan_id": policy_plan_id,
        "issuer": issuer or {"value": "UNKNOWN", "source_page": pages[0]},
        "plan": {"name": plan_name or {"value": "UNKNOWN", "source_page": pages[0]}, "product_ids": ids},
        "region": _sourced(raw.get("region"), page_set, problems, "region"),
        "coverage": {"coverage_year": _sourced(raw.get("coverage_year"), page_set, problems, "coverage_year"), **dates},
        "networks": networks,
        "sources": {"file": str(pdf), "sha256": pdf_sha256, "pages": pages},
        "extraction": {"problems": problems},
        "extraction_status": "failed" if issuer is None or plan_name is None else ("partial" if problems else "extracted"),
        "review_status": "pending_review",
        "reviewed_by": None,
    }
    validate(doc, "policy_metadata")
    return doc


def normalize_service(text: str) -> str:
    return " ".join((text or "").splitlines()[0].lower().replace("’", "'").split()) if text else ""


def build_benefit_rules(rows_by_page: dict[int, list[dict]], extractor: Extractor, policy_plan_id: str,
                        source_file: str, batch_size: int = 5) -> tuple[list[dict], list[str]]:
    """Same two-step extraction as the original importer, plus stable keys, source file/page and review status."""
    entries, problems = [], []
    for page, rows in sorted(rows_by_page.items()):
        for row_index, row in enumerate(rows, start=1):
            for column, network in (("preferred", "in-network"), ("out_of_network", "out-of-network")):
                text = row.get(column)
                if text:
                    entries.append({"entry_id": len(entries) + 1, "page": page, "row": row_index, "network": network,
                                    "setting": row.get("setting"), "service": row.get("service") or "",
                                    "benefit_text": text, "source_text": json.dumps(row, ensure_ascii=False)})
    rules: list[dict] = []
    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        out = extractor.normalize([{"entry_id": e["entry_id"], "service_text": e["service"],
                                    "benefit_text": e["benefit_text"]} for e in batch])
        if [o.get("entry_id") for o in out] != [e["entry_id"] for e in batch]:
            raise RuntimeError(f"normalization changed entry IDs for entries {batch[0]['entry_id']}-{batch[-1]['entry_id']}")
        for e, o in zip(batch, out):
            patient, plan = o.get("patient_pays_percentage"), o.get("plan_pays_percentage")
            needs_review = o.get("needs_review")
            if patient is not None and plan is not None and patient + plan != 100:
                needs_review = True
                problems.append(f"page {e['page']} row {e['row']}: percentages do not sum to 100")
            rule = {
                "schema_version": "benefit_rule.v2",
                "rule_key": f"{policy_plan_id}:p{e['page']}:r{e['row']}:{e['network']}",
                "policy_plan_id": policy_plan_id, "plan": policy_plan_id,
                "service": normalize_service(e["service"]), "setting": e["setting"], "network": e["network"],
                "copay_usd": o.get("copay_usd"), "plan_pays_percentage": plan, "patient_pays_percentage": patient,
                "deductible_applies": o.get("deductible_applies"), "prior_authorization": o.get("prior_authorization"),
                "coverage_status": o.get("coverage_status"), "source_text": e["source_text"],
                "source_file": source_file, "source_page": e["page"], "needs_review": needs_review,
                "review_status": "pending_review",
            }
            validate(rule, "benefit_rule")
            rules.append(rule)
    return rules, problems


def pending_mrf_map(metadata: dict, plan_profiles: list[dict]) -> dict:
    """One pending entry per policy network category. Candidates are listed only when the issuer printed in
    the PDF names the same company as a profile's insurer field; a human still has to choose."""
    issuer = metadata["issuer"]["value"].lower()
    candidates = [{"mrf_plan_key": p["plan_key"], "insurer": p["insurer"], "network_names": p.get("network_names")}
                  for p in plan_profiles if p.get("network_names") is not None or p.get("market")
                  if p["insurer"].split(" (")[0].split()[0].lower() in issuer]
    entries = []
    for category in ("in-network", "out-of-network"):
        printed = [n for n in metadata["networks"] if n["policy_network_category"] == category]
        entries.append({
            "policy_plan_id": metadata["policy_plan_id"], "policy_network_category": category,
            "mrf_plan_key": None, "mrf_network": None, "network_scope": None, "candidates": candidates,
            "question_for_human": (
                f"Which MRF plan_key and exact MRF network_name correspond to {metadata['plan']['name']['value']!r} "
                f"({category})? Network names printed in the PDF: "
                + (", ".join(f"{n['name_in_pdf']!r} (p.{n['source_page']})" for n in printed) or "none")),
            "evidence": [f"{n['name_in_pdf']} (PDF p.{n['source_page']})" for n in printed],
            "review_status": "pending_review", "reviewed_by": None})
    doc = {"schema_version": "policy_mrf_map.v1", "entries": entries}
    validate(doc, "policy_mrf_map")
    return doc


def run_import(policy_plan_id: str, metadata_pdf: Path, metadata_pages: list[int], benefits_pdf: Path,
               benefit_pages: list[int], out_dir: Path, extractor: Extractor,
               render: Callable[[Path, list[int], Path], dict[int, bytes]] = render_pages,
               plan_profiles_path: Path | None = None) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]*", policy_plan_id):
        raise ValueError("policy_plan_id must be lowercase letters, digits and underscores")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        meta_png = render(metadata_pdf, metadata_pages, Path(tmp) / "meta")
        meta = build_metadata(extractor.extract_metadata(meta_png), policy_plan_id, metadata_pdf, metadata_pages,
                              sha256_file(metadata_pdf))
        benefit_png = render(benefits_pdf, benefit_pages, Path(tmp) / "benefits")
        rows = {page: extractor.extract_rows(page, png) for page, png in benefit_png.items()}
    rules, problems = build_benefit_rules(rows, extractor, policy_plan_id, str(benefits_pdf))
    profiles_path = plan_profiles_path or overlay_config.load_paths().plan_profiles
    with open(profiles_path, encoding="utf-8") as f:
        profiles = json.load(f)["plans"]
    mapping = pending_mrf_map(meta, profiles)
    (out_dir / "policy_metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    (out_dir / "benefit_rules.json").write_text(json.dumps(rules, indent=2, ensure_ascii=False))
    (out_dir / "policy_mrf_map.pending.json").write_text(json.dumps(mapping, indent=2, ensure_ascii=False))
    return {"metadata_status": meta["extraction_status"], "rules": len(rules), "rule_problems": problems,
            "metadata_problems": meta["extraction"]["problems"]}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy-plan-id", required=True)
    ap.add_argument("--metadata-pdf", required=True, type=Path)
    ap.add_argument("--metadata-pages", required=True, help="e.g. 1-3,5")
    ap.add_argument("--benefits-pdf", type=Path, help="defaults to --metadata-pdf")
    ap.add_argument("--benefit-pages", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    a = ap.parse_args(argv)
    for p in (a.metadata_pdf, a.benefits_pdf or a.metadata_pdf):
        if not p.exists():
            raise SystemExit(f"PDF does not exist: {p}")
    result = run_import(a.policy_plan_id, a.metadata_pdf, parse_pages(a.metadata_pages), a.benefits_pdf or a.metadata_pdf,
                        parse_pages(a.benefit_pages), a.out_dir, AMDExtractor())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    overlay_config.install_import_paths()
    main()
