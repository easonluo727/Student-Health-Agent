"""Matching and costing on the two read-only databases. No LLM: every number and ranking is computed here.

  policy database   data/plans/policy.sqlite   plans, benefits (one row per price), plan_networks (the bridge)
  rate store        data/mrf/rates.sqlite (env RATES_DB; /data/rates.sqlite on Railway): every negotiated rate
                    the MRF pipeline kept, per network, billing code and Los Angeles provider

A *scenario* is one common encounter (data/mrf/encounters.csv: its components and their billing codes). Each
component is paid under one of the plan's benefits (SCENARIOS below). estimate() matches each component to a
benefit row, prices each component in the plan's network (plan_networks -> rate store, scripts/mrf/allowed.py), groups
components paid under the same row (one copay per visit), and runs the pure calculator (calculate_cost.py) on each
group, using the remaining deductible in order. A visit to one doctor (DOCTOR_SCENARIOS) is priced at a specific
doctor (npi: that NPI's own rate, never a median); the caller finds the doctor (provider_directory.py). Other
scenarios still take the median over every Los Angeles provider in the network, with p10-p90 (roadmap step 1).
premium() is a year's list premium for one person (age, ZIP, student level). year() is a year of care under one plan
tier: each visit priced as often as the user expects it, the deductible used up across the year, capped at the
out-of-pocket max. compare() runs year() on several plans, adds each plan's premium, and rank_plans() ranks them by
the user's rated priorities (or by the year total), recording how.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
import shutil
import sqlite3
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from calculate_cost import calculate_cost

ROOT = Path(__file__).resolve().parents[1]
POLICY_SEED_DB = ROOT / "data" / "plans" / "policy.sqlite"
POLICY_DB = Path(os.environ.get("POLICY_DB", "/data/policy.sqlite" if Path("/data").exists() else POLICY_SEED_DB))
RATES_DB = Path(os.environ.get("RATES_DB", ROOT / "data" / "mrf" / "rates.sqlite"))
ENCOUNTERS_CSV = ROOT / "data" / "mrf" / "encounters.csv"
# A price from fewer providers than this is flagged. Hospital facility fees are the usual case: most hospitals
# publish them as a percent of charges or a case rate, which are not dollar prices and are left out.
THIN_SAMPLE = 10
_POLICY_WRITE_LOCK = threading.Lock()

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
# Emergency care and prescription drugs are outside the product (decided 2026-10-27, docs/PRODUCT.md), so there is
# no ER scenario; encounters.csv keeps its rows for the MRF pipeline.
SCENARIOS: dict[str, dict[str, Part]] = {
    "office_visit_established": {"visit": Part("primary_care_visit", "office_visit")},
    "office_visit_new": {"visit": Part("primary_care_visit", "office_visit")},
    # A plan may price ordinary telehealth and its designated platform differently. The dedicated service keeps
    # those rows separate from office visits; ordinary telehealth may fall back to a PCP rule when the policy has
    # no telehealth-specific row, but the preferred-platform scenario never assumes that a generic PCP rule is a
    # preferred-platform benefit.
    "telehealth_visit": {"visit": Part("telehealth_visit", "office_visit", "primary_care_visit")},
    "preferred_telehealth_visit": {"visit": Part("telehealth_visit", "office_visit")},
    "specialist_followup": {"visit": Part("specialist_visit", "office_visit")},
    # The UC SHIP Ashe Center tier has no urgent care benefit: a same-day sick visit there is a primary care visit
    # ("No charge/visit (Ashe Ctr)", SBC p. 2; booklet p. 20, the Ashe Center is the student's medical home).
    "urgent_care_flu": {"visit": Part("urgent_care", fallback="primary_care_visit"), "flu_test": Part("lab_tests")},
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
SCENARIO_ALLOWS = {
    "telehealth_visit": re.compile(r"telehealth|online|virtual", re.I),
    "preferred_telehealth_visit": re.compile(r"preferred.*(?:online|telehealth)|livehealth", re.I),
}
SCENARIO_EXCLUDES = {
    # A generic request must not silently receive a $0 rate that is limited to the plan's designated platform.
    "telehealth_visit": re.compile(r"preferred.*(?:online|telehealth)|livehealth", re.I),
}
# Visits billed by one doctor's office, priced at a specific nearby doctor instead of a network median (2026-10-27).
# Telehealth is left out: it is often a separate platform service and has no meaningful nearby-office location.
DOCTOR_SCENARIOS = {"office_visit_established", "office_visit_new", "specialist_followup"}


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


@lru_cache(maxsize=4096)
def _component_at(rates_path: str, plan_key: str, code: str, billing_class: str, pos: str | None, modifier_rule: str,
                  npi: str, as_of: str) -> allowed.ComponentResult:
    """One doctor's own rate for a component (strict: no fallback to the network median)."""
    return allowed.component(_rates_con(rates_path), plan_key, code, billing_class, pos, modifier_rule, npi=npi,
                             as_of=as_of, strict_npi=True)


