"""Read-only adapter: existing MRF database and report files -> canonical pipeline fields.

Nothing here writes to the database or the CSVs. Source column names are resolved through explicit
alias lists (FIELD_MAP); every canonical field records its source table, source column and transform,
and every canonical row keeps the original values under "raw". Fields with different meanings are never
merged because their names look alike:

  source_rate_amount  <- negotiated_rates.negotiated_rate (one raw contract rate)
  allowed_amount      <- allowed_amounts.csv allowed_amount (the pipeline's selected/aggregated price)
  modifiers           <- negotiated_rates.modifiers (modifiers the raw rate applies to, e.g. TC/26)
  modifier_rule       <- encounters.csv modifier_rule (how a component is priced: global, mod_26, ...)
  expiration_date     <- negotiated_rates.expiration_date (rate validity)
  mrf_file_date       <- mrf_sources.file_date (snapshot of the file; never a validity date)

Canonical views:
  canonical_provider_rates        one row per (raw rate x provider NPI) with trace ids
  canonical_A_encounters          one row per insurer/network/encounter aggregate (table A of the CSV)
  canonical_B_components          one row per insurer/network/code/class/POS aggregate (table B of the CSV)
  canonical_encounter_components  encounter -> component code/class/POS/modifier_rule (encounters.csv)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

ADAPTER_VERSION = "canonical_adapter.v1"
ALL_POS = "CSTM-00"  # CMS value: the same rate applies to every place of service
# Some MRFs publish 99999999999999 as a placeholder "rate" (seen in 992 L.A. Care rows, including dollar
# types). No single CPT/HCPCS rate reaches $1 billion, so anything at or above this is not a price.
PLACEHOLDER_MIN = 1e9

BILLING_CLASS = {"professional": "professional", "prof": "professional", "p": "professional",
                 "institutional": "institutional", "inst": "institutional", "i": "institutional",
                 "facility": "institutional", "both": "both"}


class AdapterError(Exception):
    """A required table or column is missing, or an alias is ambiguous."""


@dataclass(frozen=True)
class FieldMap:
    canonical: str
    view: str
    source: str            # "sqlite:<table>", "csv:<file>[table]", "json:<file>"
    aliases: tuple          # accepted source column names, in priority order
    transform: str
    required: bool = True


FIELD_MAP: tuple[FieldMap, ...] = (
    # ---- canonical_provider_rates (SQLite)
    FieldMap("source_id", "canonical_provider_rates", "sqlite:mrf_sources", ("source_id",), "identity"),
    FieldMap("source_url", "canonical_provider_rates", "sqlite:mrf_sources", ("source_url",), "identity"),
    FieldMap("insurer", "canonical_provider_rates", "sqlite:mrf_sources", ("insurer", "issuer_name"), "identity"),
    FieldMap("plan_key", "canonical_provider_rates", "sqlite:mrf_sources", ("plan_key",),
             "identity (system plan key; never derived from the insurer string)"),
    FieldMap("network", "canonical_provider_rates", "sqlite:mrf_sources", ("network", "network_name"),
             "kept raw as network_raw; canonical network = the plan profile's exact network name"),
    FieldMap("mrf_file_date", "canonical_provider_rates", "sqlite:mrf_sources", ("file_date", "mrf_file_date"),
             "ISO date of the MRF file (snapshot), not a rate validity date"),
    FieldMap("source_status", "canonical_provider_rates", "sqlite:mrf_sources", ("status",),
             "only 'complete' sources are read"),
    FieldMap("rate_id", "canonical_provider_rates", "sqlite:negotiated_rates", ("rate_id",), "identity"),
    FieldMap("billing_code", "canonical_provider_rates", "sqlite:negotiated_rates", ("billing_code", "code"), "identity"),
    FieldMap("billing_code_type", "canonical_provider_rates", "sqlite:negotiated_rates",
             ("billing_code_type", "code_type"), "identity; never inferred from the code's digits"),
    FieldMap("billing_class", "canonical_provider_rates", "sqlite:negotiated_rates", ("billing_class",),
             "enum via BILLING_CLASS (prof->professional, inst/facility->institutional, both)"),
    FieldMap("place_of_service", "canonical_provider_rates", "sqlite:negotiated_rates",
             ("service_codes", "place_of_service", "pos"),
             "comma list -> set of POS codes; empty = none listed; CSTM-00 = all POS"),
    FieldMap("modifiers", "canonical_provider_rates", "sqlite:negotiated_rates", ("modifiers",),
             "comma list -> sorted tuple; empty = global (unmodified) rate"),
    FieldMap("source_rate_amount", "canonical_provider_rates", "sqlite:negotiated_rates", ("negotiated_rate",),
             "float; a raw contract rate, NOT an allowed_amount"),
    FieldMap("source_rate_type", "canonical_provider_rates", "sqlite:negotiated_rates", ("negotiated_type",),
             "lower-case"),
    FieldMap("expiration_date", "canonical_provider_rates", "sqlite:negotiated_rates", ("expiration_date",),
             "ISO date; missing or 9999-12-31 = open-ended"),
    FieldMap("provider_group_id", "canonical_provider_rates", "sqlite:negotiated_rates", ("provider_group_id",),
             "join key to provider_members (with source_id)"),
    FieldMap("provider_npi", "canonical_provider_rates", "sqlite:provider_members", ("npi", "NPI", "provider_npi"),
             "10-digit provider NPI (a provider identifier, never a plan id)"),
    FieldMap("tin_sha256", "canonical_provider_rates", "sqlite:provider_members", ("tin_sha256",),
             "hashed TIN; raw TINs are never read"),
    FieldMap("provider_zip", "canonical_provider_rates", "sqlite:provider_members", ("zip", "provider_zip"),
             "5-digit ZIP of the NPPES practice location"),
    FieldMap("taxonomy", "canonical_provider_rates", "sqlite:provider_members", ("taxonomy",), "NPPES primary taxonomy"),
    FieldMap("tier", "canonical_provider_rates", "sqlite:provider_members", ("tier",),
             "A = Westwood ZIPs, B = LA County", required=False),
    FieldMap("ucla_health", "canonical_provider_rates", "sqlite:provider_members", ("ucla_health",),
             "UCLA organization NPI flag", required=False),
    # ---- canonical_A_encounters / canonical_B_components (allowed_amounts.csv)
    FieldMap("table_type", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("table",),
             "A_encounter / B_component"),
    FieldMap("insurer", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("insurer",),
             "identity"),
    FieldMap("network", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("network",),
             "report label kept as network_label; plan_key and exact network resolved through plan_profiles"),
    FieldMap("encounter_key", "canonical_A_encounters", "csv:allowed_amounts.csv", ("encounter_key", "encounter"),
             "CSV holds the display label: unique label -> key via encounters.csv; missing/duplicate = error"),
    FieldMap("allowed_amount", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv",
             ("allowed_amount",), "float > 0 or None (pipeline-selected price, not a raw negotiated rate)"),
    FieldMap("allowed_amount_basis", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv",
             ("allowed_amount_basis",), "identity"),
    FieldMap("status", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("status",),
             "pipeline label verified_rate/estimated_rate/rate_unavailable (not a claim guarantee)"),
    FieldMap("components", "canonical_A_encounters", "csv:allowed_amounts.csv", ("components",),
             "display/trace only; never a join key"),
    FieldMap("n_providers", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv",
             ("n_providers",), "int (a count, never a provider identity)"),
    FieldMap("p10", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("p10",), "float"),
    FieldMap("p90", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv", ("p90",), "float"),
    FieldMap("mrf_file_date", "canonical_A_encounters|canonical_B_components", "csv:allowed_amounts.csv",
             ("mrf_file_date",), "comma list -> sorted list of ISO dates"),
    FieldMap("billing_code", "canonical_B_components", "csv:allowed_amounts.csv", ("billing_code",), "identity"),
    FieldMap("billing_code_type", "canonical_B_components", "csv:billing_codes.csv", ("code_type",),
             "looked up in the billing code catalog; None if the code is not listed"),
    FieldMap("billing_class", "canonical_B_components", "csv:allowed_amounts.csv", ("billing_class",),
             "enum via BILLING_CLASS"),
    FieldMap("place_of_service", "canonical_B_components", "csv:allowed_amounts.csv", ("place_of_service",),
             "'|' list -> ordered preference tuple; empty = none listed"),
    FieldMap("modifier_rule", "canonical_B_components", "csv:encounters.csv", ("modifier_rule",),
             "not stored in the CSV: re-derived with report.component_combos' rule (encounter component's rule, "
             "else global_or_tc_plus_26 for 7xxxx imaging, else global)"),
    FieldMap("negotiated_type_mix", "canonical_B_components", "csv:allowed_amounts.csv", ("negotiated_type_mix",),
             "identity", required=False),
    FieldMap("n_rates_excluded", "canonical_B_components", "csv:allowed_amounts.csv", ("n_rates_excluded",), "int",
             required=False),
    # ---- canonical_encounter_components (encounters.csv)
    FieldMap("encounter_key", "canonical_encounter_components", "csv:encounters.csv", ("encounter",), "identity"),
    FieldMap("encounter_label", "canonical_encounter_components", "csv:encounters.csv", ("label",), "identity"),
    FieldMap("billing_codes", "canonical_encounter_components", "csv:encounters.csv", ("codes",),
             "'|' list -> ordered alternatives"),
    FieldMap("billing_class", "canonical_encounter_components", "csv:encounters.csv", ("billing_class",),
             "enum via BILLING_CLASS"),
    FieldMap("place_of_service", "canonical_encounter_components", "csv:encounters.csv", ("place_of_service",),
             "'|' list -> ordered preference tuple"),
    FieldMap("modifier_rule", "canonical_encounter_components", "csv:encounters.csv", ("modifier_rule",), "identity"),
)


# --------------------------------------------------------------------------- value transforms

def billing_class(value) -> str | None:
    return BILLING_CLASS.get(str(value or "").strip().lower())


def pos_set(text) -> frozenset[str]:
    """Raw MRF service_codes (comma list) -> set. Empty set = no POS listed."""
    return frozenset(p.strip() for p in str(text or "").split(",") if p.strip())


def pos_preference(text) -> tuple[str, ...]:
    """encounters.csv / report POS ("20|11") -> ordered preference tuple. Empty tuple = none listed."""
    return tuple(p.strip() for p in str(text or "").split("|") if p.strip())


def pos_applies(listed: frozenset[str], wanted: tuple[str, ...]) -> str | None:
    """How a raw rate's POS list applies to a requested POS preference.
    'exact:<pos>' if it lists a requested POS, 'all_pos' if it lists none or CSTM-00, None if it conflicts.
    With no POS requested (e.g. an institutional component), every rate applies ('no_pos_requested')."""
    if not wanted:
        return "no_pos_requested"
    for p in wanted:
        if p in listed:
            return f"exact:{p}"
    if not listed or ALL_POS in listed:
        return "all_pos"
    return None


def modifiers(text) -> tuple[str, ...]:
    return tuple(sorted(m.strip() for m in str(text or "").split(",") if m.strip()))


def amount(text) -> float | None:
    """A usable price: finite, above zero and below the placeholder threshold."""
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and 0 < v < PLACEHOLDER_MIN else None


def is_placeholder(text) -> bool:
    try:
        return float(text) >= PLACEHOLDER_MIN
    except (TypeError, ValueError):
        return False


def iso_date(text) -> str | None:
    try:
        return dt.date.fromisoformat(str(text).strip()).isoformat()
    except (TypeError, ValueError):
        return None


def date_list(text) -> list[str]:
    return sorted({d for d in (iso_date(t) for t in str(text or "").split(",")) if d})


def _int(text) -> int | None:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- SQLite schema discovery

def connect_ro(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if not path.exists():
        raise AdapterError(f"database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_columns(con: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")]
    return {t: [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')] for t in tables}


def resolve_column(columns: list[str], aliases: tuple) -> str | None:
    """First alias present (case-insensitive). Two different aliases both present -> ambiguous."""
    lower = {c.lower(): c for c in columns}
    found = [lower[a.lower()] for a in aliases if a.lower() in lower]
    found = list(dict.fromkeys(found))
    if len(found) > 1:
        raise AdapterError(f"ambiguous columns {found} for aliases {aliases}")
    return found[0] if found else None


@dataclass(frozen=True)
class ColumnBinding:
    canonical: str
    table: str
    column: str | None
    transform: str
    required: bool


def bind_sqlite(con: sqlite3.Connection) -> dict[str, ColumnBinding]:
    """Resolve every canonical_provider_rates field to an actual column. Raises on missing required ones."""
    cols = table_columns(con)
    out, missing = {}, []
    for f in FIELD_MAP:
        if f.view != "canonical_provider_rates":
            continue
        table = f.source.split(":", 1)[1]
        col = resolve_column(cols.get(table, []), f.aliases) if table in cols else None
        if col is None and f.required:
            missing.append(f"{table}.{'|'.join(f.aliases)} ({f.canonical})")
        out[f.canonical] = ColumnBinding(f.canonical, table, col, f.transform, f.required)
    if missing:
        raise AdapterError("missing required tables/columns: " + "; ".join(missing))
    return out


def mapping_report(bindings: dict[str, ColumnBinding] | None = None) -> list[dict]:
    """Field-by-field trace: canonical name, view, source, resolved column, transform."""
    rows = []
    for f in FIELD_MAP:
        b = bindings.get(f.canonical) if bindings and f.view == "canonical_provider_rates" else None
        rows.append({"canonical": f.canonical, "view": f.view, "source": f.source, "aliases": list(f.aliases),
                     "resolved_column": b.column if b else None, "transform": f.transform, "required": f.required})
    return rows


# --------------------------------------------------------------------------- canonical_provider_rates

class ProviderRates:
    """canonical_provider_rates over the existing SQLite schema (read-only, aliases resolved once)."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.b = bind_sqlite(con)

    def _c(self, name: str, alias: str) -> str:
        col = self.b[name].column
        return f'{alias}."{col}"' if col else "NULL"

    RATE_KEYS = ("source_id", "source_url", "insurer", "plan_key", "network", "file_date", "rate_id", "billing_code",
                 "billing_code_type", "billing_class", "service_codes", "modifiers", "negotiated_rate",
                 "negotiated_type", "expiration_date", "provider_group_id")
    MEMBER_KEYS = ("source_id", "provider_group_id", "npi", "tin_sha256", "zip", "taxonomy", "tier", "ucla_health")

    def _sources_sql(self) -> str:
        c = self._c
        return (f"SELECT {c('source_id','s')} FROM mrf_sources s WHERE {c('plan_key','s')} = ? "
                f"AND {c('source_status','s')} = 'complete'")

    def rate_rows(self, plan_key: str, billing_code: str) -> list[dict]:
        """Rate-level rows of canonical_provider_rates (before expanding provider groups to NPIs)."""
        c = self._c
        sql = (f"SELECT {c('source_id','s')}, {c('source_url','s')}, {c('insurer','s')}, {c('plan_key','s')}, "
               f"{c('network','s')}, {c('mrf_file_date','s')}, {c('rate_id','r')}, {c('billing_code','r')}, "
               f"{c('billing_code_type','r')}, {c('billing_class','r')}, {c('place_of_service','r')}, "
               f"{c('modifiers','r')}, {c('source_rate_amount','r')}, {c('source_rate_type','r')}, "
               f"{c('expiration_date','r')}, {c('provider_group_id','r')} "
               f"FROM negotiated_rates r JOIN mrf_sources s ON {c('source_id','s')} = r.\"source_id\" "
               f"AND {c('source_status','s')} = 'complete' "
               f"WHERE {c('plan_key','s')} = ? AND {c('billing_code','r')} = ?")
        return [self.normalize_rate(dict(zip(self.RATE_KEYS, r))) for r in self.con.execute(sql, (plan_key, billing_code))]

    def _member_select(self) -> str:
        c = self._c
        return (f"SELECT m.\"source_id\", {c('provider_group_id','m')}, {c('provider_npi','m')}, {c('tin_sha256','m')}, "
                f"{c('provider_zip','m')}, {c('taxonomy','m')}, {c('tier','m')}, {c('ucla_health','m')} "
                f"FROM provider_members m ")

    def group_members(self, source_id, provider_group_id) -> list[tuple]:
        """Raw member tuples (MEMBER_KEYS order) of one provider group; normalize with normalize_member."""
        sql = self._member_select() + f"WHERE m.\"source_id\" = ? AND {self._c('provider_group_id','m')} = ?"
        return self.con.execute(sql, (source_id, provider_group_id)).fetchall()

    def npi_memberships(self, plan_key: str, npi: str) -> list[dict]:
        sql = (self._member_select() + f"WHERE {self._c('provider_npi','m')} = ? "
               f"AND m.\"source_id\" IN ({self._sources_sql()})")
        return [self.normalize_member(r) for r in self.con.execute(sql, (npi, plan_key))]

    def rows(self, plan_key: str, billing_code: str, npi: str | None = None) -> list[dict]:
        """canonical_provider_rates: one row per (rate x member NPI), with raw values of both."""
        rates = self.rate_rows(plan_key, billing_code)
        by_group: dict[tuple, list[dict]] = {}
        for r in rates:
            by_group.setdefault((r["source_id"], r["provider_group_id"]), []).append(r)
        if npi is not None:
            members = self.npi_memberships(plan_key, npi)
        else:
            members = [self.normalize_member(m) for key in by_group for m in self.group_members(*key)]
        out = []
        for m in members:
            for r in by_group.get((m["source_id"], m["provider_group_id"]), []):
                out.append({**r, **{k: v for k, v in m.items() if k != "raw"}, "raw": {**r["raw"], **m["raw"]}})
        return out

    def npi_member_count(self, plan_key: str, npi: str) -> int:
        """Membership rows of an NPI under the plan's complete sources (any code)."""
        return len(self.npi_memberships(plan_key, npi))

    def snapshot(self, plan_key: str) -> list[str]:
        sql = (f"SELECT DISTINCT {self._c('mrf_file_date','s')} FROM mrf_sources s WHERE {self._c('plan_key','s')} = ? "
               f"AND {self._c('source_status','s')} = 'complete'")
        return sorted(d for d in (iso_date(r[0]) for r in self.con.execute(sql, (plan_key,))) if d)

    @staticmethod
    def normalize_rate(raw: dict) -> dict:
        exp = iso_date(raw["expiration_date"])
        return {
            "source_id": raw["source_id"], "source_url": raw["source_url"], "insurer": raw["insurer"],
            "plan_key": raw["plan_key"], "network_raw": raw["network"], "mrf_file_date": iso_date(raw["file_date"]),
            "rate_id": raw["rate_id"], "billing_code": raw["billing_code"], "billing_code_type": raw["billing_code_type"],
            "billing_class": billing_class(raw["billing_class"]), "place_of_service": pos_set(raw["service_codes"]),
            "modifiers": modifiers(raw["modifiers"]),
            "source_rate_amount": raw["negotiated_rate"] if isinstance(raw["negotiated_rate"], (int, float)) else None,
            "source_rate_type": (raw["negotiated_type"] or "").strip().lower(),
            "expiration_date": None if exp in (None, "9999-12-31") else exp,
            "provider_group_id": raw["provider_group_id"], "raw": raw,
        }

    @classmethod
    def normalize_member(cls, row) -> dict:
        raw = dict(zip(cls.MEMBER_KEYS, row))
        return {"source_id": raw["source_id"], "provider_group_id": raw["provider_group_id"], "provider_npi": raw["npi"],
                "tin_sha256": raw["tin_sha256"], "provider_zip": (raw["zip"] or "")[:5] or None,
                "taxonomy": raw["taxonomy"], "tier": raw["tier"], "ucla_health": bool(raw["ucla_health"]), "raw": raw}


