"""Build the Los Angeles NPI set from the public NPPES bulk file (no LLM, no hand-entered data).

Tier A: UCLA / Westwood practice-location ZIPs (fixed list from the task spec).
Tier B: every ZIP whose Census 2020 ZCTA has >= 50% of its land area in Los Angeles County
        (county FIPS 06037), plus the Tier A ZIPs.

A provider is placed by its primary practice location and, if present, its secondary practice
locations (NPPES `pl_pfile`). Deactivated NPIs are dropped. The NPPES zip is read member by
member without extracting it.

Output: data/mrf/la_npis.csv with npi, entity_type, name, taxonomy, zip, city, tier, ucla_org.
`ucla_org` = 1 for organization NPIs whose legal or other name matches UCLA_PATTERN; this is a
transparent name rule, recorded in the CSV so a human can audit it.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
import zipfile

TIER_A_ZIPS = ("90024", "90025", "90095", "90049", "90064", "90067")
LA_COUNTY_FIPS = "06037"
UCLA_PATTERN = re.compile(r"\bUCLA\b|REGENTS OF THE UNIVERSITY OF CALIFORNIA.*LOS ANGELES|"
                          r"RONALD REAGAN UCLA|UNIVERSITY OF CALIFORNIA,? LOS ANGELES", re.I)

csv.field_size_limit(sys.maxsize)


def la_county_zips(crosswalk_path: str, min_share: float = 0.5) -> set[str]:
    """Census tab20_zcta520_county20_natl.txt -> ZCTAs mostly inside LA County."""
    total: dict[str, int] = {}
    in_la: dict[str, int] = {}
    with open(crosswalk_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter="|"):
            z = row["GEOID_ZCTA5_20"]
            if not z:
                continue
            area = int(row["AREALAND_PART"] or 0)
            total[z] = total.get(z, 0) + area
            if row["GEOID_COUNTY_20"] == LA_COUNTY_FIPS:
                in_la[z] = in_la.get(z, 0) + area
    return {z for z, a in in_la.items() if total[z] and a / total[z] >= min_share}


def _member(z: zipfile.ZipFile, prefix: str) -> zipfile.ZipInfo:
    return next(i for i in z.infolist() if i.filename.startswith(prefix) and not i.filename.endswith("fileheader.csv"))


def _rows(z: zipfile.ZipFile, info: zipfile.ZipInfo):
    with z.open(info) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline=""))


def _primary_taxonomy(row: dict) -> str:
    first = ""
    for i in range(1, 16):
        code = row.get(f"Healthcare Provider Taxonomy Code_{i}", "")
        if code and not first:
            first = code
        if code and row.get(f"Healthcare Provider Primary Taxonomy Switch_{i}") == "Y":
            return code
    return first


def build(nppes_zip: str, crosswalk: str, out_path: str) -> dict:
    t0 = time.time()
    tier_a = set(TIER_A_ZIPS)
    tier_b = la_county_zips(crosswalk) | tier_a
    z = zipfile.ZipFile(nppes_zip)

    def tier_of(zip5: str) -> str | None:
        return "A" if zip5 in tier_a else ("B" if zip5 in tier_b else None)

    # Secondary practice locations: NPI -> best tier (A beats B).
    secondary: dict[str, tuple[str, str, str]] = {}
    for row in _rows(z, _member(z, "pl_pfile")):
        if row.get("Provider Secondary Practice Location Address - State Name", "").upper() != "CA":
            continue
        zip5 = row.get("Provider Secondary Practice Location Address - Postal Code", "")[:5]
        t = tier_of(zip5)
        if t and (row["NPI"] not in secondary or t < secondary[row["NPI"]][0]):
            secondary[row["NPI"]] = (t, zip5, row.get("Provider Secondary Practice Location Address - City Name", ""))

    n_scanned = n_deactivated = 0
    kept = []
    for row in _rows(z, _member(z, "npidata_pfile")):
        n_scanned += 1
        npi = row["NPI"]
        if row.get("NPI Deactivation Date") and not row.get("NPI Reactivation Date"):
            n_deactivated += 1
            continue
        zip5 = row["Provider Business Practice Location Address Postal Code"][:5]
        t = tier_of(zip5) if row["Provider Business Practice Location Address State Name"].upper() == "CA" else None
        city = row["Provider Business Practice Location Address City Name"]
        if npi in secondary and (t is None or secondary[npi][0] < t):
            t, zip5, city = secondary[npi]
        if t is None:
            continue
        etype = row["Entity Type Code"]
        if etype == "2":
            name = row["Provider Organization Name (Legal Business Name)"]
            other = row.get("Provider Other Organization Name", "")
            ucla = int(bool(UCLA_PATTERN.search(name) or UCLA_PATTERN.search(other)))
        else:
            name = f'{row["Provider Last Name (Legal Name)"]}, {row["Provider First Name"]}'
            ucla = 0
        kept.append((npi, etype, name, _primary_taxonomy(row), zip5, city.title(), t, ucla))

    kept.sort()
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["npi", "entity_type", "name", "taxonomy", "zip", "city", "tier", "ucla_org"])
        w.writerows(kept)
    return {
        "nppes_file": nppes_zip.rsplit("/", 1)[-1],
        "npi_rows_scanned": n_scanned,
        "deactivated_skipped": n_deactivated,
        "tier_b_zip_count": len(tier_b),
        "kept": len(kept),
        "tier_a": sum(1 for k in kept if k[6] == "A"),
        "tier_b_only": sum(1 for k in kept if k[6] == "B"),
        "ucla_org_npis": sum(k[7] for k in kept),
        "seconds": round(time.time() - t0, 1),
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--nppes-zip", required=True)
    ap.add_argument("--crosswalk", required=True, help="Census tab20_zcta520_county20_natl.txt")
    ap.add_argument("--out", default="data/mrf/la_npis.csv")
    a = ap.parse_args(argv)
    print(build(a.nppes_zip, a.crosswalk, a.out))


if __name__ == "__main__":
    main()
