import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "mrf"))

from parse import InNetworkParser, hash_tin  # noqa: E402
from toc import scan_toc  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "mrf")
V2 = os.path.join(FIX, "cms_v2")
V1 = os.path.join(FIX, "cms_v1_3_3")


def _canon(out):
    return sorted(json.dumps([k, o if k == "rate" else o.__dict__], sort_keys=True, default=lambda x: x.__dict__)
                  for k, o in out)


def run(src, codes, networks=None, npis=None, **kw):
    """Parse with the byte-level fast path and with the ijson path; both must agree."""
    p = InNetworkParser(codes, networks, npis, **kw)
    out = list(p.parse(src))
    slow = InNetworkParser(codes, networks, npis, fast=False, **{k: v for k, v in kw.items() if k != "opener"})
    assert _canon(out) == _canon(list(slow.parse(src))), "fast path and ijson path disagree"
    groups = {g.provider_group_id: g for k, g in out if k == "group"}
    rates = [r for k, r in out if k == "rate"]
    return p, groups, rates


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, name, obj, gz=False, key_order=None):
        if key_order:
            obj = {k: obj[k] for k in key_order if k in obj} | {k: v for k, v in obj.items() if k not in key_order}
        data = json.dumps(obj).encode()
        path = os.path.join(self.tmp, name)
        with (gzip.open(path, "wb") if gz else open(path, "wb")) as f:
            f.write(data)
        return path


def load(path):
    with open(path) as f:
        return json.load(f)


class InlineReferences(unittest.TestCase):
    src = os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json")

    def test_network_and_npi_filter(self):
        p, groups, rates = run(self.src, {"99214", "27447"}, ["Comprehensive Health Network"], {"1234567890"})
        self.assertEqual(set(groups), {"1"})
        self.assertEqual([m.npi for m in groups["1"].members], ["1234567890"])  # only matching NPIs kept
        # 27447 references groups 1 and 2; group 2 is another network, so only group-1 rows remain.
        self.assertEqual(sorted((r["billing_code"], r["negotiated_rate"]) for r in rates),
                         [("27447", 8500.0), ("27447", 12000.0), ("99214", 150.0)])
        self.assertEqual(p.stats.first_section, "provider_references")
        self.assertEqual(p.stats.provider_refs_seen, 2)
        self.assertEqual(p.stats.provider_refs_needed, 2)   # both groups are referenced by 99214/27447
        self.assertEqual(p.stats.provider_refs_kept, 1)
        self.assertTrue(p.stats.fast_path_rates and p.stats.fast_path_groups)
        self.assertEqual(p.header["version"], "2.0.0")

    def test_network_match_is_exact_not_substring(self):
        # "Comprehensive Health Network" must not match "Comprehensive Health Plus Network" and vice versa.
        _, groups, _ = run(self.src, {"27447"}, ["comprehensive health plus network"])
        self.assertEqual(set(groups), {"2"})

    def test_npi_outside_set_drops_group(self):
        _, groups, rates = run(self.src, {"99214"}, None, {"0000000001"})
        self.assertEqual((groups, rates), ({}, []))

    def test_no_raw_tin_anywhere(self):
        raw_tins = {pg["tin"]["value"] for r in load(self.src)["provider_references"] for pg in r["provider_groups"]}
        _, groups, rates = run(self.src, {"99214", "27447", "99285"})
        blob = json.dumps([g.__dict__ for g in groups.values()], default=lambda o: o.__dict__) + json.dumps(rates)
        for tin in raw_tins:
            self.assertNotIn(tin, blob)
        m = groups["1"].members[0]
        self.assertEqual(len(m.tin_sha256), 64)
        self.assertEqual(len(m.tin_last4), 4)

    def test_hash_tin_ignores_formatting(self):
        self.assertEqual(hash_tin({"type": "ein", "value": "12-3456789"})[1],
                         hash_tin({"type": "ein", "value": "123456789"})[1])