class PlanDB:
    def __init__(self, path: Path = POLICY_DB, rates_path: Path = RATES_DB,
                 connection: sqlite3.Connection | None = None):
        path = Path(path)
        if connection is None and not path.exists() and path != POLICY_SEED_DB:
            with _POLICY_WRITE_LOCK:
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(POLICY_SEED_DB, path)
        self.con = connection or sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.path = None if connection is not None else Path(path)
        self.con.row_factory = sqlite3.Row
        # Without the rate store only flat copays can be computed.
        self.rates_path = str(rates_path) if Path(rates_path).exists() else None

    def add_uploaded_plan(self, plan: dict, benefits: list[dict]) -> bool:
        """Persist one normalized PDF extraction in the shared policy database.

        The regular connection remains read-only. This method opens a short, serialized write transaction so
        concurrent uploads cannot interleave. Returns False when the same content-derived plan_id already exists.
        """
        if self.path is None:
            raise ValueError("A database path is required to persist an uploaded plan.")
        plan_id = plan.get("plan_id")
        if not plan_id or not str(plan_id).startswith("upload_"):
            raise ValueError("Uploaded plan_id must start with 'upload_'.")

        def values(row, columns):
            return [",".join(row.get(c) or []) if isinstance(row.get(c), list) else row.get(c) for c in columns]

        with _POLICY_WRITE_LOCK, sqlite3.connect(self.path, timeout=30) as write:
            write.execute("PRAGMA foreign_keys=ON")
            write.execute("BEGIN IMMEDIATE")
            if write.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone():
                return False
            plan_columns = [r[1] for r in write.execute("PRAGMA table_info(plans)")]
            benefit_columns = [r[1] for r in write.execute("PRAGMA table_info(benefits)")]
            write.execute(f"INSERT INTO plans ({', '.join(plan_columns)}) "
                          f"VALUES ({', '.join('?' * len(plan_columns))})", values(plan, plan_columns))
            write.executemany(f"INSERT INTO benefits ({', '.join(benefit_columns)}) "
                              f"VALUES ({', '.join('?' * len(benefit_columns))})",
                              [values(row, benefit_columns) for row in benefits])
        return True

    def warm(self) -> None:
        """Price every component of every scenario in every linked network, so requests don't wait."""
        if self.rates_path is None:
            return
        for (plan_key,) in self.con.execute("SELECT DISTINCT mrf_plan_key FROM plan_networks"):
            for scenario in SCENARIOS:
                for row in _encounters().get(scenario, ("", []))[1]:
                    self._price(plan_key, row)

    def _price(self, plan_key: str, row: dict, npi: str | None = None) -> allowed.ComponentResult:
        r = None
        for code in row["codes"].split("|"):  # alternatives, first with a rate wins
            args = (self.rates_path, plan_key, code, row["billing_class"], row["place_of_service"] or None,
                    row["modifier_rule"])
            today = dt.date.today().isoformat()
            r = _component_at(*args, npi, today) if npi else _component(*args, today)
            if r.allowed_amount is not None:
                break
        return r

    def priced_network(self, plan_id: str, tier: str | None) -> bool:
        """Whether this tier has a confirmed price network and the rate store is here (so a doctor can be priced)."""
        link = self.network(plan_id, tier)
        return bool(link and link["confirmed"] and self.rates_path)

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
        excluded = SCENARIO_EXCLUDES.get(scenario)

        def text(r):
            return " ".join(filter(None, (r["section"], r["service_as_printed"])))

        if excluded:
            rows = [r for r in rows if not excluded.search(text(r))]
        # Scenario-specific policy rows must actually describe that scenario. This makes an unavailable preferred
        # platform return no benefit instead of falling through to an ordinary telehealth price in the same service.
        if allowed and service == "telehealth_visit":
            rows = [r for r in rows if allowed.search(text(r))]
        if not rows:
            return None

        def rank(r):
            row_text = text(r)
            special = bool(SPECIAL.search(row_text)) and not (allowed and allowed.search(row_text))
            wanted = 0 if (allowed and allowed.search(row_text)) else 1
            comp = 0 if r["component"] == component else 1 if r["component"] is None else 2
            return (special, wanted, comp, not r["calculable"], r["_order"])

        return min(rows, key=rank)

    def rules(self, plan_id: str) -> list[dict]:
        """Plan-wide rules that are not amounts (for example UC SHIP's Ashe Center referral requirement)."""
        return [{"rule": r["service_as_printed"], "text": r["limits_conditions"] or r["quote"],
                 "source_page": r["source_page"]}
                for r in self.con.execute("SELECT * FROM benefits WHERE plan_id=? AND service='plan_level' "
                                          "AND copay_usd IS NULL AND coinsurance_pct IS NULL", (plan_id,))]

    def _plan_level(self, plan_id: str, pattern: str, network: str = "in", tier: str | None = None) -> dict | None:
        """The individual plan_level row of one tier whose name matches `pattern` (medical, not drug or dental)."""
        rows = [dict(r) for r in self.con.execute(
            "SELECT * FROM benefits WHERE plan_id=? AND service='plan_level' AND network=?", (plan_id, network))]
        rows = [r for r in rows if re.search(pattern, r["service_as_printed"] or "", re.I)
                and not re.search(r"drug|pharmacy|dental", r["service_as_printed"] or "", re.I)]
        if network == "in":
            exact = [r for r in rows if (r["network_tier"] or "") == (tier or "")]
            rows = exact or [r for r in rows if r["network_tier"] is None]
        return rows[0] if rows and rows[0]["copay_usd"] is not None else None

    def deductible(self, plan_id: str, network: str = "in", tier: str | None = None) -> float | None:
        """The individual medical deductible of one tier, from its plan_level row."""
        row = self._plan_level(plan_id, r"deductible", network, tier)
        return row["copay_usd"] if row else None

    def oop_max(self, plan_id: str, tier: str | None = None) -> float | None:
        """The individual in-network out-of-pocket limit of one tier, from its plan_level row."""
        row = self._plan_level(plan_id, r"out-of-pocket", "in", tier)
        return row["copay_usd"] if row else None

    # ------------------------------------------------------------------ pricing
    def network(self, plan_id: str, tier: str | None) -> dict | None:
        row = self.con.execute("SELECT * FROM plan_networks WHERE plan_id=? AND network_tier=?",
                               (plan_id, tier or "")).fetchone()
        return dict(row) if row else None

    def allowed_amounts(self, plan_id: str, tier: str | None, scenario: str, npi: str | None = None) -> dict:
        """Allowed amount of each component of a scenario in the plan's network, or why there is none.

        npi: price at this doctor (their own rate only); otherwise the Los Angeles network median.
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
            r = self._price(link["mrf_plan_key"], row, npi)
            parts.append({"component": row["component"], "billing_code": r.billing_code,
                          "billing_class": r.billing_class, "place_of_service": r.place_of_service,
                          "allowed_amount": r.allowed_amount, "basis": r.allowed_amount_basis,
                          "n_providers": r.n_providers, "p10": None if npi else r.p10, "p90": None if npi else r.p90,
                          "mrf_file_dates": r.mrf_file_dates})
        missing = [p["component"] for p in parts if p["allowed_amount"] is None]
        complete = not missing
        warnings = [f"The {p['component']} price comes from only {p['n_providers']} provider(s) in this network, "
                    "so it is rough; most hospitals publish facility fees as a percent of charges, which has no "
                    "dollar price." for p in parts
                    if not npi and p["allowed_amount"] is not None and p["n_providers"] < THIN_SAMPLE]

        def total(key):
            vals = [p[key] for p in parts]
            return round(sum(vals), 2) if complete and all(v is not None for v in vals) else None

        whose = "this doctor" if npi else "this network"
        return {"status": "priced" if complete else "partial" if len(missing) < len(parts) else "no_rates",
                "message": None if complete else f"No negotiated rate for {whose} for: " + ", ".join(missing) + ".",
                "basis": ("provider_rate" if npi else "la_county_median") if complete else None,
                "total": total("allowed_amount"), "low": total("p10"), "high": total("p90"),
                "components": {p["component"]: p["allowed_amount"] for p in parts}, "parts": parts,
                "warnings": warnings,
                "network": link["mrf_network"] or link["mrf_plan_key"],
                "mrf_file_date": ", ".join(sorted({d for p in parts for d in p["mrf_file_dates"]}))}

    # ------------------------------------------------------------------ premiums
    def premium(self, plan_id: str, age: int | None = None, zip_code: str | None = None,
                enrollee: str | None = None) -> dict:
        """One year's premium for this person, before any subsidy, or what is missing to price it.

        status: priced, needs_information (missing_fields: age / zip / student_level), unavailable (no premium for
        this plan, place or group). An age the source doesn't print is scaled from its age-40 row on age_curve."""
        rows = [dict(r) for r in self.con.execute("SELECT * FROM premiums WHERE plan_id=?", (plan_id,))]
        if not rows:
            return {"status": "unavailable", "message": "No published premium for this plan in Los Angeles is in the "
                                                        "database, so it can't be ranked on total cost."}
        missing, region = [], None
        if any(r["rating_region"] is not None for r in rows):
            if not zip_code:
                missing.append("zip")
            else:
                found = self.con.execute("SELECT rating_region FROM rating_regions WHERE zip=?",
                                         (str(zip_code).strip()[:5],)).fetchone()
                if found is None:
                    return {"status": "unavailable", "message": f"ZIP {zip_code} is not a Los Angeles County ZIP "
                                                                "code; premiums are only loaded for Los Angeles."}
                region = found[0]
        if any(r["age"] is not None for r in rows) and age is None:
            missing.append("age")
        if any(r["enrollee"] is not None for r in rows) and enrollee is None:
            missing.append("student_level")
        if missing:
            return {"status": "needs_information", "missing_fields": missing,
                    "message": "The premium depends on " + ", ".join(missing) + "."}
        rows = [r for r in rows if r["rating_region"] in (None, region) and r["enrollee"] in (None, enrollee)]
        if not rows:
            return {"status": "unavailable", "message": "This plan has no published premium for this person's "
                                                        "group or region."}
        scaled_from = None
        if age is not None and rows[0]["age"] is not None:
            age = min(max(int(age), 0), 64)
            row = next((r for r in rows if r["age"] == age), None)
            if row is None:
                row = next(r for r in rows if r["age"] == 40)
                factor = dict(self.con.execute("SELECT age, factor FROM age_curve").fetchall())
                scaled_from = 40
        else:
            row = rows[0]
        if scaled_from is None:
            amount, annual = row["amount_usd"], row["annual_usd"]
        else:
            amount = float((Decimal(str(row["amount_usd"])) * Decimal(str(factor[age])) / Decimal(str(factor[40])))
                           .quantize(Decimal("0.01")))
            annual = float((Decimal(str(amount)) * 12).quantize(Decimal("0.01")))
        message = f"${amount:,.2f} per {row['period']}, ${annual:,.2f} a year, before any subsidy."
        if scaled_from is not None:
            message += (f" Scaled from the published age-40 premium on the federal age curve (age {age}); within "
                        "about $1 a month of the insurer's price.")
        if row["basis"].endswith("preliminary"):
            message += " Covered California's 2026 rates as of 2025-08-01 (preliminary)."
        return {"status": "priced", "annual": annual, "period": row["period"], "amount": amount,
                "monthly": amount if row["period"] == "month" else None, "rating_region": region, "age": age,
                "enrollee": enrollee, "scaled_from_age": scaled_from, "basis": row["basis"], "message": message,
                "source_url": row["source_url"], "source_page": row["source_page"], "quote": row["quote"],
                "notes": row["notes"]}

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
                 remaining_deductible: float | None = None, doctor: dict | None = None,
                 no_rates: str | None = None) -> dict:
        """What the user pays for one occurrence of a scenario under one plan, network and tier.

        doctor ({"npi", ...}): price at this doctor's own negotiated rate; it is returned as result["doctor"].
        no_rates: price without any rate (only a flat copay can then be computed), with this reason."""
        result = self._estimate(plan_id, scenario, network, tier, remaining_deductible, doctor, no_rates)
        if doctor is not None:
            result["doctor"] = doctor
        return result

    def _estimate(self, plan_id, scenario, network, tier, remaining_deductible, doctor, no_rates) -> dict:
        plan = self.plan(plan_id)
        if plan is None:
            return {"status": "invalid_input", "message": f"Unknown plan {plan_id!r}.", "patient_cost": None}
        notice = ("Automatically extracted from an uploaded PDF; not human verified. "
                  if not plan.get("verified", True) else "")

        def msg(text):
            return notice + (text or "")

        if scenario not in SCENARIOS or scenario not in _encounters():
            return {"status": "invalid_input", "message": msg(f"Unknown scenario {scenario!r}."),
                    "patient_cost": None}
        if network == "in" and tier is None and plan["in_network_tiers"]:
            return {"status": "needs_information", "missing_fields": ["network_tier"], "patient_cost": None,
                    "message": msg("This plan has several in-network tiers; choose one."),
                    "options": plan["in_network_tiers"]}
        tier = tier if network == "in" else None
        label = _encounters()[scenario][0]
        if no_rates:
            prices = {"status": "no_rates", "message": no_rates}
        elif network == "in":
            prices = self.allowed_amounts(plan_id, tier, scenario, (doctor or {}).get("npi"))
        else:
            prices = {"status": "no_rates"}

        # Components paid under the same benefit row are one bill under that row (one copay per visit).
        groups: OrderedDict[str, dict] = OrderedDict()
        for comp, part in SCENARIOS[scenario].items():
            row = self.benefit(plan_id, part.service, network, tier, part.component, scenario)
            if row is None and part.fallback:
                row = self.benefit(plan_id, part.fallback, network, tier, part.component, scenario)
            if row is None:
                return self._result("needs_review", msg(f"The plan has no {part.service} benefit for this network/tier."),
                                    plan_id, scenario, label, network, tier, prices, [])
            g = groups.setdefault(row["benefit_id"], {"row": row, "components": [], "allowed": Decimal(0),
                                                      "priced": True})
            g["components"].append(comp)
            amount = (prices.get("components") or {}).get(comp)
            if amount is None:
                g["priced"] = False
            else:
                g["allowed"] += Decimal(str(amount))

        lines, total, paid = [], Decimal(0), Decimal(0)
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
                          "missing_fields": result["missing_fields"], "rule": rule_text(row),
                          "source_page": row["source_page"], "source_url": row["source_url"],
                          "quote": row["quote"], "limits": row["limits_conditions"],
                          "review_flags": row["review_flags"], "verified": bool(row.get("verified", True))})
            if result["status"] == "not_covered":
                return self._result("not_covered", msg(f"{row['service_as_printed']} is not covered."), plan_id, scenario,
                                    label, network, tier, prices, lines)
            if result["status"] != "estimated":
                missing = result["missing_fields"]
                if "allowed_amount" in missing and prices.get("status") in ("no_rates", "partial"):
                    return self._result("needs_review", msg(prices.get("message", "No price data.")), plan_id, scenario,
                                        label, network, tier, prices, lines)
                return self._result(result["status"], msg(result["message"]), plan_id, scenario, label, network, tier,
                                    prices, lines, missing)
            total += Decimal(str(result["patient_cost"]))
            if uses_deductible and remaining is not None:
                paid += min(remaining, g["allowed"])
                remaining -= min(remaining, g["allowed"])
        return self._result("estimated", msg("Estimate for one occurrence; excludes balance billing and the out-of-pocket "
                            "maximum. Not a coverage guarantee."), plan_id, scenario, label, network, tier, prices,
                            lines, patient_cost=float(total.quantize(Decimal("0.01"))), deductible_paid=float(paid))

    @staticmethod
    def _result(status, message, plan_id, scenario, label, network, tier, prices, lines, missing=None,
                patient_cost=None, deductible_paid=0.0):
        return {"status": status, "patient_cost": patient_cost, "deductible_paid": deductible_paid,
                "missing_fields": missing or [], "message": message,
                "plan_id": plan_id, "scenario": scenario, "scenario_label": label, "network": network,
                "network_tier": tier, "allowed_amount": {k: prices.get(k) for k in
                                                          ("status", "total", "low", "high", "basis", "network",
                                                           "mrf_file_date", "parts", "warnings")} if prices else None,
                "lines": lines}

    def year(self, plan: dict, tier: str | None, counts: dict, preferred: tuple = (), find_doctor=None) -> dict:
        """One plan tier over a year. counts: {scenario: (times a year, assumed)}. Each visit is priced in turn with
        the deductible still left, so it is used up across the year; the care total is capped at the out-of-pocket
        max. preferred: tiers the user goes to (e.g. "Ashe Center" for someone keeping the Ashe Center); a visit the
        plan's preferred tier can price is priced there, the rest in `tier`. find_doctor(plan_id, tier, scenario) ->
        (doctor or None, reason): a DOCTOR_SCENARIOS visit is priced at that doctor (the nearest one with a rate in
        this network), or without a rate when there is none."""
        plan_id = plan["plan_id"]
        pref = next((t for t in preferred if t in plan["in_network_tiers"] and t != tier), None)
        ded_row = self._plan_level(plan_id, r"deductible", "in", tier)
        oop_row = self._plan_level(plan_id, r"out-of-pocket", "in", tier)
        remaining = None if ded_row is None else Decimal(str(ded_row["copay_usd"]))
        visits, care, problem = [], Decimal(0), None
        for scenario, (times, assumed) in counts.items():
            runs, total, cached, first, failed = [], Decimal(0), None, None, None
            at = {}
            if find_doctor and scenario in DOCTOR_SCENARIOS and self.priced_network(plan_id, tier):
                doctor, reason = find_doctor(plan_id, tier, scenario)
                at = {"doctor": doctor} if doctor else {"no_rates": reason}
            at_pref = self.estimate(plan_id, scenario, "in", pref, None) if pref else None
            if at_pref is not None and at_pref["status"] == "estimated":  # a campus clinic: flat copays, no deductible
                first, used = at_pref, pref
                runs = [{"times": times, "patient_cost": at_pref["patient_cost"]}]
                total = Decimal(str(at_pref["patient_cost"])) * times
                times_left = 0
            else:
                used, times_left = tier, times
            for _ in range(times_left):
                if cached is None or cached[0] != remaining:  # the same deductible left gives the same cost
                    cached = (remaining, self.estimate(plan_id, scenario, "in", tier,
                                                       None if remaining is None else float(remaining), **at))
                r = cached[1]
                first = first or r
                if r["status"] != "estimated":
                    failed = r
                    break
                if runs and runs[-1]["patient_cost"] == r["patient_cost"]:
                    runs[-1]["times"] += 1
                else:
                    runs.append({"times": 1, "patient_cost": r["patient_cost"]})
                total += Decimal(str(r["patient_cost"]))
                if remaining is not None:
                    remaining -= Decimal(str(r["deductible_paid"]))
            prices = first["allowed_amount"] or {}
            visit = {"scenario": scenario, "label": first["scenario_label"], "times": times, "assumed": assumed,
                     "network_tier": used, "doctor": at.get("doctor") if used == tier else None,
                     "doctor_search": at.get("no_rates") if used == tier else None,
                     "tier_note": (f"{pref} can't price this visit (it needs a price or the deductible), so it is "
                                   f"priced at {tier}.") if pref and used != pref else None,
                     "status": failed["status"] if failed else "estimated",
                     "message": failed["message"] if failed else None,
                     "total": None if failed else float(total.quantize(Decimal("0.01"))), "runs": runs,
                     "math": None if failed else _math(runs, float(total)),
                     "lines": [{k: line.get(k) for k in ("benefit", "rule", "components", "allowed_amount",
                                                         "patient_cost", "source_page", "source_url", "quote")}
                               for line in first["lines"]],
                     "allowed_amount": {k: prices.get(k) for k in ("status", "total", "low", "high", "network",
                                                                   "mrf_file_date", "warnings")}}
            visits.append(visit)
            if failed and problem is None:
                problem = visit
            care += total
        oop = oop_row["copay_usd"] if oop_row else None
        before = None if problem else float(care.quantize(Decimal("0.01")))
        capped = before is not None and oop is not None and before > oop
        if problem:
            message = f"{problem['label']}: {problem['message']}"
        elif capped:
            message = f"Capped at the out-of-pocket max (${oop:,.2f}); the visits add up to ${before:,.2f}."
        else:
            message = None
        return {"plan_id": plan_id, "plan_name": plan["plan_name"], "insurer": plan["insurer"],
                "plan_type": plan["plan_type"], "network_tier": tier, "preferred_tier": pref,
                "deductible": {"amount": ded_row["copay_usd"] if ded_row else None, **_source(ded_row)},
                "oop_max": {"amount": oop, **_source(oop_row)},
                "visits": visits,
                "care": {"status": problem["status"] if problem else "estimated", "message": message,
                         "total": None if before is None else (oop if capped else before),
                         "before_cap": before, "capped": capped}}

    def compare(self, plan_ids: list[str], scenarios: list[str], profile: dict | None = None,
                visits: dict | None = None, preferences: dict | None = None,
                preferred_tiers: list[str] | None = None, find_doctor=None) -> dict:
        """A year of care under each plan for this person, ranked. find_doctor: see year().

        visits: {scenario: times a year} from the user's answers (0 drops a scenario); a chosen scenario without a
        count is counted once and marked assumed. A plan with several priced tiers is priced in its cheapest one for
        these visits. profile (age, zip, student_level) prices a year's premium. preferences ({"weights": {factor:
        1-5 or None}, "monthly_budget": USD a month or None}) rank the plans (rank_plans). preferred_tiers: tiers of
        the places the user wants to keep going (from keep_providers); a plan with one prices visits there first."""
        profile = profile or {}
        preferred = tuple(preferred_tiers or ())
        counts = visit_counts(scenarios, visits or {})
        out = []
        for plan_id in plan_ids:
            plan = self.plan(plan_id)
            if plan is None:
                out.append({"plan_id": plan_id, "status": "invalid_input", "message": "Unknown plan."})
                continue
            tier_source = plan["priced_tiers"] or (plan["in_network_tiers"]
                                                    if plan["plan_id"].startswith("upload_") else [])
            tiers = [t or None for t in tier_source] or [None]
            best = min((self.year(plan, tier, counts, preferred, find_doctor) for tier in tiers),
                       key=lambda e: (e["care"]["total"] is None, e["care"]["total"] or 0))
            if len(tiers) > 1:
                best["tier_note"] = (f"Priced at {best['network_tier']}, the cheapest of its priced tiers "
                                     f"({', '.join(tiers)}) for these visits.")
            if best["preferred_tier"] and any(v["network_tier"] == best["preferred_tier"] for v in best["visits"]):
                best["tier_note"] = (f"Visits {best['preferred_tier']} can price are priced there, because you want "
                                     f"to keep going there; the rest at {best['network_tier']}.")
            premium = self.premium(plan_id, profile.get("age"), profile.get("zip"), profile.get("student_level"))
            best["premium"] = premium
            annual, care = premium.get("annual"), best["care"]["total"]
            best["year_total"] = round(annual + care, 2) if annual is not None and care is not None else None
            out.append(best)
        missing = sorted({f for e in out for f in (e.get("premium") or {}).get("missing_fields", [])})
        ranking = rank_plans(out, preferences or {}, by_premium=not missing)
        note = ("Care = each visit priced in turn over the year (the deductible used up as it is paid), capped at the "
                "out-of-pocket max; balance billing is not included. year_total = a year's premium before any "
                "subsidy (premium tax credits are not computed) + the care.")
        if any(assumed for _, assumed in counts.values()):
            note += " Visits marked assumed are counted once because the user gave no count."
        if missing:
            note += (" Premiums need " + ", ".join(missing) + ", so this ranking leaves out the largest cost and is "
                     "not a recommendation.")
        enc = _encounters()
        return {"visits": [{"scenario": s, "label": enc[s][0], "times": n, "assumed": assumed}
                           for s, (n, assumed) in counts.items()],
                "plans": out, "ranked_by": ranking["method"], "ranking": ranking, "missing_fields": missing,
                "note": note}


