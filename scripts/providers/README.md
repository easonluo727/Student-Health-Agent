# NPPES provider directory (separate from rates)

This pipeline builds `providers.sqlite` from official CMS NPPES Version 2 data for the NPIs already present in the insurer MRF rate store. No production file is created by a checkout or test. `rates.sqlite` is opened with SQLite `mode=ro` solely for the target-NPI export; its schema and bytes are not changed. NPPES identifies providers and reported *practice* locations, not coordinates, licensure, or current insurance participation.

## Offline build

1. Select and download the latest **verifiable V2 monthly full replacement** ZIP from the [CMS NPPES files index](https://download.cms.gov/nppes/NPI_Files.html). Check the archive's CMS readme/headings for both `npidata_pfile` and `pl_pfile`. Record the release and actual download dates. The real archive and rate store were not available in this development checkout, so no production match or geocoding coverage is claimed here.
2. Export target NPIs, then stream the archive. The builder reads ZIP members with `csv.DictReader` and retains only target rows; it does not extract the multi-GB CSV to disk. It stores all distinct primary and secondary practice locations (never mailing addresses), Type 1 vs Type 2, taxonomy, active/deactivated state, archive SHA-256, source URL and counts. It refuses to replace an existing output; build a new path for refreshes.

```bash
python scripts/providers/export_target_npis.py --rates-db /path/to/rates.sqlite --out data/providers/target_npis.csv
python scripts/providers/build_directory.py --nppes-zip /path/to/NPPES_V2_monthly.zip \
  --target-npis data/providers/target_npis.csv --out data/providers/providers.sqlite \
  --release-date YYYY-MM-DD --downloaded-at YYYY-MM-DD \
  --source-url https://download.cms.gov/nppes/VERIFIED_V2_ARCHIVE.zip
python scripts/providers/validate_directory.py --db data/providers/providers.sqlite \
  --target-npis data/providers/target_npis.csv
```

The schema is [`schema.sql`](schema.sql). Validation fails for SQLite integrity/foreign-key problems, invalid NPIs or coordinates, unexpected providers, or duplicate locations. Missing names, addresses, unmatched target NPIs and ungeocoded locations are reported, not invented.

## Geocode enrichment

The optional, resumable geocoder calls the [US Census Geocoding API](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html) for full US practice addresses at a configurable interval (default one request per second). Census address matches can be street-interpolated; a matched pin is not a surveyed entrance. A wrong state, ZIP, house number, implausible coordinate, or multiple/zero matches leaves coordinates null. Results are cached by normalized address. No paid API key is required. Review the Census service's current usage terms and local quota before a large run.

```bash
python scripts/providers/geocode_directory.py --db data/providers/providers.sqlite --dry-run --limit 100
python scripts/providers/geocode_directory.py --db data/providers/providers.sqlite --resume --limit 100 --min-interval 1
```

Repeat `--resume` in bounded batches. `--retry-failed` explicitly retries failed rows. Network errors record failure; the build itself works offline. To support approximate **user** ZIP position, obtain the year-appropriate ZCTA Gazetteer ZIP from the [Census Gazetteer page](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html), then run:

```bash
python scripts/providers/import_zip_centroids.py --db data/providers/providers.sqlite \
  --gazetteer-zip /path/to/Census_ZCTA_Gazetteer.zip \
  --source-url https://www2.census.gov/geo/docs/maps-data/data/gazetteer/VERIFIED_FILE.zip \
  --source-year YYYY
```

ZCTA representative points are approximate user positions **only**; they never become provider office pins. Without a ZIP point, search can return same-ZIP locations with `distance_unavailable`.

## Read-only serving

Set `PROVIDERS_DB` to the validated local path (production default `/data/providers.sqlite`). `GET /api/providers/options` lists services and confirmed plan tiers; `GET /api/providers/search` accepts `zip_code` or `latitude`/`longitude`, scenario/CPT, radius, optional repeated `plan_ids` and `plan_tiers=plan_id|tier`, and remaining deductible. The UI is `/providers`. A missing DB returns `directory_unavailable` without changing the chat estimator. Direct CPT without scenario billing context returns locations but no price. Multi-bill encounters do not turn one NPI's rate into a whole-visit cost.

For rates, the service requires an existing confirmed plan-to-MRF bridge and uses `strict_npi=True`. Missing NPI-specific rates stay unavailable; network medians are never substituted. Network membership, coverage and a given NPI-level rate's applicability at each listed location remain unverified. Negotiated rates are not guaranteed allowed or paid amounts. Patient cost is computed only from a matched, calculable benefit and required user inputs.

The local map uses Leaflet and `tile.openstreetmap.org` with visible attribution. Before any public deployment, review the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/) for identifiable requests, attribution, caching, and traffic limits; use an appropriate alternative tile service if expected traffic exceeds community-server capacity. Do not bulk-download or prefetch tiles.

## Deployment runbook — do not execute without user approval

1. Check the actual Railway project/service, `rates-data` volume mounted at `/data`, and available space. Never overwrite, rebuild or alter `/data/rates.sqlite` or the committed policy DB.
2. Build and validate `providers.sqlite` **outside production**. Review match rate, missing addresses, geocode quality, `PRAGMA integrity_check`, and `PRAGMA foreign_key_check`. For refreshes, prepare a new validated file, keep a recoverable prior copy, and swap atomically only after approval.
3. Only after separate approval, verify current [Railway volume CLI syntax](https://docs.railway.com/cli/volume) and upload the new file to the existing volume (for example `railway volume files --volume rates-data upload ./data/providers/providers.sqlite /providers.sqlite`). This task does **not** perform that upload or set Railway variables.
4. Only after approval and file validation, deploy application code with `PROVIDERS_DB=/data/providers.sqlite`. Smoke-test a real NPI and strictly NPI-specific rate if one exists, and independently verify plan participation before claiming a provider is in-network.