class NegotiatedTypesAndCodeTypes(unittest.TestCase):
    src = os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json")

    def test_all_negotiated_type_variants_pass_through(self):
        # Selection (excluding percentage / per diem) happens downstream; the parser keeps every type.
        _, _, rates = run(self.src, {"99214", "97110", "80053", "27447", "99285", "0200"},
                          code_types={"CPT", "RC"})
        self.assertEqual({r["negotiated_type"] for r in rates},
                         {"negotiated", "derived", "fee schedule", "percentage", "per diem"})
        pct = [r for r in rates if r["negotiated_type"] == "percentage" and r["billing_code"] == "99285"]
        self.assertEqual(pct[0]["negotiated_rate"], 75.5)
        self.assertEqual(pct[0]["service_code"], ["23"])

    def test_billing_code_type_filter(self):
        # RC 0200 is skipped under the default CPT/HCPCS filter even if "0200" is requested.
        p, _, rates = run(self.src, {"0200", "99214"})
        self.assertEqual({r["billing_code"] for r in rates}, {"99214"})
        self.assertEqual(p.stats.skipped_code_type, 1)
        _, _, rates = run(self.src, {"0200"}, code_types={"RC"})
        self.assertEqual([(r["negotiated_type"], r["billing_class"]) for r in rates], [("per diem", "institutional")])

    def test_multiple_negotiated_prices_per_code(self):
        src = os.path.join(V2, "in-network-rates-multiple-plans-sample.json")
        p, _, rates = run(src, {"27447"}, ["ACME Choice Provider Group"])
        # Two negotiated_rates objects for 27447, each with two prices -> four rows for group 1.
        self.assertEqual(p.stats.negotiated_rates_matched, 2)
        self.assertEqual(len(rates), 4)
        self.assertEqual(sorted({(r["billing_class"], r["negotiated_type"]) for r in rates}),
                         [("both", "derived"), ("institutional", "negotiated"), ("professional", "negotiated")])
        mod = [r for r in rates if r["billing_code_modifier"]]
        self.assertEqual(mod[0]["billing_code_modifier"], ["AS"])
        self.assertEqual(mod[0]["expiration_date"], "2022-01-01")


