"""Generate semantic policy-to-MRF mappings and test them using the AMD endpoint only.

The primary AMD model owns service matching; Python validates exact plan/network, catalog identifiers,
and eligible rate rows. A separate adversarial pass is retained for plan/network identity only.
Unsupported services remain unmapped and cannot produce a quote.

Example (run from repository root):
  python branch-a-mrf-integration/overlay/workflow/auto_map_policy.py \
    --policy-plan-id prime_500_2026 \
    --metadata local_data/policy-import/policy_metadata.json \
    --rules local_data/policy-import/benefit_rules.json \
    --out-dir local_data/policy-import/auto-mapping

Requires AMD_BASE_URL, AMD_API_KEY, AMD_MODEL. No other model provider is allowed.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import overlay_config

overlay_config.install_import_paths()

from rate_lookup import FallbackConfig, RateLookup  # noqa: E402


def _norm(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def exact_network_candidates(metadata: dict, profiles: list[dict]) -> list[dict]:
    """Match a PDF network label to exact plan-profile network aliases, allowing the payer prefix."""
    names = [x.get("name_in_pdf", {}).get("value") if isinstance(x.get("name_in_pdf"), dict)
             else x.get("name_in_pdf") for x in metadata.get("networks", [])]
    candidates = []
    for label in filter(None, names):
        label_norm = _norm(label)
        for profile in profiles:
            for network in profile.get("network_names") or []:
                net_norm = _norm(network)
                insurer_norm = _norm(profile.get("insurer"))
                stripped = label_norm
                if insurer_norm and stripped.startswith(insurer_norm + " "):
                    stripped = stripped[len(insurer_norm) + 1:]
                if stripped == net_norm or label_norm == net_norm:
                    candidates.append({"policy_network_label": label, "plan_key": profile["plan_key"],
                                       "insurer": profile["insurer"], "network": network})
    unique = {(x["plan_key"], x["network"]): x for x in candidates}
    return list(unique.values())


def load_catalog(encounters_path: Path, rates_path: Path, profiles_path: Path, billing_codes_path: Path):
    with encounters_path.open(newline="", encoding="utf-8") as f:
        encounter_rows = list(csv.DictReader(f))
    with rates_path.open(newline="", encoding="utf-8") as f:
        rate_rows = list(csv.DictReader(f))
    with billing_codes_path.open(newline="", encoding="utf-8") as f:
        code_rows = list(csv.DictReader(f))
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))["plans"]
    encounter_groups: dict[str, list[dict]] = defaultdict(list)
    for row in encounter_rows:
        encounter_groups[row["encounter"]].append(row)
    labels = {}
    for key, rows in encounter_groups.items():
        group_labels = {r["label"] for r in rows}
        if len(group_labels) != 1:
            raise ValueError(f"Encounter {key!r} has inconsistent labels")
        labels[key] = group_labels.pop()
    code_types = {r["code"]: r["code_type"] for r in code_rows if r.get("code") and r.get("code_type")}
    return encounter_rows, encounter_groups, labels, rate_rows, profiles, code_types


def _usable_amount(row: dict) -> bool:
    try:
        amount = float(row.get("allowed_amount", ""))
    except (TypeError, ValueError):
        return False
    return amount > 0 and row.get("status") in {"verified_rate", "estimated_rate"}


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _log_event(args, stage: str, status: str, **details) -> None:
    record = {"run_id": args.run_id, "timestamp": datetime.now(timezone.utc).isoformat(),
              "stage": stage, "status": status, **details}
    with (args.out_dir.resolve() / "auto_mapping_steps.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def rate_exists(rows: list[dict], *, insurer: str, network: str, encounter_label: str | None = None,
                code: str | None = None, billing_class: str | None = None, pos: str | None = None) -> bool:
    for row in rows:
        if row.get("insurer") != insurer or row.get("network") != network or not _usable_amount(row):
            continue
        if encounter_label is not None:
            if row.get("table") == "A_encounter" and row.get("encounter") == encounter_label:
                return True
        elif row.get("table") == "B_component" and row.get("billing_code") == code \
                and row.get("billing_class") == billing_class and row.get("place_of_service") == pos:
            return True
    return False


def canonicalize_component_refs(entries: list[dict], groups: dict[str, list[dict]]) -> list[dict]:
    """Repair harmless leading-dot formatting only when it uniquely names a real component."""
    changes = []
    for entry in entries:
        for option in entry.get("options", []):
            encounter_key = option.get("encounter_key")
            if option.get("kind") != "B_components" or encounter_key not in groups:
                continue
            known = {row["component"] for row in groups[encounter_key]}
            refs = option.get("components") or []
            for index, name in enumerate(refs):
                normalized = name.lstrip(".") if isinstance(name, str) else name
                if normalized != name and normalized in known:
                    refs[index] = normalized
                    changes.append({"service": entry.get("service"),
                                    "encounter_key": encounter_key,
                                    "before": name, "after": normalized,
                                    "reason": "removed leading punctuation; exact catalog component exists"})
    return changes


def build_service_candidates(rules: list[dict], encounter_groups: dict[str, list[dict]], labels: dict[str, str],
                             rates: list[dict], insurer: str, network: str) -> tuple[list[dict], list[dict]]:
    grouped: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for rule in rules:
        grouped[(rule["service"], rule.get("setting"))].append(rule)
    catalog = []
    for key, rows in sorted(encounter_groups.items()):
        components_by_name = {}
        for row in rows:
            for code in filter(None, row["codes"].split("|")):
                if rate_exists(rates, insurer=insurer, network=network, code=code,
                               billing_class=row["billing_class"], pos=row["place_of_service"]):
                    component = components_by_name.setdefault(row["component"], {
                        "component": row["component"], "billing_codes": [],
                        "billing_class": row["billing_class"],
                        "place_of_service": row["place_of_service"],
                        "modifier_rule": row["modifier_rule"]})
                    component["billing_codes"].append(code)
        components = list(components_by_name.values())
        catalog.append({"encounter_key": key, "label": labels[key], "components": components,
                        "has_aggregate_rate": rate_exists(rates, insurer=insurer, network=network,
                                                           encounter_label=labels[key])})
    result = []
    for (service, setting), service_rules in grouped.items():
        result.append({"service": service, "setting": setting,
                       "source_text_examples": sorted({r.get("source_text", "") for r in service_rules})[:3]})
    return result, catalog


def amd_client():
    base, key, model = (os.environ.get(k) for k in ("AMD_BASE_URL", "AMD_API_KEY", "AMD_MODEL"))
    if not (base and key and model):
        raise RuntimeError("Set AMD_BASE_URL, AMD_API_KEY and AMD_MODEL before automatic mapping.")
    if urlparse(base).hostname in {"api.openai.com", "chatgpt.com"}:
        raise RuntimeError("AMD-only project: refusing a non-AMD endpoint.")
    from openai import OpenAI
    # Checkpoint model requests so retrying an unfinished call is safe.
    return OpenAI(base_url=base, api_key=key, timeout=180, max_retries=1), model


def schema_for(service_candidates: list[dict]) -> dict:
    option = {"type": "object", "additionalProperties": False,
              "required": ["option_id", "label", "kind", "encounter_key", "components"],
              "properties": {
                  "option_id": {"type": "string"}, "label": {"type": "string"},
                  "kind": {"enum": ["A_encounter", "B_components"]},
                  "encounter_key": {"type": ["string", "null"]},
                 "components": {"type": "array", "items": {"type": "string"}},
              }}
    entry = {"type": "object", "additionalProperties": False,
             "required": ["service", "setting", "options", "clarification_question", "confidence", "evidence"],
             "properties": {
                 "service": {"type": "string"}, "setting": {"type": ["string", "null"]},
                 "options": {"type": "array", "items": option},
                 "clarification_question": {"type": ["string", "null"]},
                 "confidence": {"enum": ["high", "medium", "low"]},
                 "evidence": {"type": "array", "items": {"type": "string"}},
             }}
    return {"type": "object", "additionalProperties": False, "required": ["service_mappings"],
            "properties": {"service_mappings": {"type": "array", "items": entry}}}


SERVICE_MAPPING_PROMPT = (
    "You are responsible for semantic policy-to-service matching. Match by actual service meaning and clinical "
    "intent, not exact wording or label similarity: different phrases may describe the same service. For example, "
    "a user saying 'see a doctor for vaccine services' may relate to a physician-visit benefit and vaccine/"
    "administration price options; distinguish the visit from vaccine and administration charges, and offer "
    "relevant options or ask a clarifying question instead of rejecting solely because labels differ. A broad "
    "policy benefit may map to multiple concrete rate options; those are examples/choices, not a claim that one "
    "rate represents every service in the benefit. Select only supplied, rate-available MRF options. A_encounter "
    "is allowed when the full aggregate encounter is a reasonable representation; otherwise select only relevant "
    "B_components. Never invent codes, components, billing class, POS, modifiers, rates, or catalog entries. "
    "For EVERY supplied benefit service, return exactly one mapping row. If no supplied option is suitable, return "
    "an empty options list, low confidence, and explain why in evidence; do not omit the service. Use high confidence "
    "for clear semantic equivalence and medium when the user should clarify. Return the policy's exact service and "
    "setting keys, explain the semantic link in evidence, and ask a concise clarification question whenever multiple "
    "options exist or a medium-confidence choice needs user confirmation."
)


def propose_service_mappings(args, client, model: str, policy_plan_id: str,
                             service_candidates: list[dict], mrf_catalog: list[dict],
                             batch_size: int = 5) -> dict:
    """Ask the AMD model in bounded batches; retry omitted services individually."""
    merged: dict[tuple[str, str | None], dict] = {}
    batch_events = []
    for start in range(0, len(service_candidates), batch_size):
        batch_number = start // batch_size + 1
        batch = service_candidates[start:start + batch_size]
        payload = {"policy_plan_id": policy_plan_id, "benefit_services": batch,
                   "rate_available_mrf_catalog": mrf_catalog}
        response = call_json_cached(args, client, model, f"policy_service_rate_candidates_batch_{batch_number:02d}",
                                    SERVICE_MAPPING_PROMPT, payload, schema_for(batch))
        expected = {(x["service"], x["setting"]) for x in batch}
        returned = set()
        for entry in response.get("service_mappings", []):
            key = (entry.get("service"), entry.get("setting"))
            if key in expected and key not in returned:
                merged[key] = entry
                returned.add(key)
        missing = sorted(expected - returned)
        batch_events.append({"batch": batch_number, "requested": len(batch), "returned": len(returned),
                             "missing_before_retry": missing})
        _log_event(args, "service_candidate_batch", "complete", batch=batch_number,
                   requested_count=len(batch), returned_count=len(returned), missing=missing)

        # A short single-service retry prevents one incomplete JSON response from silently
        # dropping a large portion of the policy. The rest of the run still proceeds if it omits again.
        for retry_number, key in enumerate(missing, start=1):
            candidate = next(x for x in batch if (x["service"], x["setting"]) == key)
            retry_payload = {"policy_plan_id": policy_plan_id, "benefit_services": [candidate],
                             "rate_available_mrf_catalog": mrf_catalog}
            retry = call_json_cached(
                args, client, model,
                f"policy_service_rate_candidates_retry_{batch_number:02d}_{retry_number:02d}",
                SERVICE_MAPPING_PROMPT, retry_payload, schema_for([candidate]))
            retry_rows = [x for x in retry.get("service_mappings", [])
                          if (x.get("service"), x.get("setting")) == key]
            if retry_rows:
                merged[key] = retry_rows[0]
                _log_event(args, "service_candidate_retry", "recovered", service=key[0], setting=key[1],
                           batch=batch_number)
            else:
                _log_event(args, "service_candidate_retry", "still_omitted", service=key[0], setting=key[1],
                           batch=batch_number)

    return {"service_mappings": list(merged.values()), "batch_events": batch_events}


def call_json(client, model: str, name: str, system: str, payload, schema: dict) -> dict:
    response = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
    )
    return json.loads(response.choices[0].message.content)


def call_json_cached(args, client, model: str, name: str, system: str, payload, schema: dict) -> dict:
    """Checkpoint each completed AMD call so a timeout only retries the unfinished stage."""
    cache_key = hashlib.sha256(json.dumps(
        [model, name, system, payload, schema], sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")).hexdigest()[:20]
    cache_path = args.out_dir.resolve() / "auto_mapping_checkpoints" / f"{name}-{cache_key}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        _log_event(args, name, "checkpoint_reused", checkpoint=cache_path.name)
        return json.loads(cache_path.read_text(encoding="utf-8"))
    service_keys = [f"{x.get('service')}|{x.get('setting')}" for x in payload.get("benefit_services", [])]
    started = time.monotonic()
    _log_event(args, name, "request_started", checkpoint=cache_path.name, services=service_keys,
               payload_bytes=len(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
    try:
        result = call_json(client, model, name, system, payload, schema)
    except Exception as exc:
        _log_event(args, name, "request_failed", checkpoint=cache_path.name,
                   services=service_keys, error_type=type(exc).__name__,
                   duration_seconds=round(time.monotonic() - started, 2))
        raise
    _write_json(cache_path, result)
    _log_event(args, name, "checkpoint_saved", checkpoint=cache_path.name,
               services=service_keys, duration_seconds=round(time.monotonic() - started, 2))
    return result


def validate_candidate_maps(entries: list[dict], candidates: list[dict], groups: dict[str, list[dict]],
                            labels: dict[str, str], rates: list[dict], insurer: str, network: str):
    by_key = {(x["service"], x["setting"]): x for x in candidates}
    seen, valid, blocked = set(), [], []
    for entry in entries:
        key = (entry["service"], entry["setting"])
        if key not in by_key or key in seen:
            raise ValueError(f"Unexpected or duplicate benefit mapping: {key!r}")
        seen.add(key)
        if entry["confidence"] not in {"high", "medium"}:
            evidence_text = " | ".join(str(x) for x in entry.get("evidence", []))
            blocked.append({"service": key[0], "setting": key[1],
                            "reason": f"LLM returned confidence={entry['confidence']!r}; below the usable threshold. "
                                      f"Evidence: {evidence_text}"})
            continue
        if not entry["evidence"]:
            blocked.append({"service": key[0], "setting": key[1],
                            "reason": "LLM returned no semantic evidence for the proposed mapping"})
            continue
        options = entry["options"]
        if not options:
            explanation = " | ".join(str(x) for x in entry["evidence"])
            blocked.append({"service": key[0], "setting": key[1],
                            "reason": f"LLM found no suitable rate-available catalog option: {explanation}"})
            continue
        if len(options) > 1 and not entry.get("clarification_question"):
            labels_for_options = [o.get("label") or o.get("option_id") or "an option" for o in options]
            entry["clarification_question"] = (
                "Which of these best describes the service you need: "
                + "; ".join(labels_for_options) + "?"
            )
        elif entry["confidence"] == "medium" and not entry.get("clarification_question"):
            label = options[0].get("label") or options[0].get("option_id") or "this service"
            entry["clarification_question"] = f"Does your requested service match {label}?"
        try:
            for option in options:
                if option["kind"] == "A_encounter":
                    encounter_key = option.get("encounter_key")
                    if not encounter_key or encounter_key not in groups or option.get("components"):
                        raise ValueError(f"Invalid A_encounter option for {key!r}")
                    if not rate_exists(rates, insurer=insurer, network=network,
                                       encounter_label=labels[encounter_key]):
                        raise ValueError(f"No eligible A_encounter amount for {key!r}/{encounter_key}")
                else:
                    encounter_key = option.get("encounter_key")
                    names = option.get("components") or []
                    if not encounter_key or encounter_key not in groups or not names:
                        raise ValueError(f"Invalid B_components option for {key!r}")
                    source = groups[encounter_key]
                    for name in names:
                        matches = [r for r in source if r["component"] == name]
                        if len(matches) != 1:
                            raise ValueError(f"Unknown component {encounter_key}/{name} for {key!r}")
                        row = matches[0]
                        usable_codes = [code for code in row["codes"].split("|") if rate_exists(
                            rates, insurer=insurer, network=network, code=code,
                            billing_class=row["billing_class"], pos=row["place_of_service"])]
                        if not usable_codes:
                            raise ValueError(f"No eligible B_component rate for {key!r}/{encounter_key}/{name}")
        except (KeyError, TypeError, ValueError) as exc:
            blocked.append({"service": key[0], "setting": key[1], "reason": str(exc)})
            continue
        valid.append(entry)
    if seen != set(by_key):
        missing = sorted(set(by_key) - seen)
        blocked.extend({"service": service, "setting": setting,
                        "reason": "LLM omitted this benefit service from its candidate response"}
                       for service, setting in missing)
    return valid, blocked


def make_service_entries(plan_id: str, policy_category: str, accepted: list[dict], groups: dict[str, list[dict]],
                         labels: dict[str, str], rates: list[dict], insurer: str, network: str) -> list[dict]:
    output = []
    for entry in accepted:
        options = []
        for option in entry["options"]:
            if option["kind"] == "A_encounter":
                group = groups[option["encounter_key"]]
                options.append({"option_id": option["option_id"], "label": option["label"],
                                "kind": "A_encounter", "encounter_key": option["encounter_key"],
                                "component_benefit_services": {r["component"]: entry["service"] for r in group}})
            else:
                group = groups[option["encounter_key"]]
                by_name = {r["component"]: r for r in group}
                components = []
                for name in option["components"]:
                    row = by_name[name]
                    code = next(code for code in row["codes"].split("|") if code and rate_exists(
                        rates, insurer=insurer, network=network, code=code,
                        billing_class=row["billing_class"], pos=row["place_of_service"]))
                    components.append({"component": name, "billing_code": code,
                                       "billing_class": row["billing_class"],
                                       "place_of_service": row["place_of_service"],
                                       "modifier_rule": row["modifier_rule"], "benefit_service": entry["service"],
                                       "benefit_setting": entry["setting"], "required": True})
                options.append({"option_id": option["option_id"], "label": option["label"],
                                "kind": "B_components", "components": components})
        output.append({"entry_id": f"{plan_id}.{re.sub(r'[^a-z0-9]+', '_', entry['service'].lower()).strip('_')}.{policy_category[:2]}",
                       "policy_plan_id": plan_id, "service": entry["service"], "setting": entry["setting"],
                       "policy_network_category": policy_category, "clarification_question": entry.get("clarification_question"),
                       "options": options, "review_status": "verified",
                       "reviewed_by": "AMD semantic mapper + deterministic catalog validation",
                       "source": (f"LLM semantic mapping (confidence={entry['confidence']}); policy evidence: "
                                 f"{' | '.join(entry['evidence'])}; candidate codes/classes/POS/rates validated "
                                 "against local MRF files.")})
    return output


def run(args) -> dict:
    _log_event(args, "input_validation", "started")
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    rules = json.loads(args.rules.read_text(encoding="utf-8"))
    if not isinstance(rules, list) or not rules:
        raise ValueError("Rules file must be a non-empty JSON list")
    if any(r.get("policy_plan_id") != args.policy_plan_id for r in rules):
        raise ValueError("Every benefit rule must belong to the requested policy plan id")
    paths = overlay_config.load_paths()
    _, groups, labels, rates, profiles, code_types = load_catalog(
        paths.encounters_csv, paths.rates_csv, paths.plan_profiles, paths.billing_codes)
    plan_candidates = exact_network_candidates(metadata, profiles)
    if len(plan_candidates) != 1:
        raise RuntimeError(f"Need exactly one exact in-network MRF candidate; found {len(plan_candidates)}")
    target = plan_candidates[0]
    _log_event(args, "input_validation", "complete", benefit_rule_count=len(rules),
               exact_plan_network_candidates=len(plan_candidates), selected_plan_key=target["plan_key"],
               selected_network=target["network"])
    service_candidates, mrf_catalog = build_service_candidates(
        rules, groups, labels, rates, target["insurer"], target["network"])
    _log_event(args, "service_candidate_generation", "complete", service_count=len(service_candidates),
               mrf_encounter_count=len(mrf_catalog))
    client, model = amd_client()
    mapping_schema = {"type": "object", "additionalProperties": False,
                      "required": ["mrf_plan_key", "mrf_network", "confidence", "evidence"],
                      "properties": {"mrf_plan_key": {"type": "string"}, "mrf_network": {"type": "string"},
                                     "confidence": {"enum": ["high", "medium", "low"]},
                                     "evidence": {"type": "array", "items": {"type": "string"}}}}
    mapping_payload = {"policy_plan_id": args.policy_plan_id, "policy_metadata": metadata,
                       "only_exact_candidates": plan_candidates}
    proposal_plan = call_json_cached(args, client, model, "plan_network_candidate",
        "Select only the single exact in-network plan/network candidate supported by the supplied policy metadata. "
        "Do not infer from the insurer/issuer if the PDF explicitly names a provider network. Return confidence high only with direct textual evidence.",
        mapping_payload, mapping_schema)
    _log_event(args, "plan_network_candidate", "complete", confidence=proposal_plan.get("confidence"))
    plan_system = ("Adversarial reviewer: independently inspect policy metadata and exact profile candidates. "
                   "Return approve=true only if the proposed plan_key/network is the unique exact network named in the policy. "
                   "Do not accept a payer mismatch as proof of network identity; use explicit network wording.")
    review_schema = {"type": "object", "additionalProperties": False,
                     "required": ["approve", "reason"],
                     "properties": {"approve": {"type": "boolean"}, "reason": {"type": "string"}}}
    plan_review = call_json_cached(args, client, model, "plan_network_adversarial_review", plan_system,
                            {"evidence": mapping_payload, "proposal": proposal_plan}, review_schema)
    if (not plan_review["approve"] or proposal_plan["confidence"] != "high"
            or proposal_plan["mrf_plan_key"] != target["plan_key"]
            or proposal_plan["mrf_network"] != target["network"]):
        raise RuntimeError("Plan/network candidate failed adversarial approval or exact Python validation")
    _log_event(args, "plan_network_adversarial_review", "approved", reason=plan_review["reason"])

    proposal = propose_service_mappings(args, client, model, args.policy_plan_id,
                                        service_candidates, mrf_catalog)
    _log_event(args, "service_candidate_proposal", "complete",
               proposal_count=len(proposal["service_mappings"]),
               batch_count=len(proposal["batch_events"]))
    component_ref_repairs = canonicalize_component_refs(proposal["service_mappings"], groups)
    if component_ref_repairs:
        _log_event(args, "component_reference_normalization", "complete", changes=component_ref_repairs)
    accepted, blocked_candidates = validate_candidate_maps(
        proposal["service_mappings"], service_candidates, groups, labels,
        rates, target["insurer"], target["network"])
    _log_event(args, "python_catalog_validation", "complete",
               validated_count=len(accepted), blocked_count=len(blocked_candidates),
               blocked=blocked_candidates)
    service_review = {"status": "not_run",
                      "reason": "Service semantic selection is performed by the primary AMD model; "
                                "Python catalog/rate validation remains enforced.",
                      "component_reference_repairs": component_ref_repairs}
    _log_event(args, "service_adversarial_review", "skipped",
               reason="semantic broadness is not a second-model veto; deterministic validation remains active")

    out_dir = args.out_dir.resolve()
    active_data = paths.overlay_data
    for source in active_data.iterdir():
        if source.is_file() and source.name not in {"policy_mrf_map.v1.json", "service_rate_map.v1.json"}:
            shutil.copy2(source, out_dir / source.name)
    policy_map_path = active_data / "policy_mrf_map.v1.json"
    service_map_path = active_data / "service_rate_map.v1.json"
    policy_doc = json.loads(policy_map_path.read_text(encoding="utf-8"))
    service_doc = json.loads(service_map_path.read_text(encoding="utf-8"))
    policy_doc["entries"] = [e for e in policy_doc["entries"] if e["policy_plan_id"] != args.policy_plan_id]
    service_doc["entries"] = [e for e in service_doc["entries"] if e["policy_plan_id"] != args.policy_plan_id]
    evidence = [f"Policy metadata network: {target['policy_network_label']}; exact MRF profile: {target['plan_key']} / {target['network']}.",
                f"Adversarial review: {plan_review['reason']}"]
    for category in ("in-network", "out-of-network"):
        policy_doc["entries"].append({"policy_plan_id": args.policy_plan_id, "policy_network_category": category,
            "mrf_plan_key": target["plan_key"], "mrf_network": target["network"], "network_scope": None,
            "candidates": [], "question_for_human": None,
            "evidence": evidence + (["Out-of-network MRF use is prohibited by resolver hard stop."]
                                    if category == "out-of-network" else []),
            "review_status": "verified", "reviewed_by": "AMD network candidate + exact profile validation"})
    in_network_entries = make_service_entries(args.policy_plan_id, "in-network", accepted, groups, labels,
                                               rates, target["insurer"], target["network"])
    service_doc["entries"].extend(in_network_entries)

    # Validate the generated overlay via the production mapping schema and exercise each accepted rate option.
    from schema_check import validate
    validate(policy_doc, "policy_mrf_map")
    validate(service_doc, "service_rate_map")
    _log_event(args, "mapping_schema_validation", "complete", accepted_service_mappings=len(in_network_entries))
    _write_json(out_dir / "policy_mrf_map.v1.json", policy_doc)
    _write_json(out_dir / "service_rate_map.v1.json", service_doc)
    lookup = RateLookup.from_paths(paths.__class__(**{**paths.__dict__, "overlay_data": out_dir}),
                                   as_of=dt.date.today(), log_path=None,
                                   fallback=FallbackConfig(top_k=20, radius_miles=25))
    lookup_results = []
    for entry in in_network_entries:
        for option in entry["options"]:
            requests = []
            if option["kind"] == "A_encounter":
                requests = [{"insurer": target["insurer"], "plan_key": target["plan_key"],
                             "network": target["network"], "service_date": None,
                             "selected_provider_npi": None, "encounter_key": option["encounter_key"],
                             "billing_code": None}]
            else:
                for component in option["components"]:
                    requests.append({"insurer": target["insurer"], "plan_key": target["plan_key"],
                                     "network": target["network"], "service_date": None,
                                     "selected_provider_npi": None, "encounter_key": None,
                                     "billing_code": component["billing_code"],
                                     "billing_code_type": code_types.get(component["billing_code"]),
                                     "billing_class": component["billing_class"],
                                     "place_of_service": component["place_of_service"],
                                     "modifier_rule": component["modifier_rule"]})
            for request in requests:
                result = lookup.lookup(request)
                lookup_results.append({"service": entry["service"], "option_id": option["option_id"],
                                       "request": request, "result": result})
    _log_event(args, "allowed_amount_lookup_tests", "complete", lookup_count=len(lookup_results),
               success_count=sum(x["result"].get("status") in {"network_estimate", "provider_rate",
                                                                  "nearby_provider_estimate"} for x in lookup_results))
    audit = {"policy_plan_id": args.policy_plan_id, "model": model,
             "pipeline_events": [
                 {"stage": "plan_network_candidate_generation", "status": "complete",
                  "candidate_count": len(plan_candidates)},
                 {"stage": "plan_network_adversarial_review", "status": "approved" if plan_review["approve"] else "rejected",
                  "reason": plan_review["reason"]},
                 {"stage": "service_candidate_generation", "status": "complete",
                  "service_count": len(service_candidates)},
                 {"stage": "service_candidate_batching", "status": "complete",
                  "batch_count": len(proposal["batch_events"]),
                  "returned_count": len(proposal["service_mappings"])},
                 {"stage": "service_adversarial_review", "status": "skipped",
                  "reason": service_review["reason"]},
                 {"stage": "python_catalog_validation", "status": "complete",
                  "validated_count": len(accepted)},
                 {"stage": "allowed_amount_lookup_tests", "status": "complete",
                  "lookup_count": len(lookup_results)}],
             "plan_candidate": proposal_plan, "plan_adversarial_review": plan_review,
             "service_proposal": proposal, "service_adversarial_review": service_review,
             "accepted_service_mappings": len(in_network_entries),
             "blocked_service_mappings": len(service_candidates) - len(in_network_entries),
             "python_blocked_candidates": blocked_candidates,
             "lookup_tests": lookup_results,
             "summary": "generated mappings from primary-model semantic matching after Python catalog/rate validation; "
                        "unsupported services remain unmapped"}
    _write_json(out_dir / "auto_mapping_audit.json", audit)
    return audit


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--policy-plan-id", required=True)
    ap.add_argument("--metadata", type=Path, required=True)
    ap.add_argument("--rules", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    args.run_id = uuid.uuid4().hex
    args.out_dir = args.out_dir.resolve()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    _log_event(args, "run", "started", policy_plan_id=args.policy_plan_id)
    try:
        result = run(args)
    except Exception as exc:
        _log_event(args, "run", "failed", error_type=type(exc).__name__, error=str(exc))
        raise
    _log_event(args, "run", "complete", accepted_service_mappings=result["accepted_service_mappings"],
               blocked_service_mappings=result["blocked_service_mappings"])
    print(json.dumps({"out_dir": str(args.out_dir.resolve()), "accepted_service_mappings": result["accepted_service_mappings"],
                      "blocked_service_mappings": result["blocked_service_mappings"],
                      "lookup_tests": [{"service": x["service"], "status": x["result"].get("status"),
                                        "amount": x["result"].get("allowed_amount"),
                                        "reason_code": x["result"].get("reason_code")}
                                       for x in result["lookup_tests"]]}, indent=2))


if __name__ == "__main__":
    main()
