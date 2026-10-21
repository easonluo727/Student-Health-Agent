-- SQLite schema of the policy database (data/plans/policy.sqlite), built by scripts/policy/build_seed.py.
-- The allowed-amount database is a separate SQLite file (data/mrf/rates.sqlite; on Railway /data/rates.sqlite),
-- schema in scripts/mrf/mrf_rate_store.py. Python joins them through plan_networks.plan_key -> mrf_sources.plan_key.

CREATE TABLE plans (
    plan_id            TEXT PRIMARY KEY,
    insurer            TEXT NOT NULL,
    plan_name          TEXT NOT NULL,
    plan_year          TEXT,
    plan_type          TEXT CHECK (plan_type IN ('HMO', 'PPO', 'EPO', 'POS')),
    network_name       TEXT,
    deductible_in_usd  REAL CHECK (deductible_in_usd >= 0),   -- cheapest in-network tier
    deductible_out_usd REAL CHECK (deductible_out_usd >= 0),
    oop_max_in_usd     REAL CHECK (oop_max_in_usd >= 0),
    oop_max_out_usd    REAL CHECK (oop_max_out_usd >= 0),
    coinsurance_in_pct REAL CHECK (coinsurance_in_pct BETWEEN 0 AND 100),
    source_url         TEXT,
    verified           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE benefits (
    benefit_id         TEXT PRIMARY KEY,          -- stable hash of the natural key
    plan_id            TEXT NOT NULL REFERENCES plans(plan_id),
    category           TEXT NOT NULL,             -- office_visits, tests, drugs, ... (from the SBC section)
    section            TEXT,                      -- the SBC "Common Medical Event", verbatim
    service            TEXT NOT NULL CHECK (service IN (
                           'primary_care_visit', 'specialist_visit', 'urgent_care', 'emergency_room', 'ambulance',
                           'lab_tests', 'xray', 'advanced_imaging', 'generic_drugs', 'preferred_brand_drugs',
                           'mental_health_outpatient', 'preventive_care', 'plan_level', 'other')),
    service_as_printed TEXT NOT NULL,
    component          TEXT CHECK (component IN ('office_visit', 'facility', 'provider')),
    setting            TEXT,                      -- e.g. ASC, when one cell prices several places of service
    network            TEXT NOT NULL CHECK (network IN ('in', 'out')),
    network_tier       TEXT,                      -- e.g. Ashe Center, UC Family, Network; NULL = the only level
    copay_usd          REAL CHECK (copay_usd >= 0),
    coinsurance_pct    REAL CHECK (coinsurance_pct BETWEEN 0 AND 100),   -- patient share
    deductible_applies INTEGER,                   -- 1/0, NULL = not stated
    prior_auth         INTEGER,
    covered            INTEGER,
    limits_conditions  TEXT,
    quote              TEXT,
    source_url         TEXT,
    source_page        INTEGER,
    verified           INTEGER NOT NULL DEFAULT 0,
    review_flags       TEXT NOT NULL DEFAULT '',   -- comma-separated: why the calculator must not use the row as-is
    calculable         INTEGER NOT NULL DEFAULT 0  -- verified AND no review_flags
);
CREATE INDEX ix_benefits_lookup ON benefits (plan_id, service, network, network_tier);
CREATE INDEX ix_benefits_category ON benefits (category, service);

-- The bridge between the two databases: which MRF rates price each in-network tier of a plan.
-- A tier with no row (e.g. a campus clinic, or out-of-network) has no allowed amounts.
CREATE TABLE plan_networks (
    plan_id      TEXT NOT NULL REFERENCES plans(plan_id),
    network_tier TEXT NOT NULL DEFAULT '',        -- benefits.network_tier; '' = the plan's only in-network level
    mrf_plan_key TEXT NOT NULL,                   -- mrf_sources.plan_key (data/plan_profiles.v1.json)
    mrf_network  TEXT,                            -- mrf_sources.network; NULL = every network of that plan_key
    notes        TEXT,                            -- evidence for the mapping
    confirmed    INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),  -- 1 = the evidence names this network;
                                                  -- prices are only used for confirmed links
    PRIMARY KEY (plan_id, network_tier)
);
