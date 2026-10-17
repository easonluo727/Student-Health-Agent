"""Build overlay/data/zcta_centroids_ca.csv from the Census Bureau ZCTA Gazetteer file (public data).

    python build_zip_centroids.py <path to 2024_Gaz_zcta_national.zip>

Source: https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip
Keeps California ZCTAs (900xx-961xx) with their internal point (INTPTLAT/INTPTLONG). A ZCTA internal
point approximates a ZIP code's location; distances computed from it are approximate. The sidecar
zcta_centroids_ca.meta.json records the source URL and sha256 so the file can be rebuilt and checked.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

SOURCE_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_zcta_national.zip"
OUT = Path(__file__).resolve().parent.parent / "data" / "zcta_centroids_ca.csv"


def is_california(zcta: str) -> bool:
    return zcta.isdigit() and len(zcta) == 5 and 90000 <= int(zcta) <= 96199


def build(zip_path: Path, out: Path = OUT) -> dict:
    data = Path(zip_path).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        (name,) = [n for n in z.namelist() if n.endswith(".txt")]
        text = z.read(name).decode("utf-8")
    rows = []
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    i_zip, i_lat, i_lon = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
    for r in reader:
        z = r[i_zip].strip()
        if is_california(z):
            rows.append((z, round(float(r[i_lat]), 6), round(float(r[i_lon]), 6)))
    rows.sort()
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["zip", "lat", "lon"])
        w.writerows(rows)
    meta = {"source_url": SOURCE_URL, "source_file": name, "source_sha256": hashlib.sha256(data).hexdigest(),
            "filter": "California ZCTAs 90000-96199", "point": "INTPTLAT/INTPTLONG (Census internal point)",
            "rows": len(rows)}
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(build(Path(sys.argv[1])), indent=2))
