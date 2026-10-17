"""Paths and import wiring for the Branch A + MRF overlay. No I/O beyond path checks.

The overlay never copies or edits the original repo. It imports the original modules
(workflow/calculate_cost.py, workflow/agent_tools.py, workflow/guards.py, scripts/mrf/allowed.py)
from their real locations and reads data files by configurable path:

    BRANCH_A_REPO_ROOT       repository root (default: two levels above this overlay)
    BRANCH_A_RATES_CSV       data/mrf/allowed_amounts.csv
    BRANCH_A_ENCOUNTERS_CSV  data/mrf/encounters.csv
    BRANCH_A_PLAN_PROFILES   data/plan_profiles.v1.json
    BRANCH_A_RATES_SQLITE    data/mrf/rates.sqlite (optional; provider-specific lookups need it)
    BRANCH_A_OVERLAY_DATA    overlay/data (policy_mrf_map, service_rate_map, encounter labels)
    BRANCH_A_BILLING_CODES   data/mrf/billing_codes.csv (code types)
    BRANCH_A_NPI_DIRECTORY   data/mrf/la_npis.csv (NPPES locations; optional, needed for nearby estimates)
    BRANCH_A_ZIP_CENTROIDS   overlay/data/zcta_centroids_ca.csv (Census ZCTA points)
    BRANCH_A_LOG_DIR         data/mrf/work/branch_a/logs (JSONL trace logs; gitignored)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

OVERLAY_WORKFLOW = Path(__file__).resolve().parent
OVERLAY_ROOT = OVERLAY_WORKFLOW.parent
DEFAULT_REPO_ROOT = OVERLAY_ROOT.parent.parent


def _path(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class OverlayPaths:
    repo_root: Path
    rates_csv: Path
    encounters_csv: Path
    plan_profiles: Path
    rates_sqlite: Path
    overlay_data: Path
    schemas: Path
    billing_codes: Path | None = None
    npi_directory: Path | None = None
    zip_centroids: Path | None = None
    log_dir: Path | None = None

    @property
    def original_workflow(self) -> Path:
        return self.repo_root / "workflow"

    @property
    def mrf_scripts(self) -> Path:
        return self.repo_root / "scripts" / "mrf"


def load_paths() -> OverlayPaths:
    root = _path("BRANCH_A_REPO_ROOT", DEFAULT_REPO_ROOT)
    return OverlayPaths(
        repo_root=root,
        rates_csv=_path("BRANCH_A_RATES_CSV", root / "data" / "mrf" / "allowed_amounts.csv"),
        encounters_csv=_path("BRANCH_A_ENCOUNTERS_CSV", root / "data" / "mrf" / "encounters.csv"),
        plan_profiles=_path("BRANCH_A_PLAN_PROFILES", root / "data" / "plan_profiles.v1.json"),
        rates_sqlite=_path("BRANCH_A_RATES_SQLITE", root / "data" / "mrf" / "rates.sqlite"),
        overlay_data=_path("BRANCH_A_OVERLAY_DATA", OVERLAY_ROOT / "data"),
        schemas=OVERLAY_ROOT / "schemas",
        billing_codes=_path("BRANCH_A_BILLING_CODES", root / "data" / "mrf" / "billing_codes.csv"),
        npi_directory=_path("BRANCH_A_NPI_DIRECTORY", root / "data" / "mrf" / "la_npis.csv"),
        zip_centroids=_path("BRANCH_A_ZIP_CENTROIDS", OVERLAY_ROOT / "data" / "zcta_centroids_ca.csv"),
        log_dir=_path("BRANCH_A_LOG_DIR", root / "data" / "mrf" / "work" / "branch_a" / "logs"),
    )


def install_import_paths(paths: OverlayPaths | None = None) -> OverlayPaths:
    """Overlay modules first, then the original workflow (read-only imports), then scripts/mrf.
    Overlay module names never collide with original ones, so nothing original is shadowed."""
    paths = paths or load_paths()
    for p in (paths.mrf_scripts, paths.original_workflow, OVERLAY_WORKFLOW):
        s = str(p)
        if s in sys.path:
            sys.path.remove(s)
        sys.path.insert(0, s)
    return paths
