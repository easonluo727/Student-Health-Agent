"""Policy benefit rule -> MRF rate quote. No LLM, fail closed.

This module owns the *mapping* half: a Branch A benefit rule is turned into an exact MRF plan/network
(policy_mrf_map, human-verified) and an A encounter or a list of B components (service_rate_map). The
*rate* half is rate_lookup.RateLookup over the canonical views (canonical_adapter):

  A_encounter  plan_key + exact network + encounter_key (aggregate), or the selected NPI's components
  B_component  plan_key + exact network + code + billing_class + POS (aggregate), or the selected NPI's
               exact rows, or - when the NPI has none - a labelled nearby-provider estimate

Every result becomes a quote (overlay/schemas/rate_quote.schema.json). A quote is usable only with
status success/estimated and a finite amount > 0; everything else has allowed_amount = None. The lookup's
own status (provider_rate / nearby_provider_estimate / network_estimate / rate_unavailable) and its
reason_code travel in the quote as lookup_status and reason_code. Quotes are kept by QuoteStore under a
random quote_id; tools pass the id, never the amount.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import canonical_adapter as ca

USABLE = ("success", "estimated")


def norm_service(text: str | None) -> str:
    return " ".join((text or "").splitlines()[0].lower().replace("’", "'").split()) if text else ""


# --------------------------------------------------------------------------- reference data

@dataclass(frozen=True)
class EncounterDef:
    key: str
    label: str
    components: tuple[dict, ...]


def load_encounters(path: Path) -> dict[str, EncounterDef]:
    rows: dict[str, list[dict]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.setdefault(r["encounter"], []).append(r)
    out = {}
    for key, rs in rows.items():
        labels = {r["label"] for r in rs}
        if len(labels) != 1:
            raise ValueError(f"encounter {key!r} has several labels: {sorted(labels)}")
        out[key] = EncounterDef(key, labels.pop(), tuple(
            {"component": r["component"], "codes": r["codes"], "billing_class": r["billing_class"],
             "place_of_service": r["place_of_service"], "modifier_rule": r["modifier_rule"]} for r in rs))
    return out


def load_label_map(path: Path, encounters: dict[str, EncounterDef]) -> dict[str, str]:
    """Controlled key -> CSV label map. Must agree exactly with encounters.csv; labels must be unique."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    mapping = {e["encounter_key"]: e["csv_label"] for e in doc["entries"]}
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("encounter label map: duplicate labels")
    for key, label in mapping.items():
        if key not in encounters or encounters[key].label != label:
            raise ValueError(f"encounter label map out of date for {key!r}; rebuild it from encounters.csv")
    return mapping


@dataclass(frozen=True)
class PlanTarget:
    policy_plan_id: str
    policy_network_category: str
    mrf_plan_key: str
    mrf_network: str | None
    network_scope: str | None
    insurer: str
    csv_network: str
    review_status: str


