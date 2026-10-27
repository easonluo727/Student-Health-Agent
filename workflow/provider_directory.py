"""Read-only NPPES location search and strictly NPI-specific plan pricing."""
from __future__ import annotations

import datetime as dt
import heapq
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from calculate_cost import calculate_cost
from plan_db import DOCTOR_SCENARIOS, POLICY_DB, RATES_DB, PlanDB, SCENARIOS, _encounters

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DB = Path(os.environ.get("PROVIDERS_DB", "/data/providers.sqlite" if Path("/data").exists()
                                else ROOT / "data" / "providers" / "providers.sqlite"))
MAX_RADIUS_MILES = 100
MAX_RESULTS = 50
MAX_CANDIDATES = 5000
MAX_COST_SORT_CANDIDATES = 200
SINGLE_PROVIDER_SCENARIOS = {"office_visit_established", "office_visit_new", "telehealth_visit",
                             "specialist_followup", "therapy_intake", "therapy_session_45", "therapy_session_60"}
# NUCC Health Care Provider Taxonomy codes (nucc.org); a code ending in X is exact, a shorter one is a prefix that
# includes the subspecialties.
SPECIALTIES = {
    "family_medicine": "207Q",
    "internal_medicine": "207R00000X",
    "pediatrics": "2080",
    "obstetrics_gynecology": "207V",
    "dermatology": "207N",
    "psychiatry": "2084P0800X",
    "allergy_immunology": "207K",
    "pulmonology": "207RP1001X",
    "cardiology": "207RC0000X",
    "orthopaedic_surgery": "207X",
    "psychologist": "103T",
    "clinical_social_worker": "1041C0700X",
    "physical_therapist": "2251",
}
# A primary care doctor: family medicine, internal medicine, general practice.
PRIMARY_CARE = ("207Q", "207R00000X", "208D")


def check_specialty(args: dict) -> None:
    """Some endpoints don't enforce the schema's enum (Meta's API, 2026-10-27), so check it here."""
    value = args.get("specialty")
    if value is not None and value not in SPECIALTIES:
        raise ValueError(f"Unknown specialty {value!r}; use one of {', '.join(SPECIALTIES)}, or null.")


def visit_taxonomies(scenario: str, specialty: str | None) -> tuple[str, ...]:
    """Which doctors a visit is priced at: the chosen specialty, else primary care for a primary care visit."""
    if specialty:
        return (SPECIALTIES[specialty],)
    return () if scenario == "specialist_followup" else PRIMARY_CARE
NEARBY_RADIUS_MILES = 25  # how far to look for a doctor with a rate before saying there is none
NEARBY_DOCTORS = 5
NEARBY_MAX_CHECKED = 300  # nearest candidates whose rates are looked up before giving up


