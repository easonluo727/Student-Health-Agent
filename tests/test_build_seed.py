import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "policy"))
import build_seed  # noqa: E402

PLAN = ("plan_id,insurer,plan_name,plan_type,deductible_in_usd,verified\n"
        "p,Co,Plan,ppo,\"$1,000\",yes\n")
HEAD = ("plan_id,section,service,service_as_printed,component,network,network_tier,copay_usd,coinsurance_pct,"
        "deductible_applies,prior_auth,covered,limits_conditions,quote,verified\n")
OFFICE = "If you visit a health care provider's office or clinic"


def run(benefits, plan=PLAN, sources=None, networks=None, premiums=None):
    """Build from one plan folder; return (exit code, plans, benefits keyed by name+network, plan_networks)."""
    src = Path(tempfile.mkdtemp())
    (src / "extracted" / "p").mkdir(parents=True)
    (src / "extracted" / "p" / "plans.csv").write_text(plan)
    (src / "extracted" / "p" / "benefits.csv").write_text(HEAD + benefits)
    if sources:
        (src / "sources.csv").write_text(sources)
    if networks:
        (src / "plan_networks.csv").write_text(networks)
    if premiums:
        (src / "premiums.csv").write_text(premiums)
    with redirect_stdout(io.StringIO()):
        code = build_seed.main(src, src / "policy.sqlite")
    con = sqlite3.connect(src / "policy.sqlite")
    con.row_factory = sqlite3.Row
    plans, rows, links = ([dict(r) for r in con.execute(f"SELECT * FROM {t}")]
                          for t in ("plans", "benefits", "plan_networks"))
    run.premiums = [dict(r) for r in con.execute("SELECT * FROM premiums")]
    con.close()
    return code, plans, {r["service_as_printed"] + r["network"]: r for r in rows}, links