class Mappings:
    """policy_mrf_map + service_rate_map + plan profiles, validated at load."""

    def __init__(self, policy_mrf_map: dict, service_rate_map: dict, plan_profiles: list[dict]):
        from schema_check import validate
        validate(policy_mrf_map, "policy_mrf_map")
        validate(service_rate_map, "service_rate_map")
        self.profiles = {p["plan_key"]: p for p in plan_profiles}
        self.plan_entries: dict[tuple[str, str], list[dict]] = {}
        for e in policy_mrf_map["entries"]:
            self.plan_entries.setdefault((e["policy_plan_id"], e["policy_network_category"]), []).append(e)
        self.service_entries: dict[tuple, list[dict]] = {}
        for e in service_rate_map["entries"]:
            if len(e["options"]) > 1 and not e.get("clarification_question"):
                raise ValueError(f"service map entry {e['entry_id']}: several options need a clarification_question")
            key = (e["policy_plan_id"], norm_service(e["service"]), e["setting"], e["policy_network_category"])
            self.service_entries.setdefault(key, []).append(e)

    @classmethod
    def from_files(cls, policy_mrf_map: Path, service_rate_map: Path, plan_profiles: Path) -> "Mappings":
        def load(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        return cls(load(policy_mrf_map), load(service_rate_map), load(plan_profiles)["plans"])

    def plan_target(self, policy_plan_id: str, category: str) -> tuple[PlanTarget | None, str, str]:
        """Return (target, status, message). Unverified or ambiguous mappings never resolve."""
        entries = self.plan_entries.get((policy_plan_id, category), [])
        if not entries:
            return None, "mapping_missing", f"No MRF mapping for plan {policy_plan_id!r} ({category})."
        if len(entries) > 1:
            return None, "ambiguous_match", f"{len(entries)} MRF mappings for {policy_plan_id!r} ({category}); keep one."
        e = entries[0]
        if e["review_status"] != "verified" or not e["mrf_plan_key"]:
            q = e.get("question_for_human") or "Which MRF plan and exact network apply?"
            return None, "mapping_pending_review", f"Plan-to-MRF mapping awaits human review: {q}"
        profile = self.profiles.get(e["mrf_plan_key"])
        if profile is None:
            return None, "mapping_invalid", f"Unknown MRF plan_key {e['mrf_plan_key']!r}."
        names = profile.get("network_names")
        if e["mrf_network"] is None:
            if names is not None:
                return None, "mapping_invalid", "This MRF plan filters on a network; mrf_network must name it exactly."
            csv_network = profile.get("market") or "all contracts"
        elif names is None or e["mrf_network"] not in names:
            return None, "mapping_invalid", f"{e['mrf_network']!r} is not an exact network of {e['mrf_plan_key']!r}."
        else:
            csv_network = " / ".join(names)
        return PlanTarget(policy_plan_id, category, e["mrf_plan_key"], e["mrf_network"], e.get("network_scope"),
                          profile["insurer"], csv_network, e["review_status"]), "ok", ""

    def service_entry(self, policy_plan_id: str, service: str, setting, category: str) -> tuple[dict | None, str, str]:
        entries = self.service_entries.get((policy_plan_id, norm_service(service), setting, category), [])
        if not entries:
            return None, "mapping_missing", "No service-to-rate mapping for this benefit rule."
        if len(entries) > 1:
            return None, "ambiguous_match", "Several service-to-rate mappings match this benefit rule."
        e = entries[0]
        if e["review_status"] != "verified":
            return None, "mapping_pending_review", "The service-to-rate mapping for this benefit awaits human review."
        return e, "ok", ""


# Lookup reason_code -> quote status (the quote keeps the original reason_code as well).
REASON_STATUS = {
    "no_encounter_rate": "no_match", "no_code_rate": "no_match", "pos_missing": "no_match",
    "modifier_rule_mismatch": "no_match", "code_alternatives_need_provider": "needs_clarification",
    "ambiguous_rate_rows": "ambiguous_match", "encounter_key_ambiguous": "ambiguous_match",
    "encounter_label_unknown": "mapping_invalid", "encounter_key_unknown": "mapping_invalid",
    "network_not_resolved": "mapping_invalid", "billing_class_missing": "mapping_invalid",
    "source_rate_unavailable": "rate_unavailable", "invalid_rate": "invalid_rate", "placeholder_rate": "invalid_rate",
    "stale_snapshot": "stale_snapshot",
    "no_data_source": "no_data_source",
    "no_provider_npi_rows": "provider_rate_unavailable", "provider_location_unknown": "provider_rate_unavailable",
    "no_eligible_fallback_provider": "provider_rate_unavailable", "expired_only": "provider_rate_unavailable",
    "no_dollar_rate": "provider_rate_unavailable", "pos_conflict": "provider_rate_unavailable",
    "modifier_rule_unmet": "provider_rate_unavailable",
}


def quote_status(res: dict) -> str:
    if res["status"] == "provider_rate":
        return "success"
    if res["status"] == "network_estimate":
        return "success" if res.get("source_status") == "verified_rate" else "estimated"
    if res["status"] == "nearby_provider_estimate":
        return "estimated"
    return REASON_STATUS.get(res["reason_code"], "rate_unavailable")


def _snapshot_date(res: dict) -> str | None:
    return max(res["mrf_file_dates"]) if res.get("mrf_file_dates") else None


def _lookup_summary(res: dict) -> dict:
    """The lookup result as carried inside a quote (provider list kept; raw rows never)."""
    keep = ("status", "reason_code", "exact_reason_code", "allowed_amount", "allowed_amount_basis", "source_status",
            "match_path", "match_keys", "estimate_scope", "selected_provider_npi", "exact_provider_rate_found",
            "fallback_provider_count", "n_providers", "p10", "p90", "mrf_file_dates", "source_ids", "fallback",
            "code_choice", "service_date", "service_date_validated", "warnings", "message")
    return {k: res.get(k) for k in keep}


# --------------------------------------------------------------------------- quotes

@dataclass
class QuoteStore:
    """Holds every quote issued in a conversation. Callers get copies; amounts never round-trip via the LLM."""
    _quotes: dict[str, dict] = field(default_factory=dict)
    _digests: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _digest(q: dict) -> str:
        return hashlib.sha256(json.dumps(q, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def issue(self, quote: dict) -> dict:
        quote = copy.deepcopy(quote)
        quote["quote_id"] = "q_" + uuid.uuid4().hex
        self._quotes[quote["quote_id"]] = quote
        self._digests[quote["quote_id"]] = self._digest(quote)
        return copy.deepcopy(quote)

    def get(self, quote_id) -> dict | None:
        if not isinstance(quote_id, str) or quote_id not in self._quotes:
            return None
        q = self._quotes[quote_id]
        if self._digest(q) != self._digests[quote_id]:
            raise RuntimeError("stored quote was modified")
        return copy.deepcopy(q)

    def __contains__(self, quote_id) -> bool:
        return quote_id in self._quotes


def _base_quote(**kw) -> dict:
    q = {"quote_id": None, "status": None, "usable": False, "allowed_amount": None, "rate_table": None,
         "policy_plan_id": None, "mrf_plan_key": None, "insurer": None, "mrf_network": None, "network_scope": None,
         "encounter_key": None, "components": [], "provider_npi": None, "provider_specific": False,
         "aggregate": False, "allowed_amount_basis": None, "mrf_snapshot_date": None, "service_date": None,
         "source": {}, "mapping": {}, "clarification": None, "message": "", "lookup_status": "rate_unavailable",
         "reason_code": None, "estimate_scope": None, "lookup": None}
    q.update(kw)
    return q


def _amount(text) -> float | None:
    return ca.amount(text)


class RateResolver:
    def __init__(self, mappings: Mappings, label_map: dict[str, str], lookup, store: QuoteStore | None = None):
        self.m, self.labels, self.lookup = mappings, label_map, lookup
        self.store = store or QuoteStore()

    @property
    def as_of(self) -> dt.date:
        return self.lookup.as_of

    # ---- entry point used by Branch A tools
    def resolve_for_rule(self, rule: dict, option_id: str | None = None, provider_npi: str | None = None,
                         service_date: str | None = None) -> dict:
        plan_id, category = rule.get("policy_plan_id") or rule.get("plan"), rule.get("network")
        mapping = {"benefit_rule_service": norm_service(rule.get("service")), "benefit_rule_setting": rule.get("setting"),
                   "policy_network_category": category}
        common = {"policy_plan_id": plan_id, "service_date": service_date, "mapping": mapping}
        log, rid = self.lookup.log, self.lookup.log.new_request()
        if category not in ("in-network", "out-of-network"):
            return self._issue(status="mapping_invalid", message="The benefit rule has no policy network category.", **common)
        if category == "out-of-network":
            return self._issue(status="mapping_invalid",
                               message="MRF in-network negotiated rates do not apply to out-of-network benefits.", **common)
        entry, status, msg = self.m.service_entry(plan_id, rule.get("service"), rule.get("setting"), category)
        log.event(rid, "service_mapping", status, policy_plan_id=plan_id, service=mapping["benefit_rule_service"],
                  setting=rule.get("setting"), entry_id=entry["entry_id"] if entry else None)
        if entry is None:
            return self._issue(status=status, message=msg, **common)
        mapping.update({"service_map_entry_id": entry["entry_id"], "service_map_review_status": entry["review_status"]})
        options = {o["option_id"]: o for o in entry["options"]}
        if option_id is None:
            if len(options) > 1:
                return self._issue(status="needs_clarification", message=entry["clarification_question"],
                                   clarification={"entry_id": entry["entry_id"], "question": entry["clarification_question"],
                                                  "options": [{"option_id": o["option_id"], "label": o["label"]}
                                                              for o in entry["options"]]}, **common)
            option_id = next(iter(options))
        if option_id not in options:
            return self._issue(status="mapping_invalid", message=f"Unknown option {option_id!r} for this benefit.", **common)
        option = options[option_id]
        mapping["option_id"] = option_id
        target, status, msg = self.m.plan_target(plan_id, category)
        log.event(rid, "plan_mapping", status, policy_plan_id=plan_id, category=category,
                  mrf_plan_key=target.mrf_plan_key if target else None, mrf_network=target.mrf_network if target else None)
        if target is None:
            return self._issue(status=status, message=msg, **common)
        mapping["policy_mrf_map_review_status"] = target.review_status
        ctx, reason, msg = self.lookup.resolve_plan(target.mrf_plan_key, target.mrf_network)
        if ctx is None:
            return self._issue(status="mapping_invalid", reason_code=reason, message=msg, **common)
        sd = service_date or self.as_of.isoformat()
        if option["kind"] == "A_encounter":
            return self.resolve_encounter(target, ctx, option, mapping["benefit_rule_service"], provider_npi,
                                          service_date, sd, mapping, rid)
        return self.resolve_components(target, ctx, option["components"], provider_npi, service_date, sd, mapping, rid)

    # ---- A_encounter
    def resolve_encounter(self, target, ctx, option, rule_service, provider_npi, service_date, sd, mapping, rid) -> dict:
        common = self._target_fields(target) | {"rate_table": "A_encounter", "service_date": service_date,
                                                "mapping": dict(mapping), "encounter_key": option["encounter_key"],
                                                "provider_npi": provider_npi}
        enc = self.lookup.encounters.get(option["encounter_key"])
        label = self.labels.get(option["encounter_key"])
        if enc is None or label is None:
            return self._issue(status="mapping_invalid", message="Encounter key is not in the controlled label map.", **common)
        benefit_services = {k: norm_service(v) for k, v in option["component_benefit_services"].items()}
        if set(benefit_services) != {c["component"] for c in enc["components"]}:
            return self._issue(status="mapping_invalid",
                               message="The mapping must name the paying benefit for every encounter component.", **common)
        if any(v != rule_service for v in benefit_services.values()):
            return self._issue(status="mapping_invalid",
                               message="Encounter components fall under different benefits; use the B component option.",
                               **common)
        common["mapping"]["encounter_label"] = label
        res = self.lookup.encounter(ctx, option["encounter_key"], provider_npi, sd, rid)
        comps = []
        for c, part in zip(enc["components"], res["components"] or [{}] * len(enc["components"])):
            comps.append({"component": c["component"], "billing_code": "|".join(c["billing_codes"]),
                          "billing_class": c["billing_class"], "place_of_service": "|".join(c["place_of_service"]),
                          "modifier_rule": c["modifier_rule"], "status": part.get("status"),
                          "allowed_amount": part.get("allowed_amount")})
        common["components"] = comps
        return self._from_result(res, provider_npi, "A_encounter", common)

    # ---- B_component
    def resolve_components(self, target, ctx, components, provider_npi, service_date, sd, mapping, rid) -> dict:
        common = self._target_fields(target) | {"rate_table": "B_component", "service_date": service_date,
                                                "mapping": dict(mapping), "provider_npi": provider_npi}
        parts, worst = [], None
        for comp in components:
            canon = {"component": comp["component"], "billing_codes": (comp["billing_code"],),
                     "billing_class": ca.billing_class(comp["billing_class"]),
                     "place_of_service": ca.pos_preference(comp["place_of_service"]),
                     "modifier_rule": comp["modifier_rule"]}
            res = self.lookup.component(ctx, canon, provider_npi, sd, rid)
            part = {"component": comp["component"], "billing_code": comp["billing_code"],
                    "billing_class": comp["billing_class"], "place_of_service": comp["place_of_service"],
                    "modifier_rule": comp["modifier_rule"], "benefit_service": norm_service(comp["benefit_service"]),
                    "benefit_setting": comp.get("benefit_setting"), "required": comp.get("required", True),
                    "status": quote_status(res), "lookup_status": res["status"], "reason_code": res["reason_code"],
                    "allowed_amount": res["allowed_amount"], "allowed_amount_basis": res["allowed_amount_basis"],
                    "mrf_snapshot_date": _snapshot_date(res), "estimate_scope": res["estimate_scope"],
                    "provider_specific": res["estimate_scope"] == "provider_specific",
                    "source_ids": res["source_ids"], "message": res["message"], "lookup": _lookup_summary(res)}
            if _amount(part["allowed_amount"]) is None or part["status"] not in USABLE:
                part["allowed_amount"] = None
            parts.append(part)
            if part["status"] not in USABLE and part["required"] and worst is None:
                worst = part
        common["components"] = parts
        kind = "sqlite" if provider_npi else "csv"
        common["source"] = {"kind": kind, "path": self._source_path(kind), "table": "B_component",
                            "source_ids": sorted({s for p in parts for s in p["source_ids"]})}
        if worst is not None:  # no partial totals, ever
            return self._issue(status=worst["status"], reason_code=worst["reason_code"],
                               lookup_status="rate_unavailable",
                               message=f"Component {worst['component']!r}: {worst['message']}", **common)
        used = [p for p in parts if p["status"] in USABLE]
        if not used:
            return self._issue(status="rate_unavailable", message="No usable component rates.", **common)
        total = round(sum(p["allowed_amount"] for p in used), 2)
        scopes = {p["estimate_scope"] for p in used}
        provider_specific = scopes == {"provider_specific"}
        lookup_status = ("provider_rate" if provider_specific else "network_estimate" if scopes == {"network_aggregate"}
                         else "nearby_provider_estimate")
        return self._issue(status="estimated" if any(p["status"] == "estimated" for p in used) else "success",
                           usable=True, allowed_amount=total, lookup_status=lookup_status,
                           allowed_amount_basis="; ".join(f"{p['component']}={p['allowed_amount_basis']}" for p in used),
                           mrf_snapshot_date=max(p["mrf_snapshot_date"] for p in used),
                           provider_specific=provider_specific, aggregate=not provider_specific,
                           estimate_scope=scopes.pop() if len(scopes) == 1 else "mixed_provider_and_nearby",
                           message=f"Sum of {len(used)} component rates.", **common)

    # ---- helpers
    def _source_path(self, kind: str) -> str:
        snap = self.lookup.db_snapshot
        return str(snap.get("rates_sqlite", "")) if kind == "sqlite" else "allowed_amounts.csv"

    def _from_result(self, res: dict, provider_npi, table: str, common: dict) -> dict:
        kind = "sqlite" if provider_npi else "csv"
        common["source"] = {"kind": kind, "path": self._source_path(kind), "table": table,
                            "source_ids": res["source_ids"]}
        status = quote_status(res)
        scope = res["estimate_scope"]
        return self._issue(status=status, allowed_amount=res["allowed_amount"],
                           allowed_amount_basis=res["allowed_amount_basis"], mrf_snapshot_date=_snapshot_date(res),
                           provider_specific=scope == "provider_specific",
                           aggregate=scope in ("network_aggregate", "nearby_provider_cohort", "mixed_provider_and_nearby"),
                           lookup_status=res["status"], reason_code=res["reason_code"], estimate_scope=scope,
                           lookup=_lookup_summary(res), message=res["message"], **common)

    @staticmethod
    def _target_fields(t: PlanTarget) -> dict:
        return {"policy_plan_id": t.policy_plan_id, "mrf_plan_key": t.mrf_plan_key, "insurer": t.insurer,
                "mrf_network": t.mrf_network, "network_scope": t.network_scope or (None if t.mrf_network else t.csv_network)}

    def _issue(self, **kw) -> dict:
        q = _base_quote(**kw)
        q["usable"] = q["status"] in USABLE and _amount(q["allowed_amount"]) is not None
        if not q["usable"]:
            q["allowed_amount"] = None
            q["lookup_status"] = "rate_unavailable"
        return self.store.issue(q)


def build_resolver(paths, as_of: dt.date | None = None, log_path=None, fallback=None) -> RateResolver:
    """Resolver over the configured files: mappings, encounter labels and the canonical rate lookup."""
    from rate_lookup import FallbackConfig, RateLookup
    lookup = RateLookup.from_paths(paths, as_of=as_of, log_path=log_path, fallback=fallback or FallbackConfig())
    encounters = load_encounters(paths.encounters_csv)
    labels = load_label_map(paths.overlay_data / "encounter_labels.v1.json", encounters)
    mappings = Mappings.from_files(paths.overlay_data / "policy_mrf_map.v1.json",
                                   paths.overlay_data / "service_rate_map.v1.json", paths.plan_profiles)
    return RateResolver(mappings, labels, lookup)
