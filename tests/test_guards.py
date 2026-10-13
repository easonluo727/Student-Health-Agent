"""Run: python -m unittest discover tests   (no LLM, no network)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workflow"))

from agent import execute_tool  # noqa: E402
from agent_tools import PolicyTools  # noqa: E402
from guards import ConversationGuard, numbers_in  # noqa: E402

RULES = [
    {
        "plan": "test_plan", "service": "laboratory procedures", "network": "in-network",
        "setting": "outpatient", "copay_usd": None, "plan_pays_percentage": 80,
        "patient_pays_percentage": 20, "deductible_applies": True,
        "coverage_status": "covered", "needs_review": False,
        "source_text": "80% of Allowed Amount after Deductible",
    },
]


def call(name, **args):
    return SimpleNamespace(name=name, arguments=json.dumps(args))


class Confirmer:
    def __init__(self, accept, reply="yes"):
        self.accept, self.reply, self.calls = accept, reply, 0

    def __call__(self, summary):
        self.calls += 1
        self.summary = summary
        return self.accept, self.reply


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(RULES, self.tmp)
        self.tmp.close()
        self.tools = PolicyTools(selected_plan="test_plan", rules_path=self.tmp.name)
        self.rule_id = self.tools.get_catalog()[0]["rule_id"]
        self.inspected = set()

    def tearDown(self):
        Path(self.tmp.name).unlink()

    def run_tool(self, guard, name, **args):
        return execute_tool(call(name, **args), self.tools, self.inspected, guard)

    def lookup(self, guard):
        self.run_tool(guard, "get_policy_rule", rule_id=self.rule_id)

    def estimate(self, guard, allowed, remaining):
        return self.run_tool(guard, "calculate_estimate", rule_id=self.rule_id,
                             allowed_amount=allowed, remaining_deductible=remaining)

    # Hook 1 ---------------------------------------------------------------

    def test_amount_the_user_never_said_is_blocked(self):
        confirm = Confirmer(True)
        guard = ConversationGuard(confirm)
        guard.record_user("How much is a blood test?")
        self.lookup(guard)
        result = self.estimate(guard, 150, 0)
        self.assertEqual(result["status"], "ask_user")
        self.assertEqual(set(result["missing_fields"]), {"allowed_amount", "remaining_deductible"})
        self.assertEqual(confirm.calls, 0)

    def test_amounts_the_user_said_pass_with_formatting(self):
        guard = ConversationGuard(Confirmer(True))
        guard.record_user("Allowed amount is $1,200.00 and I have 0 deductible left")
        self.lookup(guard)
        self.assertEqual(self.estimate(guard, 1200, 0)["status"], "estimated")

    def test_null_amounts_are_not_blocked(self):
        guard = ConversationGuard(Confirmer(True))
        guard.record_user("How much is a blood test?")
        self.lookup(guard)
        self.assertEqual(self.estimate(guard, None, None)["status"], "needs_information")

    # Hook 2 ---------------------------------------------------------------

    def test_estimate_is_withheld_until_user_confirms(self):
        confirm = Confirmer(False, "It was out of network")
        guard = ConversationGuard(confirm)
        guard.record_user("Allowed amount 100, deductible remaining 80")
        self.lookup(guard)
        result = self.estimate(guard, 100, 80)
        self.assertEqual(result["status"], "user_rejected")
        self.assertNotIn("patient_cost", result)
        self.assertEqual(result["user_reply"], "It was out of network")
        self.assertIn("laboratory procedures", confirm.summary)

    def test_confirmation_is_asked_once_per_input_set(self):
        confirm = Confirmer(True)
        guard = ConversationGuard(confirm)
        guard.record_user("Allowed amount 100, deductible remaining 80")
        self.lookup(guard)
        first = self.estimate(guard, 100, 80)
        second = self.estimate(guard, 100, 80)
        self.assertEqual(first["patient_cost"], 84.0)
        self.assertEqual(second["patient_cost"], 84.0)
        self.assertEqual(confirm.calls, 1)

    # Hook 3 ---------------------------------------------------------------

    def test_reply_with_invented_amount_is_rejected(self):
        guard = ConversationGuard(Confirmer(True))
        guard.record_user("Allowed amount 100, deductible remaining 80")
        self.lookup(guard)
        self.estimate(guard, 100, 80)
        self.assertIsNone(guard.check_reply("You would pay about $84.00 for this test."))
        self.assertIsNotNone(guard.check_reply("You would pay about $95 for this test."))

    def test_reply_must_ask_when_information_is_missing(self):
        guard = ConversationGuard(Confirmer(True))
        guard.record_user("How much is a blood test?")
        guard.start_turn()
        self.lookup(guard)
        self.estimate(guard, None, None)
        self.assertIsNotNone(guard.check_reply("A blood test is usually cheap."))
        self.assertIsNone(guard.check_reply("What is the allowed amount and your remaining deductible?"))
        self.assertIn("allowed amount", guard.fallback_reply())

    def test_reset_forgets_previous_user_amounts(self):
        guard = ConversationGuard(Confirmer(True))
        guard.record_user("Allowed amount 100")
        guard.reset()
        self.lookup(guard)
        self.assertEqual(self.estimate(guard, 100, None)["status"], "ask_user")


class NumberParsingTest(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(numbers_in("$1,200.50 and 80"), {numbers_in("1200.5").pop(), numbers_in("80").pop()})
        self.assertEqual(numbers_in("rule_12 v2"), set())


if __name__ == "__main__":
    unittest.main()