class BuildSeed(unittest.TestCase):
    def test_normalizes_and_classifies(self):
        code, plans, rows, _ = run(
            f"p,{OFFICE},primary_care_visit,PCP,,in,UC Family,$25,NOT FOUND,no,NOT FOUND,yes,none,$25/visit,yes\n"
            "p,If you have outpatient surgery,other,Facility fee,asc,in,,NOT FOUND,20%,yes,,yes,,20% for ASC,yes\n"
            "p,Important Questions,plan_level,Overall deductible,,in,,400,,,,yes,,$400/person,\n")
        self.assertEqual(code, 0)
        self.assertEqual((plans[0]["plan_type"], plans[0]["deductible_in_usd"]), ("PPO", 1000))
        pcp = rows["PCPin"]
        self.assertEqual((pcp["category"], pcp["copay_usd"], pcp["coinsurance_pct"], pcp["limits_conditions"]),
                         ("office_visits", 25, None, None))
        self.assertEqual((pcp["deductible_applies"], pcp["calculable"], pcp["review_flags"]), (0, 1, ""))
        fee = rows["Facility feein"]
        self.assertEqual((fee["component"], fee["setting"], fee["category"]), (None, "ASC", "outpatient_surgery"))
        self.assertEqual((rows["Overall deductiblein"]["category"], rows["Overall deductiblein"]["review_flags"]),
                         ("plan_level", "unverified"))

    def test_out_of_network_notes_flag_only_out_of_network_rows(self):
        note = '"An additional 25% penalty is assessed by a Non-Contracting Hospital."'
        _, _, rows, _ = run(f"p,{OFFICE},specialist_visit,Visit,,in,,40,,no,,yes,{note},$40,yes\n"
                            f"p,{OFFICE},specialist_visit,Visit,,out,,,50,yes,,yes,{note},50%,yes\n"
                            f"p,{OFFICE},other,Drugs,,in,,,10,no,,yes,,\"10% up to a maximum of $250\",yes\n"
                            f"p,{OFFICE},other,Therapy,,in,,50,,no,,yes,,\"$50 copay; facility 30% up to $500\",yes\n")
        self.assertEqual(rows["Visitin"]["review_flags"], "")
        self.assertEqual(rows["Visitout"]["review_flags"], "penalty")
        self.assertEqual(rows["Drugsin"]["review_flags"], "cap")
        self.assertEqual(rows["Therapyin"]["review_flags"], "")  # the cap is another component's

    def test_missing_values_block_calculation(self):
        _, _, rows, _ = run(f"p,{OFFICE},urgent_care,UC,,in,,NOT FOUND,NOT FOUND,NOT FOUND,,NOT FOUND,,?,yes\n")
        self.assertEqual(rows["UCin"]["review_flags"], "coverage_unknown,missing_cost,missing_deductible")
        self.assertEqual(rows["UCin"]["calculable"], 0)

    def test_verified_by_in_sources_verifies_the_whole_plan_unless_a_row_opts_out(self):
        unverified_plan = "plan_id,insurer,plan_name,verified\np,Co,Plan,\n"
        rows_csv = (f"p,{OFFICE},primary_care_visit,PCP,,in,,25,,no,,yes,,$25,\n"
                    f"p,{OFFICE},specialist_visit,Spec,,in,,40,,no,,yes,,$40,no\n")
        _, plans, rows, _ = run(rows_csv, plan=unverified_plan)
        self.assertEqual((plans[0]["verified"], rows["PCPin"]["calculable"]), (0, 0))
        _, plans, rows, _ = run(rows_csv, plan=unverified_plan, sources="plan_id,verified_by\np,team\n")
        self.assertEqual((plans[0]["verified"], rows["PCPin"]["calculable"]), (1, 1))
        self.assertEqual(rows["Specin"]["review_flags"], "unverified")

    def test_plan_networks_need_an_existing_in_network_tier(self):
        code, _, _, links = run(f"p,{OFFICE},primary_care_visit,PCP,,in,UC Family,25,,no,,yes,,$25,yes\n",
                                networks="plan_id,network_tier,mrf_plan_key,mrf_network,notes\n"
                                         "p,UC Family,anthem_ppo,CA PPO,booklet\n"
                                         "p,Ashe Center,anthem_ppo,CA PPO,\n"
                                         "later_plan,,lacare_covered,,\n")
        self.assertEqual(code, 1)
        self.assertEqual([(r["network_tier"], r["mrf_plan_key"]) for r in links], [("UC Family", "anthem_ppo")])

    def test_premiums_need_a_source_and_a_built_plan(self):
        head = "plan_id,rating_region,age,enrollee,period,amount_usd,annual_usd,basis,source_url,source_page,quote\n"
        code, _, _, _ = run(f"p,{OFFICE},xray,Test,,in,,10,,no,,yes,,x,yes\n", premiums=head
                            + "p,16,40,,month,$367,4404,booklet,https://x,25,L.A. Care HMO* $321 - $367\n"
                            + "p,15,40,,month,347,4164,booklet,https://x,23,\n"
                            + "later_plan,,,,quarter,10,30,cal,https://x,1,q\n")
        self.assertEqual(code, 1)  # the row without a quote
        self.assertEqual([(r["rating_region"], r["amount_usd"]) for r in run.premiums], [(16, 367)])

    def test_bad_rows_skipped_and_duplicates_skip_the_plan(self):
        code, _, rows, _ = run(f"p,{OFFICE},dental,Teeth,,in,,0,,no,,yes,,x,yes\n"
                               f"p,{OFFICE},xray,Test,,in,,10,,no,,yes,,x,yes\n")
        self.assertEqual((code, list(rows)), (1, ["Testin"]))
        code, plans, rows, _ = run(f"p,{OFFICE},xray,Test,,in,,10,,no,,yes,,x,yes\n"
                                   f"p,{OFFICE},xray,Test,,in,,20,,no,,yes,,x,yes\n"
                                   f"p,{OFFICE},lab_tests,Test,,in,,5,,no,,yes,,x,yes\n")
        self.assertEqual((code, plans, rows), (1, [], {}))


if __name__ == "__main__":
    unittest.main()
