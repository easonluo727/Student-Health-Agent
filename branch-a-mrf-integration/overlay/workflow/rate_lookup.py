"""Deterministic rate lookup over the canonical views (canonical_adapter). No LLM, fail closed.

Input (rate_request.v1): insurer, plan_key, exact MRF network, encounter_key OR billing_code (+ class,
POS, optional modifier_rule), service_date, selected_provider_npi.

  A_encounter   key plan_key + exact network + encounter_key            (aggregate table A)
  B_component   key plan_key + exact network + code + class + POS        (aggregate table B)
  provider      selected NPI: exact rows in canonical_provider_rates (SQLite), per component
  nearby        selected NPI without an exact rate: up to 20 nearby in-network providers that pass every
                hard rule, scored (distance 35, POS 25, taxonomy 15, freshness 15, source quality 10),
                unweighted median of one representative amount per provider

Output (rate_lookup_result.v1): status provider_rate | nearby_provider_estimate | network_estimate |
rate_unavailable, with reason_code, basis, match path and keys, p10/p90, snapshot dates and sources.
Unavailable results always have allowed_amount = null. Nothing falls back across insurer, network, code,
billing class or POS, and a partial component sum is never returned as a total.
"""
from __future__ import annotations

import csv
import datetime as dt
import functools
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

import canonical_adapter as ca
from trace_log import TraceLog

LOOKUP_VERSION = "rate_lookup.v1"
DOLLAR_TYPES = {"negotiated", "derived", "fee schedule"}  # same as scripts/mrf/allowed.py
SOURCE_QUALITY = {"negotiated": 1.0, "fee schedule": 0.9, "derived": 0.8}
DEFAULT_MAX_SNAPSHOT_AGE_DAYS = 62
LOW_SAMPLE = 5


@dataclass(frozen=True)
class FallbackConfig:
    top_k: int = 20
    radius_miles: float = 25.0
    max_per_tin: int = 3
    weights: tuple = (("distance", 0.35), ("pos", 0.25), ("taxonomy", 0.15), ("freshness", 0.15),
                      ("source_quality", 0.10))


@dataclass(frozen=True)
class PlanContext:
    plan_key: str
    insurer: str
    network: str | None          # exact MRF network name; None for plans imported without a network filter
    network_scope: str | None    # market label used when network is None


# --------------------------------------------------------------------------- reference data