def _ro(path: Path) -> sqlite3.Connection:
    from urllib.parse import quote as escape

    con = sqlite3.connect(f"file:{escape(str(path.resolve(strict=True)))}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlon = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(min(1, math.sqrt(a)))


def _bbox(lat: float, lon: float, miles: float) -> tuple[float, float, float, float]:
    lat_delta = miles / 69.0
    lon_delta = miles / max(1.0, 69.0 * abs(math.cos(math.radians(lat))))
    return lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta


class ProviderDirectory:
    def __init__(self, providers_path: Path = PROVIDERS_DB, policy_path: Path = POLICY_DB,
                 rates_path: Path = RATES_DB):
        self.providers_path = Path(providers_path)
        self.policy_path = Path(policy_path)
        self.rates_path = Path(rates_path)

    def search(self, *, latitude: float | None = None, longitude: float | None = None,
               zip_code: str | None = None, radius_miles: float = 10, scenario: str | None = None,
               cpt: str | None = None, taxonomy: str | None = None, plan_ids: list[str] | None = None,
               plan_tiers: dict[str, str] | None = None, remaining_deductible: float | None = None,
               limit: int = 20, offset: int = 0, sort: str = "nearest") -> dict:
        if not 0 < radius_miles <= MAX_RADIUS_MILES or not 1 <= limit <= MAX_RESULTS or offset < 0:
            raise ValueError("radius, limit or offset is outside the supported range")
        if (latitude is None) != (longitude is None):
            raise ValueError("Latitude and longitude must be supplied together")
        if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid user coordinates")
        if latitude is None and not (zip_code and len(zip_code) == 5 and zip_code.isdigit()):
            raise ValueError("Supply GPS coordinates or a five-digit ZIP")
        if scenario and scenario not in SCENARIOS:
            raise ValueError("Unknown scenario")
        if cpt and (not cpt.isalnum() or len(cpt) > 7):
            raise ValueError("Invalid CPT/HCPCS code")
        if not scenario and not cpt:
            raise ValueError("Supply a scenario or CPT/HCPCS code")
        if taxonomy and (not taxonomy.isalnum() or len(taxonomy) > 10):
            raise ValueError("Invalid taxonomy prefix")
        if sort not in {"nearest", "lowest_cost"}:
            raise ValueError("Unknown sort mode")
        if remaining_deductible is not None and remaining_deductible < 0:
            raise ValueError("Remaining deductible cannot be negative")
        if not self.providers_path.is_file():
            return {"status": "directory_unavailable", "providers": [], "plans": [],
                    "message": "The provider directory has not been installed."}
        with closing(_ro(self.providers_path)) as directory:
            metadata = dict(directory.execute("SELECT key,value FROM import_metadata"))
            position_precision = "gps" if latitude is not None else "zcta_representative_point"
            if latitude is None:
                row = directory.execute("SELECT latitude,longitude,source_url FROM zip_centroids WHERE zip5=?",
                                        (zip_code,)).fetchone()
                if row:
                    latitude, longitude = row["latitude"], row["longitude"]
                else:
                    position_precision = "zip_only_distance_unavailable"
            items, candidate_truncated = self._locations(directory, latitude, longitude, zip_code,
                                                          radius_miles, scenario, taxonomy)
        policy = PlanDB(self.policy_path, self.rates_path)
        try:
            all_plans = {p["plan_id"]: p for p in policy.plans()}
            requested = list(dict.fromkeys(plan_ids or []))
            if len(requested) > 12 or any(plan_id not in all_plans for plan_id in requested):
                raise ValueError("Select at most 12 known plan IDs")
            plan_columns = [self._plan_column(policy, all_plans[plan_id], (plan_tiers or {}).get(plan_id))
                            for plan_id in requested]
            if sort == "lowest_cost":
                candidate_truncated |= len(items) > MAX_COST_SORT_CANDIDATES
                items = items[:MAX_COST_SORT_CANDIDATES]
                for item in items:
                    item["prices"] = self._prices(policy, item, plan_columns, scenario, cpt, remaining_deductible)
                items.sort(key=lambda item: (min((v["patient_cost"] for v in item["prices"].values()
                                                  if v["patient_cost"] is not None), default=math.inf),
                                             item["distance_miles"] if item["distance_miles"] is not None else math.inf,
                                             item["location_id"]))
            total = len(items)
            page = items[offset:offset + limit]
            if sort == "nearest":
                for item in page:
                    item["prices"] = self._prices(policy, item, plan_columns, scenario, cpt, remaining_deductible)
        finally:
            policy.con.close()
        return {"status": "ok", "providers": page, "plans": plan_columns, "total": total,
                "candidate_truncated": candidate_truncated,
                "sort_scope": "nearest_200_candidates" if sort == "lowest_cost" else "nearest_candidates",
                "limit": limit, "offset": offset, "user_position": {"latitude": latitude, "longitude": longitude,
                     "zip_code": zip_code, "precision": position_precision},
                "provenance": {"nppes_source_url": metadata.get("cms_source_url"),
                               "nppes_release_date": metadata.get("nppes_release_date"),
                               "nppes_archive_sha256": metadata.get("archive_sha256")},
                "message": "Locations come from NPPES. Current insurance participation is unverified."}

    def nearest_priced(self, zip_code: str, scenario: str, taxonomies: tuple[str, ...], plan_id: str,
                       tier: str | None, n: int = NEARBY_DOCTORS, cache: dict | None = None) -> dict:
        """The n nearest doctors (individuals, one entry per NPI, within NEARBY_RADIUS_MILES of the ZIP) who have
        their own negotiated rate for this visit in the plan tier's network. Nobody else's rate and no median: a
        doctor without a rate is skipped. cache keeps the candidate list across plans for one ZIP and specialty.

        {"doctors": [{npi, name, address, phone, distance_miles, primary_taxonomy, negotiated_rate,
        map_destination}], "checked": candidates looked at, "message": what was searched (and why none, if so)}."""
        what = f"within {NEARBY_RADIUS_MILES} miles of ZIP {zip_code}"
        if not (zip_code and len(zip_code) == 5 and zip_code.isdigit()):
            return {"doctors": [], "checked": 0, "message": f"ZIP {zip_code!r} is not a five-digit ZIP code."}
        if not self.providers_path.is_file():
            return {"doctors": [], "checked": 0, "message": "The provider directory is not installed, so no "
                                                             "doctor could be priced."}
        key = (zip_code, scenario, tuple(taxonomies))
        if cache is None or key not in cache:
            with closing(_ro(self.providers_path)) as directory:
                row = directory.execute("SELECT latitude,longitude FROM zip_centroids WHERE zip5=?",
                                        (zip_code,)).fetchone()
                lat, lon = (row["latitude"], row["longitude"]) if row else (None, None)
                items, _ = self._locations(directory, lat, lon, zip_code, NEARBY_RADIUS_MILES, scenario, taxonomies)
            if cache is not None:
                cache[key] = items
        else:
            items = cache[key]
        policy = PlanDB(self.policy_path, self.rates_path)
        try:
            column = self._plan_column(policy, policy.plan(plan_id), tier or "")
            doctors, seen, checked = [], set(), 0
            for item in items:
                if len(doctors) >= n or checked >= NEARBY_MAX_CHECKED:
                    break
                if item["npi"] in seen:
                    continue
                seen.add(item["npi"])
                checked += 1
                cell = self._prices(policy, item, [column], scenario, None, None)[plan_id]
                if cell["rate_available"]:
                    doctors.append({**{k: item[k] for k in ("npi", "name", "address", "phone", "distance_miles",
                                                            "primary_taxonomy", "map_destination")},
                                    "negotiated_rate": cell["negotiated_rate"]})
        finally:
            policy.con.close()
        if doctors:
            message = (f"The {len(doctors)} nearest doctor(s) {what} with their own negotiated rate in this network "
                       f"({checked} nearest doctors checked).")
        else:
            message = (f"None of the {checked} nearest doctors {what} has a negotiated rate for this visit in this "
                       "network.")
        return {"doctors": doctors, "checked": checked, "message": message}

    @staticmethod
    def _locations(con: sqlite3.Connection, lat: float | None, lon: float | None, zip_code: str | None,
                   radius: float, scenario: str | None, taxonomy) -> tuple[list[dict], bool]:
        base = ("SELECT l.*,p.entity_type,p.display_name,p.primary_taxonomy,p.nppes_last_updated "
                "FROM provider_locations l JOIN providers p ON p.npi=l.npi "
                "WHERE p.nppes_status='active' AND p.entity_type='1'")
        params: list = []
        prefixes = (taxonomy,) if isinstance(taxonomy, str) else tuple(taxonomy or ())
        if prefixes:
            base += (" AND EXISTS (SELECT 1 FROM provider_taxonomies t WHERE t.npi=p.npi AND ("
                     + " OR ".join(["t.taxonomy_code LIKE ?"] * len(prefixes)) + "))")
            params += [prefix + "%" for prefix in prefixes]
        found: dict[int, dict] = {}
        truncated = False
        if lat is not None:
            south, north, west, east = _bbox(lat, lon, radius)
            sql = base + " AND l.latitude BETWEEN ? AND ? AND l.longitude BETWEEN ? AND ? "
            sql += "ORDER BY l.location_id"
            nearest: list[tuple[float, int, sqlite3.Row]] = []
            for row in con.execute(sql, (*params, south, north, west, east)):
                distance = haversine_miles(lat, lon, row["latitude"], row["longitude"])
                if distance <= radius:
                    entry = (-distance, -row["location_id"], row)
                    if len(nearest) < MAX_CANDIDATES:
                        heapq.heappush(nearest, entry)
                    elif entry > nearest[0]:
                        heapq.heapreplace(nearest, entry)
                        truncated = True
                    else:
                        truncated = True
            for neg_distance, _, row in nearest:
                found[row["location_id"]] = ProviderDirectory._item(row, round(-neg_distance, 2))
        if zip_code:
            sql = base + " AND l.zip5=?"
            if lat is not None:
                sql += " AND l.latitude IS NULL"
            sql += " ORDER BY l.location_id LIMIT ?"
            zip_rows = con.execute(sql, (*params, zip_code, MAX_CANDIDATES + 1)).fetchall()
            truncated |= len(zip_rows) > MAX_CANDIDATES
            for row in zip_rows[:MAX_CANDIDATES]:
                found[row["location_id"]] = ProviderDirectory._item(row, None)
        result = list(found.values())
        result.sort(key=lambda item: (item["distance_miles"] if item["distance_miles"] is not None else math.inf,
                                      item["location_id"]))
        return result, truncated

    @staticmethod
    def _item(row: sqlite3.Row, distance: float | None) -> dict:
        address = ", ".join(filter(None, [row["address_line_1"], row["address_line_2"], row["city"],
                                           row["state"], row["postal_code"]]))
        if row["latitude"] is not None:
            destination = f"https://www.openstreetmap.org/directions?to={row['latitude']},{row['longitude']}"
        else:
            destination = "https://www.openstreetmap.org/search?query=" + quote(address)
        return {"npi": row["npi"], "location_id": row["location_id"], "name": row["display_name"],
                "entity_type": row["entity_type"], "primary_taxonomy": row["primary_taxonomy"],
                "nppes_last_updated": row["nppes_last_updated"], "location_type": row["location_type"],
                "address": address, "phone": row["phone"], "zip5": row["zip5"],
                "latitude": row["latitude"], "longitude": row["longitude"],
                "geocode_precision": row["geocode_precision"], "geocode_source": row["geocode_source"],
                "distance_miles": distance, "distance_status": "approximate" if distance is not None else "distance_unavailable",
                "map_destination": destination, "directory_match": "nppes_practice_location",
                "network_verification": "unverified",
                "location_rate_scope": "NPI-level rate; applicability at this location is unverified"}

    @staticmethod
    def _plan_column(policy: PlanDB, plan: dict, tier: str | None) -> dict:
        links = [dict(row) for row in policy.con.execute(
            "SELECT * FROM plan_networks WHERE plan_id=? AND confirmed=1 ORDER BY network_tier", (plan["plan_id"],))]
        if tier is not None:
            links = [link for link in links if (link["network_tier"] or "") == tier]
        status = "confirmed_plan_network" if len(links) == 1 else "tier_required" if len(links) > 1 else "network_unavailable"
        link = links[0] if len(links) == 1 else None
        return {"plan_id": plan["plan_id"], "name": plan["plan_name"], "network_tier": link["network_tier"] if link else tier,
                "plan_network_status": status, "available_tiers": [x["network_tier"] for x in links],
                "mrf_plan_key": link["mrf_plan_key"] if link else None,
                "network_evidence": link["notes"] if link else None}

    def _prices(self, policy: PlanDB, item: dict, plans: list[dict], scenario: str | None,
                cpt: str | None, remaining_deductible: float | None) -> dict:
        cells = {}
        rows = _encounters().get(scenario, (None, []))[1] if scenario else []
        single = scenario in SINGLE_PROVIDER_SCENARIOS and len(rows) == 1 and rows[0]["billing_class"] == "professional"
        for plan in plans:
            cell = {"rate_available": False, "negotiated_rate": None, "patient_cost": None,
                    "network_verification": "unverified", "coverage_status": "unknown",
                    "estimate_status": "unavailable", "rate_status": "rate_unavailable",
                    "billing_code": cpt, "provenance": None}
            cells[plan["plan_id"]] = cell
            if plan["plan_network_status"] != "confirmed_plan_network":
                cell["estimate_status"] = plan["plan_network_status"]
                continue
            if not self.rates_path.is_file():
                cell["estimate_status"] = "rate_store_unavailable"
                continue
            if not scenario:
                cell["estimate_status"] = "billing_context_required"
                continue
            if not single and not cpt:
                cell["estimate_status"] = "billing_provider_mismatch"
                continue
            selected = ([dict(row, codes=cpt) for row in rows if cpt in row["codes"].split("|")]
                        if cpt else rows)
            if not selected:
                cell["estimate_status"] = "service_mapping_unavailable"
                continue
            row = selected[0]
            with closing(_ro(self.rates_path)) as rates:
                from plan_db import allowed
                rate = None
                for code in row["codes"].split("|"):
                    rate = allowed.component(rates, plan["mrf_plan_key"], code, row["billing_class"],
                                             row["place_of_service"] or None, row["modifier_rule"],
                                             npi=item["npi"], as_of=dt.date.today().isoformat(), strict_npi=True)
                    if rate.allowed_amount is not None:
                        break
            cell["billing_code"] = rate.billing_code
            cell["provenance"] = {"mrf_source_urls": rate.source_urls, "mrf_file_dates": rate.mrf_file_dates,
                                  "basis": rate.allowed_amount_basis, "negotiated_type_mix": rate.negotiated_type_mix,
                                  "plan_network_evidence": plan["network_evidence"]}
            if rate.allowed_amount is None:
                continue
            cell["rate_available"] = True
            cell["rate_status"] = "provider_rate"
            cell["negotiated_rate"] = rate.allowed_amount
            if not single or (cpt and cpt not in rows[0]["codes"].split("|")):
                cell["estimate_status"] = "partial_rate_only"
                continue
            part = next(iter(SCENARIOS[scenario].values()))
            benefit = policy.benefit(plan["plan_id"], part.service, "in", plan["network_tier"], part.component, scenario)
            if benefit is None and part.fallback:
                benefit = policy.benefit(plan["plan_id"], part.fallback, "in", plan["network_tier"], part.component, scenario)
            if benefit is None:
                cell["estimate_status"] = "benefit_unavailable"
                continue
            rule = policy._rule(benefit)
            cell["coverage_status"] = rule["coverage_status"]
            calc = calculate_cost(rule, rate.allowed_amount, remaining_deductible)
            cell["estimate_status"] = calc["status"]
            cell["missing_fields"] = calc["missing_fields"]
            cell["patient_cost"] = calc["patient_cost"] if calc["status"] == "estimated" else None
            cell["benefit_source"] = {"url": benefit["source_url"], "page": benefit["source_page"],
                                      "quote": benefit["quote"]}
        return cells


def estimate_nearby(db: PlanDB, directory: ProviderDirectory, plan_id: str, scenario: str, network: str,
                    tier: str | None, remaining_deductible: float | None, zip_code: str | None,
                    taxonomies: tuple[str, ...]) -> dict:
    """PlanDB.estimate at specific doctors near the user. A visit to one doctor (DOCTOR_SCENARIOS) in a tier with a
    price network is priced at each of the nearest doctors with their own rate: the result is the nearest doctor's
    estimate, plus every doctor's cost ("doctors") and their range ("cost_range"). It needs the user's ZIP. With no
    such doctor nearby only a flat copay can be computed. Other visits and tiers are priced as before."""
    if not (scenario in DOCTOR_SCENARIOS and network == "in" and db.priced_network(plan_id, tier)):
        return db.estimate(plan_id, scenario, network, tier, remaining_deductible)
    if not zip_code:
        return {"status": "needs_information", "missing_fields": ["zip"], "patient_cost": None,
                "plan_id": plan_id, "scenario": scenario, "network": network, "network_tier": tier,
                "message": "This visit is priced at specific doctors near the user, so it needs their ZIP code."}
    found = directory.nearest_priced(zip_code, scenario, taxonomies, plan_id, tier)
    if not found["doctors"]:
        result = db.estimate(plan_id, scenario, network, tier, remaining_deductible, no_rates=found["message"])
        result["doctor_search"] = found["message"]
        return result
    runs = [db.estimate(plan_id, scenario, network, tier, remaining_deductible, doctor=d) for d in found["doctors"]]
    result = runs[0]
    result["doctors"] = [{**r["doctor"], "status": r["status"], "patient_cost": r["patient_cost"]} for r in runs]
    costs = [r["patient_cost"] for r in runs if r["status"] == "estimated"]
    result["cost_range"] = {"low": min(costs), "high": max(costs)} if costs else None
    result["doctor_search"] = found["message"]
    return result


def doctor_finder(directory: ProviderDirectory, zip_code: str, taxonomies_for):
    """find_doctor for PlanDB.compare: (plan_id, tier, scenario) -> (the nearest doctor with a rate, or None; why).
    taxonomies_for(scenario) gives the specialty prefixes to search. Candidates are looked up once per scenario."""
    cache: dict = {}

    def find(plan_id, tier, scenario):
        found = directory.nearest_priced(zip_code, scenario, taxonomies_for(scenario), plan_id, tier, n=1, cache=cache)
        return (found["doctors"][0] if found["doctors"] else None), found["message"]

    return find
