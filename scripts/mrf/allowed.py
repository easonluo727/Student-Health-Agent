"""Deterministic rate selection and the single `allowed_amount` value (docs/prompts/mrf-compile.md).

Per provider (NPI), for one billing code / billing class / place of service:
  1. drop `percentage` and `per diem` rates, expired rates, and $0 rates (capitated/bundled
     arrangements, not prices); all are counted in n_rates_excluded;
  2. modifiers: use the global (unmodified) rate; for imaging with no global rate, sum TC + 26;
     `mod_26` uses only the professional component (26);
  3. place of service: rates listing the encounter's POS (first match in an ordered preference
     list such as "20|11"), else rates with no POS listed or the CMS all-POS value CSTM-00;
  4. among what is left keep the latest expiration date; if several dollar values remain
     (e.g. the NPI is in several groups/TINs), take their median.
Across providers, `allowed_amount` is chosen in priority order:
  provider_rate (only when an NPI is requested) -> ucla_health_rate -> westwood_area_median
  -> la_county_median -> empty with status rate_unavailable.
No value is ever invented: every number is a median of real per-provider negotiated rates.
"""
from __future__ import annotations

import datetime as dt
import functools
import re
import sqlite3
import statistics
from dataclasses import dataclass, field

DOLLAR_TYPES = {"negotiated", "derived", "fee schedule"}
EXCLUDED_TYPES = {"percentage", "per diem"}
COMPONENT_MODIFIERS = {"TC", "26"}
UCLA_PATTERN = re.compile(r"\bUCLA\b|REGENTS OF THE UNIVERSITY OF CALIFORNIA|UNIVERSITY OF CALIFORNIA,? LOS ANGELES", re.I)
BASIS_RANK = {"provider_rate": 0, "ucla_health_rate": 1, "westwood_area_median": 2, "la_county_median": 3}
STATUS = {"provider_rate": "verified_rate", "ucla_health_rate": "verified_rate",
          "westwood_area_median": "estimated_rate", "la_county_median": "estimated_rate"}


@dataclass(slots=True)
class Candidate:
    npi: str
    tier: str
    ucla: bool
    rate: float
    negotiated_type: str
    expiration_date: str
    service_codes: tuple[str, ...]
    modifiers: tuple[str, ...]
    file_date: str
    source_url: str


@dataclass
class ComponentResult:
    plan_key: str
    billing_code: str
    billing_class: str
    place_of_service: str | None
    allowed_amount: float | None
    allowed_amount_basis: str | None
    status: str
    n_providers: int = 0
    p10: float | None = None
    median: float | None = None
    p90: float | None = None
    ucla_health_rate: float | None = None
    westwood_area_median: float | None = None
    la_county_median: float | None = None
    n_rates_excluded: int = 0
    negotiated_type_mix: str = ""
    uses_tc_plus_26: int = 0
    mrf_file_dates: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    return statistics.quantiles(vals, n=100, method="inclusive")[int(q * 100) - 1]


def _r(x):
    return None if x is None else round(x, 2)


def ucla_health_tins(con: sqlite3.Connection) -> frozenset[str]:
    """Hashed TINs that are UCLA Health organization NPIs (table built by report.py from NPPES)."""
    try:
        return frozenset(r[0] for r in con.execute("SELECT tin_sha256 FROM ucla_health_tins"))
    except sqlite3.OperationalError:
        return frozenset()


@functools.lru_cache(maxsize=4)
def fetch_candidates(con: sqlite3.Connection, plan_key: str, code: str, billing_class: str) -> tuple[Candidate, ...]:
    """All (provider, price) pairs for one plan/code/class. A provider counts as UCLA Health
    (Tier A only) if its NPI is a UCLA organization NPI, its TIN *is* a UCLA organization NPI
    (Anthem reports TINs as organization NPIs), or its TIN business name names UCLA/Regents."""
    q = """
    SELECT m.npi, m.tier, m.ucla_health, m.tin_business_name, m.tin_sha256,
           r.negotiated_rate, r.negotiated_type, r.expiration_date, r.service_codes, r.modifiers,
           s.file_date, s.source_url
    FROM negotiated_rates r
    JOIN mrf_sources s ON s.source_id = r.source_id AND s.status = 'complete'
    JOIN provider_members m ON m.source_id = r.source_id AND m.provider_group_id = r.provider_group_id
    WHERE s.plan_key = ? AND r.billing_code = ? AND r.billing_class IN (?, 'both') AND m.npi IS NOT NULL
    """
    ucla_tins = ucla_health_tins(con)
    out = []
    for npi, tier, ucla_org, bname, tin, rate, ntype, exp, sc, mods, fdate, url in con.execute(q, (plan_key, code, billing_class)):
        ucla = bool(ucla_org or (tin in ucla_tins) or UCLA_PATTERN.search(bname or ""))
        out.append(Candidate(npi, tier or "B", ucla and tier == "A", rate, (ntype or "").lower(), exp or "9999-12-31",
                             tuple(x for x in (sc or "").split(",") if x), tuple(x for x in (mods or "").split(",") if x),
                             fdate or "", url))
    return tuple(out)


