"""Pages: which tool results can be shown, what show_page accepts, and the doctor page Python builds (no LLM)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from cards import doctors_page, render  # noqa: E402
from guards import ConversationGuard  # noqa: E402
from harness.skills.pages.skill import _validate, missing, named, show, store_result  # noqa: E402


def provider(npi, name, distance, cost):
    return {"npi": npi, "name": name, "address": "1 Main St, Los Angeles, CA 90024", "phone": "3105550100",
            "distance_miles": distance, "latitude": 34.06, "longitude": -118.44,
            "map_destination": "https://www.openstreetmap.org/directions?to=34.06,-118.44",
            "prices": {"uc_ship": {"estimate_status": "estimated", "patient_cost": cost, "negotiated_rate": 120.0}}}


DOCTORS = {"status": "ok", "total": 3, "user_position": {"latitude": 34.07, "longitude": -118.44, "zip_code": "90024"},
           "plans": [{"plan_id": "uc_ship", "name": "UC SHIP"}],
           "providers": [provider("111", "Dr. Near", 0.5, 30.0), provider("111", "Dr. Near", 2.0, 30.0),
                         provider("222", "Dr. Cheap", 1.2, 20.0), provider("333", "Dr. Far", 4.0, 45.0)],
           "message": "ok"}
ARGS = {"zip_code": "90024", "scenario": "specialist_followup", "specialty": "pulmonology"}


def call(result_id, highlight=(), notes=(), title=None):
    return {"result_id": result_id, "highlight": list(highlight),
            "notes": [{"id": i, "text": t} for i, t in notes], "title": title}


class StoreTest(unittest.TestCase):
    def test_only_showable_results_get_an_id_numbered_per_kind(self):
        results = {}
        first, second = dict(DOCTORS), dict(DOCTORS)
        self.assertEqual(store_result(results, "find_providers", ARGS, first), "doctors_1")
        self.assertEqual(first["result_id"], "doctors_1")  # the model sees it
        self.assertEqual(store_result(results, "find_providers", ARGS, second), "doctors_2")
        self.assertIsNone(store_result(results, "find_providers", ARGS, {"status": "ok", "providers": []}))
        self.assertIsNone(store_result(results, "compare_plans", {}, {"plans": [], "auto_ask": ["age"]}))
        self.assertIsNone(store_result(results, "estimate_cost", {}, {"status": "needs_information"}))
        self.assertEqual(store_result(results, "estimate_cost", {}, {"status": "estimated"}), "estimate_1")
        self.assertIsNone(store_result(results, "list_plans", {}, {"plans": []}))

    def test_required_pages_until_shown(self):
        results = {}
        doctors = store_result(results, "find_providers", ARGS, dict(DOCTORS))
        estimate = store_result(results, "estimate_cost", {}, {"status": "estimated"})
        self.assertEqual(missing(results, [doctors, estimate]), [doctors])  # a single estimate may stay in text
        show(results, call(doctors))
        self.assertEqual(missing(results, [doctors, estimate]), [])


    def test_a_reply_naming_doctors_from_an_earlier_search_needs_their_page(self):
        results = {}
        store_result(results, "find_providers", ARGS, dict(DOCTORS))
        latest = store_result(results, "find_providers", ARGS, dict(DOCTORS))
        self.assertEqual(named(results, "Your nearest is DR. CHEAP, 1.2 mi away.", []), [latest])
        self.assertEqual(named(results, "Your nearest is Dr. Cheap.", [latest]), [])  # already shown this turn
        self.assertEqual(named(results, "A preventive visit is free.", []), [])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.results, self.guard = {}, ConversationGuard(confirm=None)
        self.id = store_result(self.results, "find_providers", ARGS, dict(DOCTORS))
        self.guard.post_tool("find_providers", ARGS, DOCTORS, None)

    def check(self, args):
        _validate(self.results, self.guard, args)

    def test_good_picks_pass(self):
        self.check(call(self.id, ["222", "111"], [("222", "Cheapest nearby: you pay $20."), ("111", "Closest.")]))

    def test_bad_arguments_are_refused(self):
        estimate = store_result(self.results, "estimate_cost", {}, {"status": "estimated"})
        for args in [call("doctors_9"), call(self.id, ["999"]), call(self.id, ["111", "222", "333", "4", "5", "6"]),
                     call(self.id, ["111"], [("222", "not picked")]), call(self.id, ["111"], [("111", "x" * 201)]),
                     call(self.id, ["111"], [("111", "Only $5 here.")]),  # an amount from nowhere
                     call(estimate, ["111"]), call(self.id, title="t" * 81)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.check(args)


class DoctorsPageTest(unittest.TestCase):
    def test_picks_first_with_reasons_then_the_rest_by_distance_one_card_per_doctor(self):
        stored = {"kind": "doctors", "args": ARGS, "result": DOCTORS}
        page = doctors_page("doctors_1", stored, ["222"], {"222": "Cheapest."}, None)
        self.assertEqual([d["npi"] for d in page["doctors"]], ["222", "111", "333"])
        self.assertEqual((page["doctors"][0]["pick"], page["doctors"][0]["note"]), (1, "Cheapest."))
        self.assertEqual(page["doctors"][1]["distance_miles"], 0.5)  # the nearest location of Dr. Near
        self.assertEqual(page["doctors"][0]["prices"]["uc_ship"]["patient_cost"], 20.0)
        self.assertEqual(page["title"], "Pulmonology near 90024")
        self.assertEqual(page["user_position"], {"lat": 34.07, "lng": -118.44, "label": "ZIP 90024"})

    def test_render_picks_the_event_by_kind(self):
        event, data = render("doctors", "doctors_1", {"kind": "doctors", "args": ARGS, "result": DOCTORS})
        self.assertEqual((event, data["page_id"]), ("page", "doctors_1"))


if __name__ == "__main__":
    unittest.main()