def _usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _math(runs: list[dict], total: float) -> str:
    """How a visit's year total adds up, e.g. "1 x $230.00 + 9 x $25.00 = $455.00"."""
    return " + ".join(f"{r['times']} x {_usd(r['patient_cost'])}" for r in runs) + f" = {_usd(total)}"


def _source(row: dict | None) -> dict:
    return {k: (row or {}).get(k) for k in ("source_page", "source_url", "quote")}


def rule_text(row: dict) -> str:
    """A benefit row's cost share in words, e.g. "$25.00 copay, deductible doesn't apply"."""
    if row["covered"] == 0:
        return "Not covered"
    copay, coins = row["copay_usd"], row["coinsurance_pct"]
    parts = []
    if copay is not None:
        parts.append("No charge" if copay == 0 and coins is None else f"{_usd(copay)} copay")
    if coins is not None:
        parts.append(f"{coins:g}% coinsurance")
    text = " + ".join(parts) or "No amount stated"
    if row["deductible_applies"] == 1:
        text += " after the deductible"
    elif row["deductible_applies"] == 0 and parts:
        text += ", deductible doesn't apply"
    return text


def visit_counts(scenarios: list[str], visits: dict) -> OrderedDict:
    """scenario -> (times a year, assumed). The user's counts win; 0 drops a scenario; a chosen scenario without a
    count is counted once and marked assumed; a count for a scenario the model didn't choose is still included."""
    counts: OrderedDict[str, tuple[int, bool]] = OrderedDict()
    for s in [*scenarios, *visits]:
        if s in counts or s not in SCENARIOS:
            continue
        if s in visits and visits[s] is not None:
            if int(visits[s]) > 0:
                counts[s] = (int(visits[s]), False)
        else:
            counts[s] = (1, True)
    return counts