def _pick(cands: list[Candidate], pos: str | None) -> float | None:
    """POS match, latest expiration, median of remaining dollar values. `pos` may be an ordered
    preference list "20|11" (from encounters.csv): the first POS with a rate wins, then rates that
    list no POS."""
    if pos:
        chosen: list[Candidate] = []
        for p in pos.split("|"):
            chosen = [c for c in cands if p in c.service_codes]
            if chosen:
                break
        # CSTM-00 = "same rate for every place of service" (CMS schema), i.e. like no POS listed
        cands = chosen or [c for c in cands if not c.service_codes or "CSTM-00" in c.service_codes]
    if not cands:
        return None
    latest = max(c.expiration_date for c in cands)
    vals = sorted({c.rate for c in cands if c.expiration_date == latest})
    return statistics.median(vals) if vals else None


def per_provider(cands: list[Candidate], pos: str | None, modifier_rule: str, as_of: str):
    """Return ({npi: (value, tier, ucla, used_tc26)}, n_excluded, type_mix)."""
    by_npi: dict[str, list[Candidate]] = {}
    n_excluded = 0
    mix: dict[str, int] = {}
    for c in cands:
        mix[c.negotiated_type] = mix.get(c.negotiated_type, 0) + 1
        # A $0 "rate" is a capitated or bundled arrangement (common in L.A. Care IPA contracts), not a price.
        if (c.negotiated_type in EXCLUDED_TYPES or c.negotiated_type not in DOLLAR_TYPES or c.expiration_date < as_of
                or c.rate is None or c.rate <= 0):
            n_excluded += 1
            continue
        by_npi.setdefault(c.npi, []).append(c)
    out = {}
    for npi, cs in by_npi.items():
        tier, ucla = cs[0].tier, any(c.ucla for c in cs)
        if modifier_rule == "mod_26":  # professional component only (e.g. radiologist read in the ER)
            value = _pick([c for c in cs if c.modifiers == ("26",)], pos)
            if value is not None:
                out[npi] = (value, tier, ucla, False)
            continue
        value = _pick([c for c in cs if not c.modifiers], pos)
        used = False
        if value is None and modifier_rule == "global_or_tc_plus_26":
            tc = _pick([c for c in cs if c.modifiers == ("TC",)], pos)
            pc = _pick([c for c in cs if c.modifiers == ("26",)], pos)
            if tc is not None and pc is not None:
                value, used = tc + pc, True
        if value is not None:
            out[npi] = (value, tier, ucla, used)
    return out, n_excluded, ", ".join(f"{k}:{v}" for k, v in sorted(mix.items()))


def component(con, plan_key: str, code: str, billing_class: str, pos: str | None,
              modifier_rule: str = "global", npi: str | None = None, as_of: str | None = None) -> ComponentResult:
    as_of = as_of or dt.date.today().isoformat()
    cands = fetch_candidates(con, plan_key, code, billing_class)
    prov, n_excl, mix = per_provider(cands, pos, modifier_rule, as_of)
    ucla = sorted(v[0] for v in prov.values() if v[2])
    tier_a = sorted(v[0] for v in prov.values() if v[1] == "A")
    la = sorted(v[0] for v in prov.values())
    res = ComponentResult(plan_key, code, billing_class, pos, None, None, "rate_unavailable",
                          n_rates_excluded=n_excl, negotiated_type_mix=mix,
                          uses_tc_plus_26=sum(v[3] for v in prov.values()),
                          ucla_health_rate=_r(statistics.median(ucla)) if ucla else None,
                          westwood_area_median=_r(statistics.median(tier_a)) if tier_a else None,
                          la_county_median=_r(statistics.median(la)) if la else None,
                          mrf_file_dates=sorted({c.file_date for c in cands}),
                          source_urls=sorted({c.source_url for c in cands}))
    choices = []
    if npi and npi in prov:
        choices.append(("provider_rate", [prov[npi][0]]))
    choices += [("ucla_health_rate", ucla), ("westwood_area_median", tier_a), ("la_county_median", la)]
    for basis, vals in choices:
        if vals:
            res.allowed_amount = _r(statistics.median(vals))
            res.allowed_amount_basis, res.status = basis, STATUS[basis]
            res.n_providers, res.median = len(vals), _r(statistics.median(vals))
            res.p10, res.p90 = _r(_pct(vals, 0.10)), _r(_pct(vals, 0.90))
            break
    return res


def worst_basis(bases: list[str]) -> str:
    return max(bases, key=lambda b: BASIS_RANK[b])
