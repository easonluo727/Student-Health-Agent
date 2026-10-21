"""Matching and costing on the two read-only databases. No LLM: every number and ranking is computed here.

  policy database   data/plans/policy.sqlite   plans, benefits (one row per price), plan_networks (the bridge)
  rate store        data/mrf/rates.sqlite (env RATES_DB; /data/rates.sqlite on Railway): every negotiated rate
                    the MRF pipeline kept, per network, billing code and Los Angeles provider

A *scenario* is one common encounter (data/mrf/encounters.csv: its components and their billing codes). Each
component is paid under one of the plan's benefits (SCENARIOS below). estimate() matches each component to a
benefit row, prices each component in the plan's network (plan_networks -> rate store, scripts/mrf/allowed.py:
the median over every Los Angeles provider in the network, with p10-p90), groups components paid under the same row (one copay per visit), and runs the pure
calculator (calculate_cost.py) on each group, using the remaining deductible in order. compare() runs the same
scenarios on several plans, assuming the deductible is not met and already met, and ranks them.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
import sqlite3
import sys
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from calculate_cost import calculate_cost

ROOT = Path(__file__).resolve().parents[1]
POLICY_DB = ROOT / "data" / "plans" / "policy.sqlite"
RATES_DB = Path(os.environ.get("RATES_DB", ROOT / "data" / "mrf" / "rates.sqlite"))
ENCOUNTERS_CSV = ROOT / "data" / "mrf" / "encounters.csv"
# A price from fewer providers than this is flagged. Hospital facility fees are the usual case: most hospitals
# publish them as a percent of charges or a case rate, which are not dollar prices and are left out.
THIN_SAMPLE = 10

sys.path.insert(0, str(ROOT / "scripts" / "mrf"))
import allowed  # noqa: E402  (rate selection rules, shared with the MRF pipeline)


@dataclass(frozen=True)
class Part:
    service: str                 # benefits.service the component is paid under
    component: str | None = None  # preferred benefits.component (facility / provider / office_visit)
    fallback: str | None = None   # service to use where the tier has no `service` benefit


# encounter key -> {encounters.csv component: Part}. Development-time knowledge of how claims are paid.
# Tests, labs and X-rays done during a visit are paid under their own benefit, not the visit's: the federal SBC
# coverage examples list "Emergency room care" and "Diagnostic test (x-ray)" (and office visits and "Diagnostic
# tests (blood work)") as separate cost shares. A copay never exceeds the price (calculate_cost.py).
# Reviewed 2026-10-21 against the SBCs in data/policy_pdfs/ and the Covered California 2026 benefit designs.
SCENARIOS: dict[str, dict[str, Part]] = {
    "office_visit_established": {"visit": Part("primary_care_visit", "office_visit")},
    "office_visit_new": {"visit": Part("primary_care_visit", "office_visit")},
    "telehealth_visit": {"visit": Part("primary_care_visit", "office_visit")},
    "specialist_followup": {"visit": Part("specialist_visit", "office_visit")},
    # The UC SHIP Ashe Center tier has no urgent care benefit: a same-day sick visit there is a primary care visit
    # ("No charge/visit (Ashe Ctr)", SBC p. 2; booklet p. 20, the Ashe Center is the student's medical home).
    "urgent_care_flu": {"visit": Part("urgent_care", fallback="primary_care_visit"), "flu_test": Part("lab_tests")},
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


@lru_cache(maxsize=1)
def _encounters():
    """encounter key -> (label, [component rows in order]); a row has codes, billing_class, place_of_service,
    modifier_rule."""
    out: dict[str, tuple[str, list[dict]]] = {}
    with open(ENCOUNTERS_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["encounter"], (r["label"], []))[1].append(r)
    return out


@lru_cache(maxsize=None)
def _rates_con(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


@lru_cache(maxsize=None)
def _component(rates_path: str, plan_key: str, code: str, billing_class: str, pos: str | None, modifier_rule: str,
               as_of: str) -> allowed.ComponentResult:
    """One component's price in one network. A large network takes seconds (Anthem PPO has 800k provider-rate
    pairs for 99213), so each result is kept for the day; there are only about 200 combinations."""
    return allowed.component(_rates_con(rates_path), plan_key, code, billing_class, pos, modifier_rule, as_of=as_of)


class PlanDB:
    def __init__(self, path: Path = POLICY_DB, rates_path: Path = RATES_DB):
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        # Without the rate store only flat copays can be computed.
        self.rates_path = str(rates_path) if Path(rates_path).exists() else None

    def warm(self) -> None:
        """Price every component of every scenario in every linked network, so requests don't wait."""
        for (plan_key,) in self.con.execute("SELECT DISTINCT mrf_plan_key FROM plan_networks"):
            for scenario in SCENARIOS:
                for row in _encounters().get(scenario, ("", []))[1]:
                    self._price(plan_key, row)

    def _price(self, plan_key: str, row: dict) -> allowed.ComponentResult:
        r = None
        for code in row["codes"].split("|"):  # alternatives, first with a rate wins
            r = _component(self.rates_path, plan_key, code, row["billing_class"], row["place_of_service"] or None,
                           row["modifier_rule"], dt.date.today().isoformat())
            if r.allowed_amount is not None:
                break
        return r

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

    def rules(self, plan_id: str) -> list[dict]:
        """Plan-wide rules that are not amounts (for example UC SHIP's Ashe Center referral requirement)."""
        return [{"rule": r["service_as_printed"], "text": r["limits_conditions"] or r["quote"],
                 "source_page": r["source_page"]}
                for r in self.con.execute("SELECT * FROM benefits WHERE plan_id=? AND service='plan_level' "
                                          "AND copay_usd IS NULL AND coinsurance_pct IS NULL", (plan_id,))]

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
        """Allowed amount of each component of a scenario in the plan's network, or why there is none.

        status: priced (every component), partial (some), no_rates (none, or no network / no rate store)."""
        _, rows = _encounters()[scenario]
        link = self.network(plan_id, tier)
        if link is None:
            return {"status": "no_rates", "message": "This tier has no price data (e.g. a campus clinic, or "
                                                     "out-of-network); only flat copays can be computed."}
        if not link["confirmed"]:
            return {"status": "no_rates", "message": "Which price network this plan uses is not confirmed yet "
                                                     f"({link['notes'] or 'no evidence recorded'}), so its prices "
                                                     "are not used; only flat copays can be computed."}
        if self.rates_path is None:
            return {"status": "no_rates", "message": f"The rate store ({RATES_DB}) is not available."}
        parts = []
        for row in rows:
            r = self._price(link["mrf_plan_key"], row)
            parts.append({"component": row["component"], "billing_code": r.billing_code,
                          "billing_class": r.billing_class, "place_of_service": r.place_of_service,
                          "allowed_amount": r.allowed_amount, "basis": r.allowed_amount_basis,
                          "n_providers": r.n_providers, "p10": r.p10, "p90": r.p90,
                          "mrf_file_dates": r.mrf_file_dates})
        missing = [p["component"] for p in parts if p["allowed_amount"] is None]
        complete = not missing
        warnings = [f"The {p['component']} price comes from only {p['n_providers']} provider(s) in this network, "
                    "so it is rough; most hospitals publish facility fees as a percent of charges, which has no "
                    "dollar price." for p in parts if p["allowed_amount"] is not None and p["n_providers"] < THIN_SAMPLE]

        def total(key):
            vals = [p[key] for p in parts]
            return round(sum(vals), 2) if complete and all(v is not None for v in vals) else None

        return {"status": "priced" if complete else "partial" if len(missing) < len(parts) else "no_rates",
                "message": None if complete else "No negotiated rate in this network for: " + ", ".join(missing) + ".",
                "basis": "la_county_median" if complete else None,
                "total": total("allowed_amount"), "low": total("p10"), "high": total("p90"),
                "components": {p["component"]: p["allowed_amount"] for p in parts}, "parts": parts,
                "warnings": warnings,
                "network": link["mrf_network"] or link["mrf_plan_key"],
                "mrf_file_date": ", ".join(sorted({d for p in parts for d in p["mrf_file_dates"]}))}

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
        label = _encounters()[scenario][0]
        prices = self.allowed_amounts(plan_id, tier, scenario) if network == "in" else {"status": "no_rates"}

        # Components paid under the same benefit row are one bill under that row (one copay per visit).
        groups: OrderedDict[str, dict] = OrderedDict()
        for comp, part in SCENARIOS[scenario].items():
            row = self.benefit(plan_id, part.service, network, tier, part.component, scenario)
            if row is None and part.fallback:
                row = self.benefit(plan_id, part.fallback, network, tier, part.component, scenario)
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
                          "quote": row["quote"], "limits": row["limits_conditions"],
                          "review_flags": row["review_flags"]})
            if result["status"] == "not_covered":
                return self._result("not_covered", f"{row['service_as_printed']} is not covered.", plan_id, scenario,
                                    label, network, tier, prices, lines)
            if result["status"] != "estimated":
                missing = result["missing_fields"]
                if "allowed_amount" in missing and prices.get("status") in ("no_rates", "partial"):
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
                                                           "mrf_file_date", "parts", "warnings")} if prices else None,
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
