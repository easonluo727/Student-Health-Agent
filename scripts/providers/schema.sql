PRAGMA foreign_keys=ON;
CREATE TABLE providers (
    npi TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('1','2')),
    display_name TEXT,
    first_name TEXT,
    last_name TEXT,
    credential TEXT,
    organization_name TEXT,
    primary_taxonomy TEXT,
    nppes_last_updated TEXT,
    nppes_status TEXT NOT NULL CHECK (nppes_status IN ('active','inactive'))
);
CREATE TABLE provider_taxonomies (
    npi TEXT NOT NULL REFERENCES providers(npi),
    taxonomy_code TEXT NOT NULL,
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0,1)),
    PRIMARY KEY (npi, taxonomy_code)
);
CREATE INDEX ix_provider_taxonomy ON provider_taxonomies(taxonomy_code, npi);
CREATE TABLE provider_locations (
    location_id INTEGER PRIMARY KEY,
    npi TEXT NOT NULL REFERENCES providers(npi),
    location_type TEXT NOT NULL CHECK (location_type IN ('primary','secondary')),
    address_key TEXT NOT NULL,
    address_line_1 TEXT,
    address_line_2 TEXT,
    city TEXT,
    state TEXT,
    zip5 TEXT,
    postal_code TEXT,
    country TEXT,
    phone TEXT,
    fax TEXT,
    latitude REAL,
    longitude REAL,
    geocode_status TEXT NOT NULL DEFAULT 'pending' CHECK (geocode_status IN ('pending','matched','ambiguous','failed')),
    geocode_precision TEXT,
    geocode_source TEXT,
    geocoded_at TEXT,
    UNIQUE(npi, address_key),
    CHECK ((latitude IS NULL AND longitude IS NULL) OR (latitude IS NOT NULL AND longitude IS NOT NULL))
);
CREATE INDEX ix_location_npi ON provider_locations(npi);
CREATE INDEX ix_location_zip ON provider_locations(zip5);
CREATE INDEX ix_location_coordinates ON provider_locations(latitude, longitude);
CREATE TABLE geocode_cache (
    address_key TEXT PRIMARY KEY,
    latitude REAL,
    longitude REAL,
    status TEXT NOT NULL,
    precision TEXT,
    source TEXT,
    geocoded_at TEXT
);
CREATE TABLE import_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE zip_centroids (
    zip5 TEXT PRIMARY KEY,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    source_year TEXT NOT NULL,
    source_url TEXT NOT NULL
);
