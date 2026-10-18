"""The agent harness, in four parts:

  prompts/system.md   main prompt: who the agent is, ground rules, how to work, style
  skills/             packaged abilities, each with SKILL.md instructions and its own tools
  mcp.py              the tool layer: every tool the model can call
  hooks.py            code that checks every message, tool call and reply

Harness puts them together for one conversation about one plan.
"""

import json
from pathlib import Path

from agent_tools import DEFAULT_RULES_PATH, PolicyTools

from . import mcp
from .hooks import Hooks
from .skills import load_skills

PROMPT_PATH = Path(__file__).with_name("prompts") / "system.md"
DEFINITIONS_DIR = Path(__file__).resolve().parents[2] / "policies" / "definitions"

SEARCH_ON = ("**Search for insurers that aren't in the catalog.** Use `search_insurer`. Search results are "
             "background for your judgment; never use them to calculate a cost.")
SEARCH_OFF = ("**Say so when an insurer isn't in the catalog.** Web search is off in this session, so don't "
              "guess facts about other insurers.")


PLANS_PROMPT_PATH = Path(__file__).with_name("prompts") / "system_plans.md"


class Harness:
    def __init__(self, plan, confirm, search_worker_url=None, rules_path=DEFAULT_RULES_PATH, pricing=True,
                 mode="legacy"):
        """mode "plans": cost estimates and the Arena on the plan database (plan_db.PlanDB), all plans at once.
        mode "legacy": one plan's rules JSON with get_policy_rule / calculate_estimate (the terminal agent's tools)."""
        self.mode = mode
        if mode == "plans":
            self._init_plans(confirm, search_worker_url)
            return
        self.policy = PolicyTools(selected_plan=plan, rules_path=rules_path)
        self.definitions = mcp.Definitions(DEFINITIONS_DIR / f"{self.policy.selected_plan}.txt")
        self.rates, rate_options = None, None
        if pricing:
            from .skills.pricing.skill import build_rates

            self.rates, rate_options = build_rates(self.policy)
        self.skills = load_skills(self.rates)

        tools = mcp.policy_tools(self.policy)
        if self.definitions.terms:
            tools.append(mcp.definition_tool(self.definitions, self.policy.selected_plan))
        if search_worker_url:
            tools.append(mcp.search_tool(search_worker_url))
        for skill in self.skills:
            tools.extend(skill.tools)
        self.tools = mcp.ToolRegistry(tools)

        self.hooks = Hooks(self.policy, confirm, rates=self.rates, rate_options=rate_options)
        self.system_prompt = self._render_prompt(search_on=bool(search_worker_url))

    def _init_plans(self, confirm, search_worker_url):
        from plan_db import PlanDB

        from .skills.plans import skill as plans_skill
        from .skills.questionnaire import skill as questionnaire

        self.db = PlanDB()
        self.policy, self.rates, self.definitions = None, None, None
        self.skills = [questionnaire.build(), plans_skill.build(self.db)]
        tools = [mcp.search_tool(search_worker_url)] if search_worker_url else []
        for skill in self.skills:
            tools.extend(skill.tools)
        self.tools = mcp.ToolRegistry(tools)
        self.hooks = Hooks(None, confirm)
        plan_context = ("```json\n" + "\n".join(json.dumps(p, ensure_ascii=False)
                                                for p in plans_skill.plan_summary(self.db)) + "\n```")
        text = PLANS_PROMPT_PATH.read_text(encoding="utf-8")
        for key, value in {"SEARCH_STEP": SEARCH_ON if search_worker_url else SEARCH_OFF,
                           "SKILLS": "\n\n".join(skill.prompt_section() for skill in self.skills),
                           "PLAN_CONTEXT": plan_context}.items():
            text = text.replace("{{" + key + "}}", value)
        self.system_prompt = text

    def _render_prompt(self, search_on):
        plan_context = (
            f"**Plan:** `{self.policy.selected_plan}`\n\n"
            "**Benefit catalog** (read a rule with `get_policy_rule` before using it):\n\n"
            "```json\n" + "\n".join(json.dumps(rule, ensure_ascii=False) for rule in self.policy.get_catalog())
            + "\n```"
        )
        if self.definitions.terms:
            plan_context += ("\n\n**Defined terms** (look one up with `get_definition`): "
                             + ", ".join(sorted(self.definitions.terms)))
        text = PROMPT_PATH.read_text(encoding="utf-8")
        for key, value in {
            "SEARCH_STEP": SEARCH_ON if search_on else SEARCH_OFF,
            "SKILLS": "\n\n".join(skill.prompt_section() for skill in self.skills),
            "PLAN_CONTEXT": plan_context,
        }.items():
            text = text.replace("{{" + key + "}}", value)
        return text
