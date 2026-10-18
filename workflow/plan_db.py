"""Matching and costing on the two read-only databases. No LLM: every number and ranking is computed here.

  policy database   data/plans/policy.sqlite   plans, benefits (one row per price), plan_networks (the bridge)
  allowed amounts   data/mrf/allowed_amounts.csv, built from the MRF rate store (rates.sqlite) by
                    scripts/mrf/report.py: per insurer network, the allowed amount of each common encounter
                    and of each of its components

A *scenario* is one common encounter (encounters.csv). Each of its components is paid under one of the plan's
benefits (SCENARIOS below). estimate() matches each component to a benefit row, prices it from the plan's network
through plan_networks, groups components paid under the same row (one copay per visit), and runs the pure
calculator (calculate_cost.py) on each group, using the remaining deductible in order. compare() runs the same
scenarios on several plans, assuming the deductible is not met and already met, and ranks them.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from calculate_cost import calculate_cost

ROOT = Path(__file__).resolve().parents[1]
POLICY_DB = ROOT / "data" / "plans" / "policy.sqlite"
ALLOWED_CSV = ROOT / "data" / "mrf" / "allowed_amounts.csv"
ENCOUNTERS_CSV = ROOT / "data" / "mrf" / "encounters.csv"
PROFILES_JSON = ROOT / "data" / "plan_profiles.v1.json"


@dataclass(frozen=True)
class Part:
    service: str                 # benefits.service the component is paid under
    component: str | None = None  # preferred benefits.component (facility / provider / office_visit)


# encounter key -> {encounters.csv component: Part}. Development-time knowledge of how claims are paid.
SCENARIOS: dict[str, dict[str, Part]] = {
    "office_visit_established": {"visit": Part("primary_care_visit", "office_visit")},
    "office_visit_new": {"visit": Part("primary_care_visit", "office_visit")},
    "telehealth_visit": {"visit": Part("primary_care_visit", "office_visit")},
    "specialist_followup": {"visit": Part("specialist_visit", "office_visit")},
    "urgent_care_flu": {"visit": Part("urgent_care"), "flu_test": Part("lab_tests")},
    "er_ankle_xray": {"er_physician": Part("emergency_room", "provider"), "er_facility": Part("emergency_room", "facility"),
                      "xray_read": Part("xray", "provider"), "xray_facility": Part("xray", "facility")},
    "uti_visit": {"visit": Part("primary_care_visit", "office_visit"), "urinalysis": Part("lab_tests"),
                  "culture": Part("lab_tests")},
    "routine_labs": {"cmp": Part("lab_tests"), "cbc": Part("lab_tests"), "lipid": Part("lab_tests")},
    "sti_screening": {"chlamydia": Part("lab_tests"), "gonorrhea": Part("lab_tests"), "hiv": Part("lab_tests")},
    "chest_xray": {"xray": Part("xray")},
    "knee_mri": {"mri": Part("advanced_imaging")},
    "back_mri": {"mri": Part("advanced_imaging")},
    "preventive_visit": {"visit": Part("preventive_care")},
    "flu_shot": {"vaccine": Part("preventive_care")},
    "therapy_intake": {"visit": Part("mental_health_outpatient", "office_visit")},
    "therapy_session_45": {"visit": Part("mental_health_outpatient", "office_visit")},
    "therapy_session_60": {"visit": Part("mental_health_outpatient", "office_visit")},
}
# Benefit rows that are not the everyday version of a service (unless the scenario asks for them).
SPECIAL = re.compile(r"pregnan|maternity|prenatal|postnatal|home delivery|mail|specialty|telehealth|online|virtual|"
                     r"livehealth|hospital stay|inpatient", re.I)
SCENARIO_ALLOWS = {"telehealth_visit": re.compile(r"telehealth|online|virtual|livehealth", re.I)}


def _money(text):
    return [float(m.replace(",", "")) for m in re.findall(r"\$([\d,]+(?:\.\d+)?)", text or "")]


@lru_cache(maxsize=1)
def _encounters():
    """encounter key -> (label, [component names in order])."""
    out: dict[str, tuple[str, list[str]]] = {}
    with open(ENCOUNTERS_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            label, parts = out.get(r["encounter"], (r["label"], []))
            parts.append(r["component"])
            out[r["encounter"]] = (label, parts)
    return out


@lru_cache(maxsize=1)
def _allowed():
    """(insurer, network) -> {encounter label: row} for the A_encounter table."""
    table: dict[tuple[str, str], dict[str, dict]] = {}
    with open(ALLOWED_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["table"] == "A_encounter":
                table.setdefault((r["insurer"], r["network"]), {})[r["encounter"]] = r
    return table


@lru_cache(maxsize=1)
def _profiles():
    with open(PROFILES_JSON, encoding="utf-8") as f:
        return {p["plan_key"]: p for p in json.load(f)["plans"]}


class PlanDB:
    def __init__(self, path: Path = POLICY_DB):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ catalog
    def plans(self) -> list[dict]:
        out = []
        for p in self.con.execute("SELECT * FROM plans ORDER BY plan_id"):
            tiers = [r[0] for r in self.con.execute(
                "SELECT DISTINCT network_tier FROM benefits WHERE plan_id=? AND network='in' AND network_tier IS NOT NULL",
                (p["plan_id"],))]
            priced = [r[0] for r in self.con.execute("SELECT network_tier FROM plan_networks WHERE plan_id=?",
                                                     (p["plan_id"],))]
            out.append({**dict(p), "in_network_tiers": sorted(tiers), "priced_tiers": priced})
        return out

    def plan(self, plan_id: str) -> dict | None:
        return next((p for p in self.plans() if p["plan_id"] == plan_id), None)

    @staticmethod
    def scenarios() -> list[dict]:
        enc = _encounters()
        return [{"scenario": key, "label": enc[key][0]} for key in SCENARIOS if key in enc]

    # ------------------------------------------------------------------ matching
    def benefit(self, plan_id: str, service: str, network: str = "in", tier: str | None = None,
                component: str | None = None, scenario: str | None = None) -> dict | None:
        """The one benefit row for this service: the everyday version first, then the matching component."""
        rows = [dict(r) for r in self.con.execute(
            "SELECT rowid AS _order, * FROM benefits WHERE plan_id=? AND service=? AND network=?",
            (plan_id, service, network))]
        if network == "in":
            rows = [r for r in rows if (r["network_tier"] or "") == (tier or "")]
        if not rows:
            return None
        allowed = SCENARIO_ALLOWS.get(scenario)

        def rank(r):
            text = " ".join(filter(None, (r["section"], r["service_as_printed"])))
            special = bool(SPECIAL.search(text)) and not (allowed and allowed.search(text))
            wanted = 0 if (allowed and allowed.search(text)) else 1
            comp = 0 if r["component"] == component else 1 if r["component"] is None else 2
            return (special, wanted, comp, not r["calculable"], r["_order"])

        return min(rows, key=rank)

    def deductible(self, plan_id: str, network: str = "in", tier: str | None = None) -> float | None:
        """The individual medical deductible of one tier, from its plan_level row."""
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM benefits WHERE plan_id=? AND service='plan_level' AND network=?", (plan_id, network))]
        rows = [r for r in rows if re.search(r"deductible", r["service_as_printed"] or "", re.I)
                and not re.search(r"drug|pharmacy|dental", r["service_as_printed"] or "", re.I)]
        if network == "in":
            exact = [r for r in rows if (r["network_tier"] or "") == (tier or "")]
            rows = exact or [r for r in rows if r["network_tier"] is None]
        return rows[0]["copay_usd"] if rows and rows[0]["copay_usd"] is not None else None

    # ------------------------------------------------------------------ pricing
    def network(self, plan_id: str, tier: str | None) -> dict | None:
        row = self.con.execute("SELECT * FROM plan_networks WHERE plan_id=? AND network_tier=?",
                               (plan_id, tier or "")).fetchone()
        return dict(row) if row else None

    def allowed_amounts(self, plan_id: str, tier: str | None, scenario: str) -> dict:
        """Allowed amount of each component of a scenario in the plan's network, or why there is none."""
        label, components = _encounters()[scenario]
        link = self.network(plan_id, tier)
        if link is None:
            return {"status": "no_rates", "message": "This tier has no price data (e.g. a campus clinic, or "
                                                     "out-of-network); only flat copays can be computed."}
        profile = _profiles().get(link["mrf_plan_key"], {})
        names = [link["mrf_network"]] if link["mrf_network"] else (profile.get("network_names") or [])
        table = _allowed()
        rows = [t for (ins, net), t in table.items() if ins == profile.get("insurer") and (not names or net in names)]
        row = rows[0].get(label) if len(rows) == 1 else None
        if row is None:
            return {"status": "no_rates", "message": f"No allowed amount for {label!r} in this network."}
        amounts = _money(row["components"])
        if len(amounts) != len(components):
            amounts = [float(row["allowed_amount"])] if len(components) == 1 else []
        if not amounts:
            return {"status": "no_rates", "message": "The price data does not split this encounter by component."}
        return {"status": row["status"], "basis": row["allowed_amount_basis"], "total": float(row["allowed_amount"]),
                "low": float(row["p10"]) if row["p10"] else None, "high": float(row["p90"]) if row["p90"] else None,
                "components": dict(zip(components, amounts)), "network": f"{row['insurer']} / {row['network']}",
                "mrf_file_date": row["mrf_file_date"]}

    # ------------------------------------------------------------------ costing
    @staticmethod
    def _rule(row: dict) -> dict:
        coins = row["coinsurance_pct"]
        covered = {1: "covered", 0: "not_covered"}.get(row["covered"], "unknown")
        return {"coverage_status": covered, "needs_review": not row["calculable"],
                "deductible_applies": {1: True, 0: False}.get(row["deductible_applies"]),
                "copay_usd": row["copay_usd"], "patient_pays_percentage": coins,
                "plan_pays_percentage": None if coins is None else 100 - coins}

    def estimate(self, plan_id: str, scenario: str, network: str = "in", tier: str | None = None,
                 remaining_deductible: float | None = None) -> dict:
        """What the user pays for one occurrence of a scenario under one plan, network and tier."""
        plan = self.plan(plan_id)
        if plan is None:
            return {"status": "invalid_input", "message": f"Unknown plan {plan_id!r}.", "patient_cost": None}
        if scenario not in SCENARIOS or scenario not in _encounters():
            return {"status": "invalid_input", "message": f"Unknown scenario {scenario!r}.", "patient_cost": None}
        if network == "in" and tier is None and plan["in_network_tiers"]:
            return {"status": "needs_information", "missing_fields": ["network_tier"], "patient_cost": None,
                    "message": "This plan has several in-network tiers; choose one.",
                    "options": plan["in_network_tiers"]}
        tier = tier if network == "in" else None
        label, _ = _encounters()[scenario]
        prices = self.allowed_amounts(plan_id, tier, scenario) if network == "in" else {"status": "no_rates"}

        # Components paid under the same benefit row are one bill under that row (one copay per visit).
        groups: OrderedDict[str, dict] = OrderedDict()
        for comp, part in SCENARIOS[scenario].items():
            row = self.benefit(plan_id, part.service, network, tier, part.component, scenario)
            if row is None:
                return self._result("needs_review", f"The plan has no {part.service} benefit for this network/tier.",
                                    plan_id, scenario, label, network, tier, prices, [])
            g = groups.setdefault(row["benefit_id"], {"row": row, "components": [], "allowed": Decimal(0),
                                                      "priced": True})
            g["components"].append(comp)
            amount = (prices.get("components") or {}).get(comp)
            if amount is None:
                g["priced"] = False
            else:
                g["allowed"] += Decimal(str(amount))

        lines, total = [], Decimal(0)
        remaining = None if remaining_deductible is None else Decimal(str(remaining_deductible))
        for g in groups.values():
            row, rule = g["row"], self._rule(g["row"])
            uses_deductible = rule["deductible_applies"] is True
            allowed = float(g["allowed"]) if g["priced"] else None
            result = calculate_cost(rule, allowed, float(remaining) if (uses_deductible and remaining is not None)
                                    else None)
            lines.append({"benefit": row["service_as_printed"], "service": row["service"], "section": row["section"],
                          "components": g["components"], "allowed_amount": allowed, "status": result["status"],
                          "patient_cost": result["patient_cost"], "message": result["message"],
                          "missing_fields": result["missing_fields"], "source_page": row["source_page"],
                          "quote": row["quote"], "review_flags": row["review_flags"]})
            if result["status"] == "not_covered":
                return self._result("not_covered", f"{row['service_as_printed']} is not covered.", plan_id, scenario,
                                    label, network, tier, prices, lines)
            if result["status"] != "estimated":
                missing = result["missing_fields"]
                if "allowed_amount" in missing and prices.get("status") == "no_rates":
                    return self._result("needs_review", prices.get("message", "No price data."), plan_id, scenario,
                                        label, network, tier, prices, lines)
                return self._result(result["status"], result["message"], plan_id, scenario, label, network, tier,
                                    prices, lines, missing)
            total += Decimal(str(result["patient_cost"]))
            if uses_deductible and remaining is not None:
                remaining -= min(remaining, g["allowed"])
        return self._result("estimated", "Estimate for one occurrence; excludes balance billing and the out-of-pocket "
                            "maximum. Not a coverage guarantee.", plan_id, scenario, label, network, tier, prices,
                            lines, patient_cost=float(total.quantize(Decimal("0.01"))))

    @staticmethod
    def _result(status, message, plan_id, scenario, label, network, tier, prices, lines, missing=None,
                patient_cost=None):
        return {"status": status, "patient_cost": patient_cost, "missing_fields": missing or [], "message": message,
                "plan_id": plan_id, "scenario": scenario, "scenario_label": label, "network": network,
                "network_tier": tier, "allowed_amount": {k: prices.get(k) for k in
                                                          ("status", "total", "low", "high", "basis", "network",
                                                           "mrf_file_date")} if prices else None,
                "lines": lines}

    def compare(self, plan_ids: list[str], scenarios: list[str]) -> dict:
        """Each plan's cheapest in-network tier for the scenarios, deductible not met and already met; ranked."""
        out = []
        for plan_id in plan_ids:
            plan = self.plan(plan_id)
            if plan is None:
                out.append({"plan_id": plan_id, "status": "invalid_input", "message": "Unknown plan."})
                continue
            tiers = plan["priced_tiers"] or [None]
            best = None
            for tier in tiers:
                tier = tier or None
                ded = self.deductible(plan_id, "in", tier)
                cases = {}
                for label, remaining in (("deductible_not_met", ded), ("deductible_met", 0)):
                    results = [self.estimate(plan_id, s, "in", tier, remaining) for s in scenarios]
                    ok = all(r["status"] == "estimated" for r in results)
                    cases[label] = {"total": round(sum(r["patient_cost"] for r in results), 2) if ok else None,
                                    "by_scenario": {r["scenario"]: {"status": r["status"], "patient_cost": r["patient_cost"],
                                                                    "message": r["message"]} for r in results}}
                entry = {"plan_id": plan_id, "plan_name": plan["plan_name"], "insurer": plan["insurer"],
                         "network_tier": tier, "deductible": ded, "oop_max": plan["oop_max_in_usd"], **cases}
                key = entry["deductible_not_met"]["total"]
                if best is None or (key is not None and (best["deductible_not_met"]["total"] is None
                                                         or key < best["deductible_not_met"]["total"])):
                    best = entry
            out.append(best)
        ranked = sorted([e for e in out if e.get("deductible_not_met", {}).get("total") is not None],
                        key=lambda e: e["deductible_not_met"]["total"])
        for i, e in enumerate(ranked, 1):
            e["rank"] = i
        return {"scenarios": scenarios, "plans": out,
                "note": "Totals are one occurrence of each scenario; premiums are not included. 'deductible_not_met' "
                        "assumes nothing has been paid toward the deductible this year."}