def load_centroids(path: Path | None) -> dict[str, tuple[float, float]]:
    if path is None or not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["zip"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)}


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class NpiDirectory:
    """NPPES practice location, taxonomy and entity type per NPI (data/mrf/la_npis.csv), loaded lazily.
    Being listed here says nothing about network participation."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._d: dict[str, tuple[str, str, str]] | None = None

    @property
    def available(self) -> bool:
        return self.path is not None and self.path.exists()

    def get(self, npi: str) -> dict | None:
        if self._d is None:
            self._d = {}
            if self.available:
                with open(self.path, newline="", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        self._d[r["npi"]] = (r.get("entity_type") or "", r.get("taxonomy") or "", (r.get("zip") or "")[:5])
        v = self._d.get(npi)
        return None if v is None else {"entity_type": v[0] or None, "taxonomy": v[1] or None, "zip": v[2] or None}


# --------------------------------------------------------------------------- result helpers

def _r2(x):
    return None if x is None else round(float(x), 2)


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return statistics.quantiles(vals, n=100, method="inclusive")[int(q * 100) - 1]


def result(**kw) -> dict:
    out = {"allowed_amount": None, "status": "rate_unavailable", "reason_code": None, "exact_reason_code": None,
           "allowed_amount_basis": None, "source_status": None, "match_path": None, "match_keys": {},
           "estimate_scope": None, "selected_provider_npi": None, "exact_provider_rate_found": False,
           "fallback_provider_count": 0, "n_providers": None, "p10": None, "p90": None, "mrf_file_dates": [],
           "source_ids": [], "source_urls": [], "components": [], "fallback": None, "code_choice": None,
           "service_date": None, "service_date_validated": False, "warnings": [], "message": ""}
    out.update(kw)
    if out["status"] == "rate_unavailable":
        out["allowed_amount"] = None
    return out


MESSAGES = {
    "provider_rate": "Rate from this provider's own MRF contract rows (provider-specific); still an estimate input, "
                     "not a guaranteed claim allowed amount.",
    "nearby_provider_estimate": "Estimate from nearby in-network providers, NOT a quote for the selected provider.",
    "network_estimate": "Aggregate estimate across this network's providers, not a specific provider's quote.",
}


# --------------------------------------------------------------------------- lookup service

class RateLookup:
    def __init__(self, profiles: list[dict], components: list[dict], a_rows: list[dict], b_rows: list[dict],
                 provider_rates: ca.ProviderRates | None = None, npi_directory: NpiDirectory | None = None,
                 centroids: dict | None = None, as_of: dt.date | None = None,
                 max_snapshot_age_days: int = DEFAULT_MAX_SNAPSHOT_AGE_DAYS,
                 fallback: FallbackConfig = FallbackConfig(), log: TraceLog | None = None,
                 db_snapshot: dict | None = None):
        self.profiles = {p["plan_key"]: p for p in profiles}
        self.encounters: dict[str, dict] = {}
        for c in components:
            e = self.encounters.setdefault(c["encounter_key"], {"label": c["encounter_label"], "components": []})
            e["components"].append(c)
        self.a: dict[tuple, list[dict]] = {}
        self.a_label_errors: dict[tuple, str] = {}
        for r in a_rows:
            if r["plan_key"] is None:
                continue
            if r["encounter_key"] is None:
                self.a_label_errors[(r["plan_key"], r["network"], r["encounter_label"])] = r["errors"][-1]
                continue
            self.a.setdefault((r["plan_key"], r["network"], r["encounter_key"]), []).append(r)
        self.b: dict[tuple, list[dict]] = {}
        self.b_codes: set[tuple] = set()
        for r in b_rows:
            if r["plan_key"] is None:
                continue
            self.b.setdefault((r["plan_key"], r["network"], r["billing_code"], r["billing_class"],
                               r["place_of_service"]), []).append(r)
            self.b_codes.add((r["plan_key"], r["network"], r["billing_code"], r["billing_class"]))
        self.has_aggregates = bool(a_rows or b_rows)
        self.db = provider_rates
        self.npis = npi_directory or NpiDirectory(None)
        self.centroids = centroids or {}
        self.as_of = as_of or dt.date.today()
        self.max_age = max_snapshot_age_days
        self.cfg = fallback
        self.log = log or TraceLog(None)
        self.db_snapshot = db_snapshot or {}
        self._rate_rows = functools.lru_cache(maxsize=32)(self._rate_rows)

    # ---- construction from files
    @classmethod
    def from_paths(cls, paths, as_of: dt.date | None = None, log_path: Path | None = None,
                   fallback: FallbackConfig = FallbackConfig()) -> "RateLookup":
        with open(paths.plan_profiles, encoding="utf-8") as f:
            profiles = json.load(f)["plans"]
        comps = ca.canonical_encounter_components(paths.encounters_csv)
        a_rows, b_rows = ([], [])
        if paths.rates_csv.exists():
            a_rows, b_rows = ca.canonical_csv_views(paths.rates_csv, comps, profiles,
                                                     ca.load_code_types(paths.billing_codes))
        db, snap = None, {"rates_sqlite": str(paths.rates_sqlite), "exists": paths.rates_sqlite.exists()}
        if paths.rates_sqlite.exists():
            db = ca.ProviderRates(ca.connect_ro(paths.rates_sqlite))
            st = paths.rates_sqlite.stat()
            snap.update(size_bytes=st.st_size, mtime=dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
                        .isoformat(timespec="seconds"))
        log = TraceLog(log_path, context={"adapter_version": ca.ADAPTER_VERSION, "lookup_version": LOOKUP_VERSION,
                                          "db_snapshot": snap})
        return cls(profiles, comps, a_rows, b_rows, db, NpiDirectory(paths.npi_directory),
                   load_centroids(paths.zip_centroids), as_of=as_of, fallback=fallback, log=log, db_snapshot=snap)

    # ---- plan / network
    def resolve_plan(self, plan_key: str | None, network: str | None, insurer: str | None = None):
        """Exact plan + network from the plan profiles; never across networks."""
        p = self.profiles.get(plan_key or "")
        if p is None:
            return None, "network_not_resolved", f"Unknown plan_key {plan_key!r}."
        if insurer is not None and insurer != p["insurer"]:
            return None, "network_not_resolved", f"Insurer {insurer!r} does not match plan {plan_key!r}."
        names = p.get("network_names")
        if names:
            if network not in names:
                return None, "network_not_resolved", f"{network!r} is not an exact MRF network of {plan_key!r}."
            return PlanContext(plan_key, p["insurer"], network, None), None, ""
        if network is not None:
            return None, "network_not_resolved", f"{plan_key!r} was imported without a network filter; network must be null."
        return PlanContext(plan_key, p["insurer"], None, p.get("market") or "all contracts"), None, ""

    # ---- public entry point (rate_request.v1 -> rate_lookup_result.v1)
    def lookup(self, request: dict) -> dict:
        rid = self.log.new_request()
        try:
            out = self._lookup(request, rid)
        except Exception as e:  # noqa: BLE001 - logged with stack, then re-raised
            self.log.exception(rid, "lookup", e)
            raise
        out.update(request_id=rid, run_id=self.log.run_id, adapter_version=ca.ADAPTER_VERSION,
                   lookup_version=LOOKUP_VERSION, log_status=self.log.status())
        if not self.log.ok:
            out["warnings"].append("trace log write failed: " + self.log.status())
        return out

    def _lookup(self, req: dict, rid: str) -> dict:
        from schema_check import SchemaError, validate
        safe_req = {k: v for k, v in req.items() if k != "service_description"}
        self.log.event(rid, "request", "received", request=safe_req, db_snapshot=self.db_snapshot)
        try:
            validate(req, "rate_request")
        except SchemaError as e:
            self.log.event(rid, "request", "invalid", error=str(e))
            return result(reason_code="invalid_request", message=str(e))
        service_date = req.get("service_date") or self.as_of.isoformat()
        ctx, reason, msg = self.resolve_plan(req.get("plan_key"), req.get("network"), req.get("insurer"))
        self.log.event(rid, "plan_network", "resolved" if ctx else "failed", reason_code=reason,
                       plan_key=req.get("plan_key"), network=req.get("network"))
        if ctx is None:
            return result(reason_code=reason, message=msg, service_date=service_date)
        npi = req.get("selected_provider_npi")
        if req.get("encounter_key"):
            out = self.encounter(ctx, req["encounter_key"], npi, service_date, rid)
        else:
            comp = {"component": req["billing_code"], "billing_codes": (req["billing_code"],),
                    "billing_class": ca.billing_class(req.get("billing_class")),
                    "place_of_service": ca.pos_preference(req.get("place_of_service")),
                    "modifier_rule": req.get("modifier_rule"), "billing_code_type": req.get("billing_code_type")}
            if comp["billing_class"] is None:
                return result(reason_code="billing_class_missing", message="billing_class is required for B lookups.",
                              service_date=service_date)
            out = self.component(ctx, comp, npi, service_date, rid)
        self.log.event(rid, "final", out["status"], reason_code=out["reason_code"], allowed_amount=out["allowed_amount"],
                       basis=out["allowed_amount_basis"], match_path=out["match_path"],
                       fallback_provider_count=out["fallback_provider_count"], mrf_file_dates=out["mrf_file_dates"])
        return out

    # ---- A_encounter
    def encounter(self, ctx: PlanContext, key: str, npi: str | None, service_date: str, rid: str) -> dict:
        keys = {"plan_key": ctx.plan_key, "network": ctx.network, "encounter_key": key}
        base = {"match_path": "A_encounter", "match_keys": keys, "service_date": service_date,
                "selected_provider_npi": npi}
        enc = self.encounters.get(key)
        if enc is None:
            self.log.event(rid, "encounter_mapping", "failed", reason_code="encounter_key_unknown", encounter_key=key)
            return result(reason_code="encounter_key_unknown", message=f"Unknown encounter_key {key!r}.", **base)
        self.log.event(rid, "encounter_mapping", "resolved", encounter_key=key, n_components=len(enc["components"]))
        if npi is not None:
            parts = [self.component(ctx, c, npi, service_date, rid) for c in enc["components"]]
            return self._combine(enc["components"], parts, base | {"match_path": "A_encounter_by_components"},
                                 "Encounter total = sum of its components (all required).")
        if not self.has_aggregates:
            return result(reason_code="no_data_source", message="No aggregate rate table is loaded.", **base)
        rows = self.a.get((ctx.plan_key, ctx.network, key), [])
        if not rows and (ctx.plan_key, ctx.network, enc["label"]) in self.a_label_errors:
            reason = self.a_label_errors[(ctx.plan_key, ctx.network, enc["label"])]
            self.log.event(rid, "a_lookup", "failed", reason_code=reason)
            return result(reason_code=reason, message="The table A label does not map to exactly one encounter_key.", **base)
        out = self._from_aggregate(rows, "no_encounter_rate", base)
        out["components"] = [{"component": c["component"], "billing_codes": list(c["billing_codes"]),
                              "billing_class": c["billing_class"], "place_of_service": list(c["place_of_service"]),
                              "modifier_rule": c["modifier_rule"], "included_in_total": True} for c in enc["components"]]
        self.log.event(rid, "a_lookup", out["status"], reason_code=out["reason_code"], n_rows=len(rows))
        return out

    # ---- B_component (one component, any code alternative)
    def component(self, ctx: PlanContext, comp: dict, npi: str | None, service_date: str, rid: str) -> dict:
        keys = {"plan_key": ctx.plan_key, "network": ctx.network, "billing_code": "|".join(comp["billing_codes"]),
                "billing_class": comp["billing_class"], "place_of_service": "|".join(comp["place_of_service"])}
        base = {"match_path": "B_component", "match_keys": keys, "service_date": service_date,
                "selected_provider_npi": npi}
        if npi is not None:
            return self._provider_component(ctx, comp, npi, service_date, base, rid)
        if not self.has_aggregates:
            return result(reason_code="no_data_source", message="No aggregate rate table is loaded.", **base)
        if len(comp["billing_codes"]) != 1:
            return result(reason_code="code_alternatives_need_provider",
                          message="Aggregate table B rows are per code; choose one code.", **base)
        code = comp["billing_codes"][0]
        rows = self.b.get((ctx.plan_key, ctx.network, code, comp["billing_class"], comp["place_of_service"]), [])
        if not rows:
            reason = "no_code_rate"
            if not comp["place_of_service"] and (ctx.plan_key, ctx.network, code, comp["billing_class"]) in self.b_codes:
                reason = "pos_missing"
            self.log.event(rid, "b_lookup", "failed", reason_code=reason, **keys)
            return result(reason_code=reason, message=f"No table B row for {code} {comp['billing_class']} "
                                                      f"POS {keys['place_of_service'] or 'none'} in this network.", **base)
        rule = comp.get("modifier_rule")
        if rule is not None and any(r["modifier_rule"] != rule for r in rows):
            self.log.event(rid, "b_lookup", "failed", reason_code="modifier_rule_mismatch", requested=rule,
                           derived=rows[0]["modifier_rule"])
            return result(reason_code="modifier_rule_mismatch",
                          message=f"Table B row uses modifier rule {rows[0]['modifier_rule']!r}, not {rule!r}.", **base)
        out = self._from_aggregate(rows, "no_code_rate", base)
        out["match_keys"]["billing_code_type"] = rows[0]["billing_code_type"] if len(rows) == 1 else None
        out["match_keys"]["modifier_rule"] = rows[0]["modifier_rule"]
        self.log.event(rid, "b_lookup", out["status"], reason_code=out["reason_code"], n_rows=len(rows), **keys)
        return out

    def _from_aggregate(self, rows: list[dict], none_reason: str, base: dict) -> dict:
        if not rows:
            return result(reason_code=none_reason, message="No aggregate row for these keys.", **base)
        if len(rows) > 1:
            return result(reason_code="ambiguous_rate_rows", message=f"{len(rows)} rows match; refusing to pick one.",
                          **base)
        r = rows[0]
        common = {"mrf_file_dates": r["mrf_file_dates"], "source_status": r["status"],
                  "allowed_amount_basis": r["allowed_amount_basis"], "n_providers": r["n_providers"],
                  "p10": r["p10"], "p90": r["p90"], "estimate_scope": "network_aggregate", **base}
        if r["status"] not in ("verified_rate", "estimated_rate"):
            return result(reason_code="source_rate_unavailable", message="MRF rate unavailable for these keys.", **common)
        if "allowed_amount" in r["placeholder_values"]:
            return result(reason_code="placeholder_rate", message="The aggregate is a placeholder value published in the "
                          "MRF (99999999999999), not a price.", **common)
        if r["allowed_amount"] is None:
            return result(reason_code="invalid_rate", message=f"Rate {r['allowed_amount_raw']!r} is not usable.", **common)
        if self._snapshot(r["mrf_file_dates"]) is None:
            return result(reason_code="stale_snapshot", message="MRF snapshot date missing or too old.", **common)
        warnings = ["Aggregate rows carry no per-rate expiration dates: the service date was not checked against "
                    "rate validity; this is the snapshot's aggregate."]
        if r["placeholder_values"]:
            warnings.append(f"Placeholder values in {', '.join(r['placeholder_values'])} were dropped (spread unknown).")
        return result(status="network_estimate", allowed_amount=r["allowed_amount"], message=MESSAGES["network_estimate"],
                      warnings=warnings, **common)

    def _snapshot(self, dates: list[str]) -> str | None:
        if not dates:
            return None
        lo, hi = dt.date.fromisoformat(min(dates)), dt.date.fromisoformat(max(dates))
        if (self.as_of - lo).days > self.max_age or hi > self.as_of:
            return None
        return hi.isoformat()

    # ---- provider path
    def _rate_rows(self, plan_key: str, code: str) -> tuple[dict, ...]:
        return tuple(self.db.rate_rows(plan_key, code))

    def hard_filter(self, rows, comp: dict, service_date: str) -> tuple[list[dict], list[dict]]:
        """Apply the rate-level hard rules in order; returns (survivors, [{stage, remaining, excluded}]).
        These are exclusions, never score penalties. Network and code are fixed by the query itself."""
        rule = comp.get("modifier_rule") or "global"
        allowed_mods = {"global": {()}, "mod_26": {("26",)}, "global_or_tc_plus_26": {(), ("TC",), ("26",)}}.get(rule)
        if allowed_mods is None:
            raise ValueError(f"unknown modifier_rule {rule!r}")
        stages = [
            ("billing_class", lambda r: r["billing_class"] in (comp["billing_class"], "both")),
            ("fresh_snapshot", lambda r: self._snapshot([r["mrf_file_date"]] if r["mrf_file_date"] else []) is not None),
            ("dollar_rate_type", lambda r: r["source_rate_type"] in DOLLAR_TYPES),
            ("positive_amount", lambda r: r["source_rate_amount"] is not None and r["source_rate_amount"] > 0),
            ("not_placeholder", lambda r: r["source_rate_amount"] < ca.PLACEHOLDER_MIN),
            ("valid_on_service_date", lambda r: r["expiration_date"] is None or r["expiration_date"] >= service_date),
            ("pos_applicable", lambda r: ca.pos_applies(r["place_of_service"], comp["place_of_service"]) is not None),
            ("modifier_rule", lambda r: r["modifiers"] in allowed_mods),
        ]
        trail, cur = [], list(rows)
        for name, ok in stages:
            nxt = [r for r in cur if ok(r)]
            trail.append({"stage": name, "remaining": len(nxt), "excluded": len(cur) - len(nxt)})
            cur = nxt
        return cur, trail

    @staticmethod
    def _zero_reason(trail: list[dict], n_rows: int) -> str:
        if n_rows == 0:
            return "no_code_rate"
        zeroed = next((t["stage"] for t in trail if t["remaining"] == 0), None)
        return {"billing_class": "no_code_rate", "fresh_snapshot": "stale_snapshot", "dollar_rate_type": "no_dollar_rate",
                "positive_amount": "no_dollar_rate", "not_placeholder": "placeholder_rate",
                "valid_on_service_date": "expired_only",
                "pos_applicable": "pos_conflict", "modifier_rule": "modifier_rule_unmet"}.get(zeroed, "modifier_rule_unmet")

    @staticmethod
    def representative(rate_rows: list[dict], comp: dict, service_date: str) -> float | None:
        """One provider's amount from the rate rows of all its groups/TINs, via scripts/mrf/allowed.per_provider
        (modifier rule, POS preference, latest expiration, median of remaining values)."""
        import allowed  # scripts/mrf/allowed.py, read-only
        cands = [allowed.Candidate("x", "B", False, r["source_rate_amount"], r["source_rate_type"],
                                   r["expiration_date"] or "9999-12-31", tuple(sorted(r["place_of_service"])),
                                   r["modifiers"], r["mrf_file_date"] or "", r["source_url"]) for r in rate_rows]
        values, _, _ = allowed.per_provider(cands, "|".join(comp["place_of_service"]) or None,
                                            comp.get("modifier_rule") or "global", service_date)
        return round(values["x"][0], 2) if "x" in values else None

    def _provider_component(self, ctx, comp, npi, service_date, base, rid) -> dict:
        base = base | {"match_path": "B_component_provider"}
        if self.db is None:
            self.log.event(rid, "npi_exact", "failed", reason_code="no_data_source")
            return result(reason_code="no_data_source", message="Provider-specific rates need the MRF SQLite "
                                                                "database, which is not available.", **base)
        memberships = self.db.npi_memberships(ctx.plan_key, npi)
        groups = {(m["source_id"], m["provider_group_id"]) for m in memberships}
        exact_reason, tried = None, []
        for code in comp["billing_codes"]:
            c1 = dict(comp, billing_codes=(code,))
            mine = [r for r in self._rate_rows(ctx.plan_key, code) if (r["source_id"], r["provider_group_id"]) in groups]
            survivors, trail = self.hard_filter(mine, c1, service_date)
            value = self.representative(survivors, c1, service_date) if survivors else None
            reason = None if value is not None else ("no_provider_npi_rows" if not memberships else
                                                     self._zero_reason(trail, len(mine)) if not survivors else
                                                     "modifier_rule_unmet")
            tried.append({"billing_code": code, "npi_rate_rows": len(mine), "trail": trail, "reason_code": reason})
            self.log.event(rid, "npi_exact", "found" if reason is None else "not_found", billing_code=code, npi=npi,
                           npi_memberships=len(memberships), npi_rate_rows=len(mine), trail=trail, reason_code=reason)
            if reason is None:
                choice = {"billing_code": code, "reason": "first code in the mapping's order with an exact provider rate",
                          "tried": tried} if len(comp["billing_codes"]) > 1 else None
                return result(status="provider_rate", allowed_amount=value, allowed_amount_basis="provider_rate",
                              estimate_scope="provider_specific", exact_provider_rate_found=True, n_providers=1,
                              mrf_file_dates=sorted({r["mrf_file_date"] for r in survivors if r["mrf_file_date"]}),
                              source_ids=sorted({r["source_id"] for r in survivors}),
                              source_urls=sorted({r["source_url"] for r in survivors}),
                              match_keys=base["match_keys"] | {"billing_code": code, "modifier_rule": c1.get("modifier_rule")},
                              code_choice=choice, service_date_validated=True, message=MESSAGES["provider_rate"],
                              trace=sorted([r["source_id"], r["provider_group_id"], r["rate_id"]] for r in survivors),
                              **{k: v for k, v in base.items() if k != "match_keys"})
            exact_reason = exact_reason or reason
        return self._nearby(ctx, comp, npi, service_date, base, rid, exact_reason, tried)

    # ---- nearby Top-K fallback
    def _anchor(self, npi: str, ctx: PlanContext) -> dict | None:
        d = self.npis.get(npi)
        if d and d["zip"]:
            return d | {"location_source": "nppes_directory"}
        if self.db is not None:
            for m in self.db.npi_memberships(ctx.plan_key, npi):
                if m["provider_zip"]:
                    return {"zip": m["provider_zip"], "taxonomy": m["taxonomy"], "entity_type": None,
                            "location_source": "mrf_members"}
        return None

    def _nearby(self, ctx, comp, npi, service_date, base, rid, exact_reason, tried) -> dict:
        base = base | {"match_path": "B_component_nearby", "exact_reason_code": exact_reason}
        cfg = self.cfg
        anchor = self._anchor(npi, ctx)
        point = self.centroids.get(anchor["zip"]) if anchor else None
        if point is None:
            self.log.event(rid, "nearby_anchor", "failed", reason_code="provider_location_unknown", npi=npi,
                           anchor_zip=anchor["zip"] if anchor else None)
            return result(reason_code="provider_location_unknown", message="The selected provider has no exact rate "
                          "and its location is unknown, so no nearby estimate can be made.", **base)
        self.log.event(rid, "nearby_anchor", "resolved", npi=npi, anchor_zip=anchor["zip"],
                       location_source=anchor["location_source"])
        distance = {z: haversine_miles(point, pt) for z, pt in self.centroids.items()}
        for code in comp["billing_codes"]:
            c1 = dict(comp, billing_codes=(code,))
            survivors, trail = self.hard_filter(self._rate_rows(ctx.plan_key, code), c1, service_date)
            by_group: dict[tuple, list[dict]] = {}
            for r in survivors:
                by_group.setdefault((r["source_id"], r["provider_group_id"]), []).append(r)
            # expand surviving groups to member NPIs; location is a hard rule (known ZIP within the radius)
            npis: dict[str, dict] = {}
            seen, no_loc, far = set(), set(), set()
            for key in by_group:
                for m in self.db.group_members(*key):
                    m_npi, z = m[2], (m[4] or "")[:5]
                    if m_npi == npi:
                        continue
                    seen.add(m_npi)
                    d = distance.get(z)
                    if d is None:
                        no_loc.add(m_npi)
                        continue
                    if d > cfg.radius_miles:
                        far.add(m_npi)
                        continue
                    e = npis.setdefault(m_npi, {"groups": set(), "zip": z, "distance": d, "tins": set(), "taxonomy": m[5]})
                    e["groups"].add(key)
                    if m[3]:
                        e["tins"].add(m[3])
            n_loc = len(seen - no_loc)
            trail.append({"stage": "member_npis", "remaining": len(seen), "excluded": None})
            trail.append({"stage": "location_known", "remaining": n_loc, "excluded": len(seen) - n_loc})
            trail.append({"stage": f"within_{cfg.radius_miles:g}_miles", "remaining": len(npis),
                          "excluded": n_loc - len(npis)})
            values: dict[frozenset, float | None] = {}  # NPIs with the same groups share one representative amount
            scored = []
            for p_npi, e in npis.items():
                gkey = frozenset(e["groups"])
                if gkey not in values:
                    values[gkey] = self.representative([r for g in gkey for r in by_group[g]], c1, service_date)
                if values[gkey] is not None:
                    scored.append(self._score(p_npi, e, values[gkey], [r for g in gkey for r in by_group[g]], c1, anchor))
            trail.append({"stage": "representative_amount", "remaining": len(scored), "excluded": len(npis) - len(scored)})
            scored.sort(key=lambda s: (-s["score"], s["distance_miles"], s["npi"]))
            top, per_tin, capped = [], {}, 0
            for s in scored:
                if len(top) == cfg.top_k:
                    break
                if per_tin.get(s["tin"], 0) >= cfg.max_per_tin:
                    capped += 1
                    continue
                per_tin[s["tin"]] = per_tin.get(s["tin"], 0) + 1
                top.append(s)
            trail.append({"stage": f"top_{cfg.top_k}_tin_cap_{cfg.max_per_tin}", "remaining": len(top),
                          "excluded": capped})
            zero_at = next((t["stage"] for t in trail if t["remaining"] == 0), None)
            self.log.event(rid, "nearby_filter", "ok" if top else "empty", billing_code=code, trail=trail,
                           zeroed_at=zero_at)
            self.log.event(rid, "nearby_topk", "scored", billing_code=code,
                           providers=[{k: s[k] for k in ("npi", "score", "subscores", "distance_miles", "value")}
                                      for s in top])
            tried.append({"billing_code": code, "fallback_trail": trail})
            if not top:
                continue
            vals = sorted(s["value"] for s in top)
            med, p10, p90 = statistics.median(vals), _pct(vals, 0.10), _pct(vals, 0.90)
            warnings = []
            if len(top) < LOW_SAMPLE:
                warnings.append(f"Low sample: only {len(top)} nearby providers qualified.")
            self.log.event(rid, "nearby_aggregate", "ok", n=len(top), median=_r2(med), p10=_r2(p10), p90=_r2(p90))
            rows_used = [r for s in top for g in npis[s["npi"]]["groups"] for r in by_group[g]]
            return result(
                status="nearby_provider_estimate", allowed_amount=_r2(med), allowed_amount_basis="nearby_provider_median",
                estimate_scope="nearby_provider_cohort", fallback_provider_count=len(top), n_providers=len(top),
                p10=_r2(p10), p90=_r2(p90),
                mrf_file_dates=sorted({r["mrf_file_date"] for r in rows_used if r["mrf_file_date"]}),
                source_ids=sorted({r["source_id"] for r in rows_used}),
                source_urls=sorted({r["source_url"] for r in rows_used}),
                code_choice={"billing_code": code, "reason": "first code in the mapping's order with eligible nearby "
                             "providers", "tried": tried} if len(comp["billing_codes"]) > 1 else None,
                match_keys=base["match_keys"] | {"billing_code": code, "modifier_rule": c1.get("modifier_rule")},
                fallback={"anchor_zip": anchor["zip"], "anchor_location_source": anchor["location_source"],
                          "radius_miles": cfg.radius_miles, "top_k": cfg.top_k, "max_per_tin": cfg.max_per_tin,
                          "distance_algorithm": "haversine between Census ZCTA internal points (approximate)",
                          "weights": dict(cfg.weights), "score_min": min(s["score"] for s in top),
                          "score_max": max(s["score"] for s in top), "aggregation": "unweighted median of one "
                          "representative amount per provider", "trail": trail,
                          "providers": [{k: s[k] for k in ("npi", "score", "distance_miles", "value")} for s in top]},
                service_date_validated=True, warnings=warnings,
                message=MESSAGES["nearby_provider_estimate"] + (" " + warnings[0] if warnings else ""),
                **{k: v for k, v in base.items() if k != "match_keys"})
        return result(reason_code="no_eligible_fallback_provider", message="No exact rate for the selected provider "
                      "and no nearby in-network provider passes every hard rule.", fallback={"trail": tried[-1]},
                      **base)

    def _score(self, npi: str, e: dict, value: float, rows: list[dict], comp: dict, anchor: dict) -> dict:
        wanted = comp["place_of_service"]
        pos_kinds = {ca.pos_applies(r["place_of_service"], wanted) for r in rows}
        if "no_pos_requested" in pos_kinds or (wanted and f"exact:{wanted[0]}" in pos_kinds):
            pos = 1.0
        elif any(k and k.startswith("exact:") for k in pos_kinds):
            pos = 0.8
        else:
            pos = 0.6  # all-POS / no POS listed rate
        if anchor.get("taxonomy") is None:
            taxonomy = 0.5
        elif e["taxonomy"] == anchor["taxonomy"]:
            taxonomy = 1.0
        else:
            d = self.npis.get(npi) if self.npis.available else None
            taxonomy = 0.6 if d and anchor.get("entity_type") and d["entity_type"] == anchor["entity_type"] else 0.3
        newest = max((r["mrf_file_date"] for r in rows if r["mrf_file_date"]), default=None)
        age = (self.as_of - dt.date.fromisoformat(newest)).days if newest else self.max_age
        freshness = max(0.0, min(1.0, 1 - age / self.max_age))
        quality = max(SOURCE_QUALITY.get(r["source_rate_type"], 0.0) for r in rows)
        sub = {"distance": round(1 - e["distance"] / self.cfg.radius_miles, 4), "pos": pos, "taxonomy": taxonomy,
               "freshness": round(freshness, 4), "source_quality": quality}
        score = round(sum(w * sub[k] for k, w in self.cfg.weights), 4)
        tins = sorted(e["tins"])
        return {"npi": npi, "score": score, "subscores": sub, "distance_miles": round(e["distance"], 2),
                "value": value, "tin": tins[0] if tins else f"npi:{npi}"}

    # ---- combining components
    def _combine(self, defs: list[dict], parts: list[dict], base: dict, note: str) -> dict:
        failed = next((p for p in parts if p["status"] == "rate_unavailable"), None)
        comps = [{"component": c.get("component"), **{k: p[k] for k in (
            "status", "reason_code", "exact_reason_code", "allowed_amount", "allowed_amount_basis", "match_keys",
            "fallback_provider_count", "mrf_file_dates", "source_ids")}} for c, p in zip(defs, parts)]
        if failed is not None:
            return result(reason_code=failed["reason_code"], exact_reason_code=failed.get("exact_reason_code"),
                          components=comps, message="A required component has no rate; no partial total is returned.",
                          **base)
        status = "provider_rate" if all(p["status"] == "provider_rate" for p in parts) else "nearby_provider_estimate"
        total = round(sum(p["allowed_amount"] for p in parts), 2)
        warnings = [w for p in parts for w in p["warnings"]]
        if status == "nearby_provider_estimate":
            warnings.append("Some components are nearby-provider estimates, not the selected provider's rates.")
        return result(status=status, allowed_amount=total,
                      allowed_amount_basis="; ".join(f"{c['component']}={c['allowed_amount_basis']}" for c in comps),
                      estimate_scope="provider_specific" if status == "provider_rate" else "mixed_provider_and_nearby",
                      exact_provider_rate_found=all(p["exact_provider_rate_found"] for p in parts),
                      fallback_provider_count=max(p["fallback_provider_count"] for p in parts),
                      mrf_file_dates=sorted({d for p in parts for d in p["mrf_file_dates"]}),
                      source_ids=sorted({s for p in parts for s in p["source_ids"]}),
                      source_urls=sorted({s for p in parts for s in p["source_urls"]}),
                      components=comps, service_date_validated=True, warnings=warnings,
                      message=note + " " + MESSAGES[status], **base)
