"""Smoke tests on the repository's real rate CSV, plus proof that the original files are untouched."""
import copy
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixtures as fx
from rate_resolver import CsvRates, Mappings, RateResolver, SqliteRates, load_encounters, load_label_map

ROOT = fx.PATHS.repo_root
BASELINE = fx.OVERLAY.parent / "baseline" / "manifest.json"
PROTECTED = ["workflow", "scripts", "data", "tests", "archive", "AGENTS.md", "CLAUDE.md", "README.md", "PRODUCT.md",
             "requirements.txt", ".gitignore", "docs", "MRF_pipeline"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(fx.PATHS.rates_csv.exists(), "repository rate CSV not present")
class RealData(unittest.TestCase):
    def setUp(self):
        p = fx.PATHS
        self.enc = load_encounters(p.encounters_csv)
        self.labels = load_label_map(p.overlay_data / "encounter_labels.v1.json", self.enc)
        self.csv = CsvRates(p.rates_csv)
        with open(p.overlay_data / "service_rate_map.v1.json") as f:
            self.smap = json.load(f)
        with open(p.plan_profiles) as f:
            self.profiles = json.load(f)["plans"]
        # snapshot date of the committed CSV, so the test does not age out
        with open(p.rates_csv, newline="") as f:
            dates = [d.strip() for r in csv.DictReader(f) for d in r["mrf_file_date"].split(",") if d.strip()]
        import datetime as dt
        self.as_of = dt.date.fromisoformat(max(dates))

    def resolver(self, plan_key, network, network_scope=None, verified=True):
        smap = copy.deepcopy(self.smap)
        for e in smap["entries"]:
            e["review_status"] = "verified" if verified else e["review_status"]
        pmap = {"schema_version": "policy_mrf_map.v1", "entries": [
            {"policy_plan_id": "prime_100", "policy_network_category": "in-network", "mrf_plan_key": plan_key,
             "mrf_network": network, "network_scope": network_scope, "candidates": [], "question_for_human": None,
             "evidence": ["test only"], "review_status": "verified", "reviewed_by": "test"}]}
        return RateResolver(Mappings(pmap, smap, self.profiles), self.enc, self.labels, self.csv,
                            SqliteRates(Path("/nonexistent.sqlite")), as_of=self.as_of)

    def rule(self, service):
        return {"policy_plan_id": "prime_100", "service": service, "setting": None, "network": "in-network"}

    def csv_a(self, insurer, network, label):
        rows = self.csv.a[(insurer, network, label)]
        self.assertEqual(len(rows), 1)
        return float(rows[0]["allowed_amount"])

    def test_committed_draft_mappings_stay_pending(self):
        r = self.resolver("anthem_ca_prudent_buyer_ppo", "CA PPO PRUDENT BUYER", verified=False)
        q = r.resolve_for_rule(self.rule("consultant physician fees"), option_id="office_established")
        self.assertEqual(q["status"], "mapping_pending_review")
        p = fx.PATHS
        real = Mappings.from_files(p.overlay_data / "policy_mrf_map.v1.json", p.overlay_data / "service_rate_map.v1.json",
                                   p.plan_profiles)
        self.assertEqual(real.plan_target("prime_100", "in-network")[1], "mapping_pending_review")

    def test_anthem_a_and_b_match_the_csv(self):
        r = self.resolver("anthem_ca_prudent_buyer_ppo", "CA PPO PRUDENT BUYER")
        q = r.resolve_for_rule(self.rule("consultant physician fees"), option_id="office_established")
        self.assertEqual(q["allowed_amount"], self.csv_a("Anthem Blue Cross (California)", "CA PPO PRUDENT BUYER",
                                                         "Primary care visit (established patient)"))
        self.assertTrue(q["aggregate"])
        self.assertFalse(q["provider_specific"])
        b = r.resolve_for_rule(self.rule("medical emergency expenses"))
        self.assertTrue(b["usable"], b["message"])
        self.assertEqual(len(b["components"]), 4)
        self.assertEqual(b["allowed_amount"], round(sum(c["allowed_amount"] for c in b["components"]), 2))

    def test_network_without_filter_uses_scope(self):
        r = self.resolver("lacare_covered", None)
        q = r.resolve_for_rule(self.rule("diagnostic x-ray services"), option_id="chest_xray")
        self.assertTrue(q["usable"], q["message"])
        self.assertIsNone(q["mrf_network"])
        self.assertEqual(q["network_scope"], "individual (Covered California)")

    def test_unavailable_cell_stays_unavailable(self):
        # UHC has no facility rates in its California file: the ER component path must refuse a total
        r = self.resolver("uhc_ca_choice_plus", "Choice-Plus")
        q = r.resolve_for_rule(self.rule("medical emergency expenses"))
        self.assertFalse(q["usable"])
        self.assertIsNone(q["allowed_amount"])
        self.assertEqual(q["status"], "rate_unavailable")

    def test_encounter_label_map_is_current(self):
        import build_encounter_labels
        current = json.loads((fx.PATHS.overlay_data / "encounter_labels.v1.json").read_text())
        self.assertEqual(current, build_encounter_labels.build(fx.PATHS))


class OriginalFilesUntouched(unittest.TestCase):
    def test_snapshots_match_originals(self):
        manifest = json.loads(BASELINE.read_text())
        for s in manifest["snapshots"]:
            self.assertEqual(sha(ROOT / s["source_path"]), s["sha256"], s["source_path"])
            self.assertEqual(sha(BASELINE.parent.parent / s["snapshot_path"]), s["sha256"], s["snapshot_path"])
        for r in manifest["referenced_data"] + manifest["referenced_read_only_code"]:
            self.assertEqual(sha(ROOT / r["path"]), r["sha256"], r["path"])

    def test_no_changes_outside_the_overlay_directory(self):
        out = subprocess.run(["git", "status", "--porcelain", "--", *PROTECTED], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "", "files outside branch-a-mrf-integration/ changed")

    def test_original_test_suite_still_passes(self):
        out = subprocess.run([sys.executable, "-m", "unittest", "discover", "tests"], cwd=ROOT, capture_output=True,
                             text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
