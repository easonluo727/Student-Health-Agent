"""Smoke tests on the local MRF SQLite database (gitignored; skipped when absent). Read-only."""
import dataclasses
import datetime as dt
import shutil
import tempfile
import unittest
from pathlib import Path

import fixtures as fx
import canonical_adapter as ca
from rate_lookup import RateLookup
from schema_check import validate

DB = fx.PATHS.rates_sqlite
PLAN, NET = "anthem_ca_prudent_buyer_ppo", "CA PPO PRUDENT BUYER"


@unittest.skipUnless(DB.exists(), "local MRF database not present")
class RealDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        paths = dataclasses.replace(fx.PATHS, log_dir=cls.tmp)
        cls.lk = RateLookup.from_paths(paths, log_path=cls.tmp / "log.jsonl")
        cls.lk.as_of = dt.date.fromisoformat(cls.lk.db.snapshot(PLAN)[-1])  # snapshot-relative: never ages out
        con = cls.lk.db.con
        row = con.execute("SELECT r.source_id, r.provider_group_id FROM negotiated_rates r JOIN mrf_sources s ON "
                          "s.source_id=r.source_id WHERE s.plan_key=? AND r.billing_code='99213' AND "
                          "r.billing_class='professional' AND r.negotiated_type IN ('negotiated','fee schedule') "
                          "AND r.modifiers='' AND r.negotiated_rate > 0 AND (r.service_codes='' OR "
                          "r.service_codes LIKE '%11%' OR r.service_codes LIKE '%CSTM-00%') LIMIT 1", (PLAN,)).fetchone()
        cls.npi = con.execute("SELECT npi FROM provider_members WHERE source_id=? AND provider_group_id=? LIMIT 1",
                              row).fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def req(self, **kw):
        out = self.lk.lookup(fx.request(insurer=None, plan_key=PLAN, network=NET, service_date=None, **kw))
        validate(out, "rate_lookup_result")
        return out

    def test_schema_binds_without_aliases_missing(self):
        b = ca.bind_sqlite(self.lk.db.con)
        self.assertEqual(b["provider_npi"].column, "npi")
        self.assertEqual(b["place_of_service"].column, "service_codes")
        self.assertEqual(b["source_rate_amount"].column, "negotiated_rate")

    def test_npi_joins_to_rates(self):
        out = self.req(billing_code="99213", billing_class="professional", place_of_service="11",
                       selected_provider_npi=self.npi)
        self.assertEqual((out["status"], out["exact_provider_rate_found"]), ("provider_rate", True))
        self.assertGreater(out["allowed_amount"], 0)

    def test_aggregate_a_and_b(self):
        a = self.req(encounter_key="urgent_care_flu")
        b = self.req(billing_code="99203", billing_class="professional", place_of_service="11")
        for out in (a, b):
            self.assertEqual((out["status"], out["estimate_scope"]), ("network_estimate", "network_aggregate"))
            self.assertIsNotNone(out["allowed_amount"])

    def test_no_placeholder_reaches_a_result(self):
        out = self.lk.lookup(fx.request(insurer=None, plan_key="lacare_covered", network=None, billing_code="80053",
                                        billing_class="professional", place_of_service="81", service_date=None))
        for k in ("allowed_amount", "p10", "p90"):
            self.assertTrue(out[k] is None or out[k] < ca.PLACEHOLDER_MIN, k)


if __name__ == "__main__":
    unittest.main()
