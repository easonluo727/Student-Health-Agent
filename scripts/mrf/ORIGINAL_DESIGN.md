# MRF Rate Lookup Workflow

This workflow finds and queries insurer-published in-network negotiated rates without loading an entire Machine-Readable File (MRF) into memory.

## Data flow

```text
User service description
        ↓
Service-to-code mapping (CPT/HCPCS)
        ↓
Plan, network, provider, region, date, and setting matching
        ↓
Official insurer MRF table of contents
        ↓
Selected in-network MRF (.json or .json.gz)
        ↓
Streaming parser: keep only requested CPT/HCPCS codes
        ↓
SQLite: rates + provider groups + source metadata
        ↓
Provider-specific rate, or labeled network median fallback
```

## Main files

| File | Responsibility |
|---|---|
| `data/plan_profiles.v1.json` | Stores plan/network identifiers and the official MRF index URL. |
| `branch_b.py` | Matches the plan/provider and reads the small MRF table-of-contents document. |
| `import_mrf_rates.py` | Command-line entry point for importing a selected large MRF. |
| `mrf_rate_store.py` | Streams MRF records, filters codes, writes SQLite, and performs rate lookups. |
| `agent_amd_two_branches.py` | Exposes mapping, plan matching, MRF metadata, and rate lookup tools to the AMD-hosted agent. |
| `local_data/mrf_rates.sqlite3` | Local filtered rate database. This file is ignored by Git. |

## MRF structure

The importer connects two separate sections through `provider_group_id`:

```text
in_network
  └── billing_code
      └── negotiated_rates
          ├── provider_references: [group_id]
          └── negotiated_prices

provider_references
  └── provider_group_id
      └── provider_groups
          ├── NPI
          └── TIN
```

Only requested CPT/HCPCS codes are retained. The complete MRF is not stored in SQLite.

## SQLite tables

- `mrf_sources`: source URL, plan, network, region, and MRF metadata.
- `negotiated_rates`: billing code, provider group, negotiated amount, expiration date, billing class, and place-of-service codes.
- `provider_members`: provider group, NPI, hashed TIN, TIN last four digits, and TIN type.

Raw TIN values are not stored.

## Import command

Run from `workflow/`:

```bash
python import_mrf_rates.py \
  --input "/path/to/in-network.json.gz" \
  --source-url "https://official-insurer.example/in-network.json.gz" \
  --plan-key prime_100 \
  --network "UnitedHealthcare Choice Plus" \
  --region CA \
  --rate-scope network_regional_estimate \
  --code 99213
```

Repeat `--code` to import more codes. If no code is supplied, the importer uses `data/service_code_catalog.v1.json`.

## Lookup behavior

The lookup matches:

```text
plan + billing code + region + service date + billing setting + optional NPI/TIN
```

- With a provider: returns matching provider rate candidates.
- Without a provider: returns the median of matching stored rates and explicitly labels it as a default network estimate.
- No match: returns `rate_unavailable`; it does not invent a price.

## Current status and limitation

The current local database contains the schema but no imported rate rows.

The importer currently commits only after the entire MRF finishes. If the process is interrupted, SQLite rolls back the import and loses partial progress. Before processing production-size MRFs, add periodic commits, checkpoints, resumable imports, and import-run status tracking.

MRF negotiated rates are separate from the Medicare benchmark catalog in `data/la_service_price_benchmarks.v1.json`. Medicare benchmarks must not be presented as commercial insurer negotiated rates.
