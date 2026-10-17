"""Synthetic, self-contained test data for the overlay (no MRF downloads, no LLM)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

OVERLAY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OVERLAY / "workflow"))
import overlay_config  # noqa: E402

PATHS = overlay_config.install_import_paths()

AS_OF = dt.date(2026, 10, 14)
SNAP = "2026-10-01"
INSURER = "Test Insurer"
NET = "TEST PPO"
NPI = "1234567890"

ENCOUNTERS = [
    # encounter, label, component, codes, billing_class, place_of_service, modifier_rule
    ("office_visit_established", "Primary care visit (established patient)", "visit", "99213", "professional", "11", "global"),
    ("office_visit_new", "Primary care visit (new patient)", "visit", "99203", "professional", "11", "global"),
    ("er_ankle_xray", "ER visit for ankle sprain with X-ray", "er_physician", "99284", "professional", "23", "global"),
    ("er_ankle_xray", "ER visit for ankle sprain with X-ray", "er_facility", "99284", "institutional", "", "global"),
    ("er_ankle_xray", "ER visit for ankle sprain with X-ray", "xray_read", "73610", "professional", "23|22|11", "mod_26"),
    ("flu_shot", "Flu shot", "vaccine", "90686", "professional", "11", "global"),
    ("dup_encounter", "Duplicated label row", "visit", "99214", "professional", "11", "global"),
    ("unavailable_enc", "Unavailable encounter", "visit", "99215", "professional", "11", "global"),
]

COLS = ["table", "insurer", "network", "encounter", "allowed_amount", "allowed_amount_basis", "status", "components",
        "n_providers", "p10", "p90", "ucla_health_rate", "mrf_file_date", "billing_code", "billing_class",
        "place_of_service", "negotiated_type_mix", "n_rates_excluded"]


def a_row(label, amount, status="verified_rate", basis="ucla_health_rate", date=SNAP, insurer=INSURER, network=NET):
    return {"table": "A_encounter", "insurer": insurer, "network": network, "encounter": label, "allowed_amount": amount,
            "allowed_amount_basis": basis, "status": status, "components": "", "n_providers": 42, "p10": "", "p90": "",
            "ucla_health_rate": "", "mrf_file_date": date}


def b_row(code, cls, pos, amount, status="estimated_rate", basis="la_county_median", date=SNAP, insurer=INSURER, network=NET):
    return {"table": "B_component", "insurer": insurer, "network": network, "billing_code": code, "billing_class": cls,
            "place_of_service": pos, "allowed_amount": amount, "allowed_amount_basis": basis, "status": status,
            "n_providers": 17, "mrf_file_date": date}


DEFAULT_ROWS = [
    a_row("Primary care visit (established patient)", 100.0),
    a_row("Primary care visit (new patient)", 150.0, status="estimated_rate", basis="la_county_median"),
    a_row("ER visit for ankle sprain with X-ray", 2000.0),
    a_row("Flu shot", 25.0),
    a_row("Duplicated label row", 70.0), a_row("Duplicated label row", 71.0),
    a_row("Unavailable encounter", "", status="rate_unavailable", basis=""),
    a_row("Primary care visit (established patient)", 999.0, network="OTHER NET"),
    b_row("99284", "professional", "23", 300.0),
    b_row("99284", "institutional", "", 1500.0),
    b_row("73610", "professional", "23|22|11", 40.0),
    b_row("99213", "professional", "11", 100.0, status="verified_rate", basis="ucla_health_rate"),
    b_row("99213", "professional", "20", 130.0),
    b_row("99213", "institutional", "", 400.0),
]

PROFILES = {"plans": [
    {"plan_key": "test_ppo", "insurer": INSURER, "network_names": [NET]},
    {"plan_key": "test_other", "insurer": INSURER, "network_names": ["OTHER NET"]},
    {"plan_key": "test_nonetwork", "insurer": "Contract Plan", "network_names": None, "market": "individual"},
]}

RULE_TEMPLATE = {"copay_usd": None, "plan_pays_percentage": 80, "patient_pays_percentage": 20, "deductible_applies": False,
                 "prior_authorization": "not_stated", "coverage_status": "covered", "needs_review": False,
                 "source_text": "80% of Allowed Amount", "source_page": 12}


def rules(plan="test_plan"):
    def r(service, setting, network="in-network", **kw):
        return {**RULE_TEMPLATE, "plan": plan, "policy_plan_id": plan, "service": service, "setting": setting,
                "network": network, **kw}
    return [
        r("physician office visits", "outpatient"),
        r("emergency room", "outpatient", patient_pays_percentage=10, plan_pays_percentage=90),
        r("diagnostic x-ray", "outpatient", deductible_applies=True, source_page=13),
        r("physician office visits", "outpatient", network="out-of-network", patient_pays_percentage=40,
          plan_pays_percentage=60),
        r("immunizations", "outpatient", copay_usd=0, patient_pays_percentage=None, plan_pays_percentage=None),
        r("urgent care", "outpatient", copay_usd=None),
    ]


def plan_map(entries=None, plan="test_plan"):
    return {"schema_version": "policy_mrf_map.v1", "entries": entries if entries is not None else [
        {"policy_plan_id": plan, "policy_network_category": "in-network", "mrf_plan_key": "test_ppo", "mrf_network": NET,
         "network_scope": None, "candidates": [], "question_for_human": None, "evidence": ["test"],
         "review_status": "verified", "reviewed_by": "test"}]}


def comp(component, code, cls, pos, rule, benefit, required=True):
    return {"component": component, "billing_code": code, "billing_class": cls, "place_of_service": pos,
            "modifier_rule": rule, "benefit_service": benefit, "benefit_setting": None, "required": required}


def service_map(plan="test_plan", review="verified"):
    def e(eid, service, options, question=None):
        return {"entry_id": eid, "policy_plan_id": plan, "service": service, "setting": "outpatient",
                "policy_network_category": "in-network", "clarification_question": question, "options": options,
                "review_status": review, "reviewed_by": "test", "source": "test"}
    return {"schema_version": "service_rate_map.v1", "entries": [
        e("office", "physician office visits", [
            {"option_id": "established", "label": "Established patient", "kind": "A_encounter",
             "encounter_key": "office_visit_established", "component_benefit_services": {"visit": "physician office visits"}},
            {"option_id": "new", "label": "New patient", "kind": "A_encounter",
             "encounter_key": "office_visit_new", "component_benefit_services": {"visit": "physician office visits"}}],
          "New or established patient?"),
        e("er", "emergency room", [
            {"option_id": "er_total", "label": "ER total (A)", "kind": "A_encounter", "encounter_key": "er_ankle_xray",
             "component_benefit_services": {"er_physician": "emergency room", "er_facility": "emergency room",
                                            "xray_read": "diagnostic x-ray"}},
            {"option_id": "er_parts", "label": "ER by component (B)", "kind": "B_components", "components": [
                comp("er_physician", "99284", "professional", "23", "global", "emergency room"),
                comp("er_facility", "99284", "institutional", "", "global", "emergency room"),
                comp("xray_read", "73610", "professional", "23|22|11", "mod_26", "diagnostic x-ray")]}],
          "Price the ER visit as one total or by component?"),
        e("imm", "immunizations", [
            {"option_id": "flu", "label": "Flu shot", "kind": "A_encounter", "encounter_key": "flu_shot",
             "component_benefit_services": {"vaccine": "immunizations"}}]),
        e("urgent", "urgent care", [
            {"option_id": "uc_parts", "label": "Urgent care visit", "kind": "B_components", "components": [
                comp("visit", "99213", "professional", "20", "global", "urgent care"),
                comp("missing_test", "87804", "professional", "20", "global", "urgent care")]}]),
    ]}


def write_env(tmp: Path, rows=None, encounters=None, profiles=None, pmap=None, smap=None, benefit_rules=None,
              sqlite=False) -> overlay_config.OverlayPaths:
    tmp.mkdir(parents=True, exist_ok=True)
    with open(tmp / "allowed_amounts.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows if rows is not None else DEFAULT_ROWS:
            w.writerow(r)
    with open(tmp / "encounters.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["encounter", "label", "component", "codes", "billing_class", "place_of_service", "modifier_rule",
                    "verified_by"])
        for e in encounters or ENCOUNTERS:
            w.writerow([*e, ""])
    (tmp / "plan_profiles.json").write_text(json.dumps(profiles or PROFILES))
    data = tmp / "overlay_data"
    data.mkdir(exist_ok=True)
    (data / "policy_mrf_map.v1.json").write_text(json.dumps(pmap if pmap is not None else plan_map()))
    (data / "service_rate_map.v1.json").write_text(json.dumps(smap if smap is not None else service_map()))
    enc_rows = encounters or ENCOUNTERS
    labels = {}
    for key, label, *_ in enc_rows:
        labels[key] = label
    (data / "encounter_labels.v1.json").write_text(json.dumps(
        {"schema_version": "encounter_labels.v1", "entries": [{"encounter_key": k, "csv_label": v} for k, v in labels.items()]}))
    (tmp / "rules.json").write_text(json.dumps(benefit_rules if benefit_rules is not None else rules()))
    db = tmp / "rates.sqlite"
    if sqlite:
        build_sqlite(db)
    return overlay_config.OverlayPaths(repo_root=PATHS.repo_root, rates_csv=tmp / "allowed_amounts.csv",
                                       encounters_csv=tmp / "encounters.csv", plan_profiles=tmp / "plan_profiles.json",
                                       rates_sqlite=db, overlay_data=data, schemas=PATHS.schemas,
                                       billing_codes=tmp / "billing_codes.csv", npi_directory=tmp / "npis.csv",
                                       zip_centroids=tmp / "zips.csv", log_dir=tmp / "logs")


def build_sqlite(path: Path) -> None:
    """Tiny database in the real MRF schema (scripts/mrf/mrf_rate_store.py)."""
    import mrf_rate_store as store
    con = store.connect(str(path))
    sid = store.begin_source(con, "https://example.test/file.json", insurer=INSURER, plan_key="test_ppo",
                             network=NET, region="CA", file_date=SNAP, toc_url=None)
    con.execute("UPDATE mrf_sources SET status='complete' WHERE source_id=?", (sid,))
    rates = [("99284", "1", 310.0, "professional", "23", ""), ("99284", "2", 1490.0, "institutional", "", ""),
             ("73610", "1", 35.0, "professional", "11", "26"), ("99213", "1", 95.0, "professional", "11", "")]
    for code, gid, rate, cls, pos, mods in rates:
        con.execute("INSERT INTO negotiated_rates (source_id, billing_code, billing_code_type, negotiation_arrangement,"
                    " provider_group_id, negotiated_rate, negotiated_type, expiration_date, billing_class, setting,"
                    " service_codes, modifiers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, code, "CPT", "ffs", gid, rate, "negotiated", "9999-12-31", cls, "outpatient", pos, mods))
    for gid, npi in (("1", NPI), ("2", "1999999999")):
        con.execute("INSERT INTO provider_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, gid, npi, "h" * 64, "0000", "ein", None, json.dumps([NET]), "90024", "x", "A", 0))
    con.commit()
    con.close()


def build_stack(paths, plan="test_plan", as_of=AS_OF, confirm=None):
    """BranchATools + QuoteGuard wired exactly like branch_a_agent.build_tools, with a scripted confirmer."""
    import branch_a_agent
    tools, guard = branch_a_agent.build_tools(plan, paths.rates_csv.parent / "rules.json", paths, as_of=as_of)
    guard._confirm = confirm or (lambda summary: (True, "yes"))
    return tools, guard


def resolver(paths, as_of=AS_OF, **kw):
    from rate_resolver import build_resolver
    return build_resolver(paths, as_of=as_of, log_path=paths.log_dir / "rate_lookup.jsonl", **kw)


# --------------------------------------------------------------------------- provider-level fixture (nearby fallback)

SELECTED = "1000000001"      # has 99213 and an expiring 99215 rate, no 99214 rate
OUTSIDER = "1000000099"      # in NPPES, not a member of any group
TAX = "207Q00000X"
NEAR_ZIPS = ["90024", "90025", "90049", "90064", "90034", "90095"]
FAR_ZIP, NO_CENTROID_ZIP = "92101", "99999"


def _centroids(zips):
    with open(OVERLAY / "data" / "zcta_centroids_ca.csv", newline="") as f:
        rows = {r["zip"]: r for r in csv.DictReader(f)}
    return [rows[z] for z in zips if z in rows]


def provider_rows():
    """(npi, group, code, cls, rate, type, expiration, pos, mods, zip, tin, plan)"""
    rows = [(SELECTED, "gs", "99213", "professional", 95.0, "negotiated", "9999-12-31", "11", "", "90024", "ts", "p"),
            (SELECTED, "gs", "99215", "professional", 180.0, "negotiated", "2026-11-01", "11", "", "90024", "ts", "p")]
    for i in range(1, 13):
        rows.append((f"20000000{i:02d}", f"g{i}", "99214", "professional", 100.0 + 10 * i, "fee schedule",
                     "9999-12-31", "11", "", NEAR_ZIPS[i % 6], f"t{i}", "p"))
    for i in range(13, 18):  # one TIN, five NPIs: the cap keeps three
        rows.append((f"20000000{i:02d}", f"g{i}", "99214", "professional", 500.0, "negotiated", "9999-12-31", "11", "",
                     "90024", "t_shared", "p"))
    rows += [("2000000018", "g18a", "99214", "professional", 300.0, "negotiated", "9999-12-31", "11", "", "90025", "t18", "p"),
             ("2000000018", "g18b", "99214", "professional", 320.0, "negotiated", "9999-12-31", "11", "", "90025", "t18b", "p")]
    excluded = [  # each fails exactly one hard rule; all are close and cheap so a score could not save them
        ("3000000001", "x1", "99214", "professional", 50.0, "negotiated", "9999-12-31", "11", "", FAR_ZIP, "x1", "p"),
        ("3000000002", "x2", "99214", "institutional", 60.0, "negotiated", "9999-12-31", "", "", "90024", "x2", "p"),
        ("3000000003", "x3", "99214", "professional", 70.0, "percentage", "9999-12-31", "11", "", "90024", "x3", "p"),
        ("3000000004", "x4", "99214", "professional", 80.0, "negotiated", "2026-01-01", "11", "", "90024", "x4", "p"),
        ("3000000005", "x5", "99214", "professional", 90.0, "negotiated", "9999-12-31", "23", "", "90024", "x5", "p"),
        ("3000000006", "x6", "99214", "professional", 95.0, "negotiated", "9999-12-31", "11", "26", "90024", "x6", "p"),
        ("3000000007", "x7", "99214", "professional", 99.0, "negotiated", "9999-12-31", "11", "", "90024", "x7", "o"),
        ("3000000008", "x8", "99214", "professional", 0.0, "negotiated", "9999-12-31", "11", "", "90024", "x8", "p"),
        ("3000000009", "x9", "99214", "professional", 40.0, "negotiated", "9999-12-31", "11", "", NO_CENTROID_ZIP, "x9", "p"),
        ("3000000010", "x10", "99214", "professional", 99999999999999.0, "fee schedule", "9999-12-31", "11", "", "90024",
         "x10", "p"),  # MRF placeholder value
    ]
    return rows, excluded


EXPECTED_TOP = [110.0 + 10 * i for i in range(12)] + [500.0] * 3 + [310.0]


def build_provider_sqlite(path: Path, empty=False) -> None:
    import hashlib
    import mrf_rate_store as store
    con = store.connect(str(path))
    sids = {}
    for plan, net in (("p", NET), ("o", "OTHER NET")):
        sid = store.begin_source(con, f"https://example.test/{plan}.json", insurer=INSURER,
                                 plan_key="test_ppo" if plan == "p" else "test_other", network=net, region="CA",
                                 file_date=SNAP, toc_url=None)
        con.execute("UPDATE mrf_sources SET status='complete' WHERE source_id=?", (sid,))
        sids[plan] = sid
    if not empty:
        rows, excluded = provider_rows()
        seen = set()
        for npi, gid, code, cls, rate, typ, exp, pos, mods, z, tin, plan in rows + excluded:
            sid = sids[plan]
            con.execute("INSERT INTO negotiated_rates (source_id, billing_code, billing_code_type, negotiation_arrangement,"
                        " provider_group_id, negotiated_rate, negotiated_type, expiration_date, billing_class, setting,"
                        " service_codes, modifiers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, code, "CPT", "ffs", gid, rate, typ, exp, cls, "outpatient", pos, mods))
            if (sid, gid, npi) not in seen:
                seen.add((sid, gid, npi))
                con.execute("INSERT INTO provider_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (sid, gid, npi, hashlib.sha256(tin.encode()).hexdigest(), "0000", "ein", None,
                             json.dumps([NET]), z, TAX, "A", 0))
    con.commit()
    con.close()


def write_provider_env(tmp: Path, empty=False):
    paths = write_env(tmp)
    build_provider_sqlite(paths.rates_sqlite, empty=empty)
    with open(paths.zip_centroids, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["zip", "lat", "lon"])
        w.writeheader()
        w.writerows(_centroids(NEAR_ZIPS + [FAR_ZIP]))
    rows, excluded = provider_rows()
    with open(paths.npi_directory, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["npi", "entity_type", "name", "taxonomy", "zip", "city", "tier", "ucla_org"])
        for npi in sorted({r[0] for r in rows + excluded} | {OUTSIDER}):
            w.writerow([npi, "1", "TEST", TAX, "90024", "Los Angeles", "A", "0"])
    with open(paths.billing_codes, "w", newline="") as f:
        f.write("code,code_type,service,source,verified_by\n99213,CPT,x,test,\n99284,CPT,x,test,\n73610,CPT,x,test,\n")
    return paths


def lookup(paths, as_of=AS_OF, **kw):
    from rate_lookup import RateLookup
    return RateLookup.from_paths(paths, as_of=as_of, log_path=paths.log_dir / "rate_lookup.jsonl", **kw)


def request(**kw):
    base = {"insurer": INSURER, "plan_key": "test_ppo", "network": NET, "service_description": "doctor visit for Jane",
            "encounter_key": None, "billing_code": None, "billing_code_type": None, "billing_class": None,
            "place_of_service": None, "service_date": "2026-10-20", "selected_provider_npi": None}
    base.update(kw)
    return base