class ReferencesAfterInNetwork(TmpDirCase):
    def test_spill_and_replay_matches_normal_order(self):
        doc = load(os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json"))
        order = ["reporting_entity_name", "version", "in_network", "provider_references"]
        path = self.write("after.json.gz", doc, gz=True, key_order=order)
        p, groups, rates = run(path, {"99214", "27447", "99285"}, ["Comprehensive Health Network"])
        _, _, expected = run(os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json"),
                             {"99214", "27447", "99285"}, ["Comprehensive Health Network"])
        self.assertEqual(p.stats.first_section, "in_network")
        self.assertTrue(p.stats.fast_path_rates and p.stats.fast_path_groups)
        key = lambda r: json.dumps(r, sort_keys=True)  # noqa: E731
        self.assertEqual(sorted(map(key, rates)), sorted(map(key, expected)))
        self.assertEqual(set(groups), {"1"})
        self.assertTrue(p.stats.compressed_bytes < p.stats.decompressed_bytes)  # gzip detected

    def test_billing_code_after_negotiated_rates(self):
        doc = load(os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json"))
        doc["in_network"] = [{k: i[k] for k in ("negotiated_rates", "negotiation_arrangement", "name",
                                                 "billing_code_type", "billing_code_type_version",
                                                 "billing_code", "description") if k in i}
                             for i in doc["in_network"]]
        _, _, rates = run(self.write("late_code.json", doc), {"99214"})
        self.assertEqual([r["negotiated_rate"] for r in rates], [150.0])


class ExternalReferences(TmpDirCase):
    def test_schema_1_external_location_and_inline_groups(self):
        doc = load(os.path.join(V1, "in-network-rates-fee-for-service-single-plan-sample.json"))
        shutil.copy(os.path.join(V1, "provider-reference.json"), self.tmp)
        # Point group 2 at the local provider-reference sample and make a 27448 rate use it.
        doc["provider_references"][1]["location"] = "provider-reference.json"
        doc["in_network"][1]["negotiated_rates"][0]["provider_references"] = [1, 2]
        path = self.write("v1.json", doc)
        fetched = []

        def opener(src):
            from stream import open_source
            fetched.append(os.path.basename(src))
            return open_source(src)

        p, groups, rates = run(path, {"27448"}, None, {"1111111111", "2222222222", "6666666666"}, opener=opener)
        self.assertEqual(groups["2"].source, "external_reference")
        self.assertEqual({m.npi for m in groups["2"].members}, {"1111111111", "2222222222"})
        self.assertEqual(p.stats.external_refs_fetched, 1)
        # header scan + pass 1 + pass 2 over the MRF, then the one external file that is needed
        self.assertEqual(fetched, ["v1.json", "v1.json", "v1.json", "provider-reference.json"])
        # 12003.45 is shared by groups 1 and 2; 12.45 comes from an inline provider_groups object.
        self.assertEqual(sorted((r["provider_group_id"][:7], r["negotiated_rate"]) for r in rates),
                         [("1", 12003.45), ("2", 12003.45), ("inline:", 12.45)])

    def test_external_reference_not_fetched_when_unused(self):
        doc = load(os.path.join(V1, "in-network-rates-fee-for-service-single-plan-sample.json"))
        doc["provider_references"][1]["location"] = "does-not-exist.json"
        p, _, rates = run(self.write("v1.json", doc), {"27448"})
        self.assertEqual(p.stats.external_refs_fetched, 0)
        self.assertEqual(len(rates), 2)

    def test_inline_groups_dropped_when_network_filter_set(self):
        src = os.path.join(V1, "in-network-rates-fee-for-service-single-plan-sample.json")
        p, _, rates = run(src, {"27447"}, ["Any Network"])
        self.assertEqual(rates, [])
        self.assertEqual(p.stats.inline_groups_dropped_no_network, 2)  # one per negotiated_rates object


class FastPathLayouts(TmpDirCase):
    src = os.path.join(V2, "in-network-rates-all-negotiated-types-sample.json")

    def test_pretty_printed_json(self):
        doc = load(self.src)
        path = os.path.join(self.tmp, "pretty.json")
        with open(path, "w") as f:
            json.dump(doc, f, indent=2)
        p, _, rates = run(path, {"99214", "27447"}, ["Comprehensive Health Network"])
        self.assertTrue(p.stats.fast_path_rates)
        self.assertEqual(len(rates), 3)

    def test_unsafe_first_key_falls_back(self):
        doc = load(self.src)
        # billing_code first: nested bundled_codes objects could also start with it -> no fast path
        doc["in_network"] = [{"billing_code": i["billing_code"], **{k: v for k, v in i.items() if k != "billing_code"}}
                             for i in doc["in_network"]]
        p, _, rates = run(self.write("unsafe.json", doc), {"99214"})
        self.assertFalse(p.stats.fast_path_rates)
        self.assertIn("may also occur in nested objects", p.stats.fast_path_note)
        self.assertEqual([r["negotiated_rate"] for r in rates], [150.0])

    def test_code_after_rates_falls_back(self):
        doc = load(self.src)
        doc["in_network"] = [{"negotiation_arrangement": i["negotiation_arrangement"],
                              "negotiated_rates": i["negotiated_rates"] * 40,  # push billing_code past the head
                              **{k: v for k, v in i.items() if k not in ("negotiation_arrangement", "negotiated_rates")}}
                             for i in doc["in_network"]]
        p, _, rates = run(self.write("late.json", doc), {"99214"})
        self.assertFalse(p.stats.fast_path_rates)
        self.assertEqual(len(rates), 40)

    def test_group_id_after_large_provider_groups(self):
        doc = load(self.src)
        big = [{"npi": [1000000000 + i for i in range(600)], "tin": {"type": "ein", "value": "111111111"}}]
        doc["provider_references"] = [
            {"provider_groups": r["provider_groups"] + big, "network_name": r["network_name"],
             "provider_group_id": r["provider_group_id"]} for r in doc["provider_references"]]
        p, groups, rates = run(self.write("gid_late.json", doc), {"99214"}, ["Comprehensive Health Network"],
                               {"1234567890"})
        self.assertTrue(p.stats.fast_path_groups)
        self.assertGreater(p.stats.refs_parsed_without_head_gid, 0)
        self.assertEqual(set(groups), {"1"})
        self.assertEqual(len(rates), 1)

    def test_chunk_boundaries(self):
        import fastscan
        import stream
        old = (stream.CHUNK, fastscan.CHUNK)
        stream.CHUNK = fastscan.CHUNK = 7  # every marker and head straddles reads
        try:
            p, _, rates = run(self.src, {"99214", "27447", "99285"}, ["Comprehensive Health Network"])
        finally:
            stream.CHUNK, fastscan.CHUNK = old
        self.assertTrue(p.stats.fast_path_rates and p.stats.fast_path_groups)
        self.assertEqual(len(rates), 3)


class TableOfContents(TmpDirCase):
    def test_scan_and_dedupe(self):
        doc = load(os.path.join(V2, "table-of-contents-sample.json"))
        doc["reporting_structure"].append(doc["reporting_structure"][0])  # same URLs again
        path = self.write("toc.json.gz", doc, gz=True)
        r = scan_toc(path, lambda loc, d: "chip" not in loc)
        self.assertEqual(r.reporting_structures, 3)
        self.assertEqual(r.in_network_refs, 5)
        self.assertEqual(len(r.files), 2)
        f = r.files["https://www.some_site.com/files/in-network-file-123456.json"]
        self.assertEqual(f.n_plans, 4)
        self.assertEqual(r.header["version"], "2.0.0")
        self.assertTrue(r.complete)

    def test_every_plan_of_a_market_with_its_files(self):
        path = self.write("toc.json.gz", load(os.path.join(V2, "table-of-contents-sample.json")), gz=True)
        r = scan_toc(path, lambda loc, d: "behavioral" not in loc, market_types={"individual"})
        files = {key[1]: locs for key, locs in r.plans.items()}
        # Both individual plans use the kept in-network file; the group (chip) plan is left out.
        self.assertEqual(files, {"1111111111": {"https://www.some_site.com/files/in-network-file-123456.json"},
                                 "000000000": {"https://www.some_site.com/files/in-network-file-123456.json"}})


if __name__ == "__main__":
    unittest.main()
