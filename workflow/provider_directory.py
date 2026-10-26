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
from plan_db import POLICY_DB, RATES_DB, PlanDB, SCENARIOS, _encounters

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DB = Path(os.environ.get("PROVIDERS_DB", "/data/providers.sqlite" if Path("/data").exists()
                                else ROOT / "data" / "providers" / "providers.sqlite"))
MAX_RADIUS_MILES = 100
MAX_RESULTS = 50
MAX_CANDIDATES = 5000
MAX_COST_SORT_CANDIDATES = 200
SINGLE_PROVIDER_SCENARIOS = {"office_visit_established", "office_visit_new", "telehealth_visit",
                             "specialist_followup", "therapy_intake", "therapy_session_45", "therapy_session_60"}


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

    @staticmethod
    def _locations(con: sqlite3.Connection, lat: float | None, lon: float | None, zip_code: str | None,
                   radius: float, scenario: str | None, taxonomy: str | None) -> tuple[list[dict], bool]:
        base = ("SELECT l.*,p.entity_type,p.display_name,p.primary_taxonomy,p.nppes_last_updated "
                "FROM provider_locations l JOIN providers p ON p.npi=l.npi "
                "WHERE p.nppes_status='active' AND p.entity_type='1'")
        params: list = []
        prefix = taxonomy
        if prefix:
            base += " AND EXISTS (SELECT 1 FROM provider_taxonomies t WHERE t.npi=p.npi AND t.taxonomy_code LIKE ?)"
            params.append(prefix + "%")
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