# ------------------------------------------------------------------ ranking
# The questionnaire's weight questions (bank.WEIGHT_FIELDS), and what each compares. Lower is better for every value.
FACTORS = {
    "premium": "A low premium",
    "visit_costs": "Low costs when you see a doctor",
    "worst_case": "Protection in a bad year (a low out-of-pocket max)",
    "keep_doctors": "Keeping your current doctors in network",
}
DEFAULT_ANSWER = 3  # a priority the user didn't rate counts as the middle of the 1-5 scale


def _factor_value(entry: dict, factor: str) -> float | None:
    if factor == "premium":
        return (entry.get("premium") or {}).get("annual")
    if factor == "visit_costs":
        return entry["care"]["total"]
    if factor == "worst_case":
        return entry["oop_max"]["amount"]
    return None


def rank_plans(entries: list[dict], preferences: dict, by_premium: bool = True) -> dict:
    """Rank the plans in place (entry["rank"], entry["score"]) and return how they were ranked.

    With no priority rated: by year_total (premium + care), or by care alone while premiums are missing.
    With priorities rated (weighted_score): on each priority the best plan gets 100 points and the worst 0, in a
    straight line between them by dollars. A 1-5 answer is a weight of answer - 1 (1 = don't care = 0); a priority
    not rated counts as 3. The score is the weighted average of the points. A plan whose premium is over the monthly
    budget ranks after every plan within it; ties go to the lower year total."""
    weights = preferences.get("weights") or {}
    budget = preferences.get("monthly_budget")

    def key(e):
        return e["year_total"] if by_premium else e["care"]["total"]

    ranked = []
    for e in entries:
        if "care" not in e:
            continue
        if key(e) is None:
            premium = e["premium"]
            e["not_ranked"] = (premium.get("message") if by_premium and premium.get("annual") is None
                               else e["care"]["message"])
            continue
        ranked.append(e)
        annual = e["premium"].get("annual")
        if budget is not None and annual is not None:
            e["budget"] = {"monthly_premium": round(annual / 12, 2), "monthly_budget": budget,
                           "over": annual / 12 > budget}
    names = {e["plan_id"]: e["plan_name"] for e in ranked}

    factors = []
    for factor, label in FACTORS.items():
        answer = weights.get(factor)
        weight = (DEFAULT_ANSWER if answer is None else answer) - 1
        unknown = [names[e["plan_id"]] for e in ranked if _factor_value(e, factor) is None]
        if factor == "keep_doctors":
            reason = "Not scored yet: the database can't check whether your doctors are in each plan's network."
        elif factor == "premium" and not by_premium:
            reason = "Not scored: the premium needs your age, ZIP or student level."
        elif unknown:
            reason = "Not scored: unknown for " + ", ".join(unknown) + "."
        elif weight == 0:
            reason = "You rated it 1 (don't care), so it has no weight."
        else:
            reason = None
        factors.append({"factor": factor, "label": label, "answer": answer, "weight": weight if reason is None else 0,
                        "used": reason is None, "reason": reason})
    used = [f for f in factors if f["used"]]
    total_weight = sum(f["weight"] for f in used)
    rated = any(weights.get(f) is not None for f in FACTORS)
    method = "weighted_score" if rated and total_weight > 0 else "year_total" if by_premium else "visits_only"

    if method == "weighted_score":
        for f in used:
            values = [_factor_value(e, f["factor"]) for e in ranked]
            low, high = min(values), max(values)
            for e, value in zip(ranked, values):
                points = 100.0 if high == low else round((high - value) / (high - low) * 100, 1)
                e.setdefault("score", {"total": 0.0, "parts": []})["parts"].append({
                    "factor": f["factor"], "label": f["label"], "value": value, "points": points,
                    "weight": f["weight"], "contribution": round(points * f["weight"] / total_weight, 1)})
        for e in ranked:
            e["score"]["total"] = round(sum(p["contribution"] for p in e["score"]["parts"]), 1)
        summary = ("Score out of 100 = the weighted average of each priority's points. On each priority the best plan "
                   "gets 100 points and the worst 0, in a straight line between them by dollars. Weight = your 1-5 "
                   f"rating minus 1 (1 = don't care); a priority you didn't rate counts as {DEFAULT_ANSWER}. Ties go "
                   "to the lower year total.")
    elif method == "year_total":
        summary = ("Ranked by year total: a year's premium plus your expected care. Rate your priorities to rank by "
                   "them instead.")
    else:
        summary = ("Ranked by expected care only, because premiums are still missing; this leaves out the largest "
                   "cost and is not a recommendation.")
    if budget is not None:
        summary += f" Plans whose premium is over your budget of {_usd(budget)} a month rank after the ones within it."

    ranked.sort(key=lambda e: ((e.get("budget") or {}).get("over", False),
                               -e["score"]["total"] if method == "weighted_score" else 0, key(e)))
    for i, e in enumerate(ranked, 1):
        e["rank"] = i
    return {"method": method, "summary": summary, "factors": factors if method == "weighted_score" else [],
            "monthly_budget": budget}