# --------------------------------------------------------------------------- CSV views

def resolve_plan_key(insurer: str, network_label: str, profiles: list[dict]) -> tuple[str | None, str | None, str | None, str]:
    """Report (insurer, network label) -> (plan_key, exact MRF network or None, network_scope, error).
    Uses the plan profiles only; never builds a plan key from the insurer string."""
    hits = []
    for p in profiles:
        if p.get("insurer") != insurer:
            continue
        names = p.get("network_names")
        if names and " / ".join(names) == network_label:
            hits.append((p["plan_key"], names[0] if len(names) == 1 else network_label, None))
        elif not names and (p.get("market") or "all contracts") == network_label:
            hits.append((p["plan_key"], None, network_label))
    if len(hits) != 1:
        return None, None, None, "plan_key_unresolved" if not hits else "plan_key_ambiguous"
    return (*hits[0], "")


def load_code_types(path: Path | None) -> dict[str, str]:
    if path is None or not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["code"]: r["code_type"] for r in csv.DictReader(f)}


def canonical_encounter_components(encounters_csv: Path) -> list[dict]:
    out = []
    with open(encounters_csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append({"encounter_key": r["encounter"], "encounter_label": r["label"], "component": r["component"],
                        "billing_codes": tuple(c for c in r["codes"].split("|") if c),
                        "billing_class": billing_class(r["billing_class"]),
                        "place_of_service": pos_preference(r["place_of_service"]), "modifier_rule": r["modifier_rule"],
                        "verified_by": r.get("verified_by") or None, "raw": dict(r)})
    return out


def implied_modifier_rule(code: str, bclass: str, pos: tuple[str, ...], components: list[dict]) -> str:
    """report.component_combos: the encounter component's rule for this code/class/POS, else
    global_or_tc_plus_26 for 7xxxx imaging, else global."""
    for c in components:
        if code in c["billing_codes"] and c["billing_class"] == bclass and c["place_of_service"] == pos:
            return c["modifier_rule"]
    return "global_or_tc_plus_26" if code.startswith("7") else "global"


def _csv_common(r: dict, profiles: list[dict]) -> dict:
    plan_key, network, scope, err = resolve_plan_key(r["insurer"], r["network"], profiles)
    return {"insurer": r["insurer"], "plan_key": plan_key, "network": network, "network_label": r["network"],
            "network_scope": scope, "allowed_amount": amount(r["allowed_amount"]),
            "allowed_amount_raw": r["allowed_amount"], "allowed_amount_basis": r["allowed_amount_basis"] or None,
            "status": r["status"], "n_providers": _int(r.get("n_providers")),
            "p10": amount(r.get("p10")), "p90": amount(r.get("p90")),
            "placeholder_values": [k for k in ("allowed_amount", "p10", "p90", "ucla_health_rate")
                                   if is_placeholder(r.get(k))],
            "mrf_file_dates": date_list(r.get("mrf_file_date")), "mrf_file_date_raw": r.get("mrf_file_date"),
            "source_ids": [], "errors": [err] if err else [], "raw": dict(r)}


def canonical_csv_views(csv_path: Path, components: list[dict], profiles: list[dict],
                        code_types: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """(canonical_A_encounters, canonical_B_components) from allowed_amounts.csv."""
    labels: dict[str, set[str]] = {}
    for c in components:
        labels.setdefault(c["encounter_label"], set()).add(c["encounter_key"])
    a_rows, b_rows = [], []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["table"] == "A_encounter":
                row = {"table_type": "A_encounter", **_csv_common(r, profiles), "encounter_label": r["encounter"],
                       "components": r.get("components") or ""}
                keys = labels.get(r["encounter"], set())
                if r.get("encounter_key"):
                    row["encounter_key"] = r["encounter_key"]
                elif len(keys) == 1:
                    row["encounter_key"] = next(iter(keys))
                else:
                    row["encounter_key"] = None
                    row["errors"].append("encounter_label_unknown" if not keys else "encounter_key_ambiguous")
                a_rows.append(row)
            elif r["table"] == "B_component":
                code, bclass, pos = r["billing_code"], billing_class(r["billing_class"]), pos_preference(r["place_of_service"])
                row = {"table_type": "B_component", **_csv_common(r, profiles), "billing_code": code,
                       "billing_code_type": code_types.get(code), "billing_class": bclass, "place_of_service": pos,
                       "modifier_rule": implied_modifier_rule(code, bclass, pos, components),
                       "modifier_rule_source": "derived (report.component_combos rule)",
                       "negotiated_type_mix": r.get("negotiated_type_mix") or "",
                       "n_rates_excluded": _int(r.get("n_rates_excluded"))}
                if bclass is None:
                    row["errors"].append("billing_class_unknown")
                b_rows.append(row)
    return a_rows, b_rows


def as_jsonable(obj):
    if isinstance(obj, (frozenset, set)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(type(obj))


def dumps(obj) -> str:
    return json.dumps(obj, default=as_jsonable, sort_keys=True, allow_nan=False)
