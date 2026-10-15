"""Deterministic rate resolver: policy benefit rule -> MRF rate quote. No LLM, fail closed.

Two independent lookups that return the same quote contract (overlay/schemas/rate_quote.schema.json):

  A_encounter  table A of data/mrf/allowed_amounts.csv, matched exactly on
               (MRF plan via policy_mrf_map, exact MRF network, encounter key -> controlled label)
  B_component  table B, matched exactly on
               (MRF plan, exact MRF network, CPT/HCPCS, billing_class, place_of_service, modifier rule);
               an encounter made of several components is looked up component by component

Provider-specific rates (an NPI) come only from the SQLite database (scripts/mrf/allowed.py) and only
when it exists. CSV rows are aggregates: n_providers is a head count, never a provider identity.

A quote is usable only with status success/estimated and a finite amount > 0. Everything else -
rate_unavailable, 0/many matches, pending or missing mappings, stale snapshots, missing data source -
returns allowed_amount = None. Nothing falls back across insurer, network, code, class or POS.
Quotes are kept by QuoteStore under a random quote_id; tools pass the id, never the amount.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import json
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

USABLE = ("success", "estimated")
CSV_STATUS = {"verified_rate": "success", "estimated_rate": "estimated", "rate_unavailable": "rate_unavailable"}
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 62  # MRFs are monthly; two cycles without a refresh = stale


def norm_service(text: str | None) -> str:
    return " ".join((text or "").splitlines()[0].lower().replace("’", "'").split()) if text else ""


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text.strip())


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


def implied_modifier_rule(code: str, billing_class: str, pos: str, encounters: dict[str, EncounterDef]) -> str:
    """The modifier rule report.py used for a table B row (the CSV does not store it): the encounter
    component's rule when an encounter defines that code/class/POS, otherwise global_or_tc_plus_26 for
    imaging (7xxxx) and global for everything else - same derivation as report.component_combos."""
    for enc in encounters.values():
        for c in enc.components:
            if code in c["codes"].split("|") and c["billing_class"] == billing_class and c["place_of_service"] == pos:
                return c["modifier_rule"]
    return "global_or_tc_plus_26" if code.startswith("7") else "global"


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


# --------------------------------------------------------------------------- rate sources

class CsvRates:
    """Tables A and B of allowed_amounts.csv, indexed for exact lookups (all matches kept, never deduped)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.a: dict[tuple, list[dict]] = {}
        self.b: dict[tuple, list[dict]] = {}
        with open(self.path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["table"] == "A_encounter":
                    self.a.setdefault((r["insurer"], r["network"], r["encounter"]), []).append(r)
                elif r["table"] == "B_component":
                    self.b.setdefault((r["insurer"], r["network"], r["billing_code"], r["billing_class"],
                                       r["place_of_service"]), []).append(r)


class SqliteRates:
    """Provider-specific lookups via the original scripts/mrf/allowed.py (read-only connection)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.available = self.path.exists()
        self._con = None

    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        return self._con

    def provider_component(self, plan_key: str, code: str, billing_class: str, pos: str, modifier_rule: str,
                           npi: str, as_of: str) -> dict:
        import allowed  # scripts/mrf/allowed.py
        r = allowed.component(self.con(), plan_key, code, billing_class, pos or None, modifier_rule, npi=npi, as_of=as_of)
        dates = sorted(d for d in r.mrf_file_dates if d)
        return {"allowed_amount": r.allowed_amount, "basis": r.allowed_amount_basis,
                "mrf_file_date": dates[-1] if dates else None, "n_rates_excluded": r.n_rates_excluded}


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
         "source": {}, "mapping": {}, "clarification": None, "message": ""}
    q.update(kw)
    return q


def _amount(text) -> float | None:
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


class RateResolver:
    def __init__(self, mappings: Mappings, encounters: dict[str, EncounterDef], label_map: dict[str, str],
                 csv_rates: CsvRates | None, sqlite_rates: SqliteRates | None, as_of: dt.date | None = None,
                 max_snapshot_age_days: int = DEFAULT_MAX_SNAPSHOT_AGE_DAYS, store: QuoteStore | None = None):
        self.m, self.encounters, self.labels = mappings, encounters, label_map
        self.csv, self.db = csv_rates, sqlite_rates
        self.as_of = as_of or dt.date.today()
        self.max_age = max_snapshot_age_days
        self.store = store or QuoteStore()

    # ---- entry point used by Branch A tools
    def resolve_for_rule(self, rule: dict, option_id: str | None = None, provider_npi: str | None = None,
                         service_date: str | None = None) -> dict:
        plan_id, category = rule.get("policy_plan_id") or rule.get("plan"), rule.get("network")
        mapping = {"benefit_rule_service": norm_service(rule.get("service")), "benefit_rule_setting": rule.get("setting"),
                   "policy_network_category": category}
        common = {"policy_plan_id": plan_id, "service_date": service_date, "mapping": mapping}
        if category not in ("in-network", "out-of-network"):
            return self._issue(status="mapping_invalid", message="The benefit rule has no policy network category.", **common)
        if category == "out-of-network":
            return self._issue(status="mapping_invalid",
                               message="MRF in-network negotiated rates do not apply to out-of-network benefits.", **common)
        entry, status, msg = self.m.service_entry(plan_id, rule.get("service"), rule.get("setting"), category)
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
        if target is None:
            return self._issue(status=status, message=msg, **common)
        mapping["policy_mrf_map_review_status"] = target.review_status
        if option["kind"] == "A_encounter":
            return self.resolve_encounter(target, option, norm_service(rule.get("service")), provider_npi, service_date, mapping)
        return self.resolve_components(target, option["components"], provider_npi, service_date, mapping)

    # ---- A_encounter
    def resolve_encounter(self, target: PlanTarget, option: dict, rule_service: str, provider_npi: str | None,
                          service_date: str | None, mapping: dict) -> dict:
        common = self._target_fields(target) | {"rate_table": "A_encounter", "service_date": service_date,
                                                "mapping": dict(mapping), "encounter_key": option["encounter_key"]}
        enc = self.encounters.get(option["encounter_key"])
        label = self.labels.get(option["encounter_key"])
        if enc is None or label is None:
            return self._issue(status="mapping_invalid", message="Encounter key is not in the controlled label map.", **common)
        benefit_services = {k: norm_service(v) for k, v in option["component_benefit_services"].items()}
        if set(benefit_services) != {c["component"] for c in enc.components}:
            return self._issue(status="mapping_invalid",
                               message="The mapping must name the paying benefit for every encounter component.", **common)
        if any(v != rule_service for v in benefit_services.values()):
            return self._issue(status="mapping_invalid",
                               message="Encounter components fall under different benefits; use the B component option.",
                               **common)
        common["mapping"]["encounter_label"] = label
        common["components"] = [dict(c) for c in enc.components]
        if provider_npi is not None:
            return self._provider_encounter(target, enc, provider_npi, common)
        if self.csv is None:
            return self._issue(status="no_data_source", message="No rate table is configured.", **common)
        rows = self.csv.a.get((target.insurer, target.csv_network, label), [])
        common["source"] = {"kind": "csv", "path": str(self.csv.path), "table": "A_encounter"}
        return self._from_csv_row(rows, common)

    # ---- B_component
    def resolve_components(self, target: PlanTarget, components: list[dict], provider_npi: str | None,
                           service_date: str | None, mapping: dict) -> dict:
        common = self._target_fields(target) | {"rate_table": "B_component", "service_date": service_date,
                                                "mapping": dict(mapping)}
        parts, worst = [], None
        for comp in components:
            part = self._one_component(target, comp, provider_npi)
            parts.append(part)
            if part["status"] not in USABLE and comp.get("required", True) and worst is None:
                worst = part
        common["components"] = parts
        common["provider_npi"] = provider_npi
        common["source"] = {"kind": "sqlite" if provider_npi else "csv",
                            "path": str(self.db.path if provider_npi and self.db else (self.csv.path if self.csv else "")),
                            "table": "B_component"}
        if worst is not None:  # no partial totals, ever
            return self._issue(status=worst["status"], message=f"Component {worst['component']!r}: {worst['message']}",
                               **common)
        used = [p for p in parts if p["status"] in USABLE]
        if not used:
            return self._issue(status="rate_unavailable", message="No usable component rates.", **common)
        total = round(sum(p["allowed_amount"] for p in used), 2)
        return self._issue(status="estimated" if any(p["status"] == "estimated" for p in used) else "success",
                           usable=True, allowed_amount=total,
                           allowed_amount_basis="; ".join(f"{p['component']}={p['allowed_amount_basis']}" for p in used),
                           mrf_snapshot_date=max(p["mrf_snapshot_date"] for p in used),
                           provider_specific=bool(provider_npi) and all(p["provider_specific"] for p in used),
                           aggregate=not provider_npi,
                           message=f"Sum of {len(used)} component rates.", **common)

    def _one_component(self, target: PlanTarget, comp: dict, provider_npi: str | None) -> dict:
        part = {"component": comp["component"], "billing_code": comp["billing_code"], "billing_class": comp["billing_class"],
                "place_of_service": comp["place_of_service"], "modifier_rule": comp["modifier_rule"],
                "benefit_service": norm_service(comp["benefit_service"]), "benefit_setting": comp.get("benefit_setting"),
                "required": comp.get("required", True), "status": None, "allowed_amount": None,
                "allowed_amount_basis": None, "mrf_snapshot_date": None, "provider_specific": False, "message": ""}
        if provider_npi is not None:
            if self.db is None or not self.db.available:
                return part | {"status": "no_data_source",
                               "message": "Provider-specific rates need the MRF SQLite database, which is not available."}
            r = self.db.provider_component(target.mrf_plan_key, comp["billing_code"], comp["billing_class"],
                                           comp["place_of_service"], comp["modifier_rule"], provider_npi,
                                           self.as_of.isoformat())
            if r["basis"] != "provider_rate":
                return part | {"status": "provider_rate_unavailable",
                               "message": f"No rate for NPI {provider_npi} under this plan/network/code/class/POS."}
            return self._finish_part(part, r["allowed_amount"], "success", "provider_rate", r["mrf_file_date"], True)
        if self.csv is None:
            return part | {"status": "no_data_source", "message": "No rate table is configured."}
        implied = implied_modifier_rule(comp["billing_code"], comp["billing_class"], comp["place_of_service"], self.encounters)
        if implied != comp["modifier_rule"]:
            return part | {"status": "no_match",
                           "message": f"Table B row uses modifier rule {implied!r}, not {comp['modifier_rule']!r}."}
        rows = self.csv.b.get((target.insurer, target.csv_network, comp["billing_code"], comp["billing_class"],
                               comp["place_of_service"]), [])
        if len(rows) != 1:
            return part | {"status": "no_match" if not rows else "ambiguous_match",
                           "message": f"{len(rows)} table B rows match code/class/POS for this network."}
        row = rows[0]
        status = CSV_STATUS.get(row["status"])
        if status is None or status == "rate_unavailable":
            return part | {"status": "rate_unavailable", "message": "MRF rate unavailable for this component."}
        return self._finish_part(part, row["allowed_amount"], status, row["allowed_amount_basis"], row["mrf_file_date"], False)

    def _finish_part(self, part, amount, status, basis, file_date, provider_specific) -> dict:
        value = _amount(amount)
        if value is None:
            return part | {"status": "invalid_rate", "message": f"Rate {amount!r} is not a finite amount above zero."}
        snap = self._snapshot(file_date)
        if snap is None:
            return part | {"status": "stale_snapshot", "message": "MRF snapshot date missing or too old for this estimate."}
        return part | {"status": status, "allowed_amount": value, "allowed_amount_basis": basis,
                       "mrf_snapshot_date": snap, "provider_specific": provider_specific}

    # ---- helpers
    def _provider_encounter(self, target: PlanTarget, enc: EncounterDef, npi: str, common: dict) -> dict:
        """Provider-specific A total = sum of the NPI's own component rates (SQLite only)."""
        comps = [{"component": c["component"], "billing_code": c["codes"].split("|")[0], "billing_class": c["billing_class"],
                  "place_of_service": c["place_of_service"], "modifier_rule": c["modifier_rule"],
                  "benefit_service": "", "required": True} for c in enc.components]
        if any("|" in c["codes"] for c in enc.components):
            return self._issue(status="needs_clarification",
                               message="This encounter has alternative codes; choose a component option instead.", **common)
        q = self.resolve_components(target, comps, npi, common["service_date"], common["mapping"])
        q = self.store.get(q["quote_id"])
        q.update(rate_table="A_encounter", encounter_key=enc.key)
        return self._reissue(q)

    def _from_csv_row(self, rows: list[dict], common: dict) -> dict:
        if not rows:
            return self._issue(status="no_match", message="No table A row for this plan, network and encounter.", **common)
        if len(rows) > 1:
            return self._issue(status="ambiguous_match", message=f"{len(rows)} table A rows match; refusing to pick one.",
                               **common)
        row = rows[0]
        status = CSV_STATUS.get(row["status"])
        if status is None or status == "rate_unavailable":
            return self._issue(status="rate_unavailable", message="MRF rate unavailable for this encounter.", **common)
        value = _amount(row["allowed_amount"])
        if value is None:
            return self._issue(status="invalid_rate", message=f"Rate {row['allowed_amount']!r} is not usable.", **common)
        snap = self._snapshot(row["mrf_file_date"])
        if snap is None:
            return self._issue(status="stale_snapshot", message="MRF snapshot date missing or too old for this estimate.",
                               **common)
        return self._issue(status=status, usable=True, allowed_amount=value, aggregate=True, provider_specific=False,
                           allowed_amount_basis=row["allowed_amount_basis"], mrf_snapshot_date=snap,
                           message=f"Aggregate rate across {row['n_providers']} providers (a count, not a provider).",
                           **common)

    def _snapshot(self, text) -> str | None:
        """Latest MRF file date in the row, or None when missing/stale. Not a service date or rate validity date."""
        try:
            dates = [_date(t) for t in str(text or "").split(",") if t.strip()]
        except ValueError:
            return None
        if not dates or (self.as_of - min(dates)).days > self.max_age or max(dates) > self.as_of:
            return None
        return max(dates).isoformat()

    @staticmethod
    def _target_fields(t: PlanTarget) -> dict:
        return {"policy_plan_id": t.policy_plan_id, "mrf_plan_key": t.mrf_plan_key, "insurer": t.insurer,
                "mrf_network": t.mrf_network, "network_scope": t.network_scope or (None if t.mrf_network else t.csv_network)}

    def _issue(self, **kw) -> dict:
        q = _base_quote(**kw)
        q["usable"] = q["status"] in USABLE and _amount(q["allowed_amount"]) is not None
        if not q["usable"]:
            q["allowed_amount"] = None
        return self.store.issue(q)

    def _reissue(self, q: dict) -> dict:
        q = dict(q)
        q.pop("quote_id", None)
        return self._issue(**q)
