"""SQLite store for filtered MRF rates (rates plus import status).

Tables:
  mrf_sources       one row per imported file: URL, insurer, plan/network, region, file date,
                    TOC URL, bytes read, rows scanned/kept, status (running/complete/failed)
  negotiated_rates  one row per negotiated price x kept provider group
  provider_members  provider group -> NPI, hashed TIN (+ last four, type), ZIP/taxonomy/tier
  rate_summary      built by summarize.py

Resumability: a source is committed every `commit_every` rows while it streams and marked
`complete` at the end. Re-running an import skips complete sources and deletes the partial
rows of a running/failed one before redoing it. gzip streams cannot be resumed mid-file, so
the unit of resume is the file.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict

SCHEMA = """
CREATE TABLE IF NOT EXISTS mrf_sources (
    source_id          INTEGER PRIMARY KEY,
    source_url         TEXT NOT NULL,
    insurer            TEXT NOT NULL,
    plan_key           TEXT NOT NULL,
    network            TEXT,
    region             TEXT,
    file_date          TEXT,
    toc_url            TEXT,
    reporting_entity   TEXT,
    schema_version     TEXT,
    compressed_bytes   INTEGER,
    decompressed_bytes INTEGER,
    rows_scanned       INTEGER,  -- provider_references + in_network records examined
    rows_kept          INTEGER,
    groups_kept        INTEGER,
    status             TEXT NOT NULL,
    error              TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    wall_seconds       REAL,
    peak_rss_mb        REAL,
    parser_stats       TEXT,
    UNIQUE (source_url, plan_key)  -- one file can serve several networks (e.g. Anthem Pathway EPO and HMO)
);
CREATE TABLE IF NOT EXISTS negotiated_rates (
    rate_id                 INTEGER PRIMARY KEY,
    source_id               INTEGER NOT NULL REFERENCES mrf_sources(source_id),
    billing_code            TEXT NOT NULL,
    billing_code_type       TEXT,
    negotiation_arrangement TEXT,
    provider_group_id       TEXT NOT NULL,
    negotiated_rate         REAL,
    negotiated_type         TEXT,
    expiration_date         TEXT,
    billing_class           TEXT,
    setting                 TEXT,
    service_codes           TEXT,   -- comma-separated place-of-service codes ('' = none listed)
    modifiers               TEXT    -- comma-separated billing code modifiers ('' = global)
);
CREATE INDEX IF NOT EXISTS ix_rates_code ON negotiated_rates(source_id, billing_code, billing_class);
CREATE INDEX IF NOT EXISTS ix_rates_group ON negotiated_rates(source_id, provider_group_id);
CREATE TABLE IF NOT EXISTS provider_members (
    source_id         INTEGER NOT NULL REFERENCES mrf_sources(source_id),
    provider_group_id TEXT NOT NULL,
    npi               TEXT,
    tin_sha256        TEXT,
    tin_last4         TEXT,
    tin_type          TEXT,
    tin_business_name TEXT,
    network_names     TEXT,
    zip               TEXT,
    taxonomy          TEXT,
    tier              TEXT,
    ucla_health       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_members_group ON provider_members(source_id, provider_group_id);
CREATE INDEX IF NOT EXISTS ix_members_npi ON provider_members(npi);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def source_status(con: sqlite3.Connection, url: str, plan_key: str) -> str | None:
    row = con.execute("SELECT status FROM mrf_sources WHERE source_url=? AND plan_key=?", (url, plan_key)).fetchone()
    return row[0] if row else None


def begin_source(con: sqlite3.Connection, url: str, **meta) -> int:
    """Create (or reset a partial) source row and return its id."""
    row = con.execute("SELECT source_id FROM mrf_sources WHERE source_url=? AND plan_key=?",
                      (url, meta["plan_key"])).fetchone()
    if row:
        sid = row[0]
        con.execute("DELETE FROM negotiated_rates WHERE source_id=?", (sid,))
        con.execute("DELETE FROM provider_members WHERE source_id=?", (sid,))
        con.execute("UPDATE mrf_sources SET status='running', error=NULL, started_at=?, finished_at=NULL WHERE source_id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"), sid))
    else:
        cols = ["source_url", "status", "started_at", *meta]
        vals = [url, "running", time.strftime("%Y-%m-%dT%H:%M:%S"), *meta.values()]
        sid = con.execute(f"INSERT INTO mrf_sources ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals).lastrowid
    con.commit()
    return sid


def write_stream(con: sqlite3.Connection, sid: int, records, npi_info: dict[str, tuple] | None = None,
                 commit_every: int = 5000) -> tuple[int, int]:
    """Consume parser records ("group"/"rate") into the tables, committing periodically.
    npi_info: npi -> (zip, taxonomy, tier, ucla_org)."""
    n_rates = n_groups = pending = 0
    for kind, obj in records:
        if kind == "group":
            n_groups += 1
            for m in obj.members:
                zip5, taxonomy, tier, ucla = (npi_info or {}).get(m.npi or "", (None, None, None, 0))
                con.execute(
                    "INSERT INTO provider_members VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, obj.provider_group_id, m.npi, m.tin_sha256, m.tin_last4, m.tin_type, m.business_name,
                     json.dumps(obj.network_names), zip5, taxonomy, tier, int(ucla)))
        else:
            n_rates += 1
            con.execute(
                "INSERT INTO negotiated_rates (source_id, billing_code, billing_code_type, negotiation_arrangement,"
                " provider_group_id, negotiated_rate, negotiated_type, expiration_date, billing_class, setting,"
                " service_codes, modifiers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, obj["billing_code"], obj["billing_code_type"], obj["negotiation_arrangement"],
                 obj["provider_group_id"], obj["negotiated_rate"], obj["negotiated_type"], obj["expiration_date"],
                 obj["billing_class"], obj["setting"], ",".join(obj["service_code"]),
                 ",".join(obj["billing_code_modifier"])))
        pending += 1
        if pending >= commit_every:
            con.execute("UPDATE mrf_sources SET rows_kept=?, groups_kept=? WHERE source_id=?", (n_rates, n_groups, sid))
            con.commit()
            pending = 0
    con.commit()
    return n_rates, n_groups


def finish_source(con: sqlite3.Connection, sid: int, parser, n_rates: int, n_groups: int,
                  status: str = "complete", error: str | None = None) -> None:
    st = parser.stats
    h = parser.header
    con.execute(
        "UPDATE mrf_sources SET status=?, error=?, finished_at=?, compressed_bytes=?, decompressed_bytes=?,"
        " rows_scanned=?, rows_kept=?, groups_kept=?, wall_seconds=?, peak_rss_mb=?, parser_stats=?,"
        " reporting_entity=COALESCE(?, reporting_entity), schema_version=COALESCE(?, schema_version),"
        " file_date=COALESCE(file_date, ?) WHERE source_id=?",
        (status, error, time.strftime("%Y-%m-%dT%H:%M:%S"), st.compressed_bytes, st.decompressed_bytes,
         st.provider_refs_seen + st.in_network_items_seen, n_rates, n_groups, st.wall_seconds, st.peak_rss_mb,
         json.dumps(asdict(st)), h.get("reporting_entity_name"), h.get("version"), h.get("last_updated_on"), sid))
    con.commit()
